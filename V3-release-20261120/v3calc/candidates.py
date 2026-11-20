# -*- coding: utf-8 -*-
"""候选包：把"建什么 / 换什么生产方式"变成可排序的数字。

本模块是决策层的**算账部分**。它不做决策，只回答：

* 给定一州一建筑一级，**加进去之后市场价会变成多少**（价格反馈，见 ``market.py``）；
* 因此每周净增值多少、每建造点每年增值多少、要建几周；
* 有哪些硬约束会挡住它（科技/土地/资源上限/唯一建筑/已在队列）；
* 风险有多大（价格触底、原料触顶、基建饱和、激进派）。

## 候选的"五元打分"

    score = 年增值 / 建造点 × 时间折现 × 产业链权重 × (1 − 风险)

* 年增值 = (产出 − 投入) × **推演后的价格** × 52，不是当前价；
* 时间折现 = 1 / (1 + 建造周数 / 52)，越慢的工程越不值；
* 产业链权重来自 :class:`Intent`，用于表达"这五年先搞纺织"；
* 风险压到 0~1，直接乘在分数上。

## 为什么用"包"而不是单动作

V3 的边际收益递减很强。一次建 5 级纺织和分散建 5 处结果完全不同，
所以 :meth:`CandidateEngine.plan` 是**逐步贪心**：选一个 → 更新市场 → 重新打分 →
再选下一个。这样"第 3 座纺织厂"会自动因为价格被自己砸下去而掉到后面。

## 已知近似（写在返回值里，不藏着）

* 建造工期不含州建造效率 / 法令 / 临时修正；
* 只做**部分均衡**：算自己这一单对价格的影响，不重算全国其它建筑的反应；
* 资质（qualifications）、行政力、死亡率等非商品效果只做提示，不进分数。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .construction import ConstructionModel, StateGeo
from .gamedata import GameData
from .market import PriceModel, chain_of
from .model import Snapshot

# 结构性建筑：没有商品产出，但有别的价值
CONSTRUCTION_SECTOR = "building_construction_sector"
CAPACITY_CHAIN = "建造"
STRUCTURAL_BUILDINGS = {
    CONSTRUCTION_SECTOR: "capacity",          # 不估值，只做成本校验
    "building_university": "structural",      # 资质/创新力，不按商品算
    "building_government_administration": "structural",   # 行政力
    "building_barrack": "structural",         # 军力
    "building_conscription_center": "structural",
    "building_manor_house": "structural",
}

ARABLE_GROUP_HINTS = ("farm", "ranch", "plantation", "pasture")

# 减产时最多按 1.25× 原价记"稀缺溢价"，否则会出现
# "少产钢 → 钢价暴涨 → 账面增值"的错误建议（部分均衡的经典陷阱）
PRICE_GAIN_CAP = 1.25

# 各级缩放因子：workforce_scaled/level_scaled 视为"每级"，unscaled 是全局一次性
_PER_LEVEL_SCALES = ("workforce_scaled", "level_scaled", "throughput_scaled")


# ---------------------------------------------------------------------------
# 意图：只描述"优化什么"，不产生动作
# ---------------------------------------------------------------------------
@dataclass
class Intent:
    name: str = "默认"
    priority_chains: list[str] = field(default_factory=list)
    chain_weight: float = 1.6
    ban_buildings: list[str] = field(default_factory=list)
    ban_pm_patterns: list[str] = field(default_factory=lambda: ["worker_exploitation"])
    target_states: list[int] = field(default_factory=list)
    min_score: float = 0.0
    budget_weeks: float = 12.0        # 队列深度：愿意把多少周的建造力排进去
    max_levels_per_item: int = 3

    @classmethod
    def from_dict(cls, d: dict | None) -> "Intent":
        d = dict(d or {})
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def load(cls, path: str) -> "Intent":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    def weight_for(self, chain: str) -> float:
        return self.chain_weight if chain in self.priority_chains else 1.0

    def allow_building(self, building: str) -> bool:
        return not any(b and b in building for b in self.ban_buildings)

    def allow_pm(self, pm: str) -> bool:
        return not any(p and p in pm for p in self.ban_pm_patterns)

    def state_ok(self, state_id: int) -> bool:
        return not self.target_states or state_id in self.target_states


# ---------------------------------------------------------------------------
# 建筑的商品向量
# ---------------------------------------------------------------------------
@dataclass
class UnitGoods:
    """一级该建筑的产出/投入（默认生产方式，满员，吞吐 1.0）。"""

    building: str
    pms: tuple[str, ...] = ()
    out_level: dict[str, float] = field(default_factory=dict)
    in_level: dict[str, float] = field(default_factory=dict)
    out_flat: dict[str, float] = field(default_factory=dict)
    in_flat: dict[str, float] = field(default_factory=dict)

    def flows(self, levels: int = 1) -> tuple[dict[str, float], dict[str, float]]:
        n = max(1, int(levels))
        out = {g: v * n for g, v in self.out_level.items()}
        inn = {g: v * n for g, v in self.in_level.items()}
        for g, v in self.out_flat.items():
            out[g] = out.get(g, 0.0) + v
        for g, v in self.in_flat.items():
            inn[g] = inn.get(g, 0.0) + v
        return out, inn

    def net(self, levels: int = 1) -> dict[str, float]:
        out, inn = self.flows(levels)
        net = dict(out)
        for g, v in inn.items():
            net[g] = net.get(g, 0.0) - v
        return {g: v for g, v in net.items() if v}

    @property
    def main_good(self) -> str:
        net = self.net(1)
        return max(net, key=lambda g: abs(net[g])) if net else ""


def _is_subsistence(building: str, group: str) -> bool:
    return building.startswith("building_subsistence") or \
        group.startswith("bg_subsistence")


def _pm_flows(gd: GameData, pm_name: str, levels: int,
              throughput: float) -> tuple[dict[str, float], dict[str, float]]:
    """某 PM 在该等级/吞吐下对市场的产出与投入（可能是负值）。"""
    pm = gd.pms.get(pm_name)
    out: dict[str, float] = {}
    inn: dict[str, float] = {}
    if pm is None:
        return out, inn
    n = max(1, int(levels))
    for side, target in (("output", out), ("input", inn)):
        for (_scope, scale, good), v in pm.goods(side).items():
            if scale in _PER_LEVEL_SCALES:
                q = float(v) * n * (throughput if scale != "level_scaled" else 1.0)
            else:
                q = float(v)
            target[good] = target.get(good, 0.0) + q
    return out, inn


def unit_goods(gd: GameData, building: str) -> UnitGoods:
    """默认 PM 下，每升一级的产出/投入向量。"""
    bd = gd.buildings.get(building)
    ug = UnitGoods(building=building)
    if bd is None:
        return ug
    pms: list[str] = []
    for gname in bd.production_method_groups:
        pm = gd.default_pm(gname)
        if pm is None:
            continue
        pms.append(pm.name)
        for side, per_level, flat in (
                ("output", ug.out_level, ug.out_flat),
                ("input", ug.in_level, ug.in_flat)):
            for (_scope, scale, good), v in pm.goods(side).items():
                if scale in _PER_LEVEL_SCALES:
                    per_level[good] = per_level.get(good, 0.0) + float(v)
                else:
                    flat[good] = flat.get(good, 0.0) + float(v)
    ug.pms = tuple(pms)
    return ug


# ---------------------------------------------------------------------------
# 可建性
# ---------------------------------------------------------------------------
@dataclass
class BuildCheck:
    ok: bool = False
    code: str = "invalid"                 # ok | rejected
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    existing: int = 0
    queued: int = 0
    projected: int = 0
    remaining: int | None = None
    cap: int | None = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "code": self.code, "reasons": self.reasons,
                "warnings": self.warnings, "existing": self.existing,
                "queued": self.queued, "projected": self.projected,
                "remaining": self.remaining, "cap": self.cap}


# ---------------------------------------------------------------------------
# 候选
# ---------------------------------------------------------------------------
@dataclass
class BuildOption:
    state_id: int
    building: str
    levels: int
    chain: str
    points: float
    weeks: float | None
    revenue: float = 0.0
    input_cost: float = 0.0
    weekly_value: float = 0.0
    annual_value: float = 0.0
    per_point: float = 0.0
    risk: float = 0.0
    risk_notes: list[str] = field(default_factory=list)
    price_changes: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    remaining: int | None = None
    value_basis: str = "goods"            # goods | capacity_cost（建造力不估值）
    cash_cost: float | None = None        # 建造要花的现金（经验口径，见 construction.py）
    roi: float | None = None              # 年增值 / 现金成本
    # 建造力专用（value_basis=capacity_cost 时才有值；不做估值）
    capacity_add: float | None = None     # 新增建造力/周
    weekly_upkeep: float | None = None    # 该建筑的周维护（投入商品按市价）
    cost_per_capacity: float | None = None  # 周维护 ÷ 新增容量
    affordable: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "state_id": self.state_id, "building": self.building,
            "levels": self.levels, "chain": self.chain,
            "points": round(self.points, 1), "weeks": self.weeks,
            "revenue": round(self.revenue, 1),
            "input_cost": round(self.input_cost, 1),
            "weekly_value": round(self.weekly_value, 1),
            "annual_value": round(self.annual_value, 0),
            "per_point": round(self.per_point, 2),
            "risk": round(self.risk, 2), "risk_notes": self.risk_notes,
            "price_changes": {k: round(v, 3) for k, v in self.price_changes.items()},
            "score": round(self.score, 2), "remaining": self.remaining,
            "value_basis": self.value_basis,
            "cash_cost": (round(self.cash_cost, 0) if self.cash_cost else None),
            "roi": (round(self.roi, 3) if self.roi is not None else None),
            "capacity_add": (round(self.capacity_add, 2)
                             if self.capacity_add is not None else None),
            "weekly_upkeep": (round(self.weekly_upkeep, 0)
                              if self.weekly_upkeep is not None else None),
            "cost_per_capacity": (round(self.cost_per_capacity, 1)
                                  if self.cost_per_capacity is not None else None),
            "affordable": dict(self.affordable),
            "warnings": self.warnings,
        }


@dataclass
class CapacityOption:
    """建造部门：只有成本，没有估值。"""

    state_id: int
    building: str = CONSTRUCTION_SECTOR
    levels: int = 1
    points: float = 0.0
    weeks: float | None = None
    capacity_add: float = 0.0
    weekly_upkeep: float = 0.0
    cash_cost: float | None = None
    cost_per_capacity: float | None = None
    affordable: dict = field(default_factory=dict)
    feasible: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.feasible and all(self.affordable.values())

    def to_dict(self) -> dict:
        return {
            "state_id": self.state_id, "building": self.building,
            "levels": self.levels,
            "points": round(self.points, 1), "weeks": self.weeks,
            "capacity_add": round(self.capacity_add, 2),
            "weekly_upkeep": round(self.weekly_upkeep, 0),
            "cash_cost": (round(self.cash_cost, 0) if self.cash_cost else None),
            "cost_per_capacity": (round(self.cost_per_capacity, 1)
                                  if self.cost_per_capacity is not None else None),
            "affordable": dict(self.affordable),
            "ok": self.ok, "notes": self.notes,
            "value_basis": "capacity_cost",
        }


@dataclass
class PMOption:
    building: str
    group: str
    pm: str
    current: tuple[str, ...] = ()
    weekly_value: float = 0.0
    annual_value: float = 0.0
    risk: float = 0.0
    risk_notes: list[str] = field(default_factory=list)
    price_changes: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "building": self.building, "group": self.group, "pm": self.pm,
            "current": list(self.current),
            "annual_value": round(self.annual_value, 0),
            "risk": round(self.risk, 2), "risk_notes": self.risk_notes,
            "price_changes": {k: round(v, 3) for k, v in self.price_changes.items()},
            "score": round(self.score, 2), "warnings": self.warnings,
        }


@dataclass
class PlanResult:
    items: list[BuildOption] = field(default_factory=list)
    total_points: float = 0.0
    total_annual_value: float = 0.0
    budget_points: float = 0.0
    capacity_per_week: float = 0.0
    weeks: float | None = None
    alternatives: list[BuildOption] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "items": [x.to_dict() for x in self.items],
            "total_points": round(self.total_points, 1),
            "total_annual_value": round(self.total_annual_value, 0),
            "budget_points": round(self.budget_points, 1),
            "capacity_per_week": self.capacity_per_week,
            "weeks": self.weeks,
            "alternatives": [x.to_dict() for x in self.alternatives],
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
class CandidateEngine:
    def __init__(self, gd: GameData, snap: Snapshot,
                 geo: dict[int, StateGeo] | None = None,
                 intent: Intent | None = None,
                 queued: dict[tuple[int, str], int] | None = None):
        self.gd = gd
        self.snap = snap
        self.geo = geo if geo is not None else {}
        self.intent = intent or Intent()
        self.queued = dict(queued or {})
        self.model = PriceModel.from_snapshot(gd, snap)
        self.cons = ConstructionModel(gd, snap)
        self._unit: dict[str, UnitGoods] = {}
        self._check: dict[tuple, BuildCheck] = {}
        self._catalog: list[str] | None = None
        self._resource_bound: set[str] | None = None
        self._states = {s.id: s for s in snap.states}
        self._levels: dict[tuple[int, str], int] = {}
        for b in snap.buildings:
            key = (b.state_id, b.type)
            self._levels[key] = self._levels.get(key, 0) + int(b.levels or 0)

    # -- 基础 ---------------------------------------------------------
    def unit(self, building: str) -> UnitGoods:
        ug = self._unit.get(building)
        if ug is None:
            ug = self._unit[building] = unit_goods(self.gd, building)
        return ug

    def _buildable_catalog(self) -> list[str]:
        if self._catalog is not None:
            return self._catalog
        out = []
        for name, bd in self.gd.buildings.items():
            if not bd.buildable or _is_subsistence(name, bd.building_group):
                continue
            if name in STRUCTURAL_BUILDINGS and \
                    STRUCTURAL_BUILDINGS[name] != "capacity":
                continue
            ug = self.unit(name)
            if not ug.out_level and not ug.out_flat and name != CONSTRUCTION_SECTOR:
                continue
            out.append(name)
        self._catalog = sorted(out)
        return self._catalog

    def _resource_bound_set(self) -> set[str]:
        if self._resource_bound is None:
            s: set[str] = set()
            for meta in self.geo.values():
                s.update(meta.resources)
            self._resource_bound = s
        return self._resource_bound

    def levels_of(self, state_id: int, building: str) -> int:
        return self._levels.get((state_id, building), 0)

    # -- 可建性 -------------------------------------------------------
    def check(self, state_id: int, building: str, levels: int = 1) -> BuildCheck:
        key = (state_id, building, int(levels))
        hit = self._check.get(key)
        if hit is not None:
            return hit
        out = BuildCheck()
        st = self._states.get(state_id)
        bd = self.gd.buildings.get(building)
        if st is None:
            out.reasons.append("州 %s 不在当前快照" % state_id)
            self._check[key] = out
            return out
        if st.owner is not None and self.snap.country is not None \
                and st.owner != self.snap.country.id:
            out.reasons.append("州属于国家 %s，不是玩家" % st.owner)
        if bd is None:
            out.reasons.append("游戏数据里没有建筑 %s" % building)
            self._check[key] = out
            return out
        if not bd.buildable or _is_subsistence(building, bd.building_group):
            out.reasons.append("%s 不是玩家可建造建筑" % building)
        techs = self.snap.tech_set()
        missing = [t for t in bd.unlocking_technologies if t not in techs]
        if missing:
            out.reasons.append("缺科技：" + ", ".join(missing))
        if not self.intent.allow_building(building):
            out.reasons.append("策略禁建")

        existing = self.levels_of(state_id, building)
        queued = int(self.queued.get((state_id, building), 0))
        out.existing, out.queued = existing, queued
        out.projected = existing + queued
        if bd.unique and out.projected > 0:
            out.reasons.append("唯一建筑已存在/在队列")
        if bd.max_level is not None and \
                out.projected + int(levels) > int(bd.max_level):
            out.reasons.append("超过上限 %s（已/将 %s）"
                               % (bd.max_level, out.projected + int(levels)))

        meta = self.geo.get(state_id)
        if meta is None:
            out.warnings.append("州缺少静态地理：资源/耕地上限未校验")
        else:
            self._check_geo(out, meta, building, int(levels), st)

        out.ok = not out.reasons
        out.code = "rejected" if out.reasons else "ok"
        self._check[key] = out
        return out

    def _check_geo(self, out: BuildCheck, meta: StateGeo, building: str,
                   levels: int, st) -> None:
        if building in self._resource_bound_set() and meta.resources \
                and building not in meta.resources:
            out.reasons.append("州 %s 没有该资源" % st.id)
            return
        cap = meta.resource_cap(building)
        if cap is not None:
            out.cap = cap
            out.remaining = cap - out.projected
        elif building in meta.resources and \
                any(h in building for h in ARABLE_GROUP_HINTS):
            cap = int(meta.arable_land or 0)
            used = 0
            for other in meta.resources:
                if any(h in other for h in ARABLE_GROUP_HINTS) and \
                        other not in meta.resource_caps:
                    used += self.levels_of(st.id, other) + \
                        int(self.queued.get((st.id, other), 0))
            out.cap = cap
            out.remaining = cap - used
        if out.remaining is not None and out.remaining < levels:
            out.reasons.append("剩余空间不足：剩 %s，想建 %s"
                               % (out.remaining, levels))
        elif out.remaining is not None and out.remaining < levels * 2:
            out.warnings.append("接近上限（剩 %s）" % out.remaining)

    # -- 估值 ---------------------------------------------------------
    @staticmethod
    def _value(model: PriceModel, net: dict[str, float]) -> float:
        return sum(q * model.price(g) for g, q in net.items() if q)

    @staticmethod
    def _delta_value(before: PriceModel, after: PriceModel,
                     d_out: dict[str, float], d_in: dict[str, float]) -> float:
        """增量价值：增产按推演后的低价记，减产按封顶的稀缺溢价记。

        这条规则是为了挡住"少产一点、价格暴涨、账面反而更赚"的部分均衡陷阱——
        涨价最多算到 ``PRICE_GAIN_CAP`` 倍原价，超过部分视为市场噪声。
        """
        delta = 0.0
        for g, dq in d_out.items():
            if not dq:
                continue
            p = after.price(g)
            if dq < 0:
                p = min(p, before.price(g) * PRICE_GAIN_CAP)
            delta += dq * p
        for g, dq in d_in.items():
            if not dq:
                continue
            p = after.price(g)
            if dq < 0:
                p = min(p, before.price(g) * PRICE_GAIN_CAP)
            delta -= dq * p
        return delta

    def _deltas(self, ug: UnitGoods, levels: int) -> dict[str, tuple[float, float]]:
        out, inn = ug.flows(levels)
        keys = set(out) | set(inn)
        return {g: (out.get(g, 0.0), inn.get(g, 0.0)) for g in keys}

    def _risk(self, state_id: int, ug: UnitGoods, levels: int,
              after: PriceModel, check: BuildCheck) -> tuple[float, list[str]]:
        risk = 0.0
        notes: list[str] = []
        st = self._states.get(state_id)
        if st is not None and st.infrastructure and st.infrastructure_used:
            ratio = st.infrastructure_used / st.infrastructure
            if ratio >= 1.0:
                risk += 0.6
                notes.append("基建已饱和 %.0f%%" % (ratio * 100))
            elif ratio >= 0.85:
                risk += 0.3
                notes.append("基建接近饱和 %.0f%%" % (ratio * 100))
        if st is not None and st.radicals and st.population:
            share = st.radicals / st.population
            if share >= 0.2:
                risk += 0.3
                notes.append("激进派 %.0f%%" % (share * 100))
        out, inn = ug.flows(levels)
        for g, q in out.items():
            if q <= 0:
                continue
            p = after.position(g)
            if p.at_floor:
                risk += 0.5
                notes.append("%s 价格已触底" % g)
            elif p.glut:
                risk += 0.1
                notes.append("%s 已过剩" % g)
        for g, q in inn.items():
            if q <= 0:
                continue
            p = after.position(g)
            if p.at_ceiling:
                risk += 0.5
                notes.append("%s 触顶（原料可能配给）" % g)
            elif p.shortage:
                risk += 0.2
                notes.append("%s 短缺" % g)
        if check.remaining is not None and check.remaining < levels * 2:
            risk += 0.1
        return min(1.0, risk), notes

    def _score(self, annual: float, points: float, weeks: float | None,
               chain: str, risk: float) -> float:
        if points <= 0:
            return 0.0
        per_point = annual / points
        time_factor = 1.0 / (1.0 + (weeks or 0.0) / 52.0)
        return per_point * time_factor * self.intent.weight_for(chain) * (1.0 - risk)

    def evaluate_build(self, state_id: int, building: str, levels: int = 1,
                       model: PriceModel | None = None) -> BuildOption:
        model = model or self.model
        check = self.check(state_id, building, levels)
        ug = self.unit(building)
        points = self.cons.cost_points(building, levels)
        weeks = self.cons.weeks(points)
        chain = CAPACITY_CHAIN if building == CONSTRUCTION_SECTOR \
            else chain_of(ug.main_good or building)
        opt = BuildOption(state_id=state_id, building=building, levels=levels,
                          chain=chain, points=points, weeks=weeks,
                          remaining=check.remaining, warnings=list(check.warnings))
        if not check.ok:
            opt.warnings = list(check.reasons) + opt.warnings
            opt.score = float("-inf")
            return opt
        if building == CONSTRUCTION_SECTOR:
            return self._evaluate_capacity(state_id, building, levels, model,
                                           check, chain)

        deltas = self._deltas(ug, levels)
        after = model.shifted(deltas)
        out, inn = ug.flows(levels)
        opt.revenue = sum(q * after.price(g) for g, q in out.items() if q)
        opt.input_cost = sum(q * after.price(g) for g, q in inn.items() if q)
        opt.weekly_value = self._delta_value(model, after, out, inn)
        opt.annual_value = opt.weekly_value * 52.0
        opt.per_point = opt.annual_value / points if points else 0.0
        opt.price_changes = model.price_deltas(after, list(deltas))
        opt.risk, opt.risk_notes = self._risk(state_id, ug, levels, after, check)
        opt.score = self._score(opt.annual_value, points, weeks, chain, opt.risk)
        self._fill_cash(opt)
        return opt

    def _fill_cash(self, opt: BuildOption) -> None:
        pp = self.cons.cash_per_point()
        if pp and opt.points:
            opt.cash_cost = opt.points * pp
            opt.roi = (opt.annual_value / opt.cash_cost) if opt.cash_cost else None

    def _evaluate_capacity(self, state_id: int, building: str, levels: int,
                           model: PriceModel, check: BuildCheck,
                           chain: str) -> BuildOption:
        """建造部门**不做增值估值**：只算成本，并判断是否在可控范围内。"""
        cap = self.evaluate_capacity(state_id, levels)
        opt = BuildOption(
            state_id=state_id, building=building, levels=levels, chain=chain,
            points=cap.points, weeks=cap.weeks, remaining=check.remaining,
            value_basis="capacity_cost", cash_cost=cap.cash_cost,
            capacity_add=cap.capacity_add, weekly_upkeep=cap.weekly_upkeep,
            cost_per_capacity=cap.cost_per_capacity,
            affordable=dict(cap.affordable),
            warnings=list(check.warnings) + list(cap.notes))
        # 不给分数：它不参与"每建造点年增值"的排序
        opt.score = 0.0
        return opt

    # -- 建造力：只算成本 -------------------------------------------------
    def evaluate_capacity(self, state_id: int, levels: int = 1,
                          budget_points: float | None = None,
                          cash_budget: float | None = None,
                          balance: float | None = None) -> "CapacityOption":
        """建 N 级建造部门的**成本**与可控性（不估值）。

        成本拆成三笔，任何一笔越界都算"不可控"：

        * 建造点：``points`` 要吃得下当前队列预算；
        * 现金：``cash_cost = points × 现金/点``（预算槽 7 反推）要不超现金余量；
        * 周维护：建成后每周消耗的投入商品按市价折钱，要能被**周结余**覆盖
          （结余为负时视为不可控——那是在借钱养产能）。

        另外给 ``cost_per_capacity``（周维护 ÷ 新增容量），换生产方式时用它比
        "每点容量更便宜"，而不是去猜容量值多少钱。
        """
        check = self.check(state_id, CONSTRUCTION_SECTOR, levels)
        opt = CapacityOption(state_id=int(state_id), building=CONSTRUCTION_SECTOR,
                             levels=max(1, int(levels)), feasible=check.ok)
        opt.points = self.cons.cost_points(CONSTRUCTION_SECTOR, opt.levels)
        opt.weeks = self.cons.weeks(opt.points)
        # 该州已有建造部门 → 沿用现有建造方式（升级场景）；否则按默认档（新建场景）
        existing = [b for b in self.snap.buildings
                    if b.type == CONSTRUCTION_SECTOR and b.state_id == int(state_id)]
        if existing:
            pms = sorted({p for b in existing for p in b.production_methods})
        else:
            pms = list(self.unit(CONSTRUCTION_SECTOR).pms)
        add = 0.0
        upkeep = 0.0
        for pm_name in pms:
            pm = self.gd.pms.get(pm_name)
            if pm is None:
                continue
            for _scope, scales in (pm.modifiers.values or {}).items():
                for scale, kv in (scales or {}).items():
                    v = kv.get("country_construction_add")
                    if v:
                        factor = (opt.levels if scale in ("workforce_scaled",
                                                          "level_scaled") else 1.0)
                        add += float(v) * factor
            for (_scope, scale, good), v in pm.goods("input").items():
                if scale in ("workforce_scaled", "level_scaled"):
                    upkeep += float(v) * opt.levels * self.model.price(good)
                elif v:
                    upkeep += float(v) * self.model.price(good)
        opt.capacity_add = add
        opt.weekly_upkeep = upkeep
        per_point = self.cons.cash_per_point()
        opt.cash_cost = (opt.points * per_point) if per_point else None
        opt.cost_per_capacity = (opt.weekly_upkeep / add) if add else None
        if not existing:
            opt.notes.append("新建按默认建造方式 %s；建成后可切便宜档（见 capacity_pms）"
                             % ",".join(pms))
        if cash_budget is None:
            cash_budget = self.cons.cash_budget()
        opt.affordable = {
            "budget": (True if budget_points is None
                       else opt.points <= budget_points + 1e-9),
            "cash": (True if (opt.cash_cost is None or cash_budget is None)
                     else opt.cash_cost <= cash_budget + 1e-9),
            "upkeep": (True if balance is None else opt.weekly_upkeep <= balance),
        }
        if not check.ok:
            opt.notes.extend(check.reasons)
        if not opt.affordable["budget"]:
            opt.notes.append("建造点 %.0f 超过本轮预算 %.0f"
                             % (opt.points, budget_points))
        if not opt.affordable["cash"]:
            opt.notes.append("现金 %.0f 超过余量 %.0f"
                             % (opt.cash_cost or 0, cash_budget or 0))
        if not opt.affordable["upkeep"]:
            opt.notes.append("周维护 %.0f 高于周结余 %.0f（在借钱养产能）"
                             % (opt.weekly_upkeep, balance or 0))
        return opt

    def capacity_options(self, levels: int = 1,
                         budget_points: float | None = None,
                         cash_budget: float | None = None,
                         limit: int = 3) -> list["CapacityOption"]:
        """给几个建建造部门的落点（成本全国一致，按人口排序）。

        返回**所有**评估结果（含不可控的），由调用方决定怎么展示：
        隐藏"超限"项等于把风险藏起来。
        """
        st = [s for s in sorted(self.snap.states,
                                key=lambda s: -(s.population or 0))
              if self.check(s.id, CONSTRUCTION_SECTOR, levels).ok]
        out = [self.evaluate_capacity(s.id, levels, budget_points, cash_budget,
                                      self._balance())
               for s in st[:max(1, limit)]]
        out.sort(key=lambda o: (not o.ok, -(self._states[o.state_id].population or 0)))
        return out

    def _balance(self) -> float | None:
        c = self.snap.country
        if c is None or c.budget is None:
            return None
        return c.budget.balance

    def capacity_pm_options(self) -> list[dict]:
        """建造部门的 PM：按"每周维护 ÷ 新增容量"从便宜到贵排（纯成本口径）。

        注意：换 PM 有产线损失，这里不算；只回答"哪个档位每点容量更省"。
        """
        from .pm import PMChecker, STATUS_ACTIVE, STATUS_AVAILABLE

        bd = self.gd.buildings.get(CONSTRUCTION_SECTOR)
        if bd is None:
            return []
        checker = PMChecker(self.gd, self.snap)
        rows = [b for b in self.snap.buildings if b.type == CONSTRUCTION_SECTOR]
        active = {p for b in rows for p in b.production_methods}
        out: list[dict] = []
        for gname in bd.production_method_groups:
            grp = self.gd.pm_groups.get(gname)
            if grp is None:
                continue
            for pm_name in grp.production_methods:
                pm = self.gd.pms.get(pm_name)
                if pm is None:
                    continue
                st = checker.evaluate(pm, gname, active)
                if st.status not in (STATUS_ACTIVE, STATUS_AVAILABLE):
                    continue
                add = 0.0
                for _scope, scales in (pm.modifiers.values or {}).items():
                    for scale, kv in (scales or {}).items():
                        v = kv.get("country_construction_add")
                        if v and scale in ("workforce_scaled", "level_scaled"):
                            add += float(v)
                if add <= 0:
                    continue
                upkeep = 0.0
                for (_scope, scale, good), v in pm.goods("input").items():
                    if scale in ("workforce_scaled", "level_scaled"):
                        upkeep += float(v) * self.model.price(good)
                out.append({
                    "pm": pm_name, "group": gname,
                    "status": st.status, "current": pm_name in active,
                    "capacity_add": round(add, 2),
                    "weekly_upkeep": round(upkeep, 0),
                    "cost_per_capacity": round(upkeep / add, 1),
                })
        out.sort(key=lambda x: x["cost_per_capacity"])
        return out

    # -- 静态候选表 ---------------------------------------------------
    def build_options(self, limit: int | None = 30,
                      states: list[int] | None = None,
                      levels: int = 1) -> list[BuildOption]:
        states = states or [s.id for s in self.snap.states]
        out: list[BuildOption] = []
        for building in self._buildable_catalog():
            # 建造部门不进"增值排序"：它只做成本校验（capacity_options）
            if building == CONSTRUCTION_SECTOR:
                continue
            for sid in states:
                if not self.intent.state_ok(sid):
                    continue
                if not self.check(sid, building, levels).ok:
                    continue
                opt = self.evaluate_build(sid, building, levels, self.model)
                if opt.score < self.intent.min_score:
                    continue
                out.append(opt)
        out.sort(key=lambda o: -o.score)
        return out[:limit] if limit else out

    # -- 组合规划（逐步贪心 + 市场更新） ------------------------------
    def plan(self, budget_points: float, max_items: int = 6,
             max_picks_per_site: int = 2, pool_size: int = 180,
             levels: int = 1, cash_budget: float | None = None) -> PlanResult:
        pool = self.build_options(limit=pool_size, levels=levels)
        work = self.model
        used: dict[tuple[int, str], int] = {}
        items: list[BuildOption] = []
        spent = 0.0
        cash_spent = 0.0
        if cash_budget is None:
            cash_budget = self.cons.cash_budget()
        while len(items) < max_items:
            best: BuildOption | None = None
            for base in pool:
                key = (base.state_id, base.building)
                if used.get(key, 0) >= max_picks_per_site:
                    continue
                if base.points > budget_points - spent:
                    continue
                if cash_budget is not None and (base.cash_cost or 0) > \
                        cash_budget - cash_spent:
                    continue
                opt = self.evaluate_build(base.state_id, base.building,
                                          levels, work)
                if opt.score == float("-inf"):
                    continue
                if best is None or opt.score > best.score:
                    best = opt
            if best is None or best.score < self.intent.min_score:
                break
            items.append(best)
            spent += best.points
            cash_spent += best.cash_cost or 0.0
            used[(best.state_id, best.building)] = \
                used.get((best.state_id, best.building), 0) + 1
            work = work.shifted(self._deltas(self.unit(best.building), levels))
        total = sum(o.annual_value for o in items)
        chosen = {(o.state_id, o.building) for o in items}
        alts = [o for o in pool if (o.state_id, o.building) not in chosen][:3]
        return PlanResult(
            items=items, total_points=spent, total_annual_value=total,
            budget_points=budget_points,
            capacity_per_week=self.cons.capacity_per_week,
            weeks=self.cons.weeks(spent) if spent else None,
            alternatives=alts,
            note=("逐步贪心：每选一项都用推演后的价格重算剩余候选；"
                  "预算 %.0f 建造点 ≈ %.1f 周，现金 %.0f" %
                  (budget_points, self.cons.weeks(budget_points) or 0, cash_spent)))

    # -- 生产方式 -----------------------------------------------------
    def pm_options(self, limit: int | None = 25,
                   include_conditional: bool = False) -> list[PMOption]:
        from .pm import PMChecker, STATUS_ACTIVE, STATUS_AVAILABLE

        checker = PMChecker(self.gd, self.snap)
        if self.geo:
            geo: dict[int, set[str]] = {}
            for sid, meta in self.geo.items():
                if meta.region:
                    g = self.gd.state_region_geo.get(meta.region)
                    if g:
                        geo[sid] = set(g)
            checker.state_geo = geo
        out: list[PMOption] = []
        for btype in self.snap.building_types():
            rows = [b for b in self.snap.buildings if b.type == btype]
            bd = self.gd.buildings.get(btype)
            if bd is None or not rows:
                continue
            ug = self.unit(btype)
            # 只有商品产出的建筑才按"年增值"排序；建造部门的 PM 是成本问题，
            # 走 capacity_pm_options()（每点容量的周维护），两者单位不同，不混排。
            if not ug.out_level and not ug.out_flat:
                continue
            for gname in bd.production_method_groups:
                grp = self.gd.pm_groups.get(gname)
                if grp is None:
                    continue
                members = set(grp.production_methods)
                active = {p for b in rows for p in b.production_methods} & members
                for pm_name in grp.production_methods:
                    if pm_name in active or not self.intent.allow_pm(pm_name):
                        continue
                    pm = self.gd.pms.get(pm_name)
                    if pm is None:
                        continue
                    st = checker.evaluate(pm, gname, active)
                    if st.status not in (STATUS_ACTIVE, STATUS_AVAILABLE) and \
                            not (include_conditional and
                                 st.status == "conditional"):
                        continue
                    opt = self.evaluate_pm(btype, gname, pm_name, active)
                    if opt is not None:
                        out.append(opt)
        out.sort(key=lambda o: -o.score)
        return out[:limit] if limit else out

    def evaluate_pm(self, building: str, group: str, pm: str,
                    current: set[str] | None = None) -> PMOption | None:
        """换 PM 的全国年增值（含价格反馈；不含换产线的时间损失）。"""
        rows = [b for b in self.snap.buildings if b.type == building]
        grp = self.gd.pm_groups.get(group)
        new_pm = self.gd.pms.get(pm)
        if not rows or grp is None or new_pm is None:
            return None
        members = set(grp.production_methods)
        current = current or {p for b in rows for p in b.production_methods}
        old_names = sorted(current & members)
        old_pm_name = old_names[0] if old_names else None

        net_after: dict[str, float] = {}
        d_out: dict[str, float] = {}
        d_in: dict[str, float] = {}
        for b in rows:
            n = int(b.levels or 1)
            thr = b.throughput if b.throughput and b.throughput > 0 else 1.0
            o_out: dict[str, float] = {}
            o_inn: dict[str, float] = {}
            for name in b.production_methods:
                o, i = _pm_flows(self.gd, name, n, thr)
                for g, v in o.items():
                    o_out[g] = o_out.get(g, 0.0) + v
                for g, v in i.items():
                    o_inn[g] = o_inn.get(g, 0.0) + v
            n_out, n_inn = dict(o_out), dict(o_inn)
            if old_pm_name:
                o, i = _pm_flows(self.gd, old_pm_name, n, thr)
                for g, v in o.items():
                    n_out[g] = n_out.get(g, 0.0) - v
                for g, v in i.items():
                    n_inn[g] = n_inn.get(g, 0.0) - v
            o, i = _pm_flows(self.gd, pm, n, thr)
            for g, v in o.items():
                n_out[g] = n_out.get(g, 0.0) + v
            for g, v in i.items():
                n_inn[g] = n_inn.get(g, 0.0) + v
            goods = set(o_out) | set(o_inn) | set(n_out) | set(n_inn)
            for g in goods:
                d_out[g] = d_out.get(g, 0.0) + (n_out.get(g, 0.0) - o_out.get(g, 0.0))
                d_in[g] = d_in.get(g, 0.0) + (n_inn.get(g, 0.0) - o_inn.get(g, 0.0))
                net_after[g] = net_after.get(g, 0.0) + n_out.get(g, 0.0) - n_inn.get(g, 0.0)

        after = self.model.shifted({g: (d_out.get(g, 0.0), d_in.get(g, 0.0))
                                    for g in set(d_out) | set(d_in)})
        delta = self._delta_value(self.model, after, d_out, d_in)
        opt = PMOption(building=building, group=group, pm=pm,
                       current=tuple(old_names),
                       weekly_value=delta, annual_value=delta * 52.0,
                       price_changes=self.model.price_deltas(after, list(d_out)))
        risk, notes = 0.0, []
        for g, dq in d_out.items():
            if dq < 0:
                p = self.model.position(g)
                if p.shortage or p.at_ceiling:
                    risk += 0.4
                    notes.append("%s 减产（本就短缺）" % g)
        for g, q in net_after.items():
            if q > 0 and after.position(g).at_ceiling:
                risk += 0.4
                notes.append("%s 触顶" % g)
        for g, q in d_in.items():
            if q > 0 and self.model.position(g).at_ceiling:
                risk += 0.3
                notes.append("新增需求 %s（已触顶）" % g)
        opt.risk, opt.risk_notes = min(1.0, risk), notes
        if "worker_exploitation" in pm:
            opt.risk = min(1.0, opt.risk + 0.2)
            opt.risk_notes.append("工人剥削类 PM：死亡率上升")
        chain = chain_of(self.unit(building).main_good or building)
        opt.score = opt.annual_value * (1.0 - opt.risk) * self.intent.weight_for(chain)
        opt.warnings = _pm_warnings(self.gd, new_pm)
        return opt


_PM_WARN_KEYS = (
    "country_admin_capacity_add", "building_employment_", "mortality",
    "standard_of_living", "country_construction_add", "building_infrastructure_usage_add",
)


def _pm_capacity_add(gd: GameData, pm_name: str | None) -> float:
    """该 PM 每级提供的建造力（``country_construction_add`` 之和）。

    保留给"只看产能、不估值"的临时查询用；建造力不参与增值排序。
    """
    pm = gd.pms.get(pm_name) if pm_name else None
    if pm is None:
        return 0.0
    total = 0.0
    for _scope, scales in (pm.modifiers.values or {}).items():
        for scale, kv in (scales or {}).items():
            v = kv.get("country_construction_add")
            if v and scale in _PER_LEVEL_SCALES:
                total += float(v)
    return total


def _pm_warnings(gd: GameData, pm) -> list[str]:
    out = []
    for scope, scales in (pm.modifiers.values or {}).items():
        for scale, kv in (scales or {}).items():
            for k, v in kv.items():
                if any(x in k for x in _PM_WARN_KEYS):
                    out.append("%s(%s/%s)=%s" % (k, scope, scale, v))
    return out[:8]
