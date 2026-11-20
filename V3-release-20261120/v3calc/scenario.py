# -*- coding: utf-8 -*-
"""方案（scenario）计算层：替换 PM / 局部建筑，重算 GDP，给出增量。

典型用法：
    * "当前科技/法律下，每个 PM 组换哪一档 GDP 最高？"
    * "把 textile_mill 的 pm_handsewn_clothes 换成 pm_sewing_machines，
       全国 GDP 变化多少？"
    * "只改某个州的建筑，影响多少？"（state_id 过滤）

所有函数都不修改传入的 Snapshot，内部 deepcopy 后计算。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from .gamedata import GameData
from .gdp import GDPCalculator, GDPResult
from .model import Snapshot
from .pm import (PMChecker, STATUS_ACTIVE, STATUS_AVAILABLE, STATUS_CONDITIONAL,
                 STATUS_LOCKED)


@dataclass
class PMDelta:
    building: str
    group: str
    current: list[str]
    candidate: str
    status: str
    gdp_before: float
    gdp_after: float
    delta: float
    delta_pct: float
    state_id: int | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def clone(snap: Snapshot) -> Snapshot:
    return copy.deepcopy(snap)


def set_pm(snap: Snapshot, gd: GameData, building_type: str, group: str,
           new_pm: str, state_id: int | None = None, checker=None) -> int:
    """把指定建筑类型（可限定州）在 group 里的 PM 换成 new_pm，返回改动栋数。

    ``checker`` 不为空时，逐个建筑按它**当前自己的 PM 组合**复核候选 PM；
    前置 PM 不满足的建筑会被跳过（例如没有锯木厂的伐木场不能直接换
    pm_increased_hardwood），避免把全国所有同类建筑都算成可换。
    """
    group_def = gd.pm_groups.get(group)
    if group_def is None:
        raise KeyError("没有 PM 组 %s" % group)
    members = set(group_def.production_methods)
    pm = gd.pms.get(new_pm)
    n = 0
    for b in snap.buildings:
        if b.type != building_type:
            continue
        if state_id is not None and b.state_id != state_id:
            continue
        pms = list(b.production_methods)
        if not any(p in members for p in pms):
            continue
        if checker is not None and pm is not None:
            st = checker.evaluate(pm, group, set(pms))
            if st.status == STATUS_LOCKED:
                continue
        replaced = False
        for i, p in enumerate(pms):
            if p in members:
                pms[i] = new_pm
                replaced = True
        if replaced:
            b.production_methods = pms
            n += 1
    return n


def gdp_of(gd: GameData, snap: Snapshot, price: str = "auto",
           scale: str = "staffing", buildings: str = "producers") -> GDPResult:
    return GDPCalculator(gd, snap).compute(price=price, scale=scale,
                                           buildings=buildings)


def pm_delta(gd: GameData, snap: Snapshot, building_type: str, group: str,
             new_pm: str, state_id: int | None = None, price: str = "auto",
             scale: str = "staffing", buildings: str = "producers",
             base: GDPResult | None = None, checker=None) -> PMDelta:
    base = base or gdp_of(gd, snap, price, scale, buildings)
    work = clone(snap)
    n = set_pm(work, gd, building_type, group, new_pm, state_id, checker=checker)
    if not n:
        raise ValueError("%s 在组 %s 里没有可替换的建筑" % (building_type, group))
    after = gdp_of(gd, work, price, scale, buildings)
    delta = after.annual - base.annual
    return PMDelta(
        building=building_type, group=group,
        current=[], candidate=new_pm, status="",
        gdp_before=base.annual, gdp_after=after.annual,
        delta=delta, delta_pct=(100.0 * delta / base.annual if base.annual else 0.0),
        state_id=state_id,
    )


def rank_pm_upgrades(gd: GameData, snap: Snapshot,
                     building_type: str | None = None,
                     group: str | None = None,
                     only_allowed: bool = True,
                     price: str = "auto", scale: str = "staffing",
                     buildings: str = "producers",
                     include_conditional: bool = False,
                     state_id: int | None = None) -> list[PMDelta]:
    """对每个建筑/PM 组，枚举候选 PM，按 GDP 年增量排序。"""
    checker = PMChecker(gd, snap)
    base = gdp_of(gd, snap, price, scale, buildings)
    types = [building_type] if building_type else snap.building_types()
    out: list[PMDelta] = []
    for btype in types:
        rep = checker.building_report(btype)
        for gr in rep.groups:
            if group and gr.group != group:
                continue
            current = set(gr.current)
            # 当前 PM 是"组里被启用的那个"，候选 = 其余可用项
            for st in gr.statuses:
                if st.pm in current:
                    continue
                if only_allowed and not st.allowed:
                    continue
                if st.status == STATUS_CONDITIONAL and not include_conditional:
                    continue
                if st.status not in (STATUS_AVAILABLE, STATUS_CONDITIONAL, STATUS_ACTIVE):
                    continue
                try:
                    d = pm_delta(gd, snap, btype, gr.group, st.pm,
                                 state_id=state_id,
                                 price=price, scale=scale, buildings=buildings,
                                 base=base, checker=checker)
                except ValueError:
                    continue
                d.current = sorted(current)
                d.status = st.status
                out.append(d)
    out.sort(key=lambda x: -x.delta)
    return out


def best_upgrade_per_group(gd: GameData, snap: Snapshot,
                           price: str = "auto", scale: str = "staffing",
                           buildings: str = "producers",
                           include_conditional: bool = False) -> list[PMDelta]:
    rows = rank_pm_upgrades(gd, snap, price=price, scale=scale,
                            buildings=buildings,
                            include_conditional=include_conditional)
    best: dict[tuple[str, str], PMDelta] = {}
    for r in rows:
        key = (r.building, r.group)
        if key not in best or r.delta > best[key].delta:
            best[key] = r
    return sorted(best.values(), key=lambda x: -x.delta)
