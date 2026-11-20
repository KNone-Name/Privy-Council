# -*- coding: utf-8 -*-
"""实时读取层：一次 attach，把 v3live 已解析的所有国家数据读成一个 Snapshot。

这是"总程序"的 live 后端。它不重新做逆向，只调用 ``v3live`` 里已经验证过的
读取函数/偏移：

  * 国家 / 财政 / 税收 / 关税        （CCountry、CCountryBudget）
  * 州 / 人口 / 基础设施 / 本地价    （CState）
  * 建筑 / 等级 / 就业 / 吞吐 / PM   （CBuilding）
  * 市场价 / 供给 / 需求             （CMarket）
  * 法律 / 科技 / 法令               （CCountryLaw、CCountryTechnologyStatus、CDecree）
  * 利益集团 / 政治运动              （CInterestGroup、CPoliticalMovement）
  * 机构 / 公司                      （CInstitution、CCompany）
  * 国家/州临时修正                  （CStaticModifier 实例）

每个 section 独立 try/except：某一块读失败只会让 ``diagnostics.sections`` 标记
``error``，其余数据照常返回。
"""
from __future__ import annotations

import json
import os
import struct
import time
from datetime import datetime

from v3live import cli
from v3live import proc as proc_mod

from .model import (
    BuildingRow, Budget, CompanyRow, Country, DecreeRow, Diagnostics, GameInfo,
    InstitutionRow, InterestGroupRow, LawRow, MarketRow, MovementRow, Snapshot,
    StateRow, TaxRow, TimedModifierRow,
)

FIXED = 100000.0
MAX_VEC = 200
# CBuilding +0x110 招聘率（v3live 字段表已验证，cli 里没有单独常量）
OFF_BLD_HIRING = 0x110


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _vec_raw(p, addr: int, maxn: int = MAX_VEC) -> list[int] | None:
    """读 std::vector<i64>：{ptr, size, cap}。"""
    ptr = p.read_u64(addr)
    n = p.read_u32(addr + 8)
    if not ptr or not n or n > maxn:
        return None
    raw = p.read(ptr, 8 * n)
    if len(raw) < 8 * n:
        return None
    return list(struct.unpack_from("<%dq" % n, raw, 0))


def _vec_float(p, addr: int, maxn: int = MAX_VEC) -> list[float] | None:
    v = _vec_raw(p, addr, maxn)
    return [x / FIXED for x in v] if v is not None else None


def _scan(p, cls: str, dedupe: int | None = None, keep: str = "auto",
          all_vtables: bool = False) -> list[int]:
    """取实例基址。

    关键：**先按"无去重"加载缓存**（缓存名 `_p`，与 v3live `prewarm` 写出的
    文件名一致），再在内存里按 id 去重。这样 `python v3.py prewarm` 之后，
    read 的每个类都能直接命中缓存，而不是因为 cache key 带 `_dXX` 又全量扫一遍。
    """
    cli._DEDUPE_BY = None
    cli._DEDUPE_KEEP = "auto"
    cli._REFRESH = getattr(cli, "_REFRESH", False)
    try:
        bases = cli._instances_of(p, cls, 0, verbose=False,
                                   all_vtables=all_vtables)
    except SystemExit as exc:                 # _instances_of 用 SystemExit 报"没有这个类"
        raise RuntimeError(str(exc)) from exc
    finally:
        cli._DEDUPE_BY = None
        cli._DEDUPE_KEEP = "auto"

    if dedupe is None:
        return bases
    groups: dict[int, list[int]] = {}
    for a in bases:
        v = p.read_u32(a + dedupe)
        if v is None:
            continue
        groups.setdefault(v, []).append(a)
    out = []
    for _v, cands in groups.items():
        if keep == "last":
            pick = cands[-1]
        elif keep == "first":
            pick = cands[0]
        else:
            pick = next((c for c in cands if cli._sane(p, cls, c)), cands[-1])
        out.append(pick)
    out.sort()
    return out


