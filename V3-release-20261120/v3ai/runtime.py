# -*- coding: utf-8 -*-
"""运行时：把配置、快照、世界模型、策略、审查、执行端串起来。

一个 ``Runtime`` 就是一个进程内的"决策会话"：
  * ``refresh()`` 重新读快照/队列，重建 ``DecisionContext``；
  * ``propose()`` 让审查层判断一条动作；
  * ``execute_pending()`` 把通过的动作交给执行端，并把结果记进 last_cycle；
  * ``brief()`` 给 LLM 的当轮概况（不会把 173KB 的原始快照塞进去）。
"""
from __future__ import annotations

import os
import time
from typing import Any

from v3calc.gamedata import GameData, discover_game_dir
from v3calc.model import Snapshot

from .config import Config
from .executor import ExecutionResult, Executor, make_executor
from .advisor_bridge import AdvisorBridge
from .review import Command, Reviewer, ReviewResult
from .strategy import Strategy, StrategyStore
from .util import (append_jsonl, ensure_dir, now_iso, read_jsonl,
                   truncate_json)
from .world import (DecisionContext, load_building_kb, load_construction_costs,
                    load_state_meta, mark_resource_buildings)


class RuntimeErrorV3(RuntimeError):
    pass


def _compact_world(ov: dict, queue_pressure: dict | None = None) -> dict:
    """给 LLM 的首轮概况：只留决策入口，细节让模型自己调工具。"""
    meta = dict(ov.get("meta") or {})
    meta.pop("state_meta_ok", None)
    country = dict(ov.get("country") or {})
    budget = dict(ov.get("budget") or {})
    budget = {k: budget.get(k) for k in (
        "income_total", "expense_total", "balance", "tax_level",
        "treasury", "credit_limit") if k in budget}
    construction = dict(ov.get("construction") or {})
    construction = {k: construction.get(k) for k in (
        "capacity_per_week", "from_buildings", "note") if k in construction}
    gdp = dict(ov.get("gdp") or {})
    gdp = {
        "annual": gdp.get("annual"), "per_capita": gdp.get("per_capita"),
        "price_basis": gdp.get("price_basis"),
        "top_goods": (gdp.get("top_goods") or [])[:3],
        "top_building_types": (gdp.get("top_building_types") or [])[:3],
    }
    market = dict(ov.get("market") or {})
    market = {
        "most_expensive": (market.get("most_expensive") or [])[:3],
        "cheapest": (market.get("cheapest") or [])[:3],
    }
    states = dict(ov.get("states") or {})
    states = {
        "count": states.get("count"),
        "top_population": (states.get("top_population") or [])[:3],
        "infrastructure_overloaded": (states.get("infrastructure_overloaded") or [])[:3],
        "radicals_top": (states.get("radicals_top") or [])[:3],
    }
    buildings = dict(ov.get("buildings") or {})
    buildings = {
        "count": buildings.get("count"),
        "top_types": (buildings.get("top_types") or [])[:5],
        "understaffed": (buildings.get("understaffed") or [])[:3],
        "unprofitable": (buildings.get("unprofitable") or [])[:3],
    }
    politics = dict(ov.get("politics") or {})
    politics = {
        "laws": [x.get("name") for x in (politics.get("laws") or [])],
        "interest_groups": (politics.get("interest_groups") or [])[:3],
        "movements": (politics.get("movements") or [])[:3],
        "institutions": (politics.get("institutions") or [])[:5],
        "decrees": (politics.get("decrees") or [])[:5],
    }
    queue = dict(ov.get("queue") or {})
    return {
        "meta": meta, "country": country, "budget": budget,
        "construction": construction, "gdp": gdp,
        "market": market, "states": states, "buildings": buildings,
        "politics": politics,
        "techs": {"count": (ov.get("techs") or {}).get("count")},
        "queue": {"count": queue.get("count"), "entries": (queue.get("entries") or [])[:3],
                  "pressure": queue_pressure or {}},
        "strategy": ov.get("strategy"),
        "pending": (ov.get("pending") or [])[:8],
        "feedback": ov.get("feedback"),
        "notes": (ov.get("notes") or [])[:5],
        "executor": ov.get("executor"),
        "cycle_frozen": ov.get("cycle_frozen"),
    }


