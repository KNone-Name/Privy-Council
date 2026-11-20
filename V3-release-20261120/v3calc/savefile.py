# -*- coding: utf-8 -*-
"""存档读取层（文本存档）：把 ``save.txt`` 读成与实时读取相同的 Snapshot。

Victoria 3 的文本存档是"缩进 + 花括号"格式，建筑/法律/科技/机构/公司等
都是 ``database={ id={ ... } }``。这里按缩进解析，不依赖 token 表。

支持：
  * 玩家国家自动识别（``previous_played -> idtype``，可用 ``--country`` 覆盖）
  * 国家：tag / 市场 / 宗教 / 国库 / 预算 16+22 槽 / 基础工资 / 税收等级 /
    关税 / 人口 / **GDP 趋势值（年化，界面同款，可用于校验计算层）**
  * 州：归属 / 首府 / 耕地 / 基础设施 / 人口 / （本地价存档不存，留空）
  * 建筑：类型 / 州 / 等级 / 就业 / 吞吐 / 当前 PM / 利润 / 工资
  * 法律 / 科技 / 法令 / 机构 / 公司 / 利益集团 / 政治运动
  * 市场：存档只保留世界市场价格趋势，按最后一个采样值给出（仅作参考价）

不支持：二进制 ``.v3``（zip 里的 gamestate 是 token 压缩格式，需要 token 表）。
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime

from .gamedata import GameData, discover_game_dir
from .model import (
    BuildingRow, Budget, CompanyRow, Country, DecreeRow, Diagnostics, GameInfo,
    InstitutionRow, InterestGroupRow, LawRow, MarketRow, MovementRow, Snapshot,
    StateRow, TaxRow,
)

TAX_LEVELS = ["very_low", "low", "medium", "high", "very_high"]


def _ind(l: str) -> int:
    return len(l) - len(l.lstrip("\t"))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class _Lines:
    """行 + 缩进的轻量封装。"""

    def __init__(self, lines: list[str]):
        self.lines = lines

    def __len__(self) -> int:
        return len(self.lines)

    def ind(self, i: int) -> int:
        return _ind(self.lines[i])

    def text(self, i: int) -> str:
        return self.lines[i].strip()

    def close(self, i: int) -> int:
        """返回与第 i 行 ``{`` 匹配的 ``}`` 所在行。"""
        d = self.ind(i)
        if self.lines[i].count("{") == self.lines[i].count("}"):
            return i
        for j in range(i + 1, len(self.lines)):
            if self.ind(j) == d and self.text(j) == "}":
                return j
        return len(self.lines) - 1

    def block(self, name: str, start: int = 0, end: int | None = None,
              depth: int | None = None):
        """找 ``name={``，返回 (i, close)。``depth`` 限定缩进层级，避免匹配嵌套同名块。"""
        end = len(self.lines) if end is None else end
        pat = re.compile(r"^\t*" + re.escape(name) + r"=\{\s*$")
        for i in range(start, end):
            if depth is not None and self.ind(i) != depth:
                continue
            if pat.match(self.lines[i]):
                return i, self.close(i)
        return None

    def records(self, start: int, end: int, depth: int):
        """遍历 ``N={ ... }`` 记录，返回 (id/key, i, close)。"""
        i = start + 1
        while i < end:
            t = self.text(i)
            m = re.match(r"^(\d+)=\{\s*$", t)
            if m and self.ind(i) == depth:
                c = self.close(i)
                yield int(m.group(1)), i, c
                i = c + 1
            else:
                i += 1

    def scalar(self, start: int, end: int, key: str, depth: int | None = None):
        for i in range(start + 1, end):
            t = self.text(i)
            if depth is not None and self.ind(i) != depth:
                continue
            if t.startswith(key + "=") and not t.endswith("{"):
                return t.split("=", 1)[1].strip().strip('"')
        return None

    def atom_list(self, start: int, end: int, key: str, depth: int) -> list[str]:
        """``key={ "a" "b" }`` 或跨行列表。"""
        for i in range(start + 1, end):
            t = self.text(i)
            if self.ind(i) != depth or not t.startswith(key + "="):
                continue
            out = re.findall(r'"([^"]+)"', t)
            if t.endswith("}") and "{" in t:
                return out
            c = self.close(i)
            for j in range(i + 1, c):
                out += re.findall(r'"([^"]+)"', self.lines[j])
            return out
        return []

    def number_list(self, start: int, end: int, key: str, depth: int) -> list[float]:
        for i in range(start + 1, end):
            t = self.text(i)
            if self.ind(i) != depth or not t.startswith(key + "="):
                continue
            out = [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?", t.split("=", 1)[1])]
            if t.endswith("}") and "{" in t:
                return out
            c = self.close(i)
            for j in range(i + 1, c):
                out += [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?", self.lines[j])]
            return out
        return []

    def trend_last(self, start: int, end: int, key: str, depth: int) -> float | None:
        """``key={ channels={ 0={ ... values={ v... } } } }`` 取最后一个值。"""
        b = self.block(key, start, end)
        if b is None:
            return None
        i, c = b
        vals: list[float] = []
        for j in range(i + 1, c):
            if "values={" in self.lines[j]:
                vals += [float(x) for x in
                         re.findall(r"[-+]?\d+(?:\.\d+)?", self.lines[j].split("values={", 1)[1])]
                if not self.lines[j].strip().endswith("}"):
                    for k in range(j + 1, c):
                        t = self.lines[k].strip()
                        if t == "}":
                            break
                        if "=" not in t:
                            vals += [float(x) for x in
                                     re.findall(r"[-+]?\d+(?:\.\d+)?", t)]
        return vals[-1] if vals else None


class SaveReader:
    def __init__(self, path: str, country_id: int | None = None,
                 game_dir: str | None = None, game_data=None,
                 include_pops: bool = False):
        self.path = os.path.abspath(path)
        self.country_id = country_id
        self.game_dir = game_dir or discover_game_dir()
        self._provided_gd = game_data
        self.include_pops = include_pops

    # ------------------------------------------------------------------
    def read(self) -> Snapshot:
        snap = Snapshot()
        snap.game = GameInfo(source="save", captured_at=_now(), save_path=self.path)
        diag = snap.diagnostics
        t0 = time.perf_counter()
        if not os.path.isfile(self.path):
            raise FileNotFoundError(self.path)
        with open(self.path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        if not raw.startswith("SAV"):
            raise ValueError("不是 Victoria 3 文本存档（应以 SAV 开头）：%s" % self.path)
        lines = _Lines(raw.split("\n"))
        diag.timings["load"] = round(time.perf_counter() - t0, 3)

        gd = self._provided_gd
        if gd is None and self.game_dir:
            try:
                gd = GameData(self.game_dir)
                snap.game.game_dir = gd.game_dir
            except Exception as exc:               # noqa: BLE001
                diag.warnings.append("game_data: %s" % exc)
        self.gd = gd
        if gd is not None:
            snap.game.game_dir = gd.game_dir

        self._meta(lines, snap)
        cid = self.country_id
        if cid is None:
            cid = self._player_id(lines)
        if cid is None:
            raise RuntimeError("存档里无法判断玩家国家，请用 --country 指定")
        self._country(lines, snap, cid)
        self._states(lines, snap, cid)
        if self.include_pops:
            self._pops(lines, snap, cid)
        self._buildings(lines, snap, cid)
        self._market(lines, snap)
        self._laws(lines, snap, cid)
        self._finalize_tax(snap)
        self._techs(lines, snap, cid)
        self._institutions(lines, snap, cid)
        self._companies(lines, snap, cid)
        self._igs(lines, snap, cid)
        self._movements(lines, snap, cid)
        self._decrees(lines, snap, cid)
        return snap

    # ------------------------------------------------------------------
    def _meta(self, L: _Lines, snap: Snapshot) -> None:
        b = L.block("meta_data", depth=0)
        if b is None:
            return
        i, c = b
        snap.game.game_version = L.scalar(i, c, "version", 1)
        snap.game.save_date = L.scalar(i, c, "game_date", 1)
        snap.game.save_name = L.scalar(i, c, "name", 1)

    def _player_id(self, L: _Lines) -> int | None:
        b = L.block("previous_played", depth=0)
        if b is None:
            return None
        i, c = b
        for j in range(i + 1, c):
            if L.text(j).startswith("idtype="):
                try:
                    return int(L.text(j).split("=", 1)[1])
                except ValueError:
                    return None
        return None

    # ------------------------------------------------------------------
    def _country(self, L: _Lines, snap: Snapshot, cid: int) -> None:
        bm = L.block("country_manager", depth=0)
        if bm is None:
            raise RuntimeError("存档缺 country_manager")
        i, c = bm
        db = L.block("database", i, c, depth=1)
        if db is None:
            raise RuntimeError("country_manager 缺 database")
        di, dc = db
        rec = None
        for key, ri, rc in L.records(di, dc, 2):
            if key == cid:
                rec = (ri, rc)
                break
        if rec is None:
            raise RuntimeError("存档里找不到国家 %d" % cid)
        ri, rc = rec
        co = Country(id=cid)
        co.tag = L.scalar(ri, rc, "definition", 3)
        co.religion = L.scalar(ri, rc, "religion", 3)
        m = L.scalar(ri, rc, "market", 3)
        co.market_id = int(m) if m and m.isdigit() else None
        # 预算
        bud = Budget()
        bb = L.block("budget", ri, rc, depth=3)
        if bb is not None:
            bi, bc = bb
            bud.income = [float(x) for x in L.number_list(bi, bc, "weekly_income", 4)]
            bud.expenses = [float(x) for x in L.number_list(bi, bc, "weekly_expenses", 4)]
            # 补齐到固定槽数
            bud.income = (bud.income + [0.0] * 16)[:16]
            bud.expenses = (bud.expenses + [0.0] * 22)[:22]
            bud.income_total = sum(bud.income)
            bud.expense_total = sum(bud.expenses)
            bud.balance = bud.income_total - bud.expense_total
            money = L.scalar(bi, bc, "money", 4)
            co.treasury = float(money) if money else None
            cr = L.scalar(bi, bc, "credit", 4)
            co.credit_limit = float(cr) if cr else None
            bw = L.scalar(bi, bc, "base_wage", 4)
            co.base_wage = float(bw) if bw else None
            pool = L.scalar(bi, bc, "weekly_investment_pool_change_from_investment", 4)
            bud.weekly_investment_pool = float(pool) if pool else None
        co.budget = bud
        # 税收等级 / 关税
        tax = TaxRow()
        lv = L.scalar(ri, rc, "tax_level", 3)
        if lv in TAX_LEVELS:
            tax.level = TAX_LEVELS.index(lv)
        tax.taxed_goods = L.atom_list(ri, rc, "taxed_goods", 3)
        for tag in ("import_tariffs", "export_tariffs"):
            tb = L.block(tag, ri, rc, depth=3)
            out = {}
            if tb is not None:
                ti, tc = tb
                for key, gi, gc in L.records(ti, tc, 4):
                    lvl = L.scalar(gi, gc, "level", 5)
                    if not lvl:
                        continue
                    name = str(key)
                    if self.gd is not None and 0 <= key < len(self.gd.goods):
                        name = self.gd.goods[key].name
                    out[name] = lvl
            setattr(tax, tag, out)
        snap.tax = tax
        if tax.level is not None:
            bud.tax_level = tax.level
        # 人口
        pb = L.block("pop_statistics", ri, rc, depth=3)
        if pb is not None:
            pi, pc = pb
            vals = []
            for k in ("population_lower_strata", "population_middle_strata",
                      "population_upper_strata"):
                v = L.scalar(pi, pc, k, 4)
                if v:
                    vals.append(int(float(v)))
            if vals:
                co.population = sum(vals)
        # GDP 趋势
        g = L.trend_last(ri, rc, "gdp", 3)
        co.gdp = g
        snap.country = co

    # ------------------------------------------------------------------
    def _states(self, L: _Lines, snap: Snapshot, cid: int) -> None:
        owned = []
        # country.states 从 country_manager 记录里读
        bm = L.block("country_manager", depth=0)
        if bm is not None:
            i, c = bm
            db = L.block("database", i, c, depth=1)
            if db:
                di, dc = db
                for key, ri, rc in L.records(di, dc, 2):
                    if key != cid:
                        continue
                    sb = L.block("states", ri, rc, depth=3)
                    if sb:
                        si, sc = sb
                        head = L.lines[si].split("{", 1)[1]
                        owned += [int(x) for x in re.findall(r"\d+", head)]
                        if si != sc:
                            for j in range(si + 1, sc):
                                t = L.text(j)
                                if t == "}":
                                    break
                                owned += [int(x) for x in t.split() if x.isdigit()]
                    break
        b = L.block("states", depth=0)
        if not owned:
            # 没有州（或国家记录里没有 states 列表）：不能退化成"全世界所有州"
            snap.states = []
            return
        if b is None:
            snap.states = []
            return
        i, c = b
        db = L.block("database", i, c, depth=1)
        if db is None:
            snap.states = []
            return
        di, dc = db
        states = []
        owned_set = set(owned)
        for key, si, sc in L.records(di, dc, 2):
            if key not in owned_set:
                continue
            s = StateRow(id=key)
            s.owner = int(L.scalar(si, sc, "country", 3) or cid)
            v = L.scalar(si, sc, "capital", 3)
            s.capital = int(v) if v else None
            v = L.scalar(si, sc, "arable_land", 3)
            s.arable_land = int(v) if v else None
            v = L.scalar(si, sc, "infrastructure", 3)
            s.infrastructure = float(v) if v else None
            v = L.scalar(si, sc, "infrastructure_usage", 3)
            s.infrastructure_used = float(v) if v else None
            # 州级人口统计（若存在）
            pb = L.block("pop_statistics", si, sc, depth=3)
            if pb:
                pi, pc = pb
                vals = []
                for k in ("population_lower_strata", "population_middle_strata",
                          "population_upper_strata"):
                    vv = L.scalar(pi, pc, k, 4)
                    if vv:
                        vals.append(int(float(vv)))
                if vals:
                    s.population = sum(vals)
            states.append(s)
        snap.states = states
        if snap.country is not None and not snap.country.population:
            snap.country.population = sum(s.population or 0 for s in states)

    # ------------------------------------------------------------------
    def _pops(self, L: _Lines, snap: Snapshot, cid: int) -> None:
        """按州×职业聚合 pops（workforce/dependents）。可选，解析量较大。"""
        owned = {s.id for s in snap.states}
        b = L.block("pops", depth=0)
        if b is None or not owned:
            snap.pops = []
            return
        i, c = b
        db = L.block("database", i, c, depth=1)
        if db is None:
            snap.pops = []
            return
        di, dc = db
        agg: dict[tuple[int, str], dict] = {}
        for key, pi, pc in L.records(di, dc, 2):
            loc = L.scalar(pi, pc, "location", 3)
            if not loc or not loc.isdigit() or int(loc) not in owned:
                continue
            typ = L.scalar(pi, pc, "type", 3) or "?"
            wf = float(L.scalar(pi, pc, "workforce", 3) or 0.0)
            dep = float(L.scalar(pi, pc, "dependents", 3) or 0.0)
            rec = agg.setdefault((int(loc), typ), {
                "state_id": int(loc), "profession": typ,
                "workforce": 0.0, "dependents": 0.0, "pops": 0,
                "culture": L.scalar(pi, pc, "culture", 3),
                "religion": L.scalar(pi, pc, "religion", 3),
            })
            rec["workforce"] += wf
            rec["dependents"] += dep
            rec["pops"] += 1
        snap.pops = sorted(agg.values(), key=lambda r: (r["state_id"], -r["workforce"]))

    # ------------------------------------------------------------------
    def _buildings(self, L: _Lines, snap: Snapshot, cid: int) -> None:
        owned = {s.id for s in snap.states}
        b = L.block("building_manager", depth=0)
        if b is None:
            snap.buildings = []
            return
        i, c = b
        db = L.block("database", i, c, depth=1)
        if db is None:
            snap.buildings = []
            return
        di, dc = db
        rows = []
        for key, bi, bc in L.records(di, dc, 2):
            st = L.scalar(bi, bc, "state", 3)
            if st is None or int(st) not in owned:
                continue
            btype = L.scalar(bi, bc, "building", 3)
            if not btype:
                continue
            row = BuildingRow(type=btype, state_id=int(st), id=key)
            row.levels = int(L.scalar(bi, bc, "levels", 3) or 0)
            staff = L.scalar(bi, bc, "staffing", 3)
            row.staffing = float(staff) if staff is not None else float(row.levels)
            thr = L.scalar(bi, bc, "throughput", 3)
            row.throughput = float(thr) if thr is not None else 1.0
            row.production_methods = L.atom_list(bi, bc, "production_methods", 3)
            row.active = (L.scalar(bi, bc, "active", 3) or "yes") == "yes"
            v = L.scalar(bi, bc, "salary_rate", 3)
            row.salary_rate = float(v) if v else None
            v = L.scalar(bi, bc, "profit_after_reserves", 3)
            row.profit = float(v) if v else None
            v = L.scalar(bi, bc, "hiring_rate", 3)
            row.hiring_rate = float(v) if v else None
            rows.append(row)
        snap.buildings = rows

    # ------------------------------------------------------------------
    def _market(self, L: _Lines, snap: Snapshot) -> None:
        b = L.block("market_manager", depth=0)
        if b is None:
            return
        i, c = b
        wb = L.block("world_market", i, c, depth=1)
        if wb is None:
            return
        wi, wc = wb
        m = MarketRow(id=snap.country.market_id if snap.country else None)
        # 世界市场价格趋势：channels 下标即商品 index
        pt = L.block("price_trend", wi, wc, depth=2)
        if pt:
            pi, pc = pt
            ch = L.block("channels", pi, pc, depth=3)
            if ch:
                ci, cc = ch
                for key, xi, xc in L.records(ci, cc, 4):
                    vals = L.number_list(xi, xc, "values", 5)
                    if vals and self.gd and key < len(self.gd.goods):
                        m.prices[self.gd.goods[key].name] = vals[-1]
        if self.gd:
            m.base_prices = {g.name: g.cost for g in self.gd.goods}
        snap.market = m
        snap.diagnostics.warnings.append(
            "存档只保存世界市场价格趋势；country.market 的实时价需 live 模式")

    # ------------------------------------------------------------------
    def _laws(self, L: _Lines, snap: Snapshot, cid: int) -> None:
        b = L.block("laws", depth=0)
        if b is None:
            return
        i, c = b
        db = L.block("database", i, c, depth=1)
        if db is None:
            return
        di, dc = db
        rows = []
        for key, li, lc in L.records(di, dc, 2):
            if int(L.scalar(li, lc, "country", 3) or -1) != cid:
                continue
            if (L.scalar(li, lc, "active", 3) or "no") != "yes":
                continue
            name = L.scalar(li, lc, "law", 3)
            if not name:
                continue
            group = self.gd.laws[name].group if self.gd and name in self.gd.laws else None
            rows.append(LawRow(name=name, group=group))
        snap.laws = rows

    def _finalize_tax(self, snap: Snapshot) -> None:
        if not snap.tax or not self.gd:
            return
        for law in snap.laws:
            ld = self.gd.laws.get(law.name)
            if ld is None or ld.group != "lawgroup_taxation":
                continue
            snap.tax.taxation_law = law.name
            if snap.tax.level is not None and 0 <= snap.tax.level < len(TAX_LEVELS):
                snap.tax.rates = dict(ld.tax_levels.get(TAX_LEVELS[snap.tax.level], {}))
            return

    def _techs(self, L: _Lines, snap: Snapshot, cid: int) -> None:
        b = L.block("technology", depth=0)
        if b is None:
            return
        i, c = b
        db = L.block("database", i, c, depth=1)
        if db is None:
            return
        di, dc = db
        for key, ti, tc in L.records(di, dc, 2):
            if int(L.scalar(ti, tc, "country", 3) or -1) != cid:
                continue
            snap.techs = L.atom_list(ti, tc, "acquired_technologies", 3)
            return

    def _institutions(self, L: _Lines, snap: Snapshot, cid: int) -> None:
        b = L.block("institutions", depth=0)
        if b is None:
            return
        i, c = b
        db = L.block("database", i, c, depth=1)
        if db is None:
            return
        di, dc = db
        rows = []
        for key, ii, ic in L.records(di, dc, 2):
            if int(L.scalar(ii, ic, "country", 3) or -1) != cid:
                continue
            name = L.scalar(ii, ic, "institution", 3)
            inv = int(L.scalar(ii, ic, "investment", 3) or 0)
            if name:
                rows.append(InstitutionRow(type=name, investment=inv))
        snap.institutions = rows

    def _companies(self, L: _Lines, snap: Snapshot, cid: int) -> None:
        b = L.block("companies", depth=0)
        if b is None:
            return
        i, c = b
        db = L.block("database", i, c, depth=1)
        if db is None:
            return
        di, dc = db
        rows = []
        for key, ci, cc in L.records(di, dc, 2):
            if int(L.scalar(ci, cc, "country", 3) or -1) != cid:
                continue
            rows.append(CompanyRow(
                type=L.scalar(ci, cc, "company_type", 3) or "?",
                region=L.scalar(ci, cc, "state_region", 3),
                building_id=int(L.scalar(ci, cc, "building", 3) or 0) or None,
                ceo=int(L.scalar(ci, cc, "ceo", 3) or 0) or None,
            ))
        snap.companies = rows

    def _igs(self, L: _Lines, snap: Snapshot, cid: int) -> None:
        b = L.block("interest_groups", depth=0)
        if b is None:
            return
        i, c = b
        db = L.block("database", i, c, depth=1)
        if db is None:
            return
        di, dc = db
        rows = []
        for key, gi, gc in L.records(di, dc, 2):
            if int(L.scalar(gi, gc, "country", 3) or -1) != cid:
                continue
            name = L.scalar(gi, gc, "definition", 3) or L.scalar(gi, gc, "name", 3)
            if not name:
                continue
            clout = L.scalar(gi, gc, "clout", 3)
            approval = L.scalar(gi, gc, "approval", 3)
            party = L.scalar(gi, gc, "party", 3)
            rows.append(InterestGroupRow(
                name=name,
                clout=float(clout) if clout else 0.0,
                approval=int(approval) if approval is not None else None,
                political_strength=int(L.scalar(gi, gc, "political_strength", 3) or 0),
                party=int(party) if party is not None else -1,
            ))
        snap.interest_groups = rows

    def _movements(self, L: _Lines, snap: Snapshot, cid: int) -> None:
        b = L.block("political_movement_manager", depth=0)
        if b is None:
            return
        i, c = b
        db = L.block("database", i, c, depth=1)
        if db is None:
            return
        di, dc = db
        rows = []
        for key, mi, mc in L.records(di, dc, 2):
            if int(L.scalar(mi, mc, "country", 3) or -1) != cid:
                continue
            typ = None
            ib = L.block("identity", mi, mc, depth=3)
            if ib:
                typ = L.scalar(ib[0], ib[1], "type", 4)
            rows.append(MovementRow(
                type=typ or "?",
                ideology=L.scalar(mi, mc, "ideology", 3),
                radicalism=float(L.scalar(mi, mc, "radicalism", 3) or 0.0),
                pops=len(L.number_list(mi, mc, "pops", 3)),
            ))
        snap.movements = rows

    def _decrees(self, L: _Lines, snap: Snapshot, cid: int) -> None:
        b = L.block("decree_manager", depth=0)
        if b is None:
            return
        i, c = b
        db = L.block("database", i, c, depth=1)
        if db is None:
            return
        di, dc = db
        rows = []
        for key, rdi, rdc in L.records(di, dc, 2):
            if int(L.scalar(rdi, rdc, "country", 3) or -1) != cid:
                continue
            name = L.scalar(rdi, rdc, "decree", 3) or L.scalar(rdi, rdc, "type", 3)
            st = L.scalar(rdi, rdc, "state", 3)
            if name:
                rows.append(DecreeRow(name=name, state_id=int(st) if st else None))
        snap.decrees = rows
