# -*- coding: utf-8 -*-
"""快照数据模型：内存读取层与存档读取层共用的中间格式。

设计原则：
  * **只放事实，不放结论**——GDP、可用 PM 等由计算层（``gdp.py`` / ``pm.py``）算；
  * 所有金额/价格是"游戏内英镑"，定点除法已经做完；数量是游戏单位；
  * 每个字段允许 ``None``：读不到就是读不到，不填 0 假装读到；
  * 可 JSON 序列化（``Snapshot.to_dict()`` / ``Snapshot.from_dict()``），
    这样一次实时读取可以落盘，之后离线反复做计算与回放。
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

SCHEMA = 1


@dataclass
class GameInfo:
    source: str = "live"                 # live | save | demo
    captured_at: str = ""
    game_version: str | None = None
    game_dir: str | None = None
    exe_path: str | None = None
    pid: int | None = None
    module_base: str | None = None
    process_start: str | None = None
    save_path: str | None = None
    save_date: str | None = None
    save_name: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Budget:
    income: list[float] = field(default_factory=list)     # 16 槽
    expenses: list[float] = field(default_factory=list)   # 22 槽
    income_total: float | None = None
    expense_total: float | None = None
    balance: float | None = None
    tax_level: int | None = None
    weekly_investment_pool: float | None = None


@dataclass
class Country:
    id: int
    tag: str | None = None               # CHI / RUS ...（内存读不到，存档可读）
    name: str | None = None
    market_id: int | None = None
    religion: str | None = None
    treasury: float | None = None
    credit_limit: float | None = None
    loan_principal: float | None = None
    base_wage: float | None = None
    population: int | None = None
    gdp: float | None = None             # 存档趋势值（年化），仅用于校验
    budget: Budget = field(default_factory=Budget)


@dataclass
class StateRow:
    id: int
    owner: int | None = None
    capital: int | None = None
    arable_land: int | None = None
    infrastructure: float | None = None
    infrastructure_used: float | None = None
    population: int | None = None
    population_lower: int | None = None
    population_middle: int | None = None
    population_upper: int | None = None
    radicals: int | None = None
    unemployed: int | None = None
    laborers: int | None = None
    local_prices: list[float] | None = None    # 54 项，按商品 index
    market_supply: list[float] | None = None
    market_demand: list[float] | None = None


@dataclass
class BuildingRow:
    type: str
    state_id: int
    levels: int = 0
    staffing: float = 0.0
    throughput: float = 1.0
    production_methods: list[str] = field(default_factory=list)
    id: int | None = None
    profit: float | None = None
    salary_rate: float | None = None
    hiring_rate: float | None = None
    active: bool = True


@dataclass
class MarketRow:
    id: int | None = None
    prices: dict[str, float] = field(default_factory=dict)
    base_prices: dict[str, float] = field(default_factory=dict)
    supply: dict[str, float] = field(default_factory=dict)
    demand: dict[str, float] = field(default_factory=dict)


@dataclass
class LawRow:
    name: str
    group: str | None = None
    date: int | None = None


@dataclass
class DecreeRow:
    name: str
    state_id: int | None = None


@dataclass
class InstitutionRow:
    type: str
    investment: int = 0


@dataclass
class CompanyRow:
    type: str
    region: str | None = None
    building_id: int | None = None
    ceo: int | None = None


@dataclass
class InterestGroupRow:
    name: str
    clout: float = 0.0
    approval: int | None = None
    in_government: bool = False
    party: int | None = None
    political_strength: int | None = None


@dataclass
class MovementRow:
    type: str
    ideology: str | None = None
    radicalism: float = 0.0
    pops: int = 0


@dataclass
class TimedModifierRow:
    scope: str                           # country | state
    name: str
    value: float = 0.0
    target_id: int | None = None
    start: int | None = None
    end: int | None = None


@dataclass
class TaxRow:
    level: int | None = None
    taxation_law: str | None = None
    rates: dict[str, float] = field(default_factory=dict)
    taxed_goods: list[str] = field(default_factory=list)
    import_tariffs: dict[str, str] = field(default_factory=dict)
    export_tariffs: dict[str, str] = field(default_factory=dict)


@dataclass
class Diagnostics:
    timings: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)   # ok | empty | error | skipped


@dataclass
class Snapshot:
    game: GameInfo = field(default_factory=GameInfo)
    country: Country | None = None
    states: list[StateRow] = field(default_factory=list)
    buildings: list[BuildingRow] = field(default_factory=list)
    market: MarketRow | None = None
    laws: list[LawRow] = field(default_factory=list)
    techs: list[str] = field(default_factory=list)
    decrees: list[DecreeRow] = field(default_factory=list)
    institutions: list[InstitutionRow] = field(default_factory=list)
    companies: list[CompanyRow] = field(default_factory=list)
    interest_groups: list[InterestGroupRow] = field(default_factory=list)
    movements: list[MovementRow] = field(default_factory=list)
    timed_modifiers: list[TimedModifierRow] = field(default_factory=list)
    tax: TaxRow | None = None
    pops: list[dict] | None = None       # 预留：按州×职业聚合
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    schema: int = SCHEMA

    # -- 便捷 ---------------------------------------------------------
    @property
    def country_id(self) -> int | None:
        return self.country.id if self.country else None

    def buildings_of(self, btype: str) -> list[BuildingRow]:
        return [b for b in self.buildings if b.type == btype]

    def states_of(self) -> dict[int, StateRow]:
        return {s.id: s for s in self.states}

    def law_set(self) -> set[str]:
        return {l.name for l in self.laws}

    def tech_set(self) -> set[str]:
        return set(self.techs)

    def building_types(self) -> list[str]:
        return sorted({b.type for b in self.buildings})

    # -- JSON ---------------------------------------------------------
    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        return _from(cls, d)

    def dumps(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def save(self, path: str, indent: int | None = None) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=indent)

    @classmethod
    def load(cls, path: str) -> "Snapshot":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def summary(self) -> dict:
        c = self.country
        return {
            "source": self.game.source,
            "country": c.id if c else None,
            "tag": c.tag if c else None,
            "states": len(self.states),
            "buildings": len(self.buildings),
            "levels": sum(b.levels for b in self.buildings),
            "population": c.population if c else None,
            "gdp_save": c.gdp if c else None,
            "market_prices": len(self.market.prices) if self.market else 0,
            "laws": len(self.laws),
            "techs": len(self.techs),
            "decrees": len(self.decrees),
            "institutions": len(self.institutions),
            "companies": len(self.companies),
            "interest_groups": len(self.interest_groups),
            "movements": len(self.movements),
            "timed_modifiers": len(self.timed_modifiers),
            "pops": len(self.pops) if self.pops is not None else None,
        }


# ---------------------------------------------------------------------------
# 反序列化
# ---------------------------------------------------------------------------
def _from(cls, d: Any):
    if not dataclasses.is_dataclass(cls) or not isinstance(d, dict):
        return d
    import typing

    try:
        hints = typing.get_type_hints(cls)
    except Exception:
        hints = {}
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name not in d:
            continue
        val = d[f.name]
        kwargs[f.name] = _convert(hints.get(f.name, f.type), val)
    return cls(**kwargs)


def _convert(tp, val):
    """按 dataclass 字段类型做浅转换（支持 list[...] 与嵌套 dataclass）。"""
    import types
    import typing

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in (list, typing.List):
        inner = args[0] if args else Any
        return [_convert(inner, v) for v in val] if isinstance(val, list) else val
    if origin in (dict, typing.Dict):
        return val
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return _from(tp, val)
    if origin is typing.Union or origin is types.UnionType:
        # 只处理 X | None
        for a in args:
            if a is type(None):
                continue
            conv = _convert(a, val)
            if conv is not None:
                return conv
        return val
    return val
