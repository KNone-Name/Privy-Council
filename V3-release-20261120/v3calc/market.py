# -*- coding: utf-8 -*-
"""市场视图：价格模型 + 产业链聚合 + 缺口定价。

## 为什么需要它

``gdp.py`` 用**当前市场价**算 GDP，回答"现在值多少"；决策层要回答的是
"**再加一级**会怎样"。V3 的价格是内生的：多产一单位，价格就掉一点，
所以"建纺织厂"的收益不能用当前价乘产量，必须先把价格反馈算进去。

## 价格公式（已用真实快照反推并逐项验证）

对 ``snap_live.json``（1.15.1，54 商品）用 ``market.supply`` / ``market.demand``
反推，得到与游戏完全一致的分段函数——53 个商品误差为 0（``gold`` 例外）：

    d > s :  mult = 1 + 0.75 × (d/s − 1)
    s > d :  mult = 1 − 0.75 × (s/d − 1)
    s = d = 0 : mult = 1
    统一 clamp 到 [0.25, 1.75]，价格 = 基础价 × mult

注意分母是**较小的一方**：市场越薄，边际产量对价格的冲击越大。这正是
"在一个只有 60 单位需求的小市场里建一座厂会把价格砸到地板"的数学来源，
也是本层存在的意义。

``gold`` 不参与市场定价（固定 1.0×基础价）；``d == 0`` 且 ``s > 0`` 时价格为
地板价（已验证：``explosives`` 50 → 12.5）。

存档模式的快照只有价格、没有 supply/demand（``snap.market.supply`` 为空），
此时价格模型退化为"按现价记账、不计价格反馈"，并由 ``has_orders`` 标记出来。

## 产业链

54 个商品按生产/消费关系归到 10 条链（粮食/经济作物/纺织/木材/矿产/重工/
军需/消费品/运输能源/货币）。链是给决策层看的聚合单位：它不参与计算，
只用于"一页纸"里把 54 行压成 10 行，以及策略层的"优先链"。
"""
from __future__ import annotations

from dataclasses import dataclass

from .gamedata import GameData
from .model import Snapshot

# 价格函数参数（与游戏实测一致，改这里等于改口径，务必同步更新 selftest）
PRICE_SLOPE = 0.75
PRICE_MIN = 0.25
PRICE_MAX = 1.75

# 不参与市场定价的商品：金本位货币，价格恒为基础价。
NON_MARKET_GOODS = frozenset({"gold"})

# 短缺/过剩的判定阈值（与 v3ai 的策略口径一致）
SHORTAGE_RATIO = 1.25
GLUT_RATIO = 0.80


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


def price_multiplier(supply: float | None, demand: float | None) -> float:
    """按 V3 的实际规则算价格倍率（1.0 = 基础价）。"""
    s = max(0.0, float(supply or 0.0))
    d = max(0.0, float(demand or 0.0))
    if s <= 0.0 and d <= 0.0:
        return 1.0
    if d <= 0.0:                           # 只有供给、没有需求 → 地板价
        return PRICE_MIN
    if s <= 0.0:                           # 只有需求、没有供给 → 天花板
        return PRICE_MAX
    if d > s:
        m = 1.0 + PRICE_SLOPE * (d / s - 1.0)
    elif s > d:
        m = 1.0 - PRICE_SLOPE * (s / d - 1.0)
    else:
        m = 1.0
    return _clamp(m, PRICE_MIN, PRICE_MAX)


def price_from_orders(base: float, supply: float | None,
                      demand: float | None) -> float:
    return float(base) * price_multiplier(supply, demand)


# ---------------------------------------------------------------------------
# 产业链
# ---------------------------------------------------------------------------
CHAIN_ORDER = ("粮食", "经济作物", "纺织", "木材", "矿产",
               "重工", "军需", "消费品", "运输能源", "货币")

CHAIN_GOODS: dict[str, tuple[str, ...]] = {
    "粮食": ("grain", "fish", "meat", "fruit", "groceries", "sugar"),
    "经济作物": ("tea", "coffee", "tobacco", "opium", "wine", "liquor"),
    "纺织": ("fabric", "silk", "dye", "clothes", "luxury_clothes"),
    "木材": ("wood", "hardwood", "rubber"),
    "矿产": ("iron", "coal", "lead", "copper", "sulfur", "oil"),
    "重工": ("steel", "glass", "tools", "engines", "fertilizer",
             "explosives", "paper"),
    "军需": ("ammunition", "small_arms", "artillery", "tanks", "aeroplanes",
             "manowars", "ironclads"),
    "消费品": ("furniture", "luxury_furniture", "porcelain", "fine_art",
               "telephones", "radios", "automobiles"),
    "运输能源": ("services", "transportation", "electricity",
                 "merchant_marine", "clippers", "steamers"),
    "货币": ("gold",),
}

GOOD_CHAIN: dict[str, str] = {
    good: chain for chain, goods in CHAIN_GOODS.items() for good in goods
}

