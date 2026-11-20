# -*- coding: utf-8 -*-
"""建造力 / 造价 / 州静态地理。

三件事：

1. **造价**：每个建筑每级消耗多少"建造点"。来自
   ``common/script_values/building_values.txt`` 的 ``construction_cost_*``
   与建筑自己的 ``required_construction``。
2. **建造力**：每周能完成多少建造点。来自建筑 PM 的
   ``country_construction_add``（``workforce_scaled`` = 等级 × 吞吐），
   加上 GDP 基线 10，再乘 ``country_construction_mult``。
   与 v3ai/README 记的"大清 22/周"同口径。
3. **州静态地理**：区域 / 沿海 / 可耕地 / 资源上限 / 州特质。
   来自 ``state_meta.json``（v3ai 的数据缓存，本层只读 JSON，不依赖其代码）。
   没有这个文件也能跑，只是资源与耕地上限校验会降级。

口径警告：真实的建造还有**州建造效率**（州修正、法令、技术）和**临时修正**，
快照里读不到，所以这里的 ``weeks`` 只用于量级判断，不要当精确工期。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .gamedata import GameData
from .model import Snapshot

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STATE_META = os.path.join(PROJ, "v3ai", "data", "state_meta.json")

# GDP 基线建造力（游戏里凭空给的那部分），与 v3ai 口径一致
CONSTRUCTION_BASE = 10.0
# 建筑没写 required_construction 时的兜底点数
DEFAULT_COST_POINTS = 400.0

_COST_CACHE: dict[str, dict[str, float]] = {}


def load_construction_costs(gd: GameData) -> dict[str, float]:
    """``construction_cost_high = 600`` 这类表；按游戏目录缓存。"""
    key = os.path.abspath(gd.game_dir)
    if key in _COST_CACHE:
        return _COST_CACHE[key]
    path = os.path.join(gd.game_dir, "common", "script_values",
                        "building_values.txt")
    out: dict[str, float] = {}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError:
        _COST_CACHE[key] = out
        return out
    for m in re.finditer(r"^\s*(construction_cost_[a-z0-9_]+)\s*=\s*([0-9.]+)",
                         text, re.M):
        out[m.group(1)] = float(m.group(2))
    _COST_CACHE[key] = out
    return out


# ---------------------------------------------------------------------------
# 州静态地理
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StateGeo:
    region: str = ""
    coastal: bool = False
    arable_land: int = 0
    resources: tuple[str, ...] = ()
    resource_caps: dict[str, int] = field(default_factory=dict)
    traits: tuple[str, ...] = ()
    incorporated: bool | None = None

    def resource_cap(self, building: str) -> int | None:
        if building in self.resource_caps:
            return int(self.resource_caps[building])
        return None


def default_state_meta_path() -> str | None:
    p = os.environ.get("V3_STATE_META") or DEFAULT_STATE_META
    return p if os.path.isfile(p) else None


def load_state_meta(path: str | None = None) -> dict[int, StateGeo]:
    """读 ``state_meta.json``；缺文件返回空 dict（调用方按降级处理）。"""
    path = path or default_state_meta_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    states = raw.get("states") if isinstance(raw, dict) else None
    if not isinstance(states, dict):
        return {}
    out: dict[int, StateGeo] = {}
    for k, v in states.items():
        try:
            sid = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        out[sid] = StateGeo(
            region=v.get("region") or "",
            coastal=bool(v.get("coastal")),
            arable_land=int(v.get("arable_land") or 0),
            resources=tuple(v.get("resources") or ()),
            resource_caps={kk: int(vv) for kk, vv in
                           (v.get("resource_caps") or {}).items()
                           if vv is not None},
            traits=tuple(v.get("traits") or ()),
            incorporated=v.get("incorporated"),
        )
    return out


# ---------------------------------------------------------------------------
# 建造模型
# ---------------------------------------------------------------------------
class ConstructionModel:
    def __init__(self, gd: GameData, snap: Snapshot):
        self.gd = gd
        self.snap = snap
        self.costs = load_construction_costs(gd)
        self._cap: dict | None = None

    # -- 造价 ---------------------------------------------------------
    def cost_points(self, building: str, levels: int = 1) -> float:
        bd = self.gd.buildings.get(building)
        key = bd.required_construction if bd else ""
        per = self.costs.get(key, DEFAULT_COST_POINTS) if key else \
            DEFAULT_COST_POINTS
        return float(per) * max(1, int(levels))

    def cost_category(self, building: str) -> str:
        bd = self.gd.buildings.get(building)
        return (bd.required_construction if bd else "") or ""

    # -- 建造力 -------------------------------------------------------
    def capacity(self) -> dict:
        if self._cap is not None:
            return self._cap
        from_pms = 0.0
        mult = 0.0
        sources: list[dict] = []
        for b in self.snap.buildings:
            thr = b.throughput if b.throughput and b.throughput > 0 else 1.0
            for pm_name in b.production_methods:
                pm = self.gd.pms.get(pm_name)
                if pm is None:
                    continue
                for _scope, scales in (pm.modifiers.values or {}).items():
                    for scale, kv in (scales or {}).items():
                        v = kv.get("country_construction_add")
                        if v:
                            factor = (b.levels * thr if scale == "workforce_scaled"
                                      else b.levels if scale == "level_scaled"
                                      else 1.0)
                            add = float(v) * factor
                            from_pms += add
                            if abs(add) > 0.01:
                                sources.append({"building": b.type,
                                                "state_id": b.state_id,
                                                "pm": pm_name,
                                                "add": round(add, 2)})
                        m = kv.get("country_construction_mult")
                        if m:
                            mult += float(m)
        cap = max(0.0, (CONSTRUCTION_BASE + from_pms) * (1.0 + mult))
        self._cap = {
            "capacity_per_week": round(cap, 2),
            "base": CONSTRUCTION_BASE,
            "from_buildings": round(from_pms, 2),
            "mult": round(mult, 4),
            "sources": sorted(sources, key=lambda x: -abs(x["add"]))[:8],
            "note": "近似：建筑 PM + GDP 基线；不含州建造效率/临时修正",
        }
        return self._cap

    @property
    def capacity_per_week(self) -> float:
        return float(self.capacity().get("capacity_per_week") or 0.0)

    def weeks(self, points: float) -> float | None:
        cap = self.capacity_per_week
        return round(points / cap, 2) if cap > 0 else None

    def budget_points(self, weeks: float) -> float:
        """``weeks`` 周内满负荷能投进去的建造点。"""
        return round(self.capacity_per_week * max(0.0, float(weeks)), 1)

    # -- 现金 ---------------------------------------------------------
    def cash_per_point(self) -> float | None:
        """建造 1 点要花多少钱：预算槽 7（建造商品）/ 建造力。

        这是从**当前预算**反推的经验值，不是游戏公式；用它是为了回答
        "这一轮的计划要花多少钱、国库够不够"。读不到预算就返回 None。
        """
        c = self.snap.country
        b = c.budget if c else None
        if b is None:
            return None
        exp = list(b.expenses or [])
        if len(exp) <= 7:
            return None
        spend = float(exp[7] or 0.0)
        cap = self.capacity_per_week
        if spend <= 0 or cap <= 0:
            return None
        return spend / cap

    def cash_budget(self) -> float | None:
        """还能花在建造上的现金：国库 + 一半的剩余信贷额度。"""
        c = self.snap.country
        if c is None:
            return None
        treasury = float(c.treasury or 0.0)
        credit = float(c.credit_limit or 0.0)
        loan = float(c.loan_principal or 0.0)
        headroom = max(0.0, credit - loan)
        return max(0.0, treasury) + 0.5 * headroom
