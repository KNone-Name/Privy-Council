# -*- coding: utf-8 -*-
"""执行层：把审查通过的动作送到游戏，并把执行情况读回来。

抽象出 ``Executor``，三种实现：
  * ``BridgeExecutor``：v3bridge 文件协议（requests.txt / pmm.txt / queue.txt）；
  * ``DryRunExecutor``：只记录，不写游戏（当前默认，等 bridge 适配 1.15.1）；
  * ``MockExecutor``：测试用。

bridge 0.1 的关键事实：
  * 控制文件在 DLL 同目录；写完后 DLL 把原文件改名成 ``*.done.txt``；
  * 建造结果看 ``queue.csv`` 增量 / 日志 ``queued=A->B``，不能看返回值；
  * PM 结果看 ``pmm.result.csv`` 的 ``status``；
  * 所有写操作只允许玩家自己的州，越权会 ``LOCK: denied``。
"""
from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .util import atomic_write_text, read_text, safe_float, safe_int


class ExecutorError(RuntimeError):
    pass


@dataclass
class ExecutionResult:
    command_id: str = ""
    ok: bool = False
    status: str = ""
    message: str = ""
    verified: bool = False
    latency_s: float = 0.0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id, "ok": self.ok, "status": self.status,
            "message": self.message, "verified": self.verified,
            "latency_s": round(self.latency_s, 3), "details": self.details,
        }


class Executor:
    mode = "abstract"

    def capabilities(self) -> dict:
        return {"mode": self.mode}

    def queue(self) -> list[dict]:
        return []

    def execute(self, cmd) -> ExecutionResult:
        raise NotImplementedError

    def close(self) -> None:
        pass


class DryRunExecutor(Executor):
    mode = "dry_run"

    def capabilities(self) -> dict:
        return {"mode": self.mode, "world": None, "player": None,
                "capabilities": "dry_run",
                "note": "未写控制文件；审查通过后只记录，不执行"}

    def execute(self, cmd) -> ExecutionResult:
        t0 = time.time()
        return ExecutionResult(
            command_id=getattr(cmd, "id", ""), ok=True, status="dry_run",
            message="演练通过（未写游戏）：%s" % getattr(cmd, "label", lambda: "")(),
            verified=False, latency_s=time.time() - t0,
            details={"dry_run": True},
        )


class MockExecutor(Executor):
    mode = "mock"

    def __init__(self, results: dict[str, ExecutionResult] | None = None,
                 queue: list[dict] | None = None):
        self.results = dict(results or {})
        self._queue = list(queue or [])
        self.calls: list[Any] = []

    def capabilities(self) -> dict:
        return {"mode": self.mode, "world": "1", "player": None,
                "capabilities": "mock"}

    def queue(self) -> list[dict]:
        return list(self._queue)

    def execute(self, cmd) -> ExecutionResult:
        self.calls.append(cmd)
        r = self.results.get(getattr(cmd, "id", ""))
        if r is not None:
            return r
        return ExecutionResult(getattr(cmd, "id", ""), True, "mock_ok",
                               "mock 执行成功", True, 0.0, {})