_CATEGORY_FALLBACK = {
    "staple": "粮食", "luxury": "消费品", "industrial": "重工",
    "military": "军需",
}


def chain_of(good: str, category: str = "") -> str:
    """商品 → 产业链名；未知商品按类别兜底（mod 新商品不会让统计崩掉）。"""
    hit = GOOD_CHAIN.get(good)
    if hit:
        return hit
    return "其他·" + (category or "未知")


def unknown_goods(gd: GameData) -> list[str]:
    """基础游戏数据里未归类到产业链的商品（selftest 用来保证覆盖率）。"""
    return [g.name for g in gd.goods if g.name not in GOOD_CHAIN]


# ---------------------------------------------------------------------------
# 单个商品的市场位置
# ---------------------------------------------------------------------------
@dataclass
class GoodPosition:
    good: str
    base: float = 0.0
    price: float = 0.0
    supply: float = 0.0
    demand: float = 0.0
    chain: str = ""
    category: str = ""

    @property
    def ratio(self) -> float:
        return (self.price / self.base) if self.base else 0.0

    @property
    def gap(self) -> float:
        """供需缺口数量：正值=短缺，负值=过剩。"""
        return self.demand - self.supply

    @property
    def gap_value(self) -> float:
        """缺口按当前价折算的每周金额（短缺=花不出去的钱，过剩=卖不掉的货）。"""
        if self.gap > 0:
            return self.gap * self.price
        if self.gap < 0:
            return self.gap * max(0.0, self.price)
        return 0.0

    @property
    def at_floor(self) -> bool:
        return self.base > 0 and self.ratio <= PRICE_MIN + 0.01

    @property
    def at_ceiling(self) -> bool:
        return self.base > 0 and self.ratio >= PRICE_MAX - 0.01

    @property
    def shortage(self) -> bool:
        return self.ratio >= SHORTAGE_RATIO

    @property
    def glut(self) -> bool:
        return 0.0 < self.ratio <= GLUT_RATIO

    @property
    def shadow_price(self) -> float:
        """影子价格：正常时等于市价；价格触顶时代表"配给成本"。

        触顶说明这个商品在市场上**买不到更多**，任何需要它做输入的扩产都会
        先卡在原料上，因此候选打分里要按更高成本计。
        """
        if self.at_ceiling:
            return self.price * 1.25
        return self.price

    def to_dict(self) -> dict:
        return {
            "good": self.good, "chain": self.chain, "category": self.category,
            "base": round(self.base, 2), "price": round(self.price, 2),
            "ratio": round(self.ratio, 3),
            "supply": round(self.supply, 1), "demand": round(self.demand, 1),
            "gap": round(self.gap, 1), "gap_value": round(self.gap_value, 1),
            "shortage": self.shortage, "glut": self.glut,
            "at_floor": self.at_floor, "at_ceiling": self.at_ceiling,
        }


# ---------------------------------------------------------------------------
# 价格模型
# ---------------------------------------------------------------------------
class PriceModel:
    """某个市场的可推演价格表。

    ``shifted(deltas)`` 返回**新实例**，不修改自己——规划器靠它做
    "选中一个候选 → 更新市场 → 重新打分"的逐步边际分析。
    """

    def __init__(self, gd: GameData, base: dict[str, float],
                 supply: dict[str, float], demand: dict[str, float],
                 prices: dict[str, float] | None = None,
                 has_orders: bool = True):
        self.gd = gd
        self.base = dict(base)
        self.supply = dict(supply)
        self.demand = dict(demand)
        self._given = dict(prices or {})
        self.has_orders = has_orders

    # -- 构造 ---------------------------------------------------------
    @classmethod
    def from_snapshot(cls, gd: GameData, snap: Snapshot) -> "PriceModel":
        base = {g.name: float(g.cost) for g in gd.goods}
        m = snap.market
        if m is not None:
            for name, v in (m.base_prices or {}).items():
                if v and v > 0:
                    base[name] = float(v)
            supply = {k: float(v or 0.0) for k, v in (m.supply or {}).items()}
            demand = {k: float(v or 0.0) for k, v in (m.demand or {}).items()}
            prices = {k: float(v or 0.0) for k, v in (m.prices or {}).items()}
        else:
            supply, demand, prices = {}, {}, {}
        return cls(gd, base, supply, demand, prices,
                   has_orders=bool(supply or demand))

    # -- 查询 ---------------------------------------------------------
    def price(self, good: str) -> float:
        if good in NON_MARKET_GOODS:
            return self.base.get(good, 0.0)
        if self.has_orders:
            return price_from_orders(self.base.get(good, 0.0),
                                     self.supply.get(good, 0.0),
                                     self.demand.get(good, 0.0))
        # 存档模式：没有买卖单，只能用快照里给的价格
        return self._given.get(good) or self.base.get(good, 0.0)

    def multiplier(self, good: str) -> float:
        b = self.base.get(good, 0.0)
        return (self.price(good) / b) if b else 0.0

    def position(self, good: str) -> GoodPosition:
        g = self.gd.goods_by_name.get(good)
        return GoodPosition(
            good=good, base=self.base.get(good, 0.0), price=self.price(good),
            supply=self.supply.get(good, 0.0),
            demand=self.demand.get(good, 0.0),
            chain=chain_of(good, g.category if g else ""),
            category=(g.category if g else ""),
        )

    def positions(self) -> dict[str, GoodPosition]:
        return {g.name: self.position(g.name) for g in self.gd.goods}

    # -- 推演 ---------------------------------------------------------
    def shifted(self, deltas: dict[str, tuple[float, float]]) -> "PriceModel":
        """``deltas``：商品 → (Δ供给, Δ需求)；返回平移后的新模型。"""
        if not deltas or not self.has_orders:
            return self
        supply = dict(self.supply)
        demand = dict(self.demand)
        for good, (ds, dd) in deltas.items():
            if ds:
                supply[good] = max(0.0, supply.get(good, 0.0) + float(ds))
            if dd:
                demand[good] = max(0.0, demand.get(good, 0.0) + float(dd))
        return PriceModel(self.gd, self.base, supply, demand, self._given,
                          has_orders=True)

    def price_deltas(self, other: "PriceModel",
                     goods: list[str] | None = None) -> dict[str, float]:
        out = {}
        for good in (goods or list(self.base)):
            d = other.price(good) - self.price(good)
            if abs(d) > 1e-9:
                out[good] = d
        return out


