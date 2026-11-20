# -*- coding: utf-8 -*-
"""世界模型：把"事实（快照）+ 规则（游戏文件）+ 静态地理 + 队列"拼成决策上下文。

这一层不调用 LLM，也不写游戏；只回答：
  * 现在国家是什么样（概况/预算/市场/州/建筑/政治）；
  * 某个州能不能建某建筑、还能建几级；
  * 某栋建筑能不能换某个 PM、换了有什么非GDP效果；
  * 建 N 级 / 换 PM 对 GDP 和就业的近似影响。
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from typing import Any

from v3calc.gamedata import GameData
from v3calc.gdp import GDPCalculator, GDPResult
from v3calc.model import BuildingRow, Snapshot
from v3calc.pm import (PMChecker, STATUS_ACTIVE, STATUS_AVAILABLE,
                       STATUS_CONDITIONAL, STATUS_LOCKED)
from v3calc import pdx
from v3calc.scenario import pm_delta, rank_pm_upgrades

from .util import age_seconds, safe_float, safe_int

LEGACY_BUILDINGS_KB = r"D:\Game\ai\Victoria3-RE\legacy\v3-agent-data\data\buildings.json"

# 预算槽位名称（来源：RE 逆向记录 §9.1 / §9.2）
INCOME_LABELS = {
    0: "额外收入", 1: "所得税", 2: "人头税", 3: "消费税", 4: "红利税",
    5: "外交条约", 7: "投资池回收", 8: "铸币收入", 9: "关税",
    10: "政府分红", 11: "条约(商品)", 12: "条约(资金)", 15: "补给网络",
}
EXPENSE_LABELS = {
    0: "额外支出", 1: "政府奴隶", 2: "政府建筑商品", 3: "政府工资",
    4: "军用奴隶", 5: "军事维护商品", 6: "军队工资", 7: "建造商品",
    8: "补贴", 9: "补助金", 10: "利息", 11: "外交条约",
    13: "福利金", 14: "国有建筑亏损", 15: "条约资金转让",
    16: "条约商品输送", 17: "补给船建造", 18: "船舰建造力",
    19: "补给船", 20: "军事船舰", 21: "船运线路",
}

_NON_GDP_KEYS = (
    "bureaucracy", "construction", "mortality", "standard_of_living",
    "radicals", "loyalists", "qualifications", "literacy", "innovation",
    "authority", "influence", "infrastructure",
)

# 消耗耕地的农业建筑（没有固定 resource_caps，上限按州 arable_land 算）
_ARABLE_PREFIXES = (
    "building_wheat_farm", "building_rye_farm", "building_rice_farm",
    "building_maize_farm", "building_millet_farm", "building_livestock_ranch",
    "building_vineyard", "building_cotton_plantation", "building_tobacco_plantation",
    "building_sugar_plantation", "building_coffee_plantation", "building_tea_plantation",
    "building_banana_plantation", "building_silk_plantation", "building_opium_plantation",
    "building_dye_plantation", "building_rubber_plantation", "building_cocoa_plantation",
)


def _is_arable_building(name: str) -> bool:
    return any(name.startswith(p) for p in _ARABLE_PREFIXES)


# ---------------------------------------------------------------------------
# 州静态地理
# ---------------------------------------------------------------------------
@dataclass
class StateMeta:
    state_id: int
    region: str | None = None
    coastal: bool = False
    arable_land: int | None = None
    resources: list[str] = field(default_factory=list)
    resource_caps: dict[str, int] = field(default_factory=dict)
    traits: list[str] = field(default_factory=list)
    incorporated: bool | None = None

    def arable_resources(self) -> list[str]:
        return [r for r in self.resources if _is_arable_building(r)]


def load_state_meta(path: str) -> tuple[dict[int, StateMeta], dict]:
    meta: dict[int, StateMeta] = {}
    info: dict = {}
    if not path or not os.path.isfile(path):
        return meta, {"error": "缺少 state_meta 文件：%s" % path}
    try:
        import json
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as exc:
        return meta, {"error": "state_meta 读取失败：%s" % exc}
    info = dict(d.get("_meta") or {})
    for sid, row in (d.get("states") or {}).items():
        try:
            i = int(sid)
        except (TypeError, ValueError):
            continue
        meta[i] = StateMeta(
            state_id=i,
            region=row.get("region"),
            coastal=bool(row.get("coastal")),
            arable_land=safe_int(row.get("arable_land"), None),
            resources=[str(x) for x in (row.get("resources") or [])],
            resource_caps={str(k): int(v) for k, v in (row.get("resource_caps") or {}).items()
                           if isinstance(v, (int, float))},
            traits=[str(x) for x in (row.get("traits") or [])],
            incorporated=row.get("incorporated"),
        )
    return meta, info


# ---------------------------------------------------------------------------
# 建筑知识库
# ---------------------------------------------------------------------------
@dataclass
class BuildingInfo:
    name: str
    group: str = ""
    techs: list[str] = field(default_factory=list)
    laws: list[str] = field(default_factory=list)
    required_construction: str = ""
    buildable: bool = True
    expandable: bool = True
    unique: bool = False
    max_level: int | None = None
    city_type: str = ""
    allowed_regions: list[str] = field(default_factory=list)
    requires_coastal: bool = False
    requires_incorporated: bool = False
    requires_traits: list[str] = field(default_factory=list)
    resource_bound: bool = False
    subsistence: bool = False
    source: str = "game"

    def to_dict(self) -> dict:
        return {
            "building": self.name, "group": self.group,
            "techs": self.techs, "laws": self.laws,
            "required_construction": self.required_construction,
            "buildable": self.buildable, "expandable": self.expandable,
            "unique": self.unique, "max_level": self.max_level,
            "city_type": self.city_type,
            "allowed_regions": self.allowed_regions,
            "requires_coastal": self.requires_coastal,
            "requires_incorporated": self.requires_incorporated,
            "requires_traits": self.requires_traits,
            "resource_bound": self.resource_bound,
            "subsistence": self.subsistence,
        }


def _parse_potential(block) -> dict:
    """递归扫 potential 触发器，收集州区域 / 沿海 / 整合 / 州特质条件。

    这些条件在游戏文件里可能直接写、也可能包在 OR / AND / NOT 里；
    决策层只做保守校验：拿不到就降级成 warning，不误杀。
    """
    out = {"allowed_regions": [], "requires_coastal": False,
           "requires_incorporated": False, "requires_traits": []}
    if block is None:
        return out
    regions: list[str] = []

    def walk(b):
        if b is None or not hasattr(b, "multi"):
            return
        for k, vals in b.multi.items():
            for v in vals:
                if k == "state_region" and isinstance(v, str):
                    regions.append(v)
                if k in ("is_coastal", "is_incorporated") and str(v).lower() == "yes":
                    if k == "is_coastal":
                        out["requires_coastal"] = True
                    else:
                        out["requires_incorporated"] = True
                if k in ("has_state_trait", "has_state_modifier") and isinstance(v, str):
                    out["requires_traits"].append(v)
                if hasattr(v, "multi"):
                    walk(v)
    walk(block)
    for r in regions:
        r = str(r)
        if r.startswith("s:"):
            r = r[2:]
        if r.startswith("STATE_"):
            out["allowed_regions"].append(r)
    out["allowed_regions"] = sorted(set(out["allowed_regions"]))
    out["requires_traits"] = sorted(set(out["requires_traits"]))
    return out


def load_building_kb(gd: GameData) -> tuple[dict[str, BuildingInfo], dict]:
    """从游戏文件解析建筑；用旧版 knowledge base 补齐 resource_bound 等。"""
    info: dict[str, BuildingInfo] = {}
    raw = pdx.parse_dir(os.path.join(gd.game_dir, "common", "buildings"))
    legacy = {}
    try:
        import json
        if os.path.isfile(LEGACY_BUILDINGS_KB):
            with open(LEGACY_BUILDINGS_KB, encoding="utf-8") as f:
                legacy = json.load(f) or {}
    except (OSError, ValueError):
        legacy = {}

    for name, v in raw.items():
        if not name.startswith("building_") or not isinstance(v, pdx.Obj):
            continue
        pot = _parse_potential(v.block("potential"))
        bi = BuildingInfo(
            name=name,
            group=v.get("building_group", "") or "",
            techs=list(v.atoms("unlocking_technologies")),
            laws=list(v.atoms("unlocking_laws")),
            required_construction=v.get("required_construction", "") or "",
            buildable=str(v.get("buildable", "yes")).lower() != "no",
            expandable=str(v.get("expandable", "yes")).lower() != "no",
            unique=v.flag("unique"),
            max_level=(int(v.num("max_level")) if v.get("max_level") else None),
            city_type=v.get("city_type", "") or "",
            allowed_regions=pot["allowed_regions"],
            requires_coastal=pot["requires_coastal"],
            requires_incorporated=pot["requires_incorporated"],
            requires_traits=pot["requires_traits"],
            subsistence=("bg_subsistence" in (v.get("building_group", "") or "")),
            source="game",
        )
        old = legacy.get(name) or {}
        if old:
            if old.get("resource_bound"):
                bi.resource_bound = True
            if not bi.techs and old.get("techs"):
                bi.techs = list(old.get("techs") or [])
            if not bi.allowed_regions and old.get("allowed_regions"):
                bi.allowed_regions = list(old.get("allowed_regions") or [])
            if not bi.requires_coastal and old.get("requires_coastal"):
                bi.requires_coastal = True
            if not bi.laws and old.get("laws"):
                bi.laws = list(old.get("laws") or [])
            if old.get("buildable") == "no":
                bi.buildable = False
        info[name] = bi

    # 资源类建筑：只要出现在任一州 resources 里就标记（比旧 KB 更贴近当前版本）
    res_types = set()
    for bi in info.values():
        if bi.resource_bound:
            res_types.add(bi.name)
    return info, {"count": len(info), "resource_types": len(res_types),
                  "legacy_kb": bool(legacy)}


def mark_resource_buildings(buildings: dict[str, BuildingInfo],
                            state_meta: dict[int, StateMeta]) -> None:
    """只要某建筑出现在任一周的 resources 里，就视为资源类建筑。

    旧版 knowledge base 只标了矿/伐木/渔场，漏了种植园/农田；这里用
    ``v3_states.json`` 推导的静态地理补齐。
    """
    res: set[str] = set()
    for meta in state_meta.values():
        res.update(meta.resources or [])
    for name, bi in buildings.items():
        if name in res:
            bi.resource_bound = True


def load_construction_costs(gd: GameData) -> dict[str, float]:
    path = os.path.join(gd.game_dir, "common", "script_values", "building_values.txt")
    out: dict[str, float] = {}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError:
        return out
    for m in re.finditer(r"^\s*(construction_cost_[a-z0-9_]+)\s*=\s*([0-9.]+)", text, re.M):
        out[m.group(1)] = float(m.group(2))
    return out


# ---------------------------------------------------------------------------
# 决策上下文
# ---------------------------------------------------------------------------
class DecisionContext:
    def __init__(self, snap: Snapshot, gd: GameData,
                 state_meta: dict[int, StateMeta],
                 buildings: dict[str, BuildingInfo],
                 costs: dict[str, float],
                 strategy: Any = None,
                 queue: list[dict] | None = None,
                 pending: list[Any] | None = None,
                 meta_info: dict | None = None,
                 bridge: Any = None):
        self.snap = snap
        self.gd = gd
        self.state_meta = state_meta
        self.buildings_kb = buildings
        self.costs = costs
        self.strategy = strategy
        self.queue = list(queue or [])
        self.pending = list(pending or [])
        self.meta_info = dict(meta_info or {})
        self.bridge = bridge
        self.country_id = (snap.country.id if snap.country else None)

        self.states = {s.id: s for s in snap.states}
        self.by_state_type: dict[tuple[int, str], list[BuildingRow]] = {}
        self.levels_by_type: dict[str, int] = {}
        self.buildings_by_type: dict[str, list[BuildingRow]] = {}
        for b in snap.buildings:
            self.by_state_type.setdefault((b.state_id, b.type), []).append(b)
            self.levels_by_type[b.type] = self.levels_by_type.get(b.type, 0) + b.levels
            self.buildings_by_type.setdefault(b.type, []).append(b)

        self.techs = snap.tech_set()
        self.laws = snap.law_set()
        self._gdp_cache: dict[tuple, GDPResult] = {}
        self._pm_checker: PMChecker | None = None
        self._build_cache: dict[tuple, dict] = {}

    # -- 基础 ---------------------------------------------------------
    def gdp(self, price: str = "auto", scale: str = "staffing",
            buildings: str = "producers") -> GDPResult:
        key = (price, scale, buildings)
        if key not in self._gdp_cache:
            self._gdp_cache[key] = GDPCalculator(self.gd, self.snap).compute(
                price=price, scale=scale, buildings=buildings)
        return self._gdp_cache[key]

    def pm_checker(self) -> PMChecker:
        if self._pm_checker is None:
            checker = PMChecker(self.gd, self.snap)
            # 用静态州→区域映射补上地理条件，减少 conditional
            geo: dict[int, set[str]] = {}
            for sid, meta in self.state_meta.items():
                if meta.region:
                    g = self.gd.state_region_geo.get(meta.region)
                    if g:
                        geo[sid] = set(g)
            checker.state_geo = geo
            self._pm_checker = checker
        return self._pm_checker

    def owned_geo(self) -> set[str]:
        out: set[str] = set()
        for s in self.snap.states:
            meta = self.state_meta.get(s.id)
            if meta and meta.region:
                out |= set(self.gd.state_region_geo.get(meta.region, set()))
        return out

    def cost_of(self, building: str, levels: int = 1) -> float:
        bi = self.buildings_kb.get(building)
        if not bi:
            return 0.0
        per = self.costs.get(bi.required_construction, 400.0)
        return float(per) * max(1, int(levels))

    def construction_capacity(self) -> dict:
        """每周建造力近似：建筑 PM 的 country_construction_add + GDP 基线 10。

        与 RE 文档的 capacity 命令同口径（workforce_scaled = 等级 × 吞吐），
        但不含州级效率/临时修正；用于让 LLM 对"要排多少周"有量级感。
        """
        from_pms = 0.0
        sources: list[dict] = []
        mult = 0.0
        for b in self.snap.buildings:
            thr = b.throughput if b.throughput and b.throughput > 0 else 1.0
            for pm_name in b.production_methods:
                pm = self.gd.pms.get(pm_name)
                if pm is None:
                    continue
                for scope, scales in (pm.modifiers.values or {}).items():
                    for scale, kv in (scales or {}).items():
                        v = kv.get("country_construction_add")
                        if v:
                            factor = (b.levels * thr if scale == "workforce_scaled"
                                      else b.levels if scale == "level_scaled"
                                      else 1.0)
                            add = float(v) * factor
                            from_pms += add
                            if abs(add) > 0.01:
                                sources.append({"type": b.type, "pm": pm_name,
                                                "add": round(add, 2)})
                        m = kv.get("country_construction_mult")
                        if m:
                            mult += float(m)
        base = 10.0
        capacity = max(0.0, (base + from_pms) * (1.0 + mult))
        return {
            "capacity_per_week": round(capacity, 2),
            "base": base,
            "from_buildings": round(from_pms, 2),
            "mult": round(mult, 4),
            "sources_top": sorted(sources, key=lambda x: -abs(x["add"]))[:6],
            "note": "近似值：建筑 PM + GDP 基线；不含州效率/临时修正",
        }

    # -- 队列 / 待执行 -------------------------------------------------
    def queued_entries(self, state_id: int | None = None,
                       building: str | None = None) -> list[dict]:
        out = []
        for q in self.queue:
            if state_id is not None and safe_int(q.get("state_id")) != state_id:
                continue
            if building and q.get("building") != building:
                continue
            out.append(q)
        return out

    def queued_projects_in_state(self, state_id: int) -> int:
        return sum(1 for q in self.queue if int(q.get("state_id") or 0) == int(state_id))

    def queue_stats(self) -> dict:
        queue = list(self.queue or [])
        active = 0
        for q in queue:
            total = float(q.get("pts_total") or 0)
            remain = float(q.get("pts_remaining") or 0)
            if total > 0 and 0 < remain < total:
                active += 1
        # “在建项目”只算真正有建造力投入的：进度在 0 和 1 之间。
        # 如果队列进度字段不可用，队首是当前获得建造力的项目，按 1 个在建算。
        if queue and active == 0:
            active = 1
        soft_limit = active * 3 if active else 0
        overloaded = bool(active and len(queue) > soft_limit)
        if active:
            message = ("队列 %d 项，超过在建项目 %d 的 3 倍；本轮应尽量少新增建造"
                       % (len(queue), active))
        elif queue:
            message = "队列里当前没有实际建造力投入的项目"
        else:
            message = ""
        return {
            "count": len(queue),
            "active_projects": active,
            "soft_limit": soft_limit,
            "overloaded": overloaded,
            "message": message if overloaded or not active else "",
        }

    def queued_levels(self, state_id: int, building: str) -> int:
        rows = self.queued_entries(state_id, building)
        # 队列元素通常是"一单一级"；若执行端给了 levels 就用 levels
        return sum(int(safe_float(q.get("levels"), 1) or 1) for q in rows)

    def pending_of(self, kind: str | None = None) -> list[Any]:
        if kind is None:
            return list(self.pending)
        return [c for c in self.pending if getattr(c, "kind", None) == kind]

    def pending_levels(self, state_id: int, building: str) -> int:
        n = 0
        for c in self.pending:
            if getattr(c, "kind", "") == "build" and getattr(c, "state_id", None) == state_id \
                    and getattr(c, "building", "") == building:
                n += int(getattr(c, "levels", 1) or 1)
        return n

    def projected_levels(self, state_id: int, building: str) -> int:
        cur = sum(b.levels for b in self.by_state_type.get((state_id, building), []))
        return cur + self.queued_levels(state_id, building) + self.pending_levels(state_id, building)

    def existing_levels(self, state_id: int, building: str) -> int:
        return sum(b.levels for b in self.by_state_type.get((state_id, building), []))

    # -- 建造可建性 ----------------------------------------------------
    def check_build(self, state_id: int, building: str, levels: int = 1) -> dict:
        if self.bridge is not None:
            out = self.bridge.check_build(state_id, building, levels)
            if out:
                return out
        key = (state_id, building, int(levels), len(self.pending))
        if key in self._build_cache:
            return copy.deepcopy(self._build_cache[key])
        out = {
            "ok": False, "code": "invalid", "building": building,
            "state_id": state_id, "levels": int(levels), "reasons": [], "warnings": [],
            "region": None, "remaining": None, "existing_levels": 0,
            "queued_levels": 0, "projected_levels": 0, "cost_points": 0.0,
            "cost_category": "",
        }
        st = self.states.get(state_id)
        if st is None:
            out["reasons"].append("州 %s 不在当前快照（可能不是玩家州）" % state_id)
            self._build_cache[key] = copy.deepcopy(out)
            return out
        if st.owner is not None and self.country_id is not None and st.owner != self.country_id:
            out["reasons"].append("州 %s 属于国家 %s，不是玩家 %s" % (state_id, st.owner, self.country_id))
        bi = self.buildings_kb.get(building)
        if bi is None:
            out["reasons"].append("游戏数据里没有建筑 %s" % building)
            self._build_cache[key] = copy.deepcopy(out)
            return out
        meta = self.state_meta.get(state_id)
        if meta:
            out["region"] = meta.region
        else:
            out["warnings"].append("州 %s 缺少静态地理（region/resources），资源/上限校验降级" % state_id)
        if not bi.buildable:
            out["reasons"].append("%s 不是玩家可建造建筑" % building)
        if bi.subsistence:
            out["reasons"].append("%s 是自给建筑，玩家不能主动建造" % building)
        missing_techs = [t for t in bi.techs if t not in self.techs]
        if missing_techs:
            out["reasons"].append("缺科技：" + ", ".join(missing_techs))
        missing_laws = [l for l in bi.laws if l not in self.laws]
        if missing_laws:
            out["reasons"].append("缺法律：" + ", ".join(missing_laws))
        if bi.allowed_regions:
            region = meta.region if meta else None
            if region and region not in bi.allowed_regions:
                out["reasons"].append("只能建在 %s" % ", ".join(bi.allowed_regions))
            elif not region:
                out["warnings"].append("州 %s 没有区域映射，无法校验地理限制" % state_id)
        if bi.requires_coastal and meta and not meta.coastal:
            out["reasons"].append("需要沿海州")
        if bi.requires_incorporated and meta and meta.incorporated is False:
            # 整合状态会随游戏进程变化，而实时快照没有该字段；静态地理只作提示。
            out["warnings"].append("静态地理显示该州未整合；实时整合状态未读，交由执行端裁决")
        if bi.requires_traits and meta:
            if not (set(bi.requires_traits) & set(meta.traits)):
                out["reasons"].append("需要州特质：" + ", ".join(bi.requires_traits))

        cur = self.existing_levels(state_id, building)
        queued = self.queued_levels(state_id, building)
        pending = self.pending_levels(state_id, building)
        out.update(existing_levels=cur, queued_levels=queued,
                   projected_levels=cur + queued + pending)
        if queued:
            out["reasons"].append("该州已有 %d 级在建造队列，先等队列消化" % queued)

        if bi.unique:
            if cur > 0:
                out["reasons"].append("唯一建筑已存在于本州")
            other = [b for b in self.buildings_by_type.get(building, []) if b.state_id != state_id]
            if other:
                out["reasons"].append("唯一建筑，已存在于州 %s" % ", ".join(str(b.state_id) for b in other[:4]))
            if queued:
                out["reasons"].append("唯一建筑已在队列")
        if not bi.expandable and cur > 0:
            out["reasons"].append("%s 不可扩建（已存在 %d 级）" % (building, cur))

        if bi.max_level is not None:
            if out["projected_levels"] + int(levels) > bi.max_level:
                out["reasons"].append("超过上限：已/将 %d，上限 %d"
                                      % (out["projected_levels"] + int(levels), bi.max_level))

        # 资源/耕地约束
        if bi.resource_bound or (meta and building in meta.resources):
            bi_res = meta.resources if meta else []
            cap = None
            if meta and building not in bi_res:
                out["reasons"].append("州 %s 没有该资源（%s）" % (state_id, building))
            if meta:
                if building in (meta.resource_caps or {}):
                    cap = int(meta.resource_caps[building])
                    remaining = cap - out["projected_levels"]
                elif building in meta.arable_resources():
                    cap = int(meta.arable_land or 0)
                    used = 0
                    for r in meta.arable_resources():
                        used += self.projected_levels(state_id, r)
                    remaining = cap - used
                else:
                    remaining = None
                out["remaining"] = remaining
                if remaining is not None and remaining < int(levels):
                    out["reasons"].append("剩余空间不足：剩 %d，想建 %d" % (remaining, int(levels)))
                if cap:
                    out["cap"] = cap
        out["cost_category"] = bi.required_construction
        out["cost_points"] = self.cost_of(building, int(levels))
        if out["reasons"]:
            out["code"] = "rejected"
        else:
            out["ok"] = True
            out["code"] = "ok"
        self._build_cache[key] = copy.deepcopy(out)
        return out

    # -- PM ------------------------------------------------------------
    def pm_effects(self, pm_name: str) -> dict:
        pm = self.gd.pms.get(pm_name)
        if pm is None:
            return {}
        out: dict[str, dict] = {}
        for scope, scales in (pm.modifiers.values or {}).items():
            for scale, kv in (scales or {}).items():
                for k, v in kv.items():
                    lk = k.lower()
                    if not any(x in lk for x in _NON_GDP_KEYS):
                        continue
                    if not (k.endswith("_add") or k.endswith("_mult") or "_add_" in k or "_mult_" in k):
                        # 只保留看起来是修正的键，避免把 goods_* 混进来
                        if not any(x in lk for x in _NON_GDP_KEYS):
                            continue
                    out.setdefault(k, {})["%s/%s" % (scope, scale)] = v
        return dict(sorted(out.items())[:24])

    def pm_employment(self, pm_name: str) -> dict[str, float]:
        pm = self.gd.pms.get(pm_name)
        if pm is None:
            return {}
        out: dict[str, float] = {}
        for scope, scales in (pm.modifiers.values or {}).items():
            for scale, kv in (scales or {}).items():
                for k, v in kv.items():
                    if not k.startswith("building_employment_") or not k.endswith("_add"):
                        continue
                    prof = k[len("building_employment_"):-4]
                    factor = 1.0 if scale in ("workforce_scaled", "level_scaled") else 0.0
                    if factor:
                        out[prof] = out.get(prof, 0.0) + float(v) * factor
        return out

    def _pm_group_of(self, building: str, pm: str) -> str | None:
        bd = self.gd.buildings.get(building)
        if bd is None:
            return None
        for gname in bd.production_method_groups:
            grp = self.gd.pm_groups.get(gname)
            if grp and pm in grp.production_methods:
                return gname
        return None

    def check_pm(self, state_id: int, building: str, pm: str) -> dict:
        out = {"ok": False, "code": "invalid", "state_id": state_id,
               "building": building, "pm": pm, "group": None,
               "status": None, "reasons": [], "warnings": [],
               "current": [], "effects": {}, "employment": {}}
        rows = self.by_state_type.get((state_id, building), [])
        if not rows:
            out["reasons"].append("州 %s 没有 %s" % (state_id, building))
            return out
        bd = self.gd.buildings.get(building)
        if bd is None:
            out["reasons"].append("游戏数据里没有建筑 %s" % building)
            return out
        group = self._pm_group_of(building, pm)
        if group is None:
            out["reasons"].append("%s 不属于 %s 的任何 PM 组" % (pm, building))
            return out
        out["group"] = group
        pm_obj = self.gd.pms.get(pm)
        if pm_obj is None:
            out["reasons"].append("游戏数据里没有 PM %s" % pm)
            return out
        active = sorted({p for b in rows for p in b.production_methods})
        grp_def = self.gd.pm_groups.get(group)
        group_pms = set(grp_def.production_methods) if grp_def else set()
        out["current"] = sorted(p for p in active if p in group_pms)
        if active and all(pm in b.production_methods for b in rows):
            out["code"] = "already"
            out["reasons"].append("该州所有 %s 已经在用 %s" % (building, pm))
            return out
        checker = self.pm_checker()
        st = checker.evaluate(pm_obj, group, set(active), self.owned_geo(),
                              prereq_pms=set(active))
        out["status"] = st.status
        out["reasons"].extend(st.reasons)
        if st.status == STATUS_LOCKED:
            out["code"] = "locked"
        elif st.status == STATUS_CONDITIONAL:
            out["code"] = "conditional"
        else:
            out["ok"] = True
            out["code"] = "ok"
        # 建筑本身没解锁也不能换
        if bd.unlocking_technologies:
            missing = [t for t in bd.unlocking_technologies if t not in self.techs]
            if len(missing) == len(bd.unlocking_technologies):
                out["ok"] = False
                out["code"] = "locked"
                out["reasons"].append("建筑本身缺科技：" + ", ".join(missing))
        effects = self.pm_effects(pm)
        out["effects"] = effects
        out["employment"] = self.pm_employment(pm)
        for key in effects:
            out["warnings"].append("非GDP修正 %s = %s" % (key, effects[key]))
        return out

    # -- 估算 ----------------------------------------------------------
    def _default_pms(self, building: str) -> list[str]:
        out = []
        for g in self.gd.building_groups_of(building):
            pm = self.gd.default_pm(g)
            if pm is not None:
                out.append(pm.name)
        return out

    def _jobs_of(self, building: str, levels: int) -> dict:
        jobs: dict[str, float] = {}
        for pm_name in self._default_pms(building):
            for prof, per in self.pm_employment(pm_name).items():
                jobs[prof] = round(jobs.get(prof, 0.0) + per * levels, 1)
        return jobs

    def gdp_per_level(self, building: str) -> float:
        est = self.estimate_build(0, building, 1)
        return float(est.get("gdp_delta", 0.0))

    def estimate_build(self, state_id: int, building: str, levels: int = 1) -> dict:
        """把假设建筑塞进快照副本，重算全国年 GDP；近似值，带假设说明。"""
        levels = max(1, int(levels))
        if self.bridge is not None:
            est = self.bridge.estimate(state_id, building, levels)
            if est:
                est.setdefault("jobs", self._jobs_of(building, levels))
                est.setdefault("check", self.check_build(state_id, building, levels))
                return est
        pms = self._default_pms(building)
        if not pms:
            return {"building": building, "state_id": state_id, "levels": levels,
                    "gdp_delta": 0.0, "jobs": {}, "note": "没有默认 PM，无法估算"}
        base = self.gdp()
        work = copy.deepcopy(self.snap)
        work.buildings.append(BuildingRow(
            type=building, state_id=state_id, levels=levels,
            staffing=float(levels), throughput=1.0,
            production_methods=list(pms), active=True))
        after = GDPCalculator(self.gd, work).compute(
            price=base.price_basis if base.price_basis in ("market", "base", "local") else "auto",
            scale="staffing", buildings="producers")
        jobs: dict[str, float] = {}
        for pm in pms:
            for prof, per in self.pm_employment(pm).items():
                jobs[prof] = jobs.get(prof, 0.0) + per * levels
        capacity = self.construction_capacity()
        cap_week = float(capacity.get("capacity_per_week") or 0.0)
        cost = self.cost_of(building, levels)
        return {
            "building": building, "state_id": state_id, "levels": levels,
            "gdp_before": base.annual, "gdp_after": after.annual,
            "gdp_delta": after.annual - base.annual,
            "gdp_delta_pct": (100.0 * (after.annual - base.annual) / base.annual
                              if base.annual else 0.0),
            "jobs": {k: round(v, 1) for k, v in jobs.items()},
            "construction": {
                "cost_points": round(cost, 1),
                "capacity_per_week": cap_week,
                "weeks": round(cost / cap_week, 1) if cap_week else None,
            },
            "assumptions": {
                "pms": pms, "throughput": 1.0, "staffing": levels,
                "price_basis": after.price_basis,
                "note": "假设满员、吞吐 1.0、用默认 PM；实际受州价格/就业/吞吐影响。",
            },
        }

    # -- 候选 ----------------------------------------------------------
    def focus_states(self, mode: str = "nationwide", max_states: int = 0,
                     priority_states: list[int] | None = None) -> list[int]:
        """策略层选择建设区域：全国/核心/沿海/农业/资源/京畿/均衡。"""
        mode = (mode or "nationwide").lower()
        ids = sorted(self.states)
        if mode == "custom":
            wanted = [int(x) for x in (priority_states or [])]
            out = [sid for sid in ids if sid in wanted] or ids
        elif mode == "nationwide" or mode == "balanced":
            out = ids
        elif mode == "coastal":
            out = [sid for sid in ids if getattr(self.state_meta.get(sid), "coastal", False)]
        elif mode == "agriculture":
            out = sorted(ids, key=lambda sid: -float(getattr(self.state_meta.get(sid), "arable_land", 0) or 0))
            out = out[:max(3, len(out) // 2)]
        elif mode == "resources":
            def _res(sid):
                m = self.state_meta.get(sid)
                caps = getattr(m, "resource_caps", {}) or {}
                return sum(float(v or 0) for v in caps.values()) + len(getattr(m, "resources", []) or [])
            out = sorted(ids, key=lambda sid: -_res(sid))[:max(3, len(ids) // 2)]
        elif mode == "capital":
            # 快照没有“首都州”字段，用人口最高的核心州作为京畿近似
            out = sorted(ids, key=lambda sid: -float(getattr(self.states.get(sid), "population", 0) or 0))[:3]
        elif mode == "core":
            out = sorted(ids, key=lambda sid: -float(getattr(self.states.get(sid), "population", 0) or 0))[:max(6, len(ids) // 3)]
        else:
            out = ids
        if max_states and max_states > 0:
            out = out[:max_states]
        return [int(x) for x in out]

    def build_candidates(self, state_id: int | None = None,
                         building: str | None = None,
                         top: int = 20,
                         state_ids: list[int] | None = None) -> list[dict]:
        if self.bridge is not None:
            rows = self.bridge.candidates(top=top, state_id=state_id,
                                          building=building, state_ids=state_ids)
            if rows:
                return rows
        if state_id is not None:
            states = [state_id]
        elif state_ids is not None:
            states = [int(x) for x in state_ids if int(x) in self.states]
        else:
            states = sorted(self.states)
        types = [building] if building else sorted(self.buildings_kb)
        cap_week = float(self.construction_capacity().get("capacity_per_week") or 0.0)
        out: list[dict] = []
        for btype in types:
            bi = self.buildings_kb.get(btype)
            if bi is None or not bi.buildable:
                continue
            per_level = self.gdp_per_level(btype)
            for sid in states:
                chk = self.check_build(sid, btype, 1)
                if not chk.get("ok"):
                    continue
                remaining = chk.get("remaining")
                cap_levels = max(1, min(5, int(remaining) if remaining is not None else 4))
                score = per_level * cap_levels
                cost_points = float(chk.get("cost_points") or 0.0)
                out.append({
                    "state_id": sid, "region": chk.get("region"),
                    "building": btype, "remaining": remaining,
                    "suggest_levels": cap_levels,
                    "gdp_per_level": round(per_level, 1),
                    "score_gdp": round(score, 1),
                    "cost_category": chk.get("cost_category"),
                    "cost_points_1": cost_points,
                    "weeks_1": (round(cost_points / cap_week, 1) if cap_week else None),
                    "warnings": chk.get("warnings", []),
                })
        out.sort(key=lambda x: -x["score_gdp"])
        return out[:max(1, int(top))]

    def pm_candidates(self, building: str, state_id: int | None = None,
                      top: int = 20, include_conditional: bool = False) -> list[dict]:
        rows = rank_pm_upgrades(self.gd, self.snap, building_type=building,
                                include_conditional=include_conditional)
        out = []
        for r in rows[:max(1, int(top))]:
            effects = self.pm_effects(r.candidate)
            check = self.check_pm(state_id, building, r.candidate) if state_id is not None else None
            out.append({
                "building": building, "group": r.group,
                "current": r.current, "candidate": r.candidate,
                "status": r.status, "delta": round(r.delta, 1),
                "delta_pct": round(r.delta_pct, 3),
                "state_id": state_id,
                "check": (None if check is None else {
                    "ok": check["ok"], "code": check["code"],
                    "reasons": check["reasons"], "warnings": check["warnings"][:6],
                }),
                "non_gdp_effects": effects,
            })
        return out

    # -- 摘要 ----------------------------------------------------------
    def budget_digest(self) -> dict:
        c = self.snap.country
        b = c.budget if c else None
        if b is None:
            return {}
        inc = list(b.income or [])
        exp = list(b.expenses or [])
        top_inc = sorted(
            [{"slot": i, "label": INCOME_LABELS.get(i, "槽%d" % i), "amount": round(v, 1)}
             for i, v in enumerate(inc) if v], key=lambda x: -x["amount"])[:6]
        top_exp = sorted(
            [{"slot": i, "label": EXPENSE_LABELS.get(i, "槽%d" % i), "amount": round(v, 1)}
             for i, v in enumerate(exp) if v], key=lambda x: -x["amount"])[:8]
        return {
            "income_total": round(b.income_total or 0, 1),
            "expense_total": round(b.expense_total or 0, 1),
            "balance": round(b.balance or 0, 1),
            "tax_level": b.tax_level,
            "weekly_investment_pool": b.weekly_investment_pool,
            "top_income": top_inc, "top_expenses": top_exp,
            "treasury": round(c.treasury or 0, 1),
            "credit_limit": round(c.credit_limit or 0, 1),
            "loan_principal": round(c.loan_principal or 0, 1),
        }

    def market_digest(self, top_n: int = 8) -> dict:
        m = self.snap.market
        if m is None:
            return {}
        base = {g.name: g.cost for g in self.gd.goods}
        rows = []
        for name, price in (m.prices or {}).items():
            b = base.get(name) or 0.0
            ratio = (price / b) if b else 0.0
            rows.append({"good": name, "price": round(price, 2),
                         "base": round(b, 2), "ratio": round(ratio, 3),
                         "supply": round((m.supply or {}).get(name, 0.0), 1),
                         "demand": round((m.demand or {}).get(name, 0.0), 1)})
        expensive = sorted(rows, key=lambda x: -x["ratio"])[:top_n]
        cheap = sorted(rows, key=lambda x: x["ratio"])[:top_n]
        return {"n_goods": len(rows), "most_expensive": expensive, "cheapest": cheap,
                "priced_goods": len(rows)}

    def politics_digest(self) -> dict:
        snap = self.snap
        return {
            "laws": [{"name": l.name, "group": l.group} for l in snap.laws],
            "interest_groups": [
                {"name": x.name, "clout": round(x.clout, 3),
                 "approval": x.approval, "in_government": x.in_government}
                for x in sorted(snap.interest_groups, key=lambda z: -z.clout)[:10]
            ],
            "movements": [
                {"type": x.type, "ideology": x.ideology,
                 "radicalism": round(x.radicalism, 4), "pops": x.pops}
                for x in sorted(snap.movements, key=lambda z: -z.radicalism)[:8]
            ],
            "institutions": [{"type": x.type, "investment": x.investment}
                             for x in snap.institutions],
            "decrees": [{"name": x.name, "state_id": x.state_id} for x in snap.decrees],
            "companies": [{"type": x.type, "region": x.region} for x in snap.companies],
        }

    def overview(self) -> dict:
        snap = self.snap
        c = snap.country
        gdp = self.gdp()
        bld = snap.buildings
        under = [b for b in bld if b.levels and b.staffing < 0.75 * b.levels]
        unprof = [b for b in bld if b.profit is not None and b.profit < 0]
        top_btypes = sorted(self.levels_by_type.items(), key=lambda kv: -kv[1])[:10]
        state_rows = []
        for s in snap.states:
            meta = self.state_meta.get(s.id)
            state_rows.append({
                "id": s.id, "region": meta.region if meta else None,
                "population": s.population, "arable": s.arable_land,
                "infra": round(s.infrastructure or 0, 1),
                "infra_used": round(s.infrastructure_used or 0, 1),
                "radicals": s.radicals, "unemployed": s.unemployed,
            })
        top_pop = sorted(state_rows, key=lambda x: -(x["population"] or 0))[:8]
        pressure = sorted(
            [x for x in state_rows if x["infra_used"] > x["infra"]],
            key=lambda x: -(x["infra_used"] - x["infra"]))[:6]
        radicals = sorted([x for x in state_rows if x["radicals"]],
                          key=lambda x: -x["radicals"])[:6]
        q = self.queued_entries()
        return {
            "meta": {
                "source": snap.game.source,
                "captured_at": snap.game.captured_at,
                "age_s": round(age_seconds(snap.game.captured_at) or 0, 1),
                "game_version": snap.game.game_version,
                "country_id": self.country_id,
                "states": len(snap.states), "buildings": len(bld),
                "levels": sum(b.levels for b in bld),
                "warnings": list(snap.diagnostics.warnings)[:6],
                "sections_error": [k for k, v in (snap.diagnostics.sections or {}).items() if v == "error"],
                "state_meta_ok": bool(self.state_meta),
            },
            "country": {
                "id": c.id if c else None, "tag": c.tag if c else None,
                "population": c.population if c else None,
                "treasury": round((c.treasury or 0), 1) if c else None,
                "credit_limit": round((c.credit_limit or 0), 1) if c else None,
                "base_wage": c.base_wage if c else None,
                "gdp_save": c.gdp if c else None,
            },
            "budget": self.budget_digest(),
            "construction": self.construction_capacity(),
            "gdp": {
                "annual": round(gdp.annual, 1),
                "per_capita": round(gdp.per_capita, 4) if gdp.per_capita else None,
                "price_basis": gdp.price_basis, "scale": gdp.scale,
                "building_filter": gdp.building_filter,
                "top_goods": [{"good": g.good, "net_value": round(g.net_value, 1)}
                              for g in gdp.top_goods(8)],
                "top_building_types": [
                    {"type": b.type, "levels": b.levels, "net": round(b.net, 1)}
                    for b in gdp.top_buildings(8)],
            },
            "market": self.market_digest(),
            "states": {
                "count": len(snap.states),
                "top_population": top_pop,
                "infrastructure_overloaded": pressure,
                "radicals_top": radicals,
                "top_arable": sorted(state_rows, key=lambda x: -(x["arable"] or 0))[:6],
            },
            "buildings": {
                "count": len(bld),
                "top_types": [{"type": t, "levels": n} for t, n in top_btypes],
                "understaffed": [
                    {"type": b.type, "state_id": b.state_id, "levels": b.levels,
                     "staffing": round(b.staffing, 2)}
                    for b in sorted(under, key=lambda z: z.staffing / max(1, z.levels))[:8]
                ],
                "unprofitable": [
                    {"type": b.type, "state_id": b.state_id, "profit": round(b.profit or 0, 1)}
                    for b in sorted(unprof, key=lambda z: z.profit or 0)[:8]
                ],
            },
            "politics": self.politics_digest(),
            "techs": {"count": len(snap.techs), "recent": sorted(snap.techs)[:30]},
            "queue": {
                "count": len(q),
                "entries": q[:20],
                "note": "null/空 = 执行端未连接或队列为空",
            },
            "strategy": (self.strategy.summary() if self.strategy is not None else None),
            "pending": [
                {"id": getattr(c, "id", ""), "kind": getattr(c, "kind", ""),
                 "state_id": getattr(c, "state_id", None),
                 "building": getattr(c, "building", ""),
                 "levels": getattr(c, "levels", None),
                 "pm": getattr(c, "pm", None)}
                for c in self.pending
            ],
        }
