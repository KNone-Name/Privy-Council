# -*- coding: utf-8 -*-
"""策略：玩家可定、AI 也可定，运行期可切换。

策略只描述"想优化什么 + 硬约束"，不直接产生动作；动作由 LLM 在策略下提议，
再由审查层逐条检查。这样策略切换不会绕过安全护栏。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .util import atomic_write_json, clamp, now_iso, read_json, safe_float

DEFAULT_OBJECTIVES: list[dict] = [
    {"metric": "gdp_growth", "weight": 0.35, "direction": "max", "target": None},
    {"metric": "weekly_balance", "weight": 0.20, "direction": "max", "target": None},
    {"metric": "standard_of_living", "weight": 0.15, "direction": "max", "target": None},
    {"metric": "radicals", "weight": 0.15, "direction": "min", "target": None},
    {"metric": "construction_capacity", "weight": 0.15, "direction": "max", "target": None},
]

DEFAULT_CONSTRAINTS: dict[str, Any] = {
    "max_actions_per_cycle": 8,
    "max_levels_per_action": 5,
    "max_build_actions_per_cycle": 6,
    "max_pm_actions_per_cycle": 4,
    "min_treasury": 0.0,
    "max_weekly_deficit": None,
    "allow_conditional_pm": False,
    "block_pm_patterns": [],
    "warn_pm_patterns": ["worker_exploitation_", "pm_simple_organization"],
    "forbidden_buildings": [],
    "priority_buildings": [],
    "reserve_construction_capacity": 0.0,
}


DEFAULT_DEVELOPMENT: dict[str, Any] = {
    "mode": "nationwide",
    "max_states_per_cycle": 0,
    "priority_states": [],
    "notes": "",
}


@dataclass
class Strategy:
    name: str = "均衡发展"
    source: str = "default"                 # default | player | ai
    version: int = 1
    created_at: str = field(default_factory=now_iso)
    rationale: str = ""
    objectives: list[dict] = field(default_factory=lambda: copy.deepcopy(DEFAULT_OBJECTIVES))
    constraints: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_CONSTRAINTS))
    development: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_DEVELOPMENT))
    notes: str = ""
    active: bool = True

    # -- 序列化 -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "version": self.version,
            "created_at": self.created_at,
            "rationale": self.rationale,
            "objectives": copy.deepcopy(self.objectives),
            "constraints": copy.deepcopy(self.constraints),
            "development": copy.deepcopy(self.development),
            "notes": self.notes,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Strategy":
        d = d or {}
        return cls(
            name=str(d.get("name") or "未命名策略")[:120],
            source=str(d.get("source") or "player"),
            version=int(d.get("version") or 1),
            created_at=str(d.get("created_at") or now_iso()),
            rationale=str(d.get("rationale") or "")[:4000],
            objectives=copy.deepcopy(d.get("objectives") or DEFAULT_OBJECTIVES),
            constraints=copy.deepcopy(d.get("constraints") or DEFAULT_CONSTRAINTS),
            development=copy.deepcopy(d.get("development") or DEFAULT_DEVELOPMENT),
            notes=str(d.get("notes") or "")[:4000],
            active=bool(d.get("active", True)),
        )

    # -- 校验 ---------------------------------------------------------
    def validate(self, *, ai: bool = False) -> list[str]:
        problems: list[str] = []
        if not self.name.strip():
            problems.append("name 不能为空")
        if len(self.name) > 120:
            problems.append("name 太长（>120）")
        if self.source not in ("default", "player", "ai"):
            self.source = "ai" if ai else "player"
        if not isinstance(self.objectives, list) or not self.objectives:
            problems.append("objectives 必须是非空列表")
            self.objectives = copy.deepcopy(DEFAULT_OBJECTIVES)
        clean_objs = []
        for i, o in enumerate(self.objectives or []):
            if not isinstance(o, dict):
                problems.append("objectives[%d] 不是对象" % i)
                continue
            metric = str(o.get("metric") or "").strip()
            if not metric:
                problems.append("objectives[%d].metric 为空" % i)
                continue
            w = safe_float(o.get("weight"), 0.0) or 0.0
            direction = str(o.get("direction") or "max").lower()
            if direction not in ("max", "min"):
                direction = "max"
            clean_objs.append({"metric": metric[:80],
                               "weight": clamp(w, 0.0, 10.0),
                               "direction": direction,
                               "target": safe_float(o.get("target"), None)})
        self.objectives = clean_objs or copy.deepcopy(DEFAULT_OBJECTIVES)
        if not isinstance(self.constraints, dict):
            problems.append("constraints 必须是对象")
            self.constraints = copy.deepcopy(DEFAULT_CONSTRAINTS)
        c = copy.deepcopy(DEFAULT_CONSTRAINTS)
        c.update(self.constraints or {})
        # 数值型字段温和夹紧，越界不算硬错误
        for key in ("max_actions_per_cycle", "max_levels_per_action",
                    "max_build_actions_per_cycle", "max_pm_actions_per_cycle"):
            raw = safe_float(c.get(key), 1)
            v = int(raw) if raw is not None else 1
            c[key] = max(0, min(v, 64))
        c["min_treasury"] = float(safe_float(c.get("min_treasury"), 0.0) or 0.0)
        c["max_weekly_deficit"] = safe_float(c.get("max_weekly_deficit"), None)
        c["allow_conditional_pm"] = bool(c.get("allow_conditional_pm", False))
        for key in ("block_pm_patterns", "warn_pm_patterns",
                    "forbidden_buildings", "priority_buildings"):
            v = c.get(key)
            if isinstance(v, str):
                v = [x.strip() for x in v.split(",") if x.strip()]
            if not isinstance(v, list):
                v = []
            c[key] = [str(x) for x in v if str(x).strip()]
        if ai:
            # AI 不能放开条件未知的 PM；这是安全护栏，不是策略偏好。
            c["allow_conditional_pm"] = False
            # AI 不得把动作上限抬到配置硬顶以上（审查层还会再夹一次）。
            c["max_actions_per_cycle"] = min(int(c["max_actions_per_cycle"]), 16)
        self.constraints = c
        if not isinstance(self.development, dict):
            self.development = copy.deepcopy(DEFAULT_DEVELOPMENT)
        dev = copy.deepcopy(DEFAULT_DEVELOPMENT)
        dev.update(self.development or {})
        mode = str(dev.get("mode") or "nationwide").lower()
        allowed = ("nationwide", "core", "coastal", "agriculture", "resources", "capital", "balanced", "custom")
        dev["mode"] = mode if mode in allowed else "nationwide"
        dev["max_states_per_cycle"] = max(0, min(int(safe_float(dev.get("max_states_per_cycle"), 0) or 0), 64))
        raw_states = dev.get("priority_states") or []
        if isinstance(raw_states, str):
            raw_states = [x.strip() for x in raw_states.split(",") if x.strip()]
        clean_states = []
        for x in raw_states if isinstance(raw_states, list) else []:
            try:
                sid = int(x)
            except Exception:
                continue
            if sid > 0 and sid not in clean_states:
                clean_states.append(sid)
        dev["priority_states"] = clean_states[:64]
        dev["notes"] = str(dev.get("notes") or "")[:1000]
        self.development = dev
        return problems

    # -- 展示 ---------------------------------------------------------
    def summary(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "version": self.version,
            "rationale": self.rationale,
            "objectives": self.objectives,
            "constraints": self.constraints,
            "development": self.development,
            "notes": self.notes,
        }


class StrategyStore:
    def __init__(self, path: str):
        self.path = path

    def _read(self) -> dict:
        d = read_json(self.path, default=None)
        if not isinstance(d, dict):
            return {"active": Strategy().to_dict(), "history": []}
        if "active" not in d:
            return {"active": Strategy().to_dict(), "history": []}
        return d

    def active(self) -> Strategy:
        d = self._read()
        return Strategy.from_dict(d.get("active") or {})

    def set(self, strategy: Strategy | dict, source: str = "player") -> Strategy:
        if isinstance(strategy, dict):
            strategy = Strategy.from_dict(strategy)
        if source:
            strategy.source = source
        problems = strategy.validate(ai=(source == "ai"))
        if problems:
            raise ValueError("策略不合法：" + "；".join(problems))
        d = self._read()
        old = d.get("active")
        hist = list(d.get("history") or [])
        if old:
            hist.append(old)
        strategy.version = int((old or {}).get("version", 0) or 0) + 1
        strategy.created_at = now_iso()
        strategy.active = True
        d["active"] = strategy.to_dict()
        d["history"] = hist[-50:]
        atomic_write_json(self.path, d)
        return strategy

    def history(self, tail: int = 20) -> list[dict]:
        d = self._read()
        return list(d.get("history") or [])[-tail:]

    def activate(self, index: int, source: str | None = None) -> Strategy:
        hist = list(self._read().get("history") or [])[-50:]
        if not hist:
            raise ValueError("no strategy history")
        if index < 0 or index >= len(hist):
            raise ValueError("strategy history index out of range")
        st = Strategy.from_dict(hist[index])
        return self.set(st, source=source or st.source or "player")

    def reset(self) -> Strategy:
        return self.set(Strategy(source="default"), source="default")
