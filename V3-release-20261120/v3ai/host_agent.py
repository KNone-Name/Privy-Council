# -*- coding: utf-8 -*-
r"""在游戏同一 Windows 会话里运行的轻量主机代理。

用途：DSH 沙箱里的 web 看不到 victoria3.exe，无法直接 injector / watch；
这个代理在游戏会话运行，通过工作区里的 JSON 文件收指令，代为执行：
  * inject / unload DLL
  * start / stop 快照 watch
  * 心跳 + 游戏可见性状态

启动（游戏已开、存档已加载）：
    powershell -ExecutionPolicy Bypass -File .\v3ai\host\start_host_agent.ps1

网页会检测 state/host_agent_status.json 的心跳，自动走代理。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PKG_DIR)
STATE = os.path.join(PKG_DIR, "state")
CMD = os.path.join(STATE, "host_agent_cmd.json")
RES = os.path.join(STATE, "host_agent_result.json")
STATUS = os.path.join(STATE, "host_agent_status.json")
LOG = os.path.join(STATE, "host_agent.log")
WATCH_LOG = os.path.join(STATE, "watch.log")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def now_iso() -> str:
    import datetime as dt
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def game_pids():
    try:
        from v3live import proc
        return proc.find_pids("victoria3.exe")
    except Exception:
        return []


class HostAgent:
    def __init__(self):
        from v3ai.config import Config
        self.config = Config.load()
        self.bridge = (self.config.executor.get("bridge") or {}).get("dir") or ""
        svc = self.config.data.get("service") or {}
        self.injector = self._resolve(svc.get("injector") or "")
        self.dll = self._resolve(svc.get("dll") or "")
        self.interval = int(svc.get("watch_interval") or 5)

    @staticmethod
    def _resolve(path: str) -> str:
        if not path:
            return ""
        return path if os.path.isabs(path) else os.path.join(ROOT, path)
        self.snapshot = self.config.path("snapshot")
        self.watch: subprocess.Popen | None = None
        self.watch_pid: int | None = None
        self.last_id = ""
        os.makedirs(STATE, exist_ok=True)
        os.makedirs(self.bridge, exist_ok=True)

    # -- helpers ------------------------------------------------------
    def _log(self, msg: str) -> None:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (now_iso(), msg))

    def _jlog(self) -> str:
        return os.path.join(self.bridge, "v3inject.log")

    def _tail(self, n=100) -> list[str]:
        try:
            with open(self._jlog(), encoding="utf-8", errors="replace") as f:
                return [x.rstrip("\n") for x in f.readlines()[-n:]]
        except OSError:
            return []

    def _status(self) -> dict:
        pids = game_pids()
        watch_alive = bool(self.watch is not None and self.watch.poll() is None)
        return {
            "ts": now_iso(),
            "time": time.time(),
            "pid": os.getpid(),
            "game_visible": bool(pids),
            "game_pids": pids,
            "watch_running": watch_alive,
            "watch_pid": self.watch.pid if watch_alive else None,
            "bridge": self.bridge,
            "injector": self.injector,
            "dll": self.dll,
            "log_tail": self._tail(8),
        }

    def _write_status(self) -> None:
        write_json(STATUS, self._status())

    def _write_result(self, cid: str, ok: bool, message: str, **data) -> None:
        out = {"id": cid, "ok": bool(ok), "message": message,
               "ts": now_iso(), "time": time.time()}
        out.update(data)
        write_json(RES, out)

    # -- actions ------------------------------------------------------
    def inject(self) -> tuple[bool, str, dict]:
        pids = game_pids()
        if not pids:
            return False, "当前主机会话也看不到 victoria3.exe；请确认游戏已启动、已加载存档", {}
        if not os.path.isfile(self.injector):
            return False, "找不到 injector：%s" % self.injector, {}
        if not os.path.isfile(self.dll):
            return False, "找不到 DLL：%s" % self.dll, {}
        target = str(pids[0])
        try:
            with open(os.path.join(STATE, "inject.log"), "ab") as f:
                p = subprocess.run([self.injector, target, self.dll],
                                   cwd=os.path.dirname(self.injector),
                                   stdout=f, stderr=subprocess.STDOUT,
                                   timeout=30, creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            return False, "injector 启动失败：%s" % exc, {}
        time.sleep(1.2)
        self._log("inject pid=%s injector rc=%s" % (target, p.returncode))
        wpath = os.path.join(self.bridge, "world.current.txt")
        try:
            world_age = time.time() - os.path.getmtime(wpath)
        except OSError:
            world_age = 10 ** 9
        if p.returncode != 0 and world_age > 10:
            detail = ""
            try:
                with open(os.path.join(STATE, "inject.log"), encoding="utf-8",
                          errors="replace") as f:
                    tail = f.readlines()[-12:]
                if any("OpenProcess failed" in x for x in tail):
                    detail = "；OpenProcess err=5，请用管理员身份运行 host agent"
                elif any("process not found" in x for x in tail):
                    detail = "；进程名查找失败（已改用 PID，仍失败请确认游戏未退出）"
            except OSError:
                pass
            return False, ("injector 返回 %s，且 world 未刷新%s；看 v3ai\\state\\inject.log"
                           % (p.returncode, detail)), {"rc": p.returncode}
        return True, "已注入 pid=%s，类型表扫描约 125 秒" % target, {"rc": p.returncode, "pid": pids[0]}

    def unload(self) -> tuple[bool, str, dict]:
        flag = os.path.join(self.bridge, "unload.flag")
        with open(flag, "w", encoding="ascii") as f:
            f.write("1\n")
        seen = False
        deadline = time.time() + 6
        while time.time() < deadline:
            if any(("unloading" in x or "unloaded" in x or "detach" in x)
                   for x in self._tail(120)):
                seen = True
                break
            time.sleep(0.2)
        if seen:
            try:
                os.remove(flag)
            except OSError:
                pass
            return True, "DLL 已卸载", {}
        # 扫描期间游戏主线程可能不处理 PeekMessage；保留 flag，等它自己看到。
        return True, "卸载请求已发送，等待 DLL 完成当前扫描后卸载", {"pending": True}

    def watch_start(self, snapshot: str | None = None,
                    interval: int | None = None) -> tuple[bool, str, dict]:
        if self.watch is not None and self.watch.poll() is None:
            return True, "watch 已在运行", {"pid": self.watch.pid}
        if not game_pids():
            return False, "看不到 victoria3.exe，watch 未启动", {}
        script = os.path.join(ROOT, "v3.py")
        if not os.path.isfile(script):
            return False, "找不到 v3.py", {}
        snap = snapshot or self.snapshot
        every = int(interval or self.interval)
        try:
            log = open(WATCH_LOG, "ab")
            self.watch = subprocess.Popen(
                [sys.executable, script, "watch", "--interval", str(every),
                 "--json", snap, "--no-gdp"],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
            log.close()
        except Exception as exc:
            return False, "watch 启动失败：%s" % exc, {}
        self._log("watch started pid=%s snapshot=%s" % (self.watch.pid, snap))
        return True, "watch 已启动", {"pid": self.watch.pid, "snapshot": snap}

    def watch_stop(self) -> tuple[bool, str, dict]:
        if self.watch is None:
            return True, "没有由代理启动的 watch", {}
        try:
            self.watch.terminate()
            try:
                self.watch.wait(timeout=3)
            except Exception:
                self.watch.kill()
        except Exception:
            pass
        pid = self.watch.pid
        self.watch = None
        return True, "watch 已停止", {"pid": pid}

    def execute(self, cmd: dict) -> None:
        cid = str(cmd.get("id") or "")
        action = str(cmd.get("action") or "")
        self._log("cmd %s %s" % (cid, action))
        try:
            if action == "inject":
                ok, msg, data = self.inject()
            elif action == "unload":
                ok, msg, data = self.unload()
            elif action == "watch_start":
                ok, msg, data = self.watch_start(snapshot=cmd.get("snapshot"),
                                                 interval=cmd.get("interval"))
            elif action == "watch_stop":
                ok, msg, data = self.watch_stop()
            elif action == "status":
                ok, msg, data = True, "ok", {"status": self._status()}
            else:
                ok, msg, data = False, "未知动作：%s" % action, {}
        except Exception as exc:
            ok, msg, data = False, "%s: %s" % (type(exc).__name__, exc), {}
        self._write_result(cid, ok, msg, **data)
        self._write_status()

    # -- loop ---------------------------------------------------------
    def run(self) -> int:
        self._log("host agent started pid=%s bridge=%s" % (os.getpid(), self.bridge))
        print("v3ai host agent started")
        print("  game visible:", bool(game_pids()))
        print("  bridge:", self.bridge)
        print("  command:", CMD)
        try:
            while True:
                self._write_status()
                cmd = read_json(CMD, {})
                cid = str((cmd or {}).get("id") or "")
                if cid and cid != self.last_id:
                    self.last_id = cid
                    self.execute(cmd)
                time.sleep(0.5)
        except KeyboardInterrupt:
            self._log("host agent interrupted")
        finally:
            try:
                self.watch_stop()
            except Exception:
                pass
            self._write_status()
        return 0


def main() -> int:
    return HostAgent().run()


if __name__ == "__main__":
    raise SystemExit(main())
