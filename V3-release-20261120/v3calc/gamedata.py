# -*- coding: utf-8 -*-
"""Victoria 3 游戏数据模型：商品、生产方式、建筑、科技、法律。

只读取游戏安装目录里的脚本文件（不碰存档/内存），负责回答：
  * 54 种商品的顺序、基础价、类别；
  * 每个生产方式（PM）的产出/投入与解锁条件；
  * 每个建筑有哪些 PM 组（``pmg_*``）；
  * 科技的前置、时代、修正；
  * 法律的组、解锁科技、互斥法律、修正、税率档位；
  * 州区域 → 地理区域（用于 ``unlocking_geographic_regions``）。

目录可以用 ``GameData.discover()`` 自动找，也可以显式传 ``game_dir``
（指向 ``...\\Victoria 3\\game``）。支持叠加多个 mod 目录，后加载的同名
定义覆盖先加载的（对应游戏里 mod 覆盖 base 的常见情况）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable

from . import pdx
from .pdx import Obj

# ---------------------------------------------------------------------------
# 安装路径
# ---------------------------------------------------------------------------
_CANDIDATES = [
    os.environ.get("V3_GAME", ""),
    r"E:\SteamLibrary\steamapps\common\Victoria 3\game",
    r"D:\Steam\steamapps\common\Victoria 3\game",
    r"C:\Program Files (x86)\Steam\steamapps\common\Victoria 3\game",
]


def discover_game_dir() -> str | None:
    for p in _CANDIDATES:
        if p and os.path.isdir(os.path.join(p, "common", "goods")):
            return p
    # 从 Steam 库目录猜
    for root in ("C:", "D:", "E:", "F:"):
        p = os.path.join(root, "SteamLibrary", "steamapps", "common",
                         "Victoria 3", "game")
        if os.path.isdir(os.path.join(p, "common", "goods")):
            return p
    return None


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Good:
    name: str
    index: int
    cost: float
    category: str = ""
    texture: str = ""


@dataclass
class ModifierSet:
    """一个 PM/法律/科技的作用域修正：scope -> scale -> key -> float。"""

    values: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)

    def add(self, scope: str, scale: str, key: str, value: float) -> None:
        self.values.setdefault(scope, {}).setdefault(scale, {})[key] = value

    def get(self, key: str) -> list[tuple[str, str, float]]:
        """返回 ``[(scope, scale, value)]``，用于溯源。"""
        out = []
        for scope, scales in self.values.items():
            for scale, kv in scales.items():
                if key in kv:
                    out.append((scope, scale, kv[key]))
        return out

    def keys(self) -> Iterable[str]:
        for scales in self.values.values():
            for kv in scales.values():
                yield from kv

    def numbers(self, key: str) -> float:
        return sum(v for _s, _c, v in self.get(key))


@dataclass
class PM:
    name: str
    texture: str = ""
    is_default: bool = False
    is_hidden_when_unavailable: bool = False
    unlocking_technologies: tuple[str, ...] = ()
    unlocking_laws: tuple[str, ...] = ()
    unlocking_production_methods: tuple[str, ...] = ()
    unlocking_principles: tuple[str, ...] = ()
    unlocking_company_categories: tuple[str, ...] = ()
    unlocking_identity: tuple[str, ...] = ()
    unlocking_geographic_regions: tuple[str, ...] = ()
    unlocking_religions: tuple[str, ...] = ()
    disallowing_laws: tuple[str, ...] = ()
    disallowing_religions: tuple[str, ...] = ()
    required_input_goods: tuple[str, ...] = ()
    modifiers: ModifierSet = field(default_factory=ModifierSet)

    # 便捷：产出/投入（按作用域+缩放级别）
    def goods(self, side: str) -> dict[tuple[str, str, str], float]:
        """side='output'/'input' -> {(scope, scale, good): 每单位值}。"""
        out: dict[tuple[str, str, str], float] = {}
        prefix = "goods_output_" if side == "output" else "goods_input_"
        for scope, scales in self.modifiers.values.items():
            for scale, kv in scales.items():
                for k, v in kv.items():
                    if k.startswith(prefix) and k.endswith("_add"):
                        out[(scope, scale, k[len(prefix):-4])] = v
        return out

    def has_goods_output(self) -> bool:
        return bool(self.goods("output"))


@dataclass
class PMGroup:
    name: str
    production_methods: tuple[str, ...] = ()
    is_hidden_when_unavailable: bool = False
    ai_selection: str = "most_profitable"


@dataclass
class BuildingDef:
    name: str
    building_group: str = ""
    production_method_groups: tuple[str, ...] = ()
    unlocking_technologies: tuple[str, ...] = ()
    required_construction: str = ""
    ownership_type: str = ""
    city_type: str = ""
    buildable: bool = True
    unique: bool = False
    has_max_level: bool = False
    max_level: int | None = None


@dataclass
class Tech:
    name: str
    era: str = ""
    category: str = ""
    prerequisites: tuple[str, ...] = ()
    modifiers: ModifierSet = field(default_factory=ModifierSet)
    can_research: bool = True


@dataclass
class Law:
    name: str
    group: str = ""
    parent: str = ""
    progressiveness: float = 0.0
    unlocking_technologies: tuple[str, ...] = ()
    unlocking_laws: tuple[str, ...] = ()
    requires_law_or: tuple[str, ...] = ()
    disallowing_laws: tuple[str, ...] = ()
    institution: str = ""
    modifiers: ModifierSet = field(default_factory=ModifierSet)
    tax_levels: dict[str, dict[str, float]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
_SCALES = ("workforce_scaled", "level_scaled", "throughput_scaled", "unscaled")
_SCOPES = ("country_modifiers", "state_modifiers", "building_modifiers")


def _modifiers(block: Obj) -> ModifierSet:
    ms = ModifierSet()
    for scope in _SCOPES:
        sc = block.block(scope)
        if sc is None:
            continue
        for scale in _SCALES:
            sub = sc.block(scale)
            if sub is None:
                continue
            for k, v in sub.items():
                f = pdx.as_float(v)
                if f is not None:
                    ms.add(scope, scale, k, f)
    return ms


def _atoms(block: Obj, key: str) -> tuple[str, ...]:
    return tuple(block.atoms(key))


class GameData:
    def __init__(self, game_dir: str, extra_dirs: Iterable[str] = ()):
        self.game_dir = os.path.abspath(game_dir)
        self.dirs = [self.game_dir] + [os.path.abspath(d) for d in extra_dirs]
        self.goods: list[Good] = []
        self.goods_by_name: dict[str, Good] = {}
        self.pms: dict[str, PM] = {}
        self.pm_groups: dict[str, PMGroup] = {}
        self.buildings: dict[str, BuildingDef] = {}
        self.building_groups: dict[str, str] = {}
        self.techs: dict[str, Tech] = {}
        self.laws: dict[str, Law] = {}
        self.eras: list[str] = []
        self.geo_state_regions: dict[str, set[str]] = {}
        self.state_region_geo: dict[str, set[str]] = {}
        self.decrees: dict[str, Obj] = {}
        self.static_modifiers: dict[str, Obj] = {}
        self._load()

    # -- 目录 ---------------------------------------------------------
    def _path(self, *parts: str) -> str:
        return os.path.join(self.game_dir, "common", *parts)

    def _parse_overlay(self, subdir: str, *, single_file: str | None = None) -> Obj:
        out = Obj()
        for base in self.dirs:
            p = os.path.join(base, "common", subdir)
            if single_file:
                fp = os.path.join(p, single_file)
                if os.path.isfile(fp):
                    part = pdx.parse_file(fp)
                else:
                    continue
            else:
                part = pdx.parse_dir(p)
            for k, v in part.items():
                out.add(k, v)
            for v in part.values:
                out.add_value(v)
        return out

    # -- 载入 ---------------------------------------------------------
    def _load(self) -> None:
        self._load_goods()
        self._load_pms()
        self._load_pm_groups()
        self._load_buildings()
        self._load_techs()
        self._load_laws()
        self._load_eras()
        self._load_geo()
        self._load_misc()

    def _load_goods(self) -> None:
        raw = self._parse_overlay("goods")
        for i, (name, v) in enumerate(raw.items()):
            if not isinstance(v, Obj):
                continue
            g = Good(name=name, index=i, cost=pdx.as_float(v.get("cost")) or 0.0,
                     category=v.get("category", "") or "",
                     texture=v.get("texture", "") or "")
            self.goods.append(g)
            self.goods_by_name[name] = g

    def _load_pms(self) -> None:
        raw = self._parse_overlay("production_methods")
        for name, v in raw.items():
            if not isinstance(v, Obj):
                continue
            pm = PM(
                name=name,
                texture=v.get("texture", "") or "",
                is_default=v.flag("is_default"),
                is_hidden_when_unavailable=v.flag("is_hidden_when_unavailable"),
                unlocking_technologies=_atoms(v, "unlocking_technologies"),
                unlocking_laws=_atoms(v, "unlocking_laws"),
                unlocking_production_methods=_atoms(v, "unlocking_production_methods"),
                unlocking_principles=_atoms(v, "unlocking_principles"),
                unlocking_company_categories=_atoms(v, "unlocking_company_categories"),
                unlocking_identity=_atoms(v, "unlocking_identity"),
                unlocking_geographic_regions=_atoms(v, "unlocking_geographic_regions"),
                unlocking_religions=_atoms(v, "unlocking_religions"),
                disallowing_laws=_atoms(v, "disallowing_laws"),
                disallowing_religions=_atoms(v, "disallowing_religions"),
                required_input_goods=_atoms(v, "required_input_goods"),
                modifiers=_modifiers(v),
            )
            self.pms[name] = pm

    def _load_pm_groups(self) -> None:
        raw = self._parse_overlay("production_method_groups")
        for name, v in raw.items():
            if not isinstance(v, Obj):
                continue
            self.pm_groups[name] = PMGroup(
                name=name,
                production_methods=_atoms(v, "production_methods"),
                is_hidden_when_unavailable=v.flag("is_hidden_when_unavailable"),
                ai_selection=v.get("ai_selection", "most_profitable") or "most_profitable",
            )

    def _load_buildings(self) -> None:
        raw = self._parse_overlay("buildings")
        for name, v in raw.items():
            if not isinstance(v, Obj):
                continue
            self.buildings[name] = BuildingDef(
                name=name,
                building_group=v.get("building_group", "") or "",
                production_method_groups=_atoms(v, "production_method_groups"),
                unlocking_technologies=_atoms(v, "unlocking_technologies"),
                required_construction=v.get("required_construction", "") or "",
                ownership_type=v.get("ownership_type", "") or "",
                city_type=v.get("city_type", "") or "",
                buildable=v.get("buildable", "yes") != "no",
                unique=v.flag("unique"),
                has_max_level=v.flag("has_max_level"),
                max_level=int(v.num("max_level")) if v.get("max_level") else None,
            )
        raw = self._parse_overlay("building_groups")
        for name, v in raw.items():
            if isinstance(v, Obj):
                self.building_groups[name] = v.get("category", "") or ""

    def _load_techs(self) -> None:
        raw = self._parse_overlay(os.path.join("technology", "technologies"))
        for name, v in raw.items():
            if not isinstance(v, Obj):
                continue
            self.techs[name] = Tech(
                name=name,
                era=v.get("era", "") or "",
                category=v.get("category", "") or "",
                prerequisites=_atoms(v, "unlocking_technologies"),
                modifiers=_modifiers(v),
                can_research=v.get("can_research", "yes") != "no",
            )

    def _load_laws(self) -> None:
        raw = self._parse_overlay("laws")
        for name, v in raw.items():
            if not isinstance(v, Obj):
                continue
            tax = {}
            for key in ("tax_modifier_very_low", "tax_modifier_low", "tax_modifier_medium",
                        "tax_modifier_high", "tax_modifier_very_high"):
                sub = v.block(key)
                if sub is None:
                    continue
                lv = key[len("tax_modifier_"):]
                rates = {}
                for k, val in sub.items():
                    f = pdx.as_float(val)
                    if f is not None and k.startswith("tax_"):
                        rates[k] = f
                if rates:
                    tax[lv] = rates
            self.laws[name] = Law(
                name=name,
                group=v.get("group", "") or "",
                parent=v.get("parent", "") or "",
                progressiveness=v.num("progressiveness"),
                unlocking_technologies=_atoms(v, "unlocking_technologies"),
                unlocking_laws=_atoms(v, "unlocking_laws"),
                requires_law_or=_atoms(v, "requires_law_or"),
                disallowing_laws=_atoms(v, "disallowing_laws"),
                institution=v.get("institution", "") or "",
                modifiers=_modifiers(v),
                tax_levels=tax,
            )

    def _load_eras(self) -> None:
        raw = self._parse_overlay(os.path.join("technology", "eras"))
        self.eras = [k for k, v in raw.items() if isinstance(v, Obj)]

    def _load_geo(self) -> None:
        raw = self._parse_overlay("geographic_regions")
        for geo, v in raw.items():
            if not isinstance(v, Obj):
                continue
            srs = set()
            for token in v.atoms("state_regions"):
                srs.add(token)
            self.geo_state_regions[geo] = srs
            for sr in srs:
                self.state_region_geo.setdefault(sr, set()).add(geo)

    def _load_misc(self) -> None:
        self.decrees = dict(self._parse_overlay("decrees"))
        self.static_modifiers = dict(self._parse_overlay("static_modifiers"))

    # -- 查询 ---------------------------------------------------------
    def pm(self, name: str) -> PM | None:
        return self.pms.get(name)

    def pms_of_group(self, group: str) -> list[PM]:
        g = self.pm_groups.get(group)
        if g is None:
            return []
        return [self.pms[n] for n in g.production_methods if n in self.pms]

    def building_groups_of(self, building: str) -> list[str]:
        b = self.buildings.get(building)
        return list(b.production_method_groups) if b else []

    def default_pm(self, group: str) -> PM | None:
        """该 PM 组的默认 PM：优先 is_default，否则列表第一个。"""
        pms = self.pms_of_group(group)
        for pm in pms:
            if pm.is_default:
                return pm
        return pms[0] if pms else None

    def goods_index(self) -> dict[str, int]:
        return {g.name: g.index for g in self.goods}

    def summary(self) -> dict:
        return {
            "game_dir": self.game_dir,
            "goods": len(self.goods),
            "production_methods": len(self.pms),
            "pm_groups": len(self.pm_groups),
            "buildings": len(self.buildings),
            "technologies": len(self.techs),
            "laws": len(self.laws),
            "eras": len(self.eras),
        }
