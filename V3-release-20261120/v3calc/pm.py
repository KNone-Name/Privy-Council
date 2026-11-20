# -*- coding: utf-8 -*-
"""生产方式（PM）可用性计算层。

回答两个问题：
  1. 玩家**当前科技/法律**下，某建筑/某 PM 组里哪些 PM 是允许的？
  2. 哪些是"现在就能换"的升级项？被什么卡住？

判定规则（全部来自 ``common/production_methods`` 的字段，语义按游戏触发器）：

  unlocking_technologies        任一科技已研发 → 解锁（OR）
  unlocking_laws                任一法律生效     → 解锁（OR）
  unlocking_production_methods  任一 PM 已启用    → 解锁（OR；同组 PM 互斥，
                                所以列表里通常是同组的不同档位）
  unlocking_principles          需要权力集团原则 → 条件未知（标记 conditional）
  unlocking_company_categories  需要公司类别     → 条件未知（标记 conditional）
  unlocking_identity            需要权力集团身份 → 条件未知
  unlocking_geographic_regions  需要州位于地理区域 → 有州→区域映射时判定，否则条件未知
  unlocking_religions           国教属于列表     → 可判定
  disallowing_laws              任一法律生效     → 禁止（NOR）
  disallowing_religions         国教属于列表     → 禁止
  required_input_goods          需要市场有该商品 → 市场供需可判定，否则条件未知

``conditional`` 不是"不可用"，而是"缺少判定所需的数据"（权力集团/公司/州区域）。
输出里会单独标出，不会被误报成 locked。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .gamedata import GameData, PM, PMGroup
from .model import Snapshot

STATUS_ACTIVE = "active"
STATUS_AVAILABLE = "available"
STATUS_CONDITIONAL = "conditional"
STATUS_LOCKED = "locked"

_STATUS_CN = {
    STATUS_ACTIVE: "已启用",
    STATUS_AVAILABLE: "可用",
    STATUS_CONDITIONAL: "条件未知",
    STATUS_LOCKED: "不可用",
}


@dataclass
class PMStatus:
    pm: str
    group: str
    status: str
    reasons: list[str] = field(default_factory=list)
    missing_techs: list[str] = field(default_factory=list)
    missing_laws: list[str] = field(default_factory=list)
    blocked_laws: list[str] = field(default_factory=list)
    missing_pms: list[str] = field(default_factory=list)
    conditional: list[str] = field(default_factory=list)
    hidden: bool = False
    is_default: bool = False

    @property
    def label(self) -> str:
        return _STATUS_CN.get(self.status, self.status)

    @property
    def allowed(self) -> bool:
        return self.status in (STATUS_ACTIVE, STATUS_AVAILABLE)

    def to_dict(self) -> dict:
        return {
            "pm": self.pm, "group": self.group, "status": self.status,
            "label": self.label, "reasons": self.reasons,
            "missing_techs": self.missing_techs,
            "missing_laws": self.missing_laws,
            "blocked_laws": self.blocked_laws,
            "missing_pms": self.missing_pms,
            "conditional": self.conditional,
            "hidden": self.hidden, "is_default": self.is_default,
        }


@dataclass
class GroupReport:
    group: str
    current: list[str] = field(default_factory=list)
    statuses: list[PMStatus] = field(default_factory=list)
    hidden_when_unavailable: bool = False

    @property
    def available(self) -> list[PMStatus]:
        return [s for s in self.statuses if s.status == STATUS_AVAILABLE]

    @property
    def allowed(self) -> list[PMStatus]:
        return [s for s in self.statuses if s.allowed]

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "current": self.current,
            "hidden_when_unavailable": self.hidden_when_unavailable,
            "pms": [s.to_dict() for s in self.statuses],
        }


@dataclass
class BuildingReport:
    building: str
    count: int = 0
    levels: int = 0
    locked_by_tech: bool = False       # 玩家还没解锁这座建筑本身
    missing_building_techs: list[str] = field(default_factory=list)
    groups: list[GroupReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "building": self.building, "count": self.count, "levels": self.levels,
            "building_unlocked": not self.locked_by_tech,
            "missing_building_techs": self.missing_building_techs,
            "groups": [g.to_dict() for g in self.groups],
            "warnings": self.warnings,
        }


class PMChecker:
    def __init__(self, gd: GameData, snap: Snapshot,
                 extra_techs: set[str] | None = None,
                 extra_laws: set[str] | None = None,
                 remove_laws: set[str] | None = None):
        self.gd = gd
        self.snap = snap
        self.techs = snap.tech_set() | set(extra_techs or ())
        self.laws = (snap.law_set() | set(extra_laws or ())) - set(remove_laws or ())
        self.religion = snap.country.religion if snap.country else None
        self.goods_available = set()
        if snap.market:
            for name, q in snap.market.supply.items():
                if q and q > 0:
                    self.goods_available.add(name)
            for name, q in snap.market.demand.items():
                if q and q > 0:
                    self.goods_available.add(name)
        # PM -> 所属组
        self.pm_groups: dict[str, list[str]] = {}
        for gname, group in gd.pm_groups.items():
            for pm in group.production_methods:
                self.pm_groups.setdefault(pm, []).append(gname)
        # 建筑类型 -> 已有建筑
        self.by_type: dict[str, list] = {}
        for b in snap.buildings:
            self.by_type.setdefault(b.type, []).append(b)
        # 州 -> 地理区域（存档若能给出；目前只保留接口）
        self.state_geo: dict[int, set[str]] = {}

    # ------------------------------------------------------------------
    def evaluate(self, pm: PM, group: str, active_pms: set[str],
                 owned_geo: set[str] | None = None,
                 prereq_pms: set[str] | None = None) -> PMStatus:
        st = PMStatus(pm=pm.name, group=group, status=STATUS_AVAILABLE,
                      is_default=pm.is_default)
        hard = False
        cond = False
        prereq = active_pms if prereq_pms is None else prereq_pms

        if pm.unlocking_technologies:
            missing = [t for t in pm.unlocking_technologies if t not in self.techs]
            if len(missing) == len(pm.unlocking_technologies):
                st.missing_techs = missing
                st.reasons.append("缺科技: " + ", ".join(missing))
                hard = True
        if pm.unlocking_laws:
            missing = [l for l in pm.unlocking_laws if l not in self.laws]
            if len(missing) == len(pm.unlocking_laws):
                st.missing_laws = missing
                st.reasons.append("缺法律: " + ", ".join(missing))
                hard = True
        blocked = [l for l in pm.disallowing_laws if l in self.laws]
        if blocked:
            st.blocked_laws = blocked
            st.reasons.append("被法律禁止: " + ", ".join(blocked))
            hard = True
        if pm.unlocking_religions:
            if self.religion:
                if self.religion not in pm.unlocking_religions:
                    st.reasons.append("宗教不符: 需要 %s，当前 %s"
                                      % (", ".join(pm.unlocking_religions), self.religion))
                    hard = True
            else:
                st.conditional.append("需要宗教: " + ", ".join(pm.unlocking_religions))
                cond = True
        if pm.disallowing_religions:
            if self.religion:
                if self.religion in pm.disallowing_religions:
                    st.reasons.append("宗教被禁止: " + self.religion)
                    hard = True
            else:
                st.conditional.append("宗教限制未知")
                cond = True
        if pm.unlocking_production_methods:
            if not (set(pm.unlocking_production_methods) & prereq):
                st.missing_pms = list(pm.unlocking_production_methods)
                st.reasons.append("需要先启用: " + " 或 ".join(pm.unlocking_production_methods))
                hard = True
        if pm.required_input_goods:
            if self.goods_available:
                if not (set(pm.required_input_goods) & self.goods_available):
                    st.reasons.append("市场没有输入商品: " + ", ".join(pm.required_input_goods))
                    hard = True
            else:
                st.conditional.append("输入商品: " + ", ".join(pm.required_input_goods))
                cond = True
        if pm.unlocking_company_categories:
            st.conditional.append("需要公司类别: " + ", ".join(pm.unlocking_company_categories))
            cond = True
        if pm.unlocking_principles:
            st.conditional.append("需要权力集团原则: " + ", ".join(pm.unlocking_principles))
            cond = True
        if pm.unlocking_identity:
            st.conditional.append("需要权力集团身份: " + ", ".join(pm.unlocking_identity))
            cond = True
        if pm.unlocking_geographic_regions:
            if owned_geo is not None:
                if not (set(pm.unlocking_geographic_regions) & owned_geo):
                    st.reasons.append("地理区域不符: " + ", ".join(pm.unlocking_geographic_regions))
                    hard = True
            else:
                st.conditional.append("需要地理区域: " + ", ".join(pm.unlocking_geographic_regions))
                cond = True

        if hard:
            st.status = STATUS_LOCKED
        elif cond:
            st.status = STATUS_CONDITIONAL
        elif pm.name in active_pms:
            st.status = STATUS_ACTIVE
        else:
            st.status = STATUS_AVAILABLE
        st.hidden = (pm.is_hidden_when_unavailable or
                     self.gd.pm_groups.get(group, PMGroup(group)).is_hidden_when_unavailable) \
            and st.status == STATUS_LOCKED
        return st

    # ------------------------------------------------------------------
    def building_report(self, building: str, include_locked: bool = True) -> BuildingReport:
        bd = self.gd.buildings.get(building)
        rep = BuildingReport(building=building)
        if bd is None:
            rep.warnings.append("游戏数据里没有建筑 %s" % building)
            return rep
        rows = self.by_type.get(building, [])
        rep.count = len(rows)
        rep.levels = sum(b.levels for b in rows)
        if bd.unlocking_technologies:
            missing = [t for t in bd.unlocking_technologies if t not in self.techs]
            if len(missing) == len(bd.unlocking_technologies):
                rep.locked_by_tech = True
                rep.missing_building_techs = missing

        # 当前 PM 集合（同类型建筑可能有不同配置，逐组取并集）
        current_by_group: dict[str, set[str]] = {}
        for b in rows:
            for pm_name in b.production_methods:
                for g in self.pm_groups.get(pm_name, []):
                    if g in bd.production_method_groups:
                        current_by_group.setdefault(g, set()).add(pm_name)
        if not rows:
            # 尚未建造：仅用于"前置 PM"判定，不把默认档位当成已启用
            rep.warnings.append("该国尚未建造此建筑；当前 PM 为空，前置判定按默认档位估算")
        active_all = {pm for s in current_by_group.values() for pm in s}
        ctx_pms = set(active_all)
        if not rows:
            for gname in bd.production_method_groups:
                d = self.gd.default_pm(gname)
                if d is not None:
                    ctx_pms.add(d.name)

        for gname in bd.production_method_groups:
            group = self.gd.pm_groups.get(gname)
            if group is None:
                rep.warnings.append("PM 组不存在: %s" % gname)
                continue
            gr = GroupReport(group=gname,
                             current=sorted(current_by_group.get(gname, set())),
                             hidden_when_unavailable=group.is_hidden_when_unavailable)
            owned_geo = None
            if self.state_geo:
                owned_geo = set()
                for sid in (s.id for s in self.snap.states):
                    owned_geo |= self.state_geo.get(sid, set())
            for pm_name in group.production_methods:
                pm = self.gd.pms.get(pm_name)
                if pm is None:
                    gr.statuses.append(PMStatus(
                        pm=pm_name, group=gname, status=STATUS_LOCKED,
                        reasons=["游戏数据里没有这个 PM（mod？）"]))
                    continue
                st = self.evaluate(pm, gname, active_all, owned_geo, prereq_pms=ctx_pms)
                if st.hidden and not include_locked:
                    continue
                gr.statuses.append(st)
            rep.groups.append(gr)
        return rep

    # ------------------------------------------------------------------
    def country_report(self, only_present: bool = True) -> list[BuildingReport]:
        types = self.snap.building_types() if only_present else sorted(self.gd.buildings)
        out = []
        for t in types:
            out.append(self.building_report(t))
        return out

    # ------------------------------------------------------------------
    def upgrades(self, building: str, only_allowed: bool = True) -> list[dict]:
        """该建筑每个组里"现在就能换、且不是当前"的 PM。"""
        rep = self.building_report(building)
        out = []
        for gr in rep.groups:
            for st in gr.statuses:
                if st.status == STATUS_ACTIVE:
                    continue
                if only_allowed and not st.allowed:
                    continue
                out.append({
                    "building": building, "group": gr.group,
                    "current": gr.current, "pm": st.pm,
                    "status": st.status, "reasons": st.reasons,
                })
        return out