class BridgeExecutor(Executor):
    mode = "bridge"

    def __init__(self, directory: str, timeout_s: float = 30.0,
                 poll_s: float = 0.2, log_name: str = "v3inject.log"):
        self.dir = directory
        self.timeout_s = float(timeout_s or 30.0)
        self.poll_s = max(0.02, float(poll_s or 0.2))
        self.log_name = log_name
        self.pReq = self._p("requests.txt")
        self.pReqDone = self._p("requests.done.txt")
        self.pPmm = self._p("pmm.txt")
        self.pPmmDone = self._p("pmm.done.txt")
        self.pPmmResult = self._p("pmm.result.csv")
        self.pQueue = self._p("queue.txt")
        self.pQueueDone = self._p("queue.done.txt")
        self.pQueueCSV = self._p("queue.csv")
        self.pWorld = self._p("world.current.txt")
        self.pPlayer = self._p("player.current.txt")
        self.pLog = self._p(log_name)

    def _p(self, name: str) -> str:
        return os.path.join(self.dir, name)

    # -- 基础 ---------------------------------------------------------
    def available(self) -> bool:
        return os.path.isdir(self.dir)

    def capabilities(self) -> dict:
        out = {"mode": self.mode, "dir": self.dir}
        if not self.available():
            out["error"] = "bridge 目录不存在：%s" % self.dir
            return out
        w = read_text(self.pWorld, "").strip()
        out["world"] = "1" if w.startswith("1") else ("0" if w.startswith("0") else None)
        p = read_text(self.pPlayer, "").strip()
        out["player"] = safe_int(p, None)
        caps: dict[str, str] = {}
        selfchecks: list[str] = []
        tail: list[str] = []
        try:
            with open(self.pLog, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            tail = [x.rstrip("\n") for x in lines[-30:]]
        except OSError:
            lines = []
        for line in lines[-4000:]:
            s = line.strip()
            if "capabilities:" in s:
                tail_caps = s.split("capabilities:", 1)[1]
                for part in tail_caps.split():
                    if "=" in part:
                        k, v = part.split("=", 1)
                        caps[k] = v
            elif "selfcheck:" in s:
                selfchecks.append(s.split("selfcheck:", 1)[1].strip())
            elif "build UNKNOWN" in s:
                out["fingerprint"] = "UNKNOWN"
            elif "build fingerprint OK" in s:
                out["fingerprint"] = "OK"
        out["capabilities"] = caps
        out["selfcheck"] = selfchecks[-8:]
        out["log_tail"] = tail
        return out

    def _wait_done(self, done_path: str, what: str) -> None:
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            if os.path.isfile(done_path):
                return
            time.sleep(self.poll_s)
        raise ExecutorError("等待 %s 超时（%.1fs）：%s" % (what, self.timeout_s, done_path))

    def _clear(self, *paths: str) -> None:
        for p in paths:
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass

    def _write(self, path: str, text: str) -> None:
        atomic_write_text(path, text, encoding="ascii")

    def _log_size(self) -> int:
        try:
            return os.path.getsize(self.pLog)
        except OSError:
            return 0

    def _log_since(self, offset: int) -> str:
        try:
            size = os.path.getsize(self.pLog)
            if size < offset:
                offset = 0
            with open(self.pLog, encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                return f.read()
        except OSError:
            return ""

    # -- 队列 ---------------------------------------------------------
    def queue(self, scope: str = "government") -> list[dict]:
        if not self.available():
            raise ExecutorError("bridge 目录不存在：%s" % self.dir)
        self._clear(self.pQueueDone)
        self._write(self.pQueue, "1\n")
        self._wait_done(self.pQueueDone, "queue")
        return self._read_queue_csv(scope=scope)

    def queue_all(self) -> list[dict]:
        return self.queue(scope="all")

    def _read_queue_csv(self, scope: str = "government") -> list[dict]:
        if not os.path.isfile(self.pQueueCSV):
            return []
        out: list[dict] = []
        try:
            with open(self.pQueueCSV, encoding="utf-8-sig", errors="replace", newline="") as f:
                for row in csv.DictReader(f):
                    q = {k.strip(): v for k, v in row.items() if k}
                    qtype = str(q.get("queue") or "").strip().lower()
                    if scope != "all":
                        if scope == "government" and qtype in ("private", "autonomous"):
                            continue
                        if scope == "private" and qtype not in ("private", "autonomous"):
                            continue
                    pts = safe_float(q.get("pts_total"), 0.0) or 0.0
                    rem = safe_float(q.get("pts_remaining"), 0.0) or 0.0
                    out.append({
                        "queue": q.get("queue", ""),
                        "index": safe_int(q.get("index"), None),
                        "state_id": safe_int(q.get("state_id"), None),
                        "building": q.get("building", ""),
                        "pts_total": pts,
                        "pts_remaining": rem,
                        # bridge 写 CSV 时已经除过 100000；这里不再重复除
                        "pts_total_game": round(pts, 3),
                        "pts_remaining_game": round(rem, 3),
                        "levels": 1,
                    })
        except OSError:
            return []
        return out

    @staticmethod
    def _count_queue(rows: list[dict], state_id: int, building: str) -> int:
        return sum(1 for r in rows if r.get("state_id") == state_id
                   and r.get("building") == building)

    # -- 执行 ---------------------------------------------------------
    def execute(self, cmd) -> ExecutionResult:
        t0 = time.time()
        caps = self.capabilities()
        if caps.get("error"):
            return ExecutionResult(getattr(cmd, "id", ""), False, "bridge_unavailable",
                                   caps["error"], False, time.time() - t0, caps)
        if caps.get("world") == "0":
            return ExecutionResult(getattr(cmd, "id", ""), False, "not_in_world",
                                   "游戏在主菜单/加载中，拒绝执行", False,
                                   time.time() - t0, {"world": "0"})
        if getattr(cmd, "kind", "") == "build":
            return self._execute_build(cmd, t0)
        if getattr(cmd, "kind", "") == "pm":
            return self._execute_pm(cmd, t0)
        return ExecutionResult(getattr(cmd, "id", ""), False, "bad_kind",
                               "未知指令类型", False, time.time() - t0, {})

    def _execute_build(self, cmd, t0: float) -> ExecutionResult:
        sid = int(cmd.state_id)
        btype = str(cmd.building)
        before: list[dict] = []
        before_err = ""
        try:
            before = self.queue()
        except ExecutorError as exc:
            before_err = str(exc)
        log_off = self._log_size()
        self._clear(self.pReqDone)
        self._write(self.pReq, "%d %s %d\n" % (sid, btype, int(cmd.levels)))
        try:
            self._wait_done(self.pReqDone, "requests")
        except ExecutorError as exc:
            return ExecutionResult(cmd.id, False, "timeout", str(exc), False,
                                   time.time() - t0, {"before_count": len(before)})
        log = self._log_since(log_off)
        after: list[dict] = []
        after_err = ""
        try:
            after = self.queue()
        except ExecutorError as exc:
            after_err = str(exc)
        n_before = self._count_queue(before, sid, btype) if before else 0
        n_after = self._count_queue(after, sid, btype) if after else 0
        # 日志里的 queued=A->B 是引擎级证据；解析出增长才算成功。
        import re as _re
        queued_pairs = [tuple(map(int, m)) for m in
                        _re.findall(r"queued=(\d+)->(\d+)", log)]
        log_queued_up = any(b > a for a, b in queued_pairs)
        verified = n_after > n_before
        ok = verified or log_queued_up
        status = "ok" if ok else "failed"
        msg = "入队成功：%s（队列 %d→%d）" % (cmd.label(), n_before, n_after) if ok \
            else "未确认入队：%s" % cmd.label()
        details = {
            "state_id": sid, "building": btype, "levels": cmd.levels,
            "before_count": n_before, "after_count": n_after,
            "queue_before_error": before_err, "queue_after_error": after_err,
            "log_tail": log[-1200:],
        }
        # 日志里的明确失败/越权，优先作为失败原因
        for marker in ("LOCK: denied", "unknown building key", "build: refused",
                       "requests done: ok=0"):
            if marker in log:
                ok = False
                status = "denied" if "denied" in marker else "failed"
                msg = "执行端拒绝/失败：%s（%s）" % (cmd.label(), marker)
                break
        return ExecutionResult(cmd.id, ok, status, msg, verified,
                               time.time() - t0, details)

    def _execute_pm(self, cmd, t0: float) -> ExecutionResult:
        sid = int(cmd.state_id)
        btype = str(cmd.building)
        pm = str(cmd.pm)
        log_off = self._log_size()
        self._clear(self.pPmmDone, self.pPmmResult)
        self._write(self.pPmm, "%d %s %s\n" % (sid, btype, pm))
        try:
            self._wait_done(self.pPmmDone, "pmm")
        except ExecutorError as exc:
            return ExecutionResult(cmd.id, False, "timeout", str(exc), False,
                                   time.time() - t0, {})
        rows: list[dict] = []
        try:
            with open(self.pPmmResult, encoding="utf-8-sig", errors="replace", newline="") as f:
                rows = [{k.strip(): v for k, v in r.items() if k}
                        for r in csv.DictReader(f)]
        except OSError:
            rows = []
        row = None
        for r in rows:
            if (safe_int(r.get("state_id")) == sid
                    and r.get("building") == btype and r.get("pm") == pm):
                row = r
                break
        if row is None and rows:
            row = rows[0]
        status = (row or {}).get("status", "")
        ok = status in ("ok", "already")
        log = self._log_since(log_off)
        if row is None and "pmm done: ok=1" in log:
            # 结果文件缺失但日志明确成功（例如文件被占用），按日志放行
            ok = True
            status = "ok(log)"
        if row is None and "pmm: bad line" in log:
            ok = False
            status = "bad_line"
        msg = ("PM 切换成功：%s（status=%s）" % (cmd.label(), status) if ok
               else "PM 切换失败：%s（status=%s）" % (cmd.label(), status or "no_row"))
        return ExecutionResult(cmd.id, ok, status or "no_row", msg, ok,
                               time.time() - t0,
                               {"row": row, "rows": rows[:6], "log_tail": log[-1200:]})


def make_executor(config, mode: str | None = None) -> Executor:
    mode = (mode or config.executor.get("mode") or "dry_run").lower()
    if mode == "bridge":
        b = config.executor.get("bridge") or {}
        return BridgeExecutor(
            directory=b.get("dir") or "",
            timeout_s=float(b.get("timeout_s", 30) or 30),
            poll_s=float(b.get("poll_s", 0.2) or 0.2),
            log_name=b.get("log") or "v3inject.log",
        )
    if mode == "mock":
        return MockExecutor()
    return DryRunExecutor()