def _msvc(p, addr: int) -> str | None:
    try:
        return p.msvc_string(addr)
    except Exception:
        return None


class MainMenuError(RuntimeError):
    """游戏在主菜单/尚未加载存档：没有 CJominiPlayer，也就没有玩家国家。"""


class LiveReader:
    def __init__(self, pid: int | None = None, country_id: int | None = None,
                 include_local_prices: bool = True,
                 include_modifiers: bool = True,
                 include_politics: bool = True,
                 include_orgs: bool = True,
                 refresh: bool = False,
                 progress=None,
                 game_data=None):
        self.pid = pid
        self.country_id = country_id
        self._building_scan_at = 0.0
        self.include_local_prices = include_local_prices
        self.include_modifiers = include_modifiers
        self.include_politics = include_politics
        self.include_orgs = include_orgs
        self.refresh = refresh
        self.progress = progress or (lambda msg: None)
        self._provided_gd = game_data

    # ------------------------------------------------------------------
    def read(self) -> Snapshot:
        p = proc_mod.attach("victoria3.exe", pid=self.pid)
        try:
            return self.read_with(p)
        finally:
            p.close()

    def read_with(self, p) -> Snapshot:
        """用已 attach 的进程读一次；watch 模式复用同一个 Proc 句柄。"""
        snap = Snapshot()
        snap.game = GameInfo(source="live", captured_at=_now())
        diag = snap.diagnostics
        cli._REFRESH = bool(self.refresh)

        self._fill_game(p, snap)
        self.gd = self._provided_gd
        if self.gd is None and snap.game.game_dir:
            try:
                from .gamedata import GameData
                self.gd = GameData(snap.game.game_dir)
            except Exception as exc:      # noqa: BLE001
                diag.warnings.append("game_data: %s" % exc)
        cid = self.country_id
        if cid is None:
            cid = cli._player_country_id(p)
        if cid is None:
            raise MainMenuError(
                "当前在游戏主菜单/尚未加载存档：找不到 CJominiPlayer。"
                "请先在游戏里加载存档，再执行读取。")
        snap.game.notes.append("country_id=%d" % cid)

        caddr = self._section(diag, "country", lambda: self._read_country(p, snap, cid))
        state_addrs = self._section(diag, "states",
                                    lambda: self._read_states(p, snap, cid))
        self._section(diag, "buildings",
                      lambda: self._read_buildings(p, snap, state_addrs or []))
        self._section(diag, "market", lambda: self._read_market(p, snap, caddr))
        self._section(diag, "laws", lambda: self._read_laws(p, snap, cid))
        self._section(diag, "techs", lambda: self._read_techs(p, snap, cid))
        self._section(diag, "decrees", lambda: self._read_decrees(p, snap, cid))
        self._section(diag, "tax", lambda: self._read_tax(p, snap, caddr, cid))
        if self.include_politics:
            self._section(diag, "interest_groups",
                          lambda: self._read_igs(p, snap, cid))
            self._section(diag, "movements",
                          lambda: self._read_movements(p, snap, cid))
        if self.include_orgs:
            self._section(diag, "institutions",
                          lambda: self._read_institutions(p, snap, cid))
            self._section(diag, "companies",
                          lambda: self._read_companies(p, snap, cid))
        if self.include_modifiers:
            self._section(diag, "timed_modifiers",
                          lambda: self._read_timed(p, snap, caddr, state_addrs or []))
        if not snap.states:
            # 主菜单被 CJominiPlayer 挡住了；这里兜住“正在加载存档”的半成品
            raise RuntimeError("读到 0 个州：游戏可能仍在加载存档，请稍后重试")
        return snap

    # ------------------------------------------------------------------
    def _fill_game(self, p, snap: Snapshot) -> None:
        base = p.module_base("victoria3.exe")
        exe = None
        for m in p.modules():
            if m["name"].lower() == "victoria3.exe":
                exe = m.get("path")
                break
        snap.game.pid = p.pid
        snap.game.module_base = "0x%X" % base if base else None
        snap.game.exe_path = exe
        if exe:
            # v3live 默认 EXE 写死在 E: 盘；如果游戏从别的 Steam 库启动，
            # RTTI 会解析错 exe。这里以进程模块路径为准，并清掉已缓存的 RTTI。
            try:
                if os.path.abspath(exe) != os.path.abspath(cli.EXE):
                    cli.EXE = exe
                    cli._rtti = None
            except Exception:
                pass
        try:
            snap.game.process_start = "%d" % p.start_time()
        except Exception:
            pass
        if exe:
            game_dir = os.path.join(os.path.dirname(os.path.dirname(exe)), "game")
            if os.path.isdir(os.path.join(game_dir, "common")):
                snap.game.game_dir = game_dir
        if not snap.game.game_dir:
            from .gamedata import discover_game_dir
            snap.game.game_dir = discover_game_dir()
        if snap.game.game_dir:
            br = os.path.join(os.path.dirname(snap.game.game_dir), "caligula_branch.txt")
            try:
                with open(br, encoding="utf-8") as f:
                    snap.game.game_version = f.read().strip().split("/")[-1]
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _section(self, diag: Diagnostics, name: str, fn):
        t0 = time.perf_counter()
        self.progress("[读取] %s …" % name)
        try:
            out = fn()
            diag.sections[name] = "ok" if out not in (None, [], {}) else "empty"
            return out
        except Exception as exc:                     # noqa: BLE001
            diag.sections[name] = "error"
            diag.warnings.append("%s: %s: %s" % (name, type(exc).__name__, exc))
            return None
        finally:
            diag.timings[name] = round(time.perf_counter() - t0, 3)

    # ------------------------------------------------------------------
    def _country_addr(self, p, cid: int) -> int | None:
        for a in _scan(p, "CCountry", cli.OFF_COUNTRY_ID):
            if p.read_u32(a + cli.OFF_COUNTRY_ID) == cid:
                return a
        return None

    def _read_country(self, p, snap: Snapshot, cid: int) -> int | None:
        addr = self._country_addr(p, cid)
        if addr is None:
            raise RuntimeError("内存里找不到国家 %d" % cid)
        c = Country(id=cid)
        c.market_id = p.read_u32(addr + cli.OFF_COUNTRY_MARKET)
        if c.market_id == 0xFFFFFFFF:
            c.market_id = None
        c.treasury = p.read_i64(addr + cli.OFF_GOLD) / FIXED
        c.credit_limit = p.read_i64(addr + cli.OFF_CREDIT_LIMIT) / FIXED
        c.loan_principal = p.read_i64(addr + cli.OFF_PRINCIPAL) / FIXED
        c.base_wage = p.read_i64(addr + cli.OFF_BASE_WAGE) / FIXED
        b = Budget()
        b.income = [(p.read_i64(addr + cli.OFF_INCOME + 8 * i) or 0) / FIXED
                    for i in range(cli.N_INCOME)]
        b.expenses = [(p.read_i64(addr + cli.OFF_EXPENSES + 8 * i) or 0) / FIXED
                      for i in range(cli.N_EXPENSES)]
        b.income_total = sum(b.income)
        b.expense_total = sum(b.expenses)
        b.balance = b.income_total - b.expense_total
        b.tax_level = p.read_u32(addr + cli.OFF_TAX_LEVEL)
        b.weekly_investment_pool = (p.read_i64(addr + cli.OFF_WEEKLY_POOL) or 0) / FIXED
        c.budget = b
        snap.country = c
        return addr

    # ------------------------------------------------------------------
    def _read_states(self, p, snap: Snapshot, cid: int) -> list[int]:
        addrs = _scan(p, "CState", cli.OFF_STATE_ID)
        states, keep = [], []
        for a in addrs:
            if p.read_u32(a + cli.OFF_STATE_OWNER) != cid:
                continue
            sid = p.read_u32(a + cli.OFF_STATE_ID)
            s = StateRow(id=sid, owner=cid)
            s.capital = p.read_u32(a + cli.OFF_STATE_CAPITAL)
            s.arable_land = p.read_u32(a + cli.OFF_STATE_ARABLE)
            lo = p.read_i64(a + cli.OFF_POP_LOWER) or 0
            mid = p.read_i64(a + cli.OFF_POP_MIDDLE) or 0
            hi = p.read_i64(a + cli.OFF_POP_UPPER) or 0
            s.population_lower, s.population_middle, s.population_upper = lo, mid, hi
            s.population = lo + mid + hi
            s.radicals = p.read_i64(a + cli.OFF_POP_RADICALS)
            s.unemployed = p.read_i64(a + cli.OFF_POP_UNEMPLOYED)
            s.laborers = p.read_i64(a + cli.OFF_POP_LABORER)
            s.infrastructure = (p.read_i64(a + cli.OFF_STATE_INFRA) or 0) / FIXED
            s.infrastructure_used = (p.read_i64(a + cli.OFF_STATE_INFRA_USED) or 0) / FIXED
            if self.include_local_prices:
                s.local_prices = _vec_float(p, a + cli.OFF_STATE_LOCAL_PRICES, 60)
                s.market_supply = _vec_float(p, a + cli.OFF_STATE_MKT_SUPPLY, 60)
                s.market_demand = _vec_float(p, a + cli.OFF_STATE_MKT_DEMAND, 60)
            states.append(s)
            keep.append(a)
        if snap.country is not None:
            snap.country.population = sum(s.population or 0 for s in states)
        snap.states = states
        return keep

    # ------------------------------------------------------------------
    def _read_buildings(self, p, snap: Snapshot, state_addrs: list[int]) -> int:
        owned = {s.id for s in snap.states}
        # CBuilding 实例会随着建造增加；缓存超过 30 秒就强制重扫，否则新建筑不会出现在快照里。
        import time as _time
        refresh = (_time.time() - self._building_scan_at > 30)
        old_refresh = getattr(cli, "_REFRESH", False)
        if refresh:
            cli._REFRESH = True
        try:
            addrs = _scan(p, "CBuilding", cli.OFF_BLD_ID)
        finally:
            if refresh:
                cli._REFRESH = old_refresh
                self._building_scan_at = _time.time()
        tcache: dict[int, str] = {}
        rows = []
        for a in addrs:
            sid = p.read_u32(a + cli.OFF_BLD_STATE)
            if sid not in owned:
                continue
            tp = p.read_u64(a + cli.OFF_BLD_TYPE)
            tname = tcache.get(tp)
            if tname is None and tp:
                tname = _msvc(p, tp + cli.OFF_BTYPE_NAME) or "?"
                tcache[tp] = tname
            b = BuildingRow(
                type=tname or "?",
                state_id=sid,
                levels=p.read_i32(a + cli.OFF_BLD_LEVELS) or 0,
                staffing=(p.read_i64(a + cli.OFF_BLD_STAFFING) or 0) / FIXED,
                throughput=(p.read_i64(a + cli.OFF_BLD_THROUGHPUT) or 0) / FIXED,
                production_methods=cli._building_pms(p, a),
            )
            b.id = p.read_u32(a + cli.OFF_BLD_ID)
            b.salary_rate = (p.read_i64(a + cli.OFF_BLD_SALARY) or 0) / FIXED
            b.profit = (p.read_i64(a + cli.OFF_BLD_PROFIT) or 0) / FIXED
            b.hiring_rate = (p.read_i64(a + cli.OFF_BLD_HIRING) or 0) / FIXED
            if b.throughput <= 0:
                snap.diagnostics.warnings.append(
                    "建筑 %s@%d 吞吐量读为 0（可能是旧对象）" % (b.type, b.state_id))
            rows.append(b)
        snap.buildings = rows
        return len(rows)

    # ------------------------------------------------------------------
    def _read_market(self, p, snap: Snapshot, caddr: int | None) -> int | None:
        mid = snap.country.market_id if snap.country else None
        if mid is None:
            return None
        addr = cli._market_with_prices(p, mid)
        if addr is None:
            raise RuntimeError("找不到市场 %d 的有效价格表" % mid)
        prices = cli._read_vec54(p, addr + cli.OFF_MARKET_PRICES)
        supply = cli._read_vec54(p, addr + cli.OFF_MARKET_SUPPLY)
        demand = cli._read_vec54(p, addr + cli.OFF_MARKET_DEMAND)
        gd = self.gd
        base = {g.name: g.cost for g in gd.goods} if gd else {}
        m = MarketRow(id=mid)
        if prices:
            for g in (gd.goods if gd else []):
                m.prices[g.name] = prices[g.index] / FIXED if g.index < len(prices) else 0.0
        if supply:
            for g in (gd.goods if gd else []):
                m.supply[g.name] = supply[g.index] / FIXED if g.index < len(supply) else 0.0
        if demand:
            for g in (gd.goods if gd else []):
                m.demand[g.name] = demand[g.index] / FIXED if g.index < len(demand) else 0.0
        m.base_prices = base
        snap.market = m
        return addr

    # ------------------------------------------------------------------
    def _read_laws(self, p, snap: Snapshot, cid: int) -> list[LawRow]:
        gd = self.gd
        raw = cli._laws_from_memory(p, cid)
        best: dict[str, tuple[str, int | None, int]] = {}
        for name, when, addr in raw:
            group = (gd.laws[name].group if gd and name in gd.laws else name)
            cur = best.get(group)
            if cur is None or addr < cur[2]:
                best[group] = (name, when, addr)
        snap.laws = [LawRow(name=n, group=g, date=w)
                     for g, (n, w, _a) in sorted(best.items())]
        return snap.laws

    def _read_techs(self, p, snap: Snapshot, cid: int) -> list[str]:
        snap.techs = cli._techs_from_memory(p, cid)
        return snap.techs

    def _empty_cache_recent(self, p, cls: str, ttl: float = 900.0) -> bool:
        """空缓存（addrs=[]）在 TTL 内视为“确实没有该对象”，避免每轮重扫。"""
        if getattr(cli, "_REFRESH", False):
            return False
        try:
            base = p.module_base("victoria3.exe")
            path = cli._cache_path(cls + "_p", p.pid, base, p.start_time())
            if not os.path.exists(path):
                return False
            if time.time() - os.path.getmtime(path) > ttl:
                return False
            with open(path, encoding="utf-8") as f:
                return not (json.load(f).get("addrs") or [])
        except Exception:
            return False

    def _read_decrees(self, p, snap: Snapshot, cid: int) -> list[DecreeRow]:
        # 开局 CDecree=0 时，_all_decrees 的空缓存会被 _load_cache 当成 miss，
        # 导致每轮都全内存重扫（实测 16 秒）。空缓存 TTL 内直接返回空。
        if self._empty_cache_recent(p, "CDecree"):
            snap.decrees = []
            return []
        snap.decrees = [DecreeRow(name=n, state_id=s)
                        for n, s in cli._country_decrees(p, cid)]
        return snap.decrees

    # ------------------------------------------------------------------
    def _read_tax(self, p, snap: Snapshot, caddr: int | None, cid: int) -> TaxRow | None:
        if caddr is None:
            return None
        # 直接从已定位的 CCountry 读，避免 _tax_from_memory 再扫一遍（且可能命中旧对象）
        t = TaxRow(level=p.read_u32(caddr + cli.OFF_TAX_LEVEL))
        ptr = p.read_u64(caddr + cli.OFF_TAXED_PTR)
        n = p.read_u32(caddr + cli.OFF_TAXED_N)
        if ptr and n and 0 < n <= 120:
            raw = p.read(ptr, n * 8)
            for i in range(n):
                g = struct.unpack_from("<Q", raw, i * 8)[0]
                if not g:
                    continue
                name = _msvc(p, g + cli.OFF_GOODS_NAME)
                if name:
                    t.taxed_goods.append(name)
        gd = self.gd
        # 税收法律
        if gd:
            for law in snap.laws:
                if gd.laws.get(law.name) and gd.laws[law.name].group == "lawgroup_taxation":
                    t.taxation_law = law.name
                    levels = gd.laws[law.name].tax_levels
                    names = ["very_low", "low", "medium", "high", "very_high"]
                    if t.level is not None and 0 <= t.level < len(names):
                        t.rates = dict(levels.get(names[t.level], {}))
                    break
        # 关税等级
        try:
            lv = cli._tariff_levels_from_memory(p, caddr)
            names = cli.TARIFF_LEVELS
            for tag in ("import", "export"):
                out = {}
                for good, level in lv.get(tag, []):
                    out[good] = names[level] if 0 <= level < len(names) else str(level)
                setattr(t, tag + "_tariffs", out)
        except Exception as exc:
            snap.diagnostics.warnings.append("tariffs: %s" % exc)
        snap.tax = t
        return t

    # ------------------------------------------------------------------
    def _read_igs(self, p, snap: Snapshot, cid: int) -> list[InterestGroupRow]:
        snap.interest_groups = [
            InterestGroupRow(name=x["name"], clout=x["clout"], approval=x["approval"],
                             in_government=x["in_gov"], party=x["party"],
                             political_strength=x["polstr"])
            for x in cli._igs_from_memory(p, cid)
        ]
        return snap.interest_groups

    def _read_movements(self, p, snap: Snapshot, cid: int) -> list[MovementRow]:
        snap.movements = [
            MovementRow(type=x["type"], ideology=x["ideology"],
                        radicalism=x["radicalism"], pops=x["pops"])
            for x in cli._moves_from_memory(p, cid)
        ]
        return snap.movements

    def _read_institutions(self, p, snap: Snapshot, cid: int) -> list[InstitutionRow]:
        snap.institutions = [
            InstitutionRow(type=x["type"], investment=x["investment"])
            for x in cli._institutions_from_memory(p, cid)
        ]
        return snap.institutions

    def _read_companies(self, p, snap: Snapshot, cid: int) -> list[CompanyRow]:
        snap.companies = [
            CompanyRow(type=x["type"], region=x["region"],
                       building_id=x["building"], ceo=x["ceo"])
            for x in cli._companies_from_memory(p, cid)
        ]
        return snap.companies

    # ------------------------------------------------------------------
    def _read_timed(self, p, snap: Snapshot, caddr: int | None,
                    state_addrs: list[int]) -> list[TimedModifierRow]:
        rows: list[TimedModifierRow] = []

        def read_list(base: int, off: int, scope: str, target_id: int | None):
            ptr = p.read_u64(base + off)
            n = p.read_u32(base + off + 8)
            if not ptr or not n or n > 200:
                return
            raw = p.read(ptr, 8 * n)
            for i in range(n):
                inst = struct.unpack_from("<Q", raw, i * 8)[0]
                if not inst:
                    continue
                d = p.read_u64(inst + cli.OFF_TMOD_DEF)
                name = None
                if d:
                    np = p.read_u64(d + cli.OFF_STATICMOD_NAME)
                    name = cli._first_string(p, np) if np else None
                rows.append(TimedModifierRow(
                    scope=scope, name=name or "?",
                    value=(p.read_i64(inst + cli.OFF_TMOD_VALUE) or 0) / FIXED,
                    target_id=target_id,
                    start=p.read_i64(inst + cli.OFF_TMOD_START),
                    end=p.read_i64(inst + cli.OFF_TMOD_END),
                ))

        if caddr is not None:
            read_list(caddr, cli.OFF_COUNTRY_TIMED, "country",
                      snap.country.id if snap.country else None)
        for a in state_addrs:
            sid = p.read_u32(a + cli.OFF_STATE_ID)
            read_list(a, cli.OFF_STATE_TIMED, "state", sid)
        snap.timed_modifiers = rows
        return rows