class Runtime:
    def __init__(self, config: Config, *, snapshot_path: str | None = None,
                 source: str | None = None, mode: str | None = None,
                 strategy_path: str | None = None, quiet: bool = False):
        self.config = config
        self.quiet = quiet
        self.source = (source or config.get("source", "auto") or "auto").lower()
        self.mode = mode
        self.notes: list[str] = []
        self.cycle_no = 0
        self.last_cycle: dict | None = None
        self.pending: list[Command] = []
        self.cycle_active = False
        self._caps: dict = {}

        self.game_dir = config.path("game_dir") or discover_game_dir()
        if not self.game_dir:
            raise RuntimeErrorV3("找不到 Victoria 3 游戏目录（设置 V3_GAME 或 config.paths.game_dir）")
        self.gd = GameData(self.game_dir)
        self.state_meta, self._state_meta_info = load_state_meta(config.path("state_meta"))
        if not self.state_meta:
            self.notes.append("州静态地理缺失：建造上限/资源校验会降级为 warning")
        self.buildings_kb, self._kb_info = load_building_kb(self.gd)
        mark_resource_buildings(self.buildings_kb, self.state_meta)
        self.costs = load_construction_costs(self.gd)

        self.strategy_store = StrategyStore(strategy_path or config.strategy_path)
        self.strategy = self.strategy_store.active()

        self.executor: Executor = make_executor(config, mode)
        self.snapshot_path = snapshot_path or config.path("snapshot")
        self.snap: Snapshot | None = None
        self.queue: list[dict] = []
        self.ctx: DecisionContext | None = None
        self.bridge: AdvisorBridge | None = None
        self._reviewer: Reviewer | None = None
        ensure_dir(config.path("state_dir"))
        self.last_cycle = self._load_last_cycle()
        self.refresh()

    def _load_last_cycle(self) -> dict | None:
        try:
            rows = read_jsonl(self.config.journal_path, tail=20)
        except Exception:
            return None
        for r in reversed(rows):
            if r.get("type") == "cycle":
                return r
        return None

    # -- 快照 ---------------------------------------------------------
    def _load_snapshot(self, path: str | None = None) -> Snapshot:
        path = path or self.snapshot_path
        if self.source in ("live", "auto"):
            try:
                from v3live import proc as _proc
                pids = _proc.find_pids("victoria3.exe")
            except Exception:
                pids = []
            if pids:
                from v3calc.live import LiveReader
                try:
                    reader = LiveReader(country_id=None, progress=None)
                    snap = reader.read()
                    snap.game.source = snap.game.source or "live"
                    return snap
                except Exception as exc:
                    if self.source == "live":
                        raise
                    self.notes.append("live 读取失败（%s），回退到快照文件：%s" % (exc, path))
            if self.source == "live":
                raise RuntimeErrorV3("未发现 victoria3.exe 进程；无法 live 读取")
            self.notes.append("未发现游戏进程，回退到快照文件：%s" % path)
        if not path or not os.path.isfile(path):
            raise RuntimeErrorV3("快照文件不存在：%s" % path)
        snap = Snapshot.load(path)
        if snap.game.source == "live":
            # 文件是之前落盘的 live 快照，保留来源但标记读取方式
            snap.game.notes = list(snap.game.notes or []) + ["loaded-from-file"]
        return snap

    def refresh(self) -> None:
        self.strategy = self.strategy_store.active()
        self.snap = self._load_snapshot()
        # 队列：只有 bridge 模式才真去读；dry-run 保持空
        if self.executor.mode == "bridge":
            try:
                self.queue = self.executor.queue()
            except Exception as exc:
                self.queue = []
                self.notes.append("读取建造队列失败：%s" % exc)
        elif self.executor.mode == "mock":
            try:
                self.queue = self.executor.queue()
            except Exception:
                self.queue = []
        self.bridge = self._make_bridge()
        self.ctx = DecisionContext(
            self.snap, self.gd, self.state_meta, self.buildings_kb, self.costs,
            strategy=self.strategy, queue=self.queue, pending=self.pending,
            meta_info=self._state_meta_info, bridge=self.bridge,
        )
        self._sync_pending()
        try:
            self._caps = self.executor.capabilities()
        except Exception as exc:
            self._caps = {"mode": self.executor.mode, "error": str(exc)}
        self._reviewer = Reviewer(self.config, self.strategy)

    def _make_bridge(self) -> AdvisorBridge | None:
        """决策计算层适配器；不可用时静默降级（旧逻辑继续跑）。"""
        if not bool(self.config.get("advisor.enabled", True)):
            return None
        try:
            return AdvisorBridge(
                self.snap, self.gd, self.state_meta, self.buildings_kb,
                strategy=self.strategy, queue=self.queue, pending=self.pending,
                config=self.config)
        except Exception as exc:                       # noqa: BLE001
            self.notes.append("决策计算层不可用（%s）：退回旧估算" % exc)
            return None

    def _sync_pending(self) -> None:
        """待执行动作变化后，让 ctx 与桥接层看到同一份列表。"""
        if self.ctx is not None:
            self.ctx.pending = list(self.pending)
        if self.bridge is not None:
            self.bridge.pending = list(self.pending)

    def advise_payload(self, char_budget: int | None = None,
                       top: int | None = None) -> dict:
        """给 LLM 的"算好的简报"：文本 + 信号 + 贪心方案前三项。"""
        if self.bridge is None or not self.bridge.available:
            return {}
        cfg = dict(self.config.data.get("advisor") or {})
        budget = int(char_budget or cfg.get("char_budget") or 2400)
        n = int(top or cfg.get("top") or 8)
        text = self.bridge.brief_text(char_budget=budget, top=n)
        if not text:
            return {}
        plan = self.bridge.plan(max_items=int(cfg.get("plan_items") or 3))
        items = []
        for it in (plan.get("items") or [])[:3]:
            items.append({k: it.get(k) for k in (
                "state_id", "building", "levels", "annual_value",
                "per_point", "weeks", "risk", "chain")})
        # 信号只留决策需要的三个字段：正文里已有完整句子，别再重复 evidence
        sig = [{"kind": s.get("kind"), "severity": s.get("severity"),
                "title": s.get("title")}
               for s in self.bridge.signals()[:6]]
        return {
            "text": text,
            "signals": sig,
            "plan": {"items": items,
                     "total_points": plan.get("total_points"),
                     "total_annual_value": plan.get("total_annual_value"),
                     "weeks": plan.get("weeks")},
            "capacity": (self.bridge.capacity(limit=1).get("capacity") or [])[:1],
            "note": ("text 是算好的简报：年增值已扣价格反馈、risk 已计入；"
                     "建造力只算成本不估值（看 capacity 的可控性判定）；"
                     "需要细节再调 build_candidates / advise_plan / advise_capacity。"),
        }

    def _record_advise(self, commands: list[Command]) -> None:
        """把真正执行成功的建造记进计算层台账（预测 → 日后对账）。"""
        if not commands or self.bridge is None:
            return
        try:
            rec = self.bridge.record(commands)
        except Exception as exc:                       # noqa: BLE001
            self.journal_event({"type": "advise_record", "ok": False,
                                "error": str(exc)})
            return
        if rec:
            self.journal_event({"type": "advise_record", "ok": True,
                                "id": rec.get("id"),
                                "predicted_annual_value":
                                    rec.get("predicted_annual_value")})

    # -- 回合冻结 -----------------------------------------------------
    def begin_cycle(self) -> None:
        """冻结本回合的世界数据：LLM 思考期间工具只能读这份快照。"""
        if self.cycle_active:
            return
        self.refresh()
        if self.config.agent.get("freeze_cycle", True):
            self.cycle_active = True

    def end_cycle(self) -> None:
        self.cycle_active = False

    # -- 策略 ---------------------------------------------------------
    def reviewer(self) -> Reviewer:
        if self._reviewer is None:
            self._reviewer = Reviewer(self.config, self.strategy)
        return self._reviewer

    def set_strategy(self, strategy: Strategy | dict, source: str = "player") -> Strategy:
        if source == "ai" and not self.config.agent.get("allow_ai_strategy_switch", True):
            raise RuntimeErrorV3("配置禁止 AI 切换策略（agent.allow_ai_strategy_switch=false）")
        st = self.strategy_store.set(strategy, source=source)
        self.strategy = st
        if self.ctx is not None:
            self.ctx.strategy = st
        self._reviewer = Reviewer(self.config, st)
        self.journal_event({"type": "strategy", "source": source,
                            "strategy": st.summary()})
        return st

    def strategy_history(self, tail: int = 10) -> list[dict]:
        return self.strategy_store.history(tail)

    # -- 动作 ---------------------------------------------------------
    def propose(self, cmd: Command | dict) -> ReviewResult:
        if isinstance(cmd, dict):
            cmd = Command.from_dict(cmd)
        if self.ctx is None:
            return ReviewResult(False, "no_context", "世界模型未就绪")
        res = self.reviewer().review(cmd, self.ctx)
        cmd.review = res.to_dict()
        if res.approved:
            cmd.status = "approved"
            self.pending.append(cmd)
            self.ctx.pending.append(cmd)
        else:
            cmd.status = "rejected"
        self._sync_pending()
        return res

    def execute_pending(self, only_ids: set[str] | None = None) -> list[ExecutionResult]:
        if not self.pending:
            return []
        selected = [c for c in self.pending
                    if (only_ids is None or c.id in only_ids)
                    and getattr(c, "status", "") == "approved"]
        results: list[ExecutionResult] = []
        for cmd in selected:
            if self.executor.mode == "bridge" and self.config.review.get("require_world_loaded", True):
                caps = self.executor.capabilities()
                if caps.get("world") != "1":
                    r = ExecutionResult(cmd.id, False, "not_in_world",
                                        "游戏不在存档内（world.current.txt != 1），拒绝执行",
                                        False, 0.0,
                                        {"world": caps.get("world"),
                                         "error": caps.get("error")})
                    results.append(r)
                    cmd.status = "failed"
                    cmd.execution = r.to_dict()
                    continue
            try:
                r = self.executor.execute(cmd)
            except Exception as exc:
                r = ExecutionResult(cmd.id, False, "exception", str(exc), False, 0.0, {})
            results.append(r)
            cmd.execution = r.to_dict()
            cmd.status = "executed" if r.ok else "failed"
        # 已处理的移出 pending
        done_ids = {r.command_id for r in results}
        self.pending = [c for c in self.pending if c.id not in done_ids]
        self._sync_pending()
        # 真执行出去的动作才记台账（dry_run 不记，避免"没建也记账"）
        actually = [c for c in selected
                    if getattr(c, "status", "") == "executed"
                    and c.id in done_ids]
        self._record_advise(actually)
        if results:
            self.journal_event({"type": "execution",
                                "executions": [r.to_dict() for r in results]})
        return results

    def cancel(self, cmd_id: str) -> bool:
        before = len(self.pending)
        self.pending = [c for c in self.pending if c.id != cmd_id]
        self._sync_pending()
        return len(self.pending) != before

    # -- 反馈 / 日志 ---------------------------------------------------
    def journal_event(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("ts", now_iso())
        append_jsonl(self.config.journal_path, event)

    def recent_history(self, tail: int = 10) -> list[dict]:
        if tail and tail > 0:
            rows = read_jsonl(self.config.journal_path, tail=max(50, tail * 5))
        else:
            rows = read_jsonl(self.config.journal_path)
        out = []
        for r in rows:
            if r.get("type") != "cycle":
                continue
            out.append({
                "cycle": r.get("cycle"),
                "ts": r.get("ts"),
                "summary": r.get("summary"),
                "rationale": r.get("rationale"),
                "proposals": r.get("proposals", [])[:6],
                "executions": r.get("executions", [])[:6],
                "strategy": (r.get("strategy") or {}).get("name") if isinstance(r.get("strategy"), dict) else None,
            })
        return out[-tail:]

    def feedback(self) -> dict:
        if not self.last_cycle:
            return {"last": None}
        return {
            "last": {
                "cycle": self.last_cycle.get("cycle"),
                "summary": self.last_cycle.get("summary"),
                "proposals": self.last_cycle.get("proposals", []),
                "executions": self.last_cycle.get("executions", []),
                "rejected": [p for p in self.last_cycle.get("proposals", [])
                             if not p.get("approved")],
            }
        }

    # -- 概况 ---------------------------------------------------------
    def overview(self) -> dict:
        if self.ctx is None:
            raise RuntimeErrorV3("世界模型未就绪")
        ov = self.ctx.overview()
        if isinstance(ov.get("queue"), dict):
            ov["queue"]["pressure"] = self.ctx.queue_stats()
        ov["executor"] = dict(self._caps)
        ov["cycle_frozen"] = self.cycle_active
        ov["feedback"] = self.feedback()
        ov["notes"] = list(self.notes)[-8:]
        return ov

    def brief(self) -> dict:
        ov = self.overview()
        level = str(self.config.agent.get("brief_level") or "compact").lower()
        pressure = self.ctx.queue_stats() if self.ctx is not None else {}
        world = ov if level == "full" else _compact_world(ov, pressure)
        raw_hist = self.recent_history(self.config.agent.get("max_history_cycles", 12))
        cur_ver = self.strategy.version if self.strategy else None
        hist = []
        for h in raw_hist:
            sv = h.get("strategy") if isinstance(h.get("strategy"), dict) else {}
            if cur_ver is not None and sv.get("version") is not None and sv.get("version") != cur_ver:
                # 旧策略版本的 summary/rationale 会污染当前决策，直接不喂给模型
                continue
            hist.append({
                "started_at": h.get("started_at"), "cycle": h.get("cycle"),
                "strategy_version": sv.get("version"),
                "proposals": h.get("proposals") or [],
                "executions": h.get("executions") or [],
                "error": h.get("error"),
            })
        return {
            "world": world,
            "advise": self.advise_payload(),
            "recent_cycles": hist,
            "rules": [
                "GDP 绝对值误差约 ±0.5%（live + market + staffing + producers）；save 模式只看相对。",
                "PM 只有 available 可自动执行；conditional 需人工确认；locked 不可执行。",
                "upgrades 只按商品产出−投入排序，不含行政力/就业/死亡率/生活水平。",
                "建造/换 PM 执行后下一轮快照才可见；暂停时数值不变是正常的。",
                "每个 propose_* 会立即经过脚本审查；被拒时看 reasons 再调整，不要重复同一被拒动作。",
            ],
            "instruction": (
                "world 是 compact 概况。需要完整细节时调用 get_overview / query_* / calc_* / "
                "pm_* / build_candidates。决策用 propose_build / propose_pm 提交，每次提交会"
                "立刻审查；全部提交完调用 finish_cycle 写总结并结束本回合。"
            ),
        }

    # -- 关闭 ---------------------------------------------------------
    def close(self) -> None:
        try:
            self.executor.close()
        except Exception:
            pass
