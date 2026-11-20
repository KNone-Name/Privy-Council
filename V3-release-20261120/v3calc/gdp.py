# -*- coding: utf-8 -*-
"""GDP 计算层。

公式（与 v3live 已验证口径一致，但把每个可选项都显式化）：

    GDP(周) = Σ_建筑 Σ_PM Σ_商品 (产出数量 − 投入数量) × 价格
    GDP(年) = GDP(周) × 52

数量来自 PM 的 ``goods_output_<good>_add`` / ``goods_input_<good>_add``，
再按缩放级别乘系数（**默认：实际就业规模 × 吞吐量**）：

    workforce_scaled  × 就业规模 × 吞吐      （默认，校准最优）
                      × 等级 × 吞吐          （--scale throughput）
                      × 等级                 （--scale levels）
    level_scaled      × 等级
    throughput_scaled × 等级 × 吞吐
    unscaled          × 1

价格基准 ``--price``：
    market  本国市场统一价（**默认；校准 −0.36%**）
    base    商品基础价（common/goods 的 cost）
    local   各州本地价（生产地价格，实测比界面低 ~17%，不是 GDP 口径）
    auto    live→market，save→base（存档没有本国市场价）

建筑范围 ``--buildings``：
    producers 只算有商品产出的建筑（**默认；校准最优**）
    all       全部建筑（政府/军营会单方面扣投入，系统性偏低 3~6%）
    <类型,...> 只算指定建筑类型

精度说明见 README：实时（市场价 + 实际就业 + 只算生产建筑）与界面差约
−0.4%~−3%；存档模式只有世界市场趋势价，绝对值和界面会有系统性偏差，
**用于相对比较/趋势/PM 方案对比**。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .gamedata import GameData, PM
from .model import BuildingRow, Snapshot

WEEKS_PER_YEAR = 52.0


@dataclass
class GoodContribution:
    good: str
    output_qty: float = 0.0
    input_qty: float = 0.0
    output_value: float = 0.0
    input_value: float = 0.0

    @property
    def net_qty(self) -> float:
        return self.output_qty - self.input_qty

    @property
    def net_value(self) -> float:
        return self.output_value - self.input_value


@dataclass
class BuildingContribution:
    type: str
    levels: int = 0
    staffing: float = 0.0
    throughput: float = 0.0
    output: float = 0.0
    input: float = 0.0
    count: int = 0

    @property
    def net(self) -> float:
        return self.output - self.input


@dataclass
class StateContribution:
    state_id: int
    output: float = 0.0
    input: float = 0.0

    @property
    def net(self) -> float:
        return self.output - self.input


@dataclass
class GDPResult:
    weekly: float = 0.0
    output_weekly: float = 0.0
    input_weekly: float = 0.0
    price_basis: str = ""
    scale: str = ""
    building_filter: str = ""
    goods: list[GoodContribution] = field(default_factory=list)
    building_types: list[BuildingContribution] = field(default_factory=list)
    states: list[StateContribution] = field(default_factory=list)
    population: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def annual(self) -> float:
        return self.weekly * WEEKS_PER_YEAR

    @property
    def annual_output(self) -> float:
        return self.output_weekly * WEEKS_PER_YEAR

    @property
    def annual_input(self) -> float:
        return self.input_weekly * WEEKS_PER_YEAR

    @property
    def per_capita(self) -> float | None:
        if not self.population:
            return None
        return self.annual / self.population

    def top_goods(self, n: int = 10) -> list[GoodContribution]:
        return sorted(self.goods, key=lambda g: -abs(g.net_value))[:n]

    def top_buildings(self, n: int = 10) -> list[BuildingContribution]:
        return sorted(self.building_types, key=lambda b: -abs(b.net))[:n]

    def to_dict(self) -> dict:
        return {
            "weekly": self.weekly,
            "annual": self.annual,
            "output_weekly": self.output_weekly,
            "input_weekly": self.input_weekly,
            "per_capita": self.per_capita,
            "population": self.population,
            "price_basis": self.price_basis,
            "scale": self.scale,
            "building_filter": self.building_filter,
            "goods": [{**g.__dict__, "net_qty": g.net_qty,
                       "net_value": g.net_value} for g in self.goods],
            "building_types": [
                {**b.__dict__, "net": b.net} for b in self.building_types],
            "states": [{**s.__dict__, "net": s.net} for s in self.states],
            "warnings": self.warnings,
        }


SCALES = ("throughput", "staffing", "levels", "staffing_only")


class GDPCalculator:
    def __init__(self, gd: GameData, snap: Snapshot):
        self.gd = gd
        self.snap = snap
        self._base = {g.name: g.cost for g in gd.goods}
        self._idx = {g.name: g.index for g in gd.goods}
        self._local: dict[int, dict[str, float]] = {}
        for st in snap.states:
            if not st.local_prices:
                continue
            d = {}
            for g in gd.goods:
                if g.index < len(st.local_prices):
                    d[g.name] = st.local_prices[g.index]
            self._local[st.id] = d
        self._market = dict(snap.market.prices) if snap.market else {}

    # -- 价格 ---------------------------------------------------------
    def price(self, good: str, state_id: int, basis: str) -> float:
        if basis in ("local", "auto"):
            v = self._local.get(state_id, {}).get(good, 0.0)
            if v and v > 0:
                return v
            if basis == "local":
                pass                       # local 无值 → 继续回退
        if basis in ("market", "auto", "local"):
            v = self._market.get(good, 0.0)
            if v and v > 0:
                return v
        return self._base.get(good, 0.0)

    def price_basis_for(self, requested: str) -> str:
        if requested != "auto":
            return requested
        # 逆向层校准结论：GDP 用本国市场统一价最接近界面。
        # 存档只有世界市场趋势价，不是本国市场价，所以 save 模式退回基础价。
        if self.snap.game.source == "save":
            return "base"
        if self._market:
            return "market"
        if self._local:
            return "local"
        return "base"

    # -- 建筑范围 -----------------------------------------------------
    def _buildings(self, mode: str) -> tuple[list[BuildingRow], str]:
        if mode == "all":
            return self.snap.buildings, mode
        if mode == "producers":
            out = []
            for b in self.snap.buildings:
                pms = [self.gd.pms.get(n) for n in b.production_methods]
                if any(p and p.has_goods_output() for p in pms):
                    out.append(b)
            return out, mode
        wanted = {x.strip() for x in mode.split(",") if x.strip()}
        return [b for b in self.snap.buildings if b.type in wanted], mode

    # -- 核心 ---------------------------------------------------------
    def compute(self, price: str = "auto", scale: str = "staffing",
                buildings: str = "producers", include_inactive: bool = False,
                exclude_goods: set[str] | None = None) -> GDPResult:
        if scale not in SCALES:
            raise ValueError("scale 必须是 %s 之一" % (SCALES,))
        exclude_goods = set(exclude_goods or ())
        basis = self.price_basis_for(price)
        rows, fname = self._buildings(buildings)
        res = GDPResult(price_basis=basis, scale=scale, building_filter=fname)
        res.population = (self.snap.country.population if self.snap.country else None)
        if basis == "market" and self.snap.game.source == "save":
            res.warnings.append("存档只有世界市场价格趋势，不是本国市场价；"
                                "绝对值仅供对比，建议 --price base 或 live 模式")
        if price == "local" and not self._local:
            res.warnings.append("快照没有州本地价（save 模式），local 已回退到 "
                                "market/base")

        goods: dict[str, GoodContribution] = {}
        btypes: dict[str, BuildingContribution] = {}
        states: dict[int, StateContribution] = {}

        for b in rows:
            if not include_inactive and not b.active:
                continue
            bt = btypes.setdefault(b.type, BuildingContribution(type=b.type))
            bt.count += 1
            bt.levels += b.levels
            bt.staffing += b.staffing
            st = states.setdefault(b.state_id, StateContribution(state_id=b.state_id))
            thr = b.throughput if b.throughput and b.throughput > 0 else 1.0
            if b.throughput and b.throughput <= 0:
                res.warnings.append("%s@%d 吞吐量<=0，按 1.0 处理" % (b.type, b.state_id))
            for pm_name in b.production_methods:
                pm = self.gd.pms.get(pm_name)
                if pm is None:
                    res.warnings.append("游戏数据里没有 PM：%s（mod？）" % pm_name)
                    continue
                for (scope, sc, good), per in pm.goods("output").items():
                    if good in exclude_goods:
                        continue
                    q = per * self._factor(sc, b, thr, scale)
                    if not q:
                        continue
                    g = goods.setdefault(good, GoodContribution(good=good))
                    p = self.price(good, b.state_id, basis)
                    g.output_qty += q
                    g.output_value += q * p
                    bt.output += q * p
                    st.output += q * p
                for (scope, sc, good), per in pm.goods("input").items():
                    if good in exclude_goods:
                        continue
                    q = per * self._factor(sc, b, thr, scale)
                    if not q:
                        continue
                    g = goods.setdefault(good, GoodContribution(good=good))
                    p = self.price(good, b.state_id, basis)
                    g.input_qty += q
                    g.input_value += q * p
                    bt.input += q * p
                    st.input += q * p

        res.goods = sorted(goods.values(), key=lambda g: -abs(g.net_value))
        res.building_types = sorted(btypes.values(), key=lambda b: -abs(b.net))
        res.states = sorted(states.values(), key=lambda s: -abs(s.net))
        res.output_weekly = sum(g.output_value for g in res.goods)
        res.input_weekly = sum(g.input_value for g in res.goods)
        res.weekly = res.output_weekly - res.input_weekly
        return res

    @staticmethod
    def _factor(scale: str, b: BuildingRow, thr: float, mode: str) -> float:
        if scale == "level_scaled":
            return float(b.levels)
        if scale == "throughput_scaled":
            return float(b.levels) * thr
        if scale == "unscaled":
            return 1.0
        # workforce_scaled
        if mode == "levels":
            return float(b.levels)
        if mode == "staffing":
            return b.staffing * thr
        if mode == "staffing_only":
            return b.staffing
        return float(b.levels) * thr

    # -- 校准 ---------------------------------------------------------
    def calibrate(self, target: float | None = None) -> list[dict]:
        """在价格基准 × 缩放方式 × 建筑范围上扫一遍，和存档 GDP 对比。

        只对带 GDP 趋势值（save 模式）的快照有意义。
        """
        target = target if target is not None else (
            self.snap.country.gdp if self.snap.country else None)
        if not target:
            return []
        out = []
        for price in ("base", "market", "local"):
            for scale in SCALES:
                for bf in ("all", "producers"):
                    r = self.compute(price=price, scale=scale, buildings=bf)
                    out.append({
                        "price": price, "scale": scale, "buildings": bf,
                        "annual": r.annual, "target": target,
                        "diff_pct": 100.0 * (r.annual / target - 1.0),
                    })
        out.sort(key=lambda x: abs(x["diff_pct"]))
        return out