# ---------------------------------------------------------------------------
# 市场视图（给决策层看的聚合）
# ---------------------------------------------------------------------------
@dataclass
class ChainRow:
    chain: str
    n_goods: int = 0
    n_shortage: int = 0
    n_glut: int = 0
    shortage_value: float = 0.0
    tightest: str = ""
    tightest_ratio: float = 0.0
    loosest: str = ""
    loosest_ratio: float = 0.0

    def to_dict(self) -> dict:
        return {
            "chain": self.chain, "n": self.n_goods,
            "shortage": self.n_shortage, "glut": self.n_glut,
            "shortage_value": round(self.shortage_value, 0),
            "tightest": self.tightest, "tightest_ratio": round(self.tightest_ratio, 3),
            "loosest": self.loosest, "loosest_ratio": round(self.loosest_ratio, 3),
        }


class MarketView:
    def __init__(self, gd: GameData, model: PriceModel):
        self.gd = gd
        self.model = model
        self.pos: dict[str, GoodPosition] = model.positions()

    # -- 单商品 -------------------------------------------------------
    def get(self, good: str) -> GoodPosition:
        return self.pos.get(good) or self.model.position(good)

    def shortages(self, n: int = 8) -> list[GoodPosition]:
        rows = [p for p in self.pos.values() if p.shortage]
        return sorted(rows, key=lambda p: -p.gap_value)[:n]

    def gluts(self, n: int = 8) -> list[GoodPosition]:
        rows = [p for p in self.pos.values() if p.glut]
        return sorted(rows, key=lambda p: p.gap_value)[:n]

    def at_ceiling(self) -> list[GoodPosition]:
        return [p for p in self.pos.values() if p.at_ceiling]

    # -- 按链 ---------------------------------------------------------
    def chains(self) -> list[ChainRow]:
        rows: dict[str, ChainRow] = {}
        for p in self.pos.values():
            row = rows.get(p.chain)
            if row is None:
                row = rows[p.chain] = ChainRow(chain=p.chain)
            row.n_goods += 1
            if p.shortage:
                row.n_shortage += 1
                row.shortage_value += p.gap_value
            if p.glut:
                row.n_glut += 1
            if p.base > 0:
                if p.ratio > row.tightest_ratio:
                    row.tightest, row.tightest_ratio = p.good, p.ratio
                if row.loosest_ratio == 0.0 or p.ratio < row.loosest_ratio:
                    row.loosest, row.loosest_ratio = p.good, p.ratio
        order = {c: i for i, c in enumerate(CHAIN_ORDER)}
        return sorted(rows.values(),
                      key=lambda r: (order.get(r.chain, 99), -r.shortage_value))

    def chain(self, name: str) -> ChainRow | None:
        for r in self.chains():
            if r.chain == name:
                return r
        return None

    # -- 机会 ---------------------------------------------------------
    def opportunities(self, n: int = 8) -> list[GoodPosition]:
        """最值得扩产的商品：短缺且缺口金额大。"""
        rows = [p for p in self.pos.values() if p.shortage and not p.at_ceiling]
        return sorted(rows, key=lambda p: -p.gap_value)[:n]

    def summary(self) -> dict:
        return {
            "has_orders": self.model.has_orders,
            "n_goods": len(self.pos),
            "n_shortage": sum(1 for p in self.pos.values() if p.shortage),
            "n_glut": sum(1 for p in self.pos.values() if p.glut),
            "n_ceiling": sum(1 for p in self.pos.values() if p.at_ceiling),
            "n_floor": sum(1 for p in self.pos.values() if p.at_floor),
        }
