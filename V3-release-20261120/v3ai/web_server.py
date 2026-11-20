# -*- coding: utf-8 -*-
"""v3ai Web 控制台：实时国家数据 + 御前 AI 对话。

只用标准库（ThreadingHTTPServer），不引入 FastAPI/uvicorn。
数据来源：
  * ``--snapshot``（默认 snap.json）：由游戏会话里的 ``v3.py watch`` 持续原子覆盖；
  * ``--live``：在能看到 victoria3.exe 的会话里每轮直接读内存。
页面不会展示州级明细，只展示国家/经济/建造/市场/政治聚合数据。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .agent import AgentLoop, _parse_json_object
from .config import Config
from .executor import BridgeExecutor
from .llm import LLMClient
from .runtime import Runtime, RuntimeErrorV3
from .strategy import Strategy, StrategyStore
from .util import age_seconds, atomic_write_json, now_iso, read_json

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
KNOWN_COUNTRY_NAMES = {3: "俄罗斯", 157: "大清"}
COUNTRY_TAG_NAMES = {"RUS": "俄罗斯", "CHI": "大清", "QNG": "大清"}
NAMES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "zh_cn.json")
NAMES_DATA = read_json(NAMES_PATH, {}) or {}
NAMES_MAPS = NAMES_DATA.get("maps") or {}
STATE_LABELS = {
    "STATE_SHANDONG": "山东", "STATE_SUZHOU": "苏州", "STATE_URGA": "库伦",
    "STATE_ULIASTAI": "乌里雅苏台", "STATE_DZUNGARIA": "准噶尔",
    "STATE_TIANSHAN": "天山", "STATE_SICHUAN": "四川", "STATE_YUNNAN": "云南",
    "STATE_GUIZHOU": "贵州", "STATE_SHAOZHOU": "韶州", "STATE_ALXA": "阿拉善",
    "STATE_HINGGAN": "兴安", "STATE_GUANGXI": "广西", "STATE_BEIJING": "北京",
    "STATE_SHENGJING": "盛京", "STATE_OUTER_MANCHURIA": "外满洲",
    "STATE_SOUTHERN_MANCHURIA": "南满", "STATE_NORTHERN_MANCHURIA": "北满",
    "STATE_SHANXI": "山西", "STATE_NINGXIA": "宁夏", "STATE_GANSU": "甘肃",
    "STATE_XIAN": "西安", "STATE_QINGHAI": "青海", "STATE_CHONGQING": "重庆",
    "STATE_GUANGDONG": "广东", "STATE_FUJIAN": "福建", "STATE_ZHEJIANG": "浙江",
    "STATE_NANJING": "南京", "STATE_JIANGSU": "江苏", "STATE_HENAN": "河南",
    "STATE_ZHILI": "直隶", "STATE_JIANGXI": "江西", "STATE_SOUTHERN_ANHUI": "皖南",
    "STATE_NORTHERN_ANHUI": "皖北", "STATE_WESTERN_HUBEI": "鄂西",
    "STATE_EASTERN_HUBEI": "鄂东", "STATE_HUNAN": "湖南", "STATE_FORMOSA": "台湾",
    "STATE_KIRGHIZIA": "吉尔吉斯", "STATE_JETISY": "七河", "STATE_TUVA": "图瓦",
    "STATE_ALTAI": "阿尔泰", "STATE_AMUR": "阿穆尔",
}
# 上面只是手工修正；完整州名（1614 条，覆盖所有国家）来自游戏本地化数据，
# 这样玩俄罗斯/日本/美国时界面也能显示中文州名，而不是只认识大清。
STATE_LABELS = {**(NAMES_MAPS.get("states") or {}), **STATE_LABELS}


def _country_name(snap) -> str:
    c = snap.country if snap else None
    if c is None:
        return "未知国家"
    if c.tag:
        return COUNTRY_TAG_NAMES.get(str(c.tag).upper(), c.tag)
    return KNOWN_COUNTRY_NAMES.get(c.id, "国家 #%s" % c.id)


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class DataService:
    """快照/队列的读取与轮询。"""

    def __init__(self, config: Config, snapshot_path: str | None = None,
                 live: bool = False, queue_interval: float = 5.0):
        self.config = config
        self.snapshot_path = snapshot_path or config.path("snapshot")
        self.live = bool(live)
        self.queue_interval = max(1.0, float(queue_interval or 5.0))
        self._lock = threading.RLock()
        self._queue_lock = threading.Lock()
        self.queue_paused = False
        self._queue: list[dict] = []
        self._private_queue: list[dict] = []
        self._queue_error = ""
        self._queue_at = 0.0
        self._stop = threading.Event()
        self._mtime = 0.0
        self._last_refresh = 0.0
        self.live_error = ""
        self.rt: Runtime | None = None
        self._init_runtime()
        self._queue_thread = threading.Thread(target=self._queue_loop, daemon=True)
        self._queue_thread.start()

    def _init_runtime(self) -> None:
        try:
            source = "live" if self.live else "file"
            self.rt = Runtime(self.config, snapshot_path=self.snapshot_path,
                              source=source, mode="dry_run")
        except Exception as exc:
            if self.live:
                self.live = False
                self.live_error = str(exc)
                self.rt = Runtime(self.config, snapshot_path=self.snapshot_path,
                                  source="file", mode="dry_run")
            else:
                raise
        self._mtime = self._stat_mtime()

    def _stat_mtime(self) -> float:
        try:
            return os.path.getmtime(self.snapshot_path)
        except OSError:
            return 0.0

    def refresh_if_needed(self) -> None:
        with self._lock:
            if self.rt is None:
                self._init_runtime()
                return
            if self.live:
                if time.time() - self._last_refresh < 2.0:
                    return
                try:
                    self.rt.refresh()
                    self._last_refresh = time.time()
                except Exception as exc:
                    self.live_error = str(exc)
                return
            m = self._stat_mtime()
            if m and m != self._mtime:
                self._mtime = m
                self.rt.refresh()

    # -- 队列 ---------------------------------------------------------
    def _queue_loop(self) -> None:
        bridge = self.config.executor.get("bridge") or {}
        directory = bridge.get("dir") or ""
        if not directory or not os.path.isdir(directory):
            return
        ex = BridgeExecutor(directory, timeout_s=8.0, poll_s=0.2,
                            log_name=bridge.get("log") or "v3inject.log")
        while not self._stop.is_set():
            if self.queue_paused:
                self._stop.wait(1.0)
                continue
            try:
                rows = ex.queue_all()
                gov = [r for r in rows if str(r.get("queue") or "").lower() not in ("private", "autonomous")]
                priv = [r for r in rows if str(r.get("queue") or "").lower() in ("private", "autonomous")]
                with self._queue_lock:
                    self._queue = gov
                    self._private_queue = priv
                    self._queue_error = ""
                    self._queue_at = time.time()
            except Exception as exc:
                with self._queue_lock:
                    self._queue_error = str(exc)
            self._stop.wait(self.queue_interval)

    def queue(self) -> list[dict]:
        with self._queue_lock:
            return list(self._queue)

    def private_queue(self) -> list[dict]:
        with self._queue_lock:
            return list(self._private_queue)

    # -- 数据 ---------------------------------------------------------
    def payload(self) -> dict:
        try:
            self.refresh_if_needed()
        except Exception as exc:
            return {"ok": False, "error": "读取快照失败：%s" % exc,
                    "updated_at": now_iso()}
        with self._lock:
            rt = self.rt
            if rt is None or rt.snap is None:
                return {"ok": False, "error": "世界模型未就绪", "updated_at": now_iso()}
            snap = rt.snap
            ov = rt.overview()
            queue = self.queue()
            private_queue = self.private_queue()
        meta = dict(ov.get("meta") or {})
        captured = snap.game.captured_at
        age = age_seconds(captured)
        c = snap.country
        gdp = ov.get("gdp") or {}
        budget = ov.get("budget") or {}
        construction = ov.get("construction") or {}
        market = ov.get("market") or {}
        politics = ov.get("politics") or {}
        buildings = ov.get("buildings") or {}
        techs = ov.get("techs") or {}
        drive_queue_items = [{
            "building": q.get("building", ""),
            "levels": q.get("levels", 1),
            "pts_total": _num(q.get("pts_total")),
            "pts_remaining": _num(q.get("pts_remaining")),
            "progress": (max(0.0, min(1.0, round(1.0 - _num(q.get("pts_remaining")) /
                                                   _num(q.get("pts_total")), 3)))
                         if _num(q.get("pts_total")) > 0 else 0.0),
        } for q in queue]
        private_items = [{
            "building": q.get("building", ""),
            "levels": q.get("levels", 1),
            "pts_total": _num(q.get("pts_total")),
            "pts_remaining": _num(q.get("pts_remaining")),
            "progress": (max(0.0, min(1.0, round(1.0 - _num(q.get("pts_remaining")) /
                                                   _num(q.get("pts_total")), 3)))
                         if _num(q.get("pts_total")) > 0 else 0.0),
        } for q in private_queue]
        queue_items = drive_queue_items
        queue_total_points = round(sum(_num(q.get("pts_total")) for q in queue_items), 2)
        queue_remaining_points = round(sum(_num(q.get("pts_remaining")) for q in queue_items), 2)
        queue_weeks = (round(queue_remaining_points / _num(construction.get("capacity_per_week")), 1)
                       if _num(construction.get("capacity_per_week")) > 0 else None)
        warnings = list(meta.get("warnings") or [])
        with self._queue_lock:
            qerr = self._queue_error
        if qerr:
            warnings.append("队列读取失败：%s" % qerr)
        return {
            "ok": True,
            "updated_at": now_iso(),
            "meta": {
                "source": snap.game.source,
                "captured_at": captured,
                "age_s": round(age or 0, 1),
                "stale": bool(age and age > 120),
                "game_version": snap.game.game_version,
                "country_id": c.id if c else None,
                "country_name": _country_name(snap),
                "country_tag": c.tag if c else None,
                "date_label": snap.game.save_date or "实时",
                "live": self.live,
                "live_error": self.live_error,
                "queue_at": self._queue_at,
            },
            "headline": {
                "gdp_annual": _num(gdp.get("annual")),
                "gdp_per_capita": _num(gdp.get("per_capita")),
                "population": c.population if c else None,
                "treasury": _num(c.treasury) if c else 0.0,
                "balance": _num(budget.get("balance")),
                "construction_capacity": _num(construction.get("capacity_per_week")),
                "queue_count": len(queue_items),
                "private_queue_count": len(private_items),
                "techs": int(techs.get("count") or 0),
                "laws": len(politics.get("laws") or []),
            },
            "economy": {
                "gdp_annual": _num(gdp.get("annual")),
                "gdp_weekly": _num(gdp.get("annual") or 0) / 52.0,
                "gdp_per_capita": _num(gdp.get("per_capita")),
                "price_basis": gdp.get("price_basis"),
                "income_total": _num(budget.get("income_total")),
                "expense_total": _num(budget.get("expense_total")),
                "balance": _num(budget.get("balance")),
                "treasury": _num(c.treasury) if c else 0.0,
                "credit_limit": _num(c.credit_limit) if c else 0.0,
                "investment_pool": _num(budget.get("weekly_investment_pool")),
                "tax_level": budget.get("tax_level"),
                "top_income": budget.get("top_income") or [],
                "top_expenses": budget.get("top_expenses") or [],
            },
            "construction": {
                "capacity_per_week": _num(construction.get("capacity_per_week")),
                "base": _num(construction.get("base")),
                "from_buildings": _num(construction.get("from_buildings")),
                "mult": _num(construction.get("mult")),
                "sector_count": sum(1 for b in (snap.buildings or [])
                                    if b.type == "building_construction_sector"),
                "queue_count": len(queue_items),
                "queue": queue_items,
                "queue_total_points": queue_total_points,
                "queue_remaining_points": queue_remaining_points,
                "queue_weeks": queue_weeks,
                "private_queue_count": len(private_items),
                "private_queue": private_items,
                "note": construction.get("note"),
            },
            "market": {
                "n_goods": market.get("n_goods"),
                "most_expensive": market.get("most_expensive") or [],
                "cheapest": market.get("cheapest") or [],
            },
            "politics": {
                "laws": politics.get("laws") or [],
                "interest_groups": politics.get("interest_groups") or [],
                "movements": politics.get("movements") or [],
                "institutions": politics.get("institutions") or [],
                "decrees": politics.get("decrees") or [],
            },
            "techs": {"count": int(techs.get("count") or 0),
                      "recent": (techs.get("recent") or [])[:10]},
            "buildings": {
                "count": buildings.get("count"),
                "levels": meta.get("levels"),
                "top_types": (buildings.get("top_types") or [])[:8],
            },
            "warnings": warnings[:12],
            "strategy": ov.get("strategy"),
        }

    def chat_context(self) -> dict:
        p = self.payload()
        if not p.get("ok"):
            return p
        return {
            "meta": p["meta"],
            "headline": p["headline"],
            "economy": p["economy"],
            "construction": {k: p["construction"][k] for k in (
                "capacity_per_week", "queue_count", "note")},
            "market": {
                "most_expensive": p["market"]["most_expensive"][:4],
                "cheapest": p["market"]["cheapest"][:4],
            },
            "politics": {
                "laws": p["politics"]["laws"],
                "interest_groups": p["politics"]["interest_groups"][:4],
                "movements": p["politics"]["movements"][:4],
                "institutions": p["politics"]["institutions"][:4],
            },
            "techs": p["techs"],
            "buildings": p["buildings"],
            "strategy": p.get("strategy"),
        }

    def status(self) -> dict:
        p = self.payload()
        return {
            "ok": p.get("ok"),
            "provider": self.config.provider_name,
            "model": self.config.provider().get("model"),
            "agent_mode": self.config.agent.get("mode"),
            "live": self.live,
            "live_error": self.live_error,
            "snapshot": p.get("meta"),
            "headline": p.get("headline"),
            "queue_count": len(self.queue()),
            "updated_at": now_iso(),
        }

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue_thread.join(timeout=2)
        except Exception:
            pass


class ChatService:
    def __init__(self, data: DataService, config: Config, llm=None):
        self.data = data
        self.config = config
        self.llm = llm or LLMClient(config.provider())
        self.history: list[dict] = []
        self._lock = threading.Lock()

    def reload(self) -> None:
        with self._lock:
            self.llm = LLMClient(self.config.provider())

    def _system(self, ctx: dict) -> str:
        return (
            "你是 Victoria 3 的御前 AI 顾问，服务当前玩家国家。"
            "只能依据下面提供的当前国家数据回答；数据没有的字段就说不知道，"
            "不要编造州级明细。回答用中文，简洁、具体，最多 8 句；"
            "如果玩家问建造/政策，给出优先级和风险。\n\n"
            "当前国家数据（JSON）：\n" + json.dumps(ctx, ensure_ascii=False)
        )

    def ask(self, message: str) -> dict:
        message = (message or "").strip()
        if not message:
            return {"ok": False, "error": "消息为空"}
        if len(message) > 4000:
            message = message[:4000]
        ctx = self.data.chat_context()
        if not ctx.get("ok", True):
            return {"ok": False, "error": ctx.get("error") or "世界数据未就绪"}
        with self._lock:
            messages = [{"role": "system", "content": self._system(ctx)}]
            messages.extend(self.history[-8:])
            messages.append({"role": "user", "content": message})
            t0 = time.time()
            try:
                resp = self.llm.chat(messages, tools=None, temperature=0.5,
                                     max_tokens=self.config.agent.get("max_tokens"))
            except Exception as exc:
                return {"ok": False, "error": "LLM 调用失败：%s" % exc}
            text = resp.content or resp.reasoning or ""
            self.history.append({"role": "user", "content": message})
            self.history.append({"role": "assistant", "content": text})
            self.history = self.history[-20:]
            return {
                "ok": True,
                "reply": text or "（模型没有返回内容）",
                "reasoning": resp.reasoning or "",
                "model": getattr(self.llm, "model", ""),
                "usage": resp.usage or {},
                "latency_s": round(time.time() - t0, 2),
            }


class DecisionService:
    def __init__(self, data: DataService, config: Config):
        self.data = data
        self.config = config
        self._lock = threading.Lock()
        self._rt: Runtime | None = None
        self._pending: list = []
        self._last: dict | None = None
        self.ready_check = None

    def _mode(self) -> str:
        b = self.config.executor.get("bridge") or {}
        return "bridge" if os.path.isdir(b.get("dir") or "") else "dry_run"

    def _close_rt(self) -> None:
        if self._rt is not None:
            try:
                self._rt.close()
            except Exception:
                pass
            self._rt = None
            self._pending = []

    def run(self, execute: bool = False) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "已有一轮决策正在进行"}
        try:
            if self.ready_check is not None and not self.ready_check():
                return {"ok": False, "error": "DLL 扫描尚未完成，操作已锁定"}
            self._close_rt()
            cfg = Config.load()
            rt = Runtime(cfg, snapshot_path=self.data.snapshot_path,
                         source=("live" if self.data.live else "file"),
                         mode=self._mode())
            agent = AgentLoop(rt)
            res = agent.run_cycle(plan_only=not execute)
            self._rt = rt
            self._pending = list(rt.pending)
            out = res.to_dict()
            out["ok"] = res.error is None
            out["pending_count"] = len(self._pending)
            self._last = out
            return out
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            self._lock.release()

    def execute_pending(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "已有一轮决策正在进行"}
        try:
            if self.ready_check is not None and not self.ready_check():
                return {"ok": False, "error": "DLL 扫描尚未完成，操作已锁定"}
            if self._rt is None or not self._pending:
                return {"ok": False, "error": "没有待执行动作"}
            results = self._rt.execute_pending()
            out = {"ok": True, "executions": [r.to_dict() for r in results]}
            self._pending = list(self._rt.pending)
            if self._last is not None:
                self._last["executions"] = out["executions"]
            return out
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            self._lock.release()

    @property
    def pending_count(self) -> int:
        return len(self._pending)


class AutoLoop:
    """连续模式：一轮结束后按 interval 自动开始下一轮。"""

    def __init__(self, app: "WebApp"):
        self.app = app
        self.config = app.config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.enabled = bool(self.config.data.get("loop", {}).get("enabled", True))
        self.interval = float(self.config.data.get("loop", {}).get("interval_s", 20) or 0)
        self.execute = bool(self.config.data.get("loop", {}).get("execute", True))
        self.pause_if_stale = bool(self.config.data.get("loop", {}).get("pause_if_stale", True))
        self.rounds = 0
        self.last: dict | None = None
        self.last_at = 0.0
        self.next_at = time.time() + self.interval
        self.error = ""
        self.paused_reason = "" if self.enabled else "连续模式关闭"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="v3ai-loop")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            try:
                self._thread.join(timeout=2)
            except Exception:
                pass

    def update(self) -> None:
        lp = self.config.data.get("loop", {}) or {}
        self.enabled = bool(lp.get("enabled", True))
        self.interval = max(0.0, float(lp.get("interval_s", 20) or 0))
        self.execute = bool(lp.get("execute", True))
        self.pause_if_stale = bool(lp.get("pause_if_stale", True))
        self.next_at = time.time() + (self.interval if self.enabled else 1.0)
        self.paused_reason = "" if self.enabled else "连续模式关闭"

    def status(self) -> dict:
        last = None
        if self.last:
            last = {
                "cycle": self.last.get("cycle"),
                "summary": self.last.get("summary"),
                "rationale": self.last.get("rationale"),
                "proposals": (self.last.get("proposals") or [])[:8],
                "executions": (self.last.get("executions") or [])[:8],
                "tool_calls": (self.last.get("tool_calls") or [])[:20],
                "llm_usage": self.last.get("llm_usage") or {},
                "latency_s": self.last.get("latency_s"),
                "ts": self.last_at,
            }
        return {
            "enabled": self.enabled,
            "interval_s": self.interval,
            "execute": self.execute,
            "pause_if_stale": self.pause_if_stale,
            "running": bool(self._thread and self._thread.is_alive()),
            "rounds": self.rounds,
            "next_at": self.next_at,
            "next_in": max(0.0, round(self.next_at - time.time(), 1)),
            "paused_reason": self.paused_reason,
            "error": self.error,
            "last": last,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.enabled:
                self.paused_reason = "连续模式关闭"
                self._stop.wait(1.0)
                continue
            now = time.time()
            if now < self.next_at:
                self._stop.wait(min(1.0, self.next_at - now))
                continue
            if self.pause_if_stale:
                try:
                    p = self.app.data.payload()
                    meta = p.get("meta") or {}
                    if not p.get("ok") or meta.get("stale"):
                        self.paused_reason = "快照过期或不可用"
                        self.next_at = time.time() + max(5.0, self.interval or 5.0)
                        continue
                except Exception as exc:
                    self.paused_reason = "读取快照失败"
                    self.error = str(exc)
                    self.next_at = time.time() + max(5.0, self.interval or 5.0)
                    continue
            try:
                if not self.app.service.dll_ready():
                    self.paused_reason = "DLL 扫描中，决策已锁定"
                    self.next_at = time.time() + 3.0
                    continue
            except Exception:
                pass
            if self.app.decision._lock.locked():
                self.next_at = time.time() + 2.0
                continue
            self.paused_reason = ""
            try:
                res = self.app.decision.run(execute=self.execute)
                self.rounds += 1
                self.last = res
                self.last_at = time.time()
                if not res.get("ok"):
                    self.error = res.get("error") or "决策失败"
                else:
                    self.error = ""
            except Exception as exc:
                self.error = "%s: %s" % (type(exc).__name__, exc)
            self.next_at = time.time() + self.interval
            if self.interval <= 0:
                self._stop.wait(0.05)


class ServiceManager:
    """一键启动/停止：DLL 注入、快照 watch、连续决策循环。

    网页本身不关闭；这样“停止服务”后还能从同一页面重新启动。
    """

    def __init__(self, app: "WebApp"):
        self.app = app
        self.config = app.config
        self._lock = threading.Lock()
        self._watch: subprocess.Popen | None = None
        self._watch_external = False
        self._watch_error = ""
        self._state = "unknown"
        self._error = ""
        self._last_action = ""
        self._last_action_at = 0.0
        self._last_watch_try = 0.0

    # -- paths --------------------------------------------------------
    @property
    def bridge_dir(self) -> str:
        return (self.config.executor.get("bridge") or {}).get("dir") or ""

    @property
    def log_path(self) -> str:
        return os.path.join(self.bridge_dir, "v3inject.log")

    def _service_cfg(self) -> dict:
        return self.config.data.get("service") or {}

    # -- host agent ---------------------------------------------------
    @property
    def agent_status_path(self) -> str:
        return os.path.join(self.config.path("state_dir"), "host_agent_status.json")

    @property
    def agent_cmd_path(self) -> str:
        return os.path.join(self.config.path("state_dir"), "host_agent_cmd.json")

    @property
    def agent_result_path(self) -> str:
        return os.path.join(self.config.path("state_dir"), "host_agent_result.json")

    def agent_status(self) -> dict:
        d = read_json(self.agent_status_path, {}) or {}
        age = time.time() - float(d.get("time") or 0)
        return {
            "online": age < 10,
            "age_s": round(max(0.0, age), 1),
            "pid": d.get("pid"),
            "game_visible": bool(d.get("game_visible")),
            "watch_running": bool(d.get("watch_running")),
            "watch_pid": d.get("watch_pid"),
        }

    def agent_call(self, action: str, timeout: float = 30.0, **payload) -> dict:
        cid = uuid.uuid4().hex
        try:
            cmd = {"id": cid, "action": action, "ts": now_iso()}
            cmd.update(payload)
            atomic_write_json(self.agent_cmd_path, cmd)
        except Exception as exc:
            return {"ok": False, "error": "写主机代理指令失败：%s" % exc}
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = read_json(self.agent_result_path, {}) or {}
            if str(r.get("id")) == cid:
                return {"ok": bool(r.get("ok")), "message": r.get("message") or "",
                        "data": r, "via": "agent"}
            time.sleep(0.2)
        return {"ok": False, "error": "主机代理 %.0f 秒内没有返回" % timeout,
                "via": "agent"}

    def _agent_available(self) -> bool:
        a = self.agent_status()
        return bool(a.get("online") and a.get("game_visible"))

    def dll_ready(self) -> bool:
        d = self.dll_state()
        if d.get("ready"):
            self.app.data.queue_paused = False
        return bool(d.get("ready"))

    def _game_pids(self) -> list[int]:
        try:
            from v3live import proc
            return list(proc.find_pids("victoria3.exe"))
        except Exception:
            return []

    def _game_visible(self):
        try:
            from v3live import proc
            return bool(proc.find_pids("victoria3.exe"))
        except Exception:
            return None

    def _injector(self) -> str:
        p = self._service_cfg().get("injector") or ""
        return p if os.path.isabs(p) else os.path.join(ROOT_DIR, p)

    def _dll(self) -> str:
        p = self._service_cfg().get("dll") or ""
        return p if os.path.isabs(p) else os.path.join(ROOT_DIR, p)

    def _read_log(self, n: int = 200) -> list[str]:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                return [x.rstrip("\n") for x in f.readlines()[-n:]]
        except OSError:
            return []

    def dll_state(self) -> dict:
        caps = {}
        try:
            if self.bridge_dir and os.path.isdir(self.bridge_dir):
                ex = BridgeExecutor(self.bridge_dir, timeout_s=1.0, poll_s=0.15,
                                    log_name=(self.config.executor.get("bridge") or {}).get("log") or "v3inject.log")
                caps = ex.capabilities()
        except Exception as exc:
            caps = {"error": str(exc)}
        tail = self._read_log(300)
        start_idx = -1
        unload_idx = -1
        for i, line in enumerate(tail):
            s = str(line)
            if "=== v3bridge" in s:
                start_idx = i
            if "unloading" in s or "unloaded" in s or "detach" in s:
                unload_idx = i
        if unload_idx > start_idx:
            loaded = False
            ready = False
            window = []
        elif start_idx >= 0:
            loaded = True
            window = tail[start_idx + 1:]
            # capabilities: 在类型扫描之前就会写出（types 可能还是 0），不能当完成标志。
            ready = any("types ready" in str(x) for x in window)
        else:
            loaded = False
            ready = False
            window = []
        # 日志可能已滚过 === v3bridge；用 world.current.txt 的刷新时间判断 DLL 是否还活着。
        # 注意：world 只代表“进入存档”，types=0 时扫描还没完成，不能算 ready。
        try:
            wpath = os.path.join(self.bridge_dir, "world.current.txt")
            world_age = time.time() - os.path.getmtime(wpath)
        except OSError:
            world_age = 10 ** 9
        cap = caps.get("capabilities") or {}
        try:
            ppath = os.path.join(self.bridge_dir, "player.current.txt")
            player_age = time.time() - os.path.getmtime(ppath)
        except OSError:
            player_age = 10 ** 9
        if unload_idx <= start_idx and caps.get("world") == "1" and caps.get("player") and world_age < 10:
            loaded = True
            if start_idx < 0:
                ready = bool(cap.get("build") == "on" and cap.get("pm") == "on")
        # 游戏重启/卸载后旧 world 文件可能还在；world/player 都长期不更新说明 DLL 不在了
        # world.current.txt 是 DLL 每帧/定期写的；长时间不更新说明 DLL 已经不在了。
        # 旧逻辑只检查 player 文件缺失，日志/世界文件过期时会把“已经死掉的 DLL”误判为 loaded，
        # 导致 stop 一直卡在 stopping。这里直接按 world 新鲜度判定。
        if world_age > 120:
            loaded = False
            ready = False
        return {
            "loaded": bool(loaded),
            "ready": bool(ready),
            "types_ready": bool(ready),
            "world": caps.get("world"),
            "player": caps.get("player"),
            "world_age_s": round(world_age, 1) if world_age < 10 ** 8 else None,
            "fingerprint": caps.get("fingerprint"),
            "capabilities": cap,
            "error": caps.get("error", ""),
            "log_tail": tail[-12:],
        }

    # -- operations ---------------------------------------------------
    def _write_flag(self, name: str, text: str = "1") -> str:
        path = os.path.join(self.bridge_dir, name)
        os.makedirs(self.bridge_dir, exist_ok=True)
        with open(path, "w", encoding="ascii") as f:
            f.write(text + "\n")
        return path

    def inject(self) -> dict:
        st = self.dll_state()
        if st.get("loaded") and st.get("world") == "1":
            return {"ok": True, "already": True, "message": "DLL 已在存档中", "dll": st}
        visible = self._game_visible()
        if visible is False:
            if self._agent_available():
                r = self.agent_call("inject", timeout=45)
                time.sleep(1.0)
                dll_now = self.dll_state()
                ok = bool(r.get("ok") and dll_now.get("loaded"))
                return {"ok": ok, "message": r.get("message") or "",
                        "error": None if ok else (r.get("error") or r.get("message")
                                                  or "注入后 DLL 仍未载入"),
                        "dll": dll_now, "via": "agent"}
            return {"ok": False,
                    "error": "当前 web 会话看不到 victoria3.exe（权限/会话隔离）；"
                             "请在游戏会话运行 v3ai\\host\\start_host_agent.ps1，"
                             "或用 run_live.ps1 -Web 启动 web"}
        if st.get("loaded") and st.get("world") == "1":
            return {"ok": True, "already": True, "message": "DLL 已在存档中", "dll": st}
        injector, dll = self._injector(), self._dll()
        if not os.path.isfile(injector):
            return {"ok": False, "error": "找不到 injector：%s" % injector}
        if not os.path.isfile(dll):
            return {"ok": False, "error": "找不到 DLL：%s" % dll}
        log = os.path.join(self.config.path("state_dir"), "inject.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        pids = self._game_pids()
        target = str(pids[0]) if pids else "victoria3"
        cmd = [injector, target, dll]
        try:
            with open(log, "ab") as f:
                p = subprocess.run(cmd, cwd=os.path.dirname(injector),
                                   stdout=f, stderr=subprocess.STDOUT,
                                   timeout=30, creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            return {"ok": False, "error": "启动 injector 失败：%s" % exc}
        time.sleep(1.2)
        st = self.dll_state()
        if p.returncode != 0 and not st.get("loaded"):
            tail = ""
            try:
                with open(log, encoding="utf-8", errors="replace") as f:
                    tail = "".join(f.readlines()[-4:])
            except OSError:
                pass
            return {"ok": False,
                    "error": "injector 返回 %s：%s" % (p.returncode, tail.strip() or "看 inject.log"),
                    "dll": st}
        self._last_action = "inject"
        self._last_action_at = time.time()
        return {"ok": True, "message": "已注入，类型表扫描可能需要约 125 秒",
                "dll": st, "inject_log": log}

    def unload(self) -> dict:
        if not self.bridge_dir or not os.path.isdir(self.bridge_dir):
            return {"ok": False, "error": "bridge 目录不存在"}
        flag = self._write_flag("unload.flag")
        deadline = time.time() + 6
        seen = False
        while time.time() < deadline:
            tail = " ".join(self._read_log(120))
            if "unloading" in tail or "unloaded" in tail or "detach" in tail:
                seen = True
                break
            time.sleep(0.2)
        self._last_action = "unload"
        self._last_action_at = time.time()
        if not seen:
            return {"ok": True, "pending": True,
                    "message": "卸载请求已发送，等待 DLL 完成当前扫描",
                    "dll": self.dll_state()}
        try:
            os.remove(flag)
        except OSError:
            pass
        return {"ok": True, "message": "DLL 已卸载", "dll": self.dll_state()}

    def _watch_log(self) -> str:
        return os.path.join(self.config.path("state_dir"), "watch.log")

    def start_watch(self) -> dict:
        if self.app.data.live:
            self._watch_external = False
            return {"ok": True, "message": "web 以 --live 运行，无需 watch"}
        if self._game_visible() is False:
            if self._agent_available():
                r = self.agent_call("watch_start", timeout=20,
                                    snapshot=self.app.data.snapshot_path,
                                    interval=int(self._service_cfg().get("watch_interval") or 5))
                self._watch_external = bool(r.get("ok"))
                return {"ok": r.get("ok"), "message": r.get("message") or "",
                        "error": None if r.get("ok") else (r.get("error") or r.get("message")),
                        "via": "agent"}
            return {"ok": False,
                    "error": "当前会话看不到 victoria3.exe；watch 未启动。"
                             "请在游戏会话启动 host agent 或手动运行 v3.py watch"}
        if self._watch is not None and self._watch.poll() is None:
            return {"ok": True, "message": "watch 已在运行", "pid": self._watch.pid}
        # 外部已有 watch 在写快照时，不再开第二个
        try:
            age = age_seconds((self.app.data.payload().get("meta") or {}).get("captured_at"))
            if age is not None and age < 12:
                self._watch_external = True
                self._watch_error = ""
                return {"ok": True, "message": "检测到外部快照更新，跳过启动 watch",
                        "external": True}
        except Exception:
            pass
        script = self._service_cfg().get("watch_script") or "v3.py"
        script = script if os.path.isabs(script) else os.path.join(ROOT_DIR, script)
        if not os.path.isfile(script):
            return {"ok": False, "error": "找不到 watch 脚本：%s" % script}
        snapshot = self.app.data.snapshot_path
        interval = int(self._service_cfg().get("watch_interval") or 5)
        os.makedirs(os.path.dirname(self._watch_log()), exist_ok=True)
        try:
            log = open(self._watch_log(), "ab")
            self._watch = subprocess.Popen(
                [sys.executable, script, "watch", "--interval", str(interval),
                 "--json", snapshot, "--no-gdp"],
                cwd=ROOT_DIR, stdout=log, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
            log.close()
        except Exception as exc:
            self._watch_error = str(exc)
            return {"ok": False, "error": "启动 watch 失败：%s" % exc}
        self._watch_external = False
        self._watch_error = ""
        return {"ok": True, "message": "watch 已启动", "pid": self._watch.pid,
                "log": self._watch_log()}

    def stop_watch(self) -> dict:
        if self._agent_available():
            r = self.agent_call("watch_stop", timeout=15)
            if not r.get("ok"):
                return {"ok": False, "error": r.get("error") or "主机代理停止 watch 失败"}
            return {"ok": True, "message": "主机代理 watch 已停止", "via": "agent"}
        if self._watch is None:
            return {"ok": True, "message": "没有由本服务启动的 watch"}
        try:
            self._watch.terminate()
            try:
                self._watch.wait(timeout=3)
            except Exception:
                self._watch.kill()
        except Exception:
            pass
        self._watch = None
        return {"ok": True, "message": "watch 已停止"}

    def scan_progress(self) -> dict:
        """优先读 DLL 的真实 type scan 区域进度；没有时按 125 秒估算。"""
        import datetime as _dt
        import re
        dll = self.dll_state()
        if dll.get("ready"):
            return {"phase": "ready", "progress": 1.0, "elapsed_s": None,
                    "estimate_s": 125, "message": "类型表已就绪"}
        tail = self._read_log(300)
        start_ts = None
        ready = False
        last_scan = None
        for line in tail:
            s = str(line)
            mtime = None
            try:
                if s.startswith("[") and "]" in s:
                    t = s[1:s.index("]")]
                    hh, mm, ss = (int(x) for x in t.split(":"))
                    mtime = _dt.datetime.combine(_dt.date.today(), _dt.time(hh, mm, ss)).timestamp()
            except Exception:
                mtime = None
            if "=== v3bridge" in s:
                start_ts = mtime or time.time()
                ready = False
                last_scan = None
            elif "types ready" in s:
                ready = True
            m = re.search(r"type scan:\s*(\d+)/(\d+) regions,\s*(\d+) keys,\s*(\d+) ms", s)
            if m:
                last_scan = {
                    "current": int(m.group(1)), "total": int(m.group(2)),
                    "keys": int(m.group(3)), "elapsed_s": round(int(m.group(4)) / 1000.0, 1),
                }
        if self._last_action == "inject" and self._last_action_at:
            start_ts = max(start_ts or 0, self._last_action_at)
        if ready:
            return {"phase": "ready", "progress": 1.0, "elapsed_s": None,
                    "estimate_s": 125, "message": "类型表已就绪", "regions": None}
        if last_scan and dll.get("loaded"):
            progress = max(0.03, min(0.97, last_scan["current"] / max(1, last_scan["total"])))
            return {"phase": "scanning", "progress": round(progress, 3),
                    "elapsed_s": last_scan["elapsed_s"], "estimate_s": 125,
                    "message": "区域 %d/%d · %ss" % (last_scan["current"], last_scan["total"], int(last_scan["elapsed_s"])),
                    "regions": last_scan}
        recent_inject = (self._last_action == "inject" and self._last_action_at
                         and time.time() - self._last_action_at < 300)
        if start_ts and (dll.get("loaded") or recent_inject) and self._state != "error":
            elapsed = max(0.0, time.time() - start_ts)
            progress = max(0.03, min(0.95, elapsed / 125.0))
            return {"phase": "scanning", "progress": round(progress, 3),
                    "elapsed_s": round(elapsed, 1), "estimate_s": 125,
                    "message": "类型表扫描 %ds / 预计 ~125s" % int(elapsed), "regions": None}
        if dll.get("loaded"):
            return {"phase": "waiting", "progress": 0.08, "elapsed_s": None,
                    "estimate_s": 125, "message": "DLL 已载入，等待进入存档", "regions": None}
        return {"phase": "idle", "progress": 0.0, "elapsed_s": None,
                "estimate_s": 125, "message": "DLL 未注入", "regions": None}

    def _start_worker(self) -> None:
        try:
            self.app.data.queue_paused = True
            r = self.inject()
            if not r.get("ok"):
                self._error = r.get("error") or "DLL 注入失败"
                self._state = "error"
                return
            w = self.start_watch()
            if not w.get("ok"):
                self._watch_error = w.get("error") or "watch 启动失败"
            # 连续模式默认关闭；启动服务只负责 DLL/watch，由玩家显式开启 AI 连续运行
            # DLL 未完成 types ready 前保持 starting，避免页面误以为已就绪
            self._state = "starting"
            self._error = ""
        except Exception as exc:
            self._error = "%s: %s" % (type(exc).__name__, exc)
            self._state = "error"

    def start_all(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "服务操作进行中"}
        try:
            if self._state in ("stopping", "partial"):
                return {"ok": False, "error": "当前是部分停止/卸载中，请先完成停止，再重新启动"}
            self._state = "starting"
            self._error = ""
            self._last_action = "start"
            self._last_action_at = time.time()
            threading.Thread(target=self._start_worker, daemon=True,
                             name="v3ai-service-start").start()
            return {"ok": True, "message": "正在启动 DLL / watch / 连续模式",
                    "service": self.status()}
        finally:
            self._lock.release()

    def stop_all(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "服务操作进行中"}
        try:
            self._state = "stopping"
            self.app.data.queue_paused = True
            self.app.set_loop({"enabled": False})
            w = self.stop_watch()
            visible = self._game_visible()
            dll_now = self.dll_state()
            if not dll_now.get("loaded"):
                self._state = "stopped"
                self._last_action = "stop"
                self._last_action_at = time.time()
                self._error = ""
                return {"ok": True, "message": "连续/watch 已停止；DLL 本就不在运行",
                        "watch": w, "service": self.status()}
            if visible is False and not self._agent_available():
                # 这个会话看不到游戏进程：可以停 watch/连续，但不能安全卸载后再注入
                self._state = "partial"
                self._last_action = "stop"
                self._last_action_at = time.time()
                self._error = "当前会话看不到 victoria3.exe，已停止连续/watch，但保留 DLL"
                return {"ok": True, "warning": self._error,
                        "message": "已停止连续/watch；DLL 保留（当前会话无法重新注入）",
                        "watch": w, "service": self.status()}
            if visible is False and self._agent_available():
                r = self.agent_call("unload", timeout=25)
                pending = bool((r.get("data") or {}).get("pending"))
                if pending:
                    self._state = "stopping"
                    self._error = "卸载请求已发送，等待 DLL 完成当前扫描"
                else:
                    self._state = "stopped" if r.get("ok") else "partial"
                    self._error = "" if r.get("ok") else (r.get("error") or r.get("message") or "")
                self._last_action = "stop"
                self._last_action_at = time.time()
                return {"ok": bool(r.get("ok") or pending),
                        "pending": pending,
                        "message": r.get("message") or ("已停止" if r.get("ok") else "卸载失败"),
                        "watch": w, "unload": r, "service": self.status()}
            u = self.unload()
            self._state = "stopping" if u.get("pending") else "stopped"
            self._last_action = "stop"
            self._last_action_at = time.time()
            self._error = ("卸载请求已发送，等待 DLL 完成当前扫描"
                           if u.get("pending") else ("") if u.get("ok") else (u.get("error") or ""))
            return {"ok": True, "pending": bool(u.get("pending")),
                    "message": u.get("message") or "已停止 DLL / watch / 连续模式",
                    "watch": w, "unload": u, "service": self.status()}
        finally:
            self._lock.release()

    def status(self) -> dict:
        dll = self.dll_state()
        if self._state == "stopping" and not dll.get("loaded"):
            self._state = "stopped"
            self._error = ""
        elif (self._state == "stopping" and dll.get("loaded")
              and self._last_action_at and time.time() - self._last_action_at > 20):
            self._state = "partial"
            self._error = "DLL 未响应卸载（world.current 仍新鲜）；请重启游戏后再启动服务"
        if not dll.get("loaded") and self._state not in ("starting", "stopping"):
            try:
                os.remove(os.path.join(self.bridge_dir, "unload.flag"))
            except OSError:
                pass
        if dll.get("ready"):
            self.app.data.queue_paused = False
        agent = self.agent_status()
        watch_running = bool(self._watch is not None and self._watch.poll() is None) \
            or bool(agent.get("online") and agent.get("watch_running"))
        if self._state == "unknown":
            self._state = ("running" if dll.get("ready")
                           else ("partial" if dll.get("loaded") else "stopped"))
        if self._state == "starting" and dll.get("ready"):
            self._state = "running"
            self._error = ""
        if self._state == "partial" and dll.get("ready"):
            self._state = "running"
            self._error = ""
        loop = self.app.loop.status()
        meta = {}
        try:
            meta = self.app.data.payload().get("meta") or {}
        except Exception:
            pass
        # 文件模式的外部 watch 可能已经退出：快照过期且本服务没有 watch 时自动补一个。
        try:
            if (not self.app.data.live and not watch_running and meta.get("stale")
                    and self._state not in ("stopping", "error")
                    and self._game_visible() is not False
                    and time.time() - self._last_watch_try > 30):
                self._last_watch_try = time.time()
                w = self.start_watch()
                if not w.get("ok"):
                    self._watch_error = w.get("error") or "自动启动 watch 失败"
        except Exception as exc:
            self._watch_error = "自动启动 watch 失败：%s" % exc
        provider = self.config.provider()
        return {
            "state": self._state,
            "game_visible": self._game_visible(),
            "live": bool(self.app.data.live),
            "startup": {"active": False, "remaining_s": 0.0, "total_s": 0.0, "progress": 0.0},
            "agent": agent,
            "dll": dll,
            "scan": self.scan_progress(),
            "watch": {
                "running": watch_running,
                "external": self._watch_external,
                "pid": self._watch.pid if (self._watch and self._watch.poll() is None)
                else agent.get("watch_pid"),
                "log": self._watch_log(),
                "error": self._watch_error,
            },
            "loop": loop,
            "snapshot": {
                "path": self.app.data.snapshot_path,
                "captured_at": meta.get("captured_at"),
                "age_s": meta.get("age_s"),
                "stale": meta.get("stale"),
            },
            "llm": {
                "provider": self.config.provider_name,
                "model": provider.get("model"),
                "base_url": provider.get("base_url"),
            },
            "error": self._error,
            "last_action": self._last_action,
            "last_action_at": self._last_action_at,
        }


class WebApp:
    def __init__(self, config: Config, snapshot_path: str | None = None,
                 live: bool = False, queue_interval: float = 5.0):
        self.config = config
        self.data = DataService(config, snapshot_path=snapshot_path, live=live,
                                queue_interval=queue_interval)
        self.chat = ChatService(self.data, config)
        self.decision = DecisionService(self.data, config)
        self.strategy_store = StrategyStore(config.strategy_path)
        self.loop = AutoLoop(self)
        self.service = ServiceManager(self)
        self.decision.ready_check = self.service.dll_ready

    # -- web settings -------------------------------------------------
    def settings(self) -> dict:
        cfg = self.config
        providers = []
        for name, p in (cfg.data.get("providers") or {}).items():
            providers.append({
                "name": name,
                "label": p.get("label") or name,
                "model": p.get("model") or "",
                "base_url": p.get("base_url") or "",
                "has_key": bool(p.get("api_key")),
            })
        a = cfg.agent
        return {
            "ok": True,
            "active_provider": cfg.provider_name,
            "providers": providers,
            "agent": {
                "mode": a.get("mode") or "oneshot",
                "toolset": a.get("toolset") or "core",
                "max_actions_per_cycle": a.get("max_actions_per_cycle", 1),
                "max_levels_per_action": a.get("max_levels_per_action", 5),
                "max_tool_rounds": a.get("max_tool_rounds", 3),
                "max_tool_result_chars": a.get("max_tool_result_chars", 16000),
                "max_context_chars": a.get("max_context_chars", 0),
                "max_tokens": a.get("max_tokens") or 0,
                "max_history_cycles": a.get("max_history_cycles", 12),
                "freeze_cycle": bool(a.get("freeze_cycle", True)),
                "brief_level": a.get("brief_level") or "compact",
            },
            "snapshot": {
                "path": self.data.snapshot_path,
                "live": self.data.live,
                "live_error": self.data.live_error,
            },
            "loop": {
                "enabled": bool(self.config.data.get("loop", {}).get("enabled", True)),
                "interval_s": self.config.data.get("loop", {}).get("interval_s", 20),
                "execute": bool(self.config.data.get("loop", {}).get("execute", True)),
                "pause_if_stale": bool(self.config.data.get("loop", {}).get("pause_if_stale", True)),
            },
        }

    def update_settings(self, patch: dict) -> dict:
        cfg = self.config
        providers = cfg.data.get("providers") or {}
        name = str(patch.get("provider") or "").strip()
        if name and name in providers:
            cfg.data["active_provider"] = name
        active = cfg.provider_name
        model = str(patch.get("model") or "").strip()
        base_url = str(patch.get("base_url") or "").strip()
        api_key = str(patch.get("api_key") or "").strip()
        if model:
            cfg.set("providers.%s.model" % active, model)
        if base_url:
            cfg.set("providers.%s.base_url" % active, base_url)
        if api_key:
            cfg.set("providers.%s.api_key" % active, api_key)
        mode = str(patch.get("mode") or "").lower()
        if mode in ("oneshot", "tools"):
            cfg.set("agent.mode", mode)
        toolset = str(patch.get("toolset") or "").lower()
        if toolset in ("core", "full"):
            cfg.set("agent.toolset", toolset)
        if patch.get("max_actions") is not None:
            cfg.set("agent.max_actions_per_cycle",
                    max(0, min(8, int(patch.get("max_actions")))))
        if patch.get("max_levels") is not None:
            cfg.set("agent.max_levels_per_action",
                    max(0, min(10, int(patch.get("max_levels")))))
        if patch.get("max_tool_rounds") is not None:
            cfg.set("agent.max_tool_rounds",
                    max(0, min(200, int(patch.get("max_tool_rounds")))))
        if patch.get("max_tool_result_chars") is not None:
            cfg.set("agent.max_tool_result_chars",
                    max(0, min(2_000_000, int(patch.get("max_tool_result_chars")))))
        if patch.get("max_context_chars") is not None:
            cfg.set("agent.max_context_chars",
                    max(0, min(4_000_000, int(patch.get("max_context_chars")))))
        if patch.get("max_tokens") is not None:
            n = max(0, min(131072, int(patch.get("max_tokens"))))
            cfg.set("agent.max_tokens", n if n > 0 else None)
        if patch.get("max_history_cycles") is not None:
            cfg.set("agent.max_history_cycles",
                    max(0, min(10000, int(patch.get("max_history_cycles")))))
        if patch.get("freeze") is not None:
            cfg.set("agent.freeze_cycle", bool(patch.get("freeze")))
        loop = cfg.data.setdefault("loop", {})
        if patch.get("loop_enabled") is not None:
            loop["enabled"] = bool(patch.get("loop_enabled"))
        if patch.get("loop_interval") is not None:
            loop["interval_s"] = max(0, min(3600, int(patch.get("loop_interval"))))
        if patch.get("loop_execute") is not None:
            loop["execute"] = bool(patch.get("loop_execute"))
        if patch.get("loop_stale") is not None:
            loop["pause_if_stale"] = bool(patch.get("loop_stale"))
        cfg.save()
        self.chat.reload()
        if getattr(self, "loop", None) is not None:
            self.loop.update()
        return self.settings()

    def test_settings(self, patch: dict) -> dict:
        cfg = self.config
        name = str(patch.get("provider") or cfg.provider_name)
        try:
            prof = cfg.provider(name)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if patch.get("model"):
            prof["model"] = str(patch["model"])
        if patch.get("base_url"):
            prof["base_url"] = str(patch["base_url"])
        if patch.get("api_key"):
            prof["api_key"] = str(patch["api_key"])
        try:
            ok, msg = LLMClient(prof).ping()
            return {"ok": bool(ok), "message": msg}
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    # -- strategy board ------------------------------------------------
    def strategy(self) -> dict:
        st = self.strategy_store.active()
        hist = []
        for i, h in enumerate(self.strategy_store.history(50)):
            try:
                sh = Strategy.from_dict(h)
                hist.append({"index": i, "name": sh.name, "source": sh.source,
                             "version": sh.version, "created_at": sh.created_at})
            except Exception:
                continue
        focus = []
        try:
            rt = self.data.rt
            if rt is not None and rt.ctx is not None:
                dev = st.development or {}
                focus = rt.ctx.focus_states(dev.get("mode") or "nationwide",
                                            int(dev.get("max_states_per_cycle") or 0),
                                            dev.get("priority_states") or [])
        except Exception:
            focus = []
        return {"ok": True, "active": st.to_dict(), "history": hist,
                "buildings": sorted(self.data.rt.buildings_kb)
                if self.data.rt is not None else [],
                "states": self._state_options(), "focus_states": focus}

    def _state_options(self) -> list[dict]:
        try:
            self.data.refresh_if_needed()
        except Exception:
            pass
        rt = self.data.rt
        if rt is None or rt.snap is None:
            return []
        rows = []
        for s in rt.snap.states:
            meta = rt.ctx.state_meta.get(s.id) if rt.ctx is not None else None
            region = getattr(meta, "region", None) or ""
            rows.append({
                "id": int(s.id),
                "label": (NAMES_MAPS.get("states") or {}).get(region)
                or STATE_LABELS.get(region, region or ("州 %d" % int(s.id))),
                "region": region,
                "population": int(s.population or 0),
            })
        rows.sort(key=lambda x: (-x["population"], x["id"]))
        return rows

    def _sync_strategy(self) -> None:
        """保存后让当前世界模型/审查器立刻使用新策略。"""
        try:
            if self.data.rt is not None:
                self.data.rt.strategy = self.strategy_store.active()
                if self.data.rt.ctx is not None:
                    self.data.rt.ctx.strategy = self.data.rt.strategy
                self.data.rt._reviewer = None
        except Exception:
            pass

    def save_strategy(self, payload: dict, source: str = "player") -> dict:
        if payload.get("source") in ("player", "ai"):
            source = str(payload.get("source"))
        raw = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else payload
        if not isinstance(raw, dict):
            return {"ok": False, "error": "策略必须是 JSON 对象"}
        try:
            st = Strategy.from_dict(raw)
            st.source = source
            saved = self.strategy_store.set(st, source=source)
        except Exception as exc:
            return {"ok": False, "error": "策略不合法：%s" % exc}
        self._sync_strategy()
        return {"ok": True, "active": saved.to_dict()}

    def activate_strategy(self, payload: dict) -> dict:
        try:
            idx = int(payload.get("index"))
            saved = self.strategy_store.activate(idx)
        except Exception as exc:
            return {"ok": False, "error": "切换方略失败：%s" % exc}
        self._sync_strategy()
        return {"ok": True, "active": saved.to_dict()}

    def reset_strategy(self) -> dict:
        try:
            st = self.strategy_store.reset()
            self._sync_strategy()
            return {"ok": True, "active": st.to_dict()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def draft_strategy(self) -> dict:
        ctx = self.data.chat_context()
        if not ctx.get("ok", True):
            return {"ok": False, "error": ctx.get("error") or "世界数据未就绪"}
        active = self.strategy_store.active().summary()
        schema = {
            "name": "策略名",
            "rationale": "为什么现在用这个策略",
            "objectives": [
                {"metric": "gdp_growth", "weight": 0.35, "direction": "max"},
                {"metric": "weekly_balance", "weight": 0.2, "direction": "max"},
                {"metric": "standard_of_living", "weight": 0.2, "direction": "max"},
                {"metric": "radicals", "weight": 0.15, "direction": "min"},
                {"metric": "construction_capacity", "weight": 0.1, "direction": "max"},
            ],
            "constraints": {
                "max_actions_per_cycle": 0,
                "max_levels_per_action": 0,
                "min_treasury": 0,
                "allow_conditional_pm": False,
                "block_pm_patterns": ["worker_exploitation_"],
                "priority_buildings": [],
            },
            "development": {
                "mode": "nationwide",
                "max_states_per_cycle": 0,
                "notes": "nationwide / core / coastal / agriculture / resources / capital / balanced",
            },
            "notes": "给执行层的补充说明；0 表示不限制",
        }
        messages = [
            {"role": "system", "content": (
                "你是 Victoria 3 的御前策略顾问。根据当前国家数据和当前策略，"
                "起草一份新的发展策略。只输出严格 JSON，不要 markdown。"
                "rationale / notes 各不超过 120 字；objectives 3-5 条；constraints 只写关键项。"
                "objectives 的 metric 可用 gdp_growth / weekly_balance / "
                "standard_of_living / radicals / construction_capacity / employment / "
                "bureaucracy 等；constraints 里 0 表示不限制。"
                "development.mode 可选 nationwide / core / coastal / agriculture / "
                "resources / capital / balanced，决定建设优先区域。"
                "不要编造州级数据。")},
            {"role": "user", "content": json.dumps({
                "current_data": ctx,
                "current_strategy": active,
                "output_schema_example": schema,
            }, ensure_ascii=False)},
        ]
        t0 = time.time()
        try:
            resp = LLMClient(self.config.provider()).chat(
                messages, tools=None, temperature=0.5, max_tokens=4096)
            obj = _parse_json_object(resp.content or resp.reasoning or "")
            if not isinstance(obj, dict):
                raw = (resp.content or resp.reasoning or "")[:700]
                return {"ok": False, "error": "模型没有返回可解析的策略 JSON",
                        "raw": raw}
            st = Strategy.from_dict(obj)
            problems = st.validate(ai=True)
            return {"ok": True, "strategy": st.to_dict(), "problems": problems,
                    "usage": resp.usage or {}, "latency_s": round(time.time() - t0, 2)}
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    # -- continuous loop ----------------------------------------------
    def set_loop(self, patch: dict) -> dict:
        loop = self.config.data.setdefault("loop", {})
        if patch.get("enabled") is not None:
            loop["enabled"] = bool(patch.get("enabled"))
        if patch.get("interval_s") is not None:
            loop["interval_s"] = max(0, min(3600, int(patch.get("interval_s"))))
        if patch.get("execute") is not None:
            loop["execute"] = bool(patch.get("execute"))
        if patch.get("pause_if_stale") is not None:
            loop["pause_if_stale"] = bool(patch.get("pause_if_stale"))
        self.config.save()
        self.loop.update()
        return {"ok": True, "loop": self.loop.status()}

    def stop(self) -> None:
        self.loop.stop()
        try:
            self.service.stop_watch()
        except Exception:
            pass
        self.data.stop()
        if self.decision._rt is not None:
            try:
                self.decision._rt.close()
            except Exception:
                pass


def _send_json(handler, obj: Any, status: int = 200) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(data)


def _read_json(handler) -> dict:
    try:
        n = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        n = 0
    if n <= 0 or n > 2_000_000:
        return {}
    raw = handler.rfile.read(n)
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "v3ai-web/0.1"

        def log_message(self, fmt, *args):
            return

        def _serve_file(self, name: str) -> None:
            path = os.path.join(WEB_DIR, name)
            if not os.path.isfile(path):
                self.send_error(404)
                return
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".webp": "image/webp",
            }.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._serve_file("index.html")
                return
            if path in ("/app.css", "/app.js", "/favicon.svg"):
                self._serve_file(path.lstrip("/"))
                return
            if path == "/api/country":
                _send_json(self, app.data.payload())
                return
            if path == "/api/status":
                out = app.data.status()
                out["pending_count"] = app.decision.pending_count
                out["loop"] = app.loop.status()
                _send_json(self, out)
                return
            if path == "/api/loop":
                _send_json(self, {"ok": True, "loop": app.loop.status()})
                return
            if path == "/api/service":
                _send_json(self, {"ok": True, "service": app.service.status()})
                return
            if path == "/api/names":
                _send_json(self, {"ok": True, "source": NAMES_DATA.get("source"),
                                  "maps": NAMES_MAPS})
                return
            if path == "/api/settings":
                _send_json(self, app.settings())
                return
            if path == "/api/strategy":
                _send_json(self, app.strategy())
                return
            if path == "/api/ping":
                _send_json(self, {"ok": True, "ts": now_iso()})
                return
            self.send_error(404)

        def do_POST(self):
            path = urlparse(self.path).path
            body = _read_json(self)
            if path == "/api/chat":
                out = app.chat.ask(str(body.get("message") or ""))
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            if path == "/api/decide":
                out = app.decision.run(execute=bool(body.get("execute")))
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            if path == "/api/execute":
                out = app.decision.execute_pending()
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            if path == "/api/settings":
                out = app.update_settings(body)
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            if path == "/api/settings/test":
                out = app.test_settings(body)
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            if path == "/api/strategy":
                out = app.save_strategy(body, source="player")
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            if path == "/api/strategy/draft":
                out = app.draft_strategy()
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            if path == "/api/strategy/activate":
                out = app.activate_strategy(body)
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            if path == "/api/strategy/reset":
                out = app.reset_strategy()
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            if path == "/api/loop":
                out = app.set_loop(body)
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            if path == "/api/service/start":
                out = app.service.start_all()
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            if path == "/api/service/stop":
                out = app.service.stop_all()
                _send_json(self, out, 200 if out.get("ok") else 400)
                return
            self.send_error(404)

    return Handler


def serve(config: Config, host: str = "127.0.0.1", port: int = 8765,
          snapshot_path: str | None = None, live: bool = False,
          open_browser: bool = False, queue_interval: float = 5.0) -> int:
    app = WebApp(config, snapshot_path=snapshot_path, live=live,
                 queue_interval=queue_interval)
    handler = make_handler(app)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = "http://%s:%d" % (host, httpd.server_address[1])
    print("v3ai web: %s" % url)
    app.loop.start()
    try:
        meta = app.data.payload().get("meta") or {}
        print("snapshot: %s  country=%s" % (meta.get("captured_at"),
                                             meta.get("country_name")))
    except Exception as exc:
        print("snapshot: error %s" % exc)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        httpd.server_close()
        app.stop()
    return 0
