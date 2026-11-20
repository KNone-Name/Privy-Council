# -*- coding: utf-8 -*-
"""v3calc 核心测试。

运行：
    python -m unittest discover -s tests -v

需要：
  * 游戏安装目录（自动发现，或设置 V3_GAME）
  * 文本存档（默认用 Victoria3-RE 的 save.txt，可用 V3_SAVE 覆盖）
    没有存档时，依赖存档的测试会跳过。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from v3calc import pdx
from v3calc.gamedata import GameData, discover_game_dir
from v3calc.gdp import GDPCalculator
from v3calc.model import BuildingRow, Country, LawRow, Snapshot, StateRow
from v3calc.pm import PMChecker, STATUS_AVAILABLE, STATUS_LOCKED
from v3calc.savefile import SaveReader
from v3calc.scenario import gdp_of, pm_delta

ROOT = Path(__file__).resolve().parent.parent
SAVE = Path(os.environ.get(
    "V3_SAVE", r"D:\Game\ai\Victoria3-RE\data\saves\save.txt"))
GAME = discover_game_dir()


class TestPdx(unittest.TestCase):
    def test_parser(self):
        src = '''
        # comment
        top = {
            list = { a b "c d" }
            nested = { key = 1 key = 2 }
            text = "a # not comment"
            flag ?= yes
            bad = { x = { y = 3 } }
        }
        '''
        obj = pdx.parse_text(src)
        top = obj.block("top")
        self.assertIsNotNone(top)
        self.assertEqual(top.atoms("list"), ["a", "b", "c d"])
        self.assertEqual(top.block("nested").getall("key"), ["1", "2"])
        self.assertEqual(top.get("text"), "a # not comment")
        self.assertEqual(top.get("flag"), "yes")
        self.assertEqual(top.block("bad").block("x").num("y"), 3.0)


@unittest.skipUnless(GAME, "没有找到游戏安装目录")
class TestGameData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gd = GameData(GAME)

    def test_counts(self):
        self.assertEqual(len(self.gd.goods), 54)
        self.assertGreater(len(self.gd.pms), 400)
        self.assertGreater(len(self.gd.buildings), 100)
        self.assertEqual(len(self.gd.laws), 148)
        self.assertEqual(len(self.gd.techs), 179)

    def test_goods_order_matches_v3live(self):
        try:
            from v3live import cli
        except Exception:
            self.skipTest("v3live 不可用")
        expect = [(n, c, k) for n, c, k in cli.goods_table()]
        got = [(g.name, g.cost, g.category) for g in self.gd.goods]
        self.assertEqual(got, expect)

    def test_pm_fields(self):
        pm = self.gd.pms["pm_elastics"]
        self.assertIn("pm_sewing_machines", pm.unlocking_production_methods)
        self.assertEqual(pm.goods("output").get(
            ("building_modifiers", "workforce_scaled", "luxury_clothes")), 70.0)
        law = self.gd.laws["law_per_capita_based_taxation"]
        self.assertEqual(law.tax_levels["very_high"]["tax_income_add"], 0.15)


@unittest.skipUnless(SAVE.exists() and GAME, "没有文本存档或游戏目录")
class TestSaveAndCalc(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gd = GameData(GAME)
        cls.snap = SaveReader(str(SAVE), game_dir=GAME).read()
        cls.calc = GDPCalculator(cls.gd, cls.snap)

    # -- 快照 ---------------------------------------------------------
    def test_country_basics(self):
        s = self.snap
        self.assertEqual(s.country.id, 157)
        self.assertEqual(s.country.tag, "CHI")
        self.assertEqual(len(s.states), 43)
        self.assertEqual(len(s.buildings), 302)
        self.assertEqual(sum(b.levels for b in s.buildings), 19498)
        self.assertEqual(s.country.population, 366405617)
        self.assertAlmostEqual(s.country.gdp, 84674628.3968, places=3)
        self.assertEqual(len(s.laws), 24)
        self.assertEqual(len(s.techs), 24)
        self.assertEqual(s.tax.taxation_law, "law_land_based_taxation")

    def test_roundtrip(self):
        d = self.snap.to_dict()
        again = Snapshot.from_dict(d)
        self.assertEqual(again.country.id, 157)
        self.assertEqual(len(again.buildings), 302)
        self.assertEqual(again.buildings[0].type, self.snap.buildings[0].type)
        self.assertEqual(again.states[0].id, self.snap.states[0].id)
        json.dumps(d)                       # 必须可 JSON 序列化

    # -- GDP ----------------------------------------------------------
    def test_gdp_totals_consistent(self):
        r = self.calc.compute(price="base", scale="staffing")
        self.assertGreater(r.annual, 0)
        self.assertAlmostEqual(r.weekly, r.output_weekly - r.input_weekly, places=6)
        self.assertAlmostEqual(r.annual, r.weekly * 52, places=6)
        self.assertAlmostEqual(
            r.output_weekly, sum(g.output_value for g in r.goods), places=6)
        self.assertAlmostEqual(
            r.input_weekly, sum(g.input_value for g in r.goods), places=6)
        self.assertIsNotNone(r.per_capita)

    def test_gdp_target_is_near_but_not_exact_offline(self):
        # 存档没有本地价：只能用世界市场价格趋势/基础价，绝对值有系统性偏差
        r = self.calc.compute(price="base", scale="staffing")
        ratio = r.annual / self.snap.country.gdp
        self.assertGreater(ratio, 1.0)
        self.assertLess(ratio, 1.6)

    def test_default_convention_is_market_staffing_producers(self):
        # save 没有本国市场价 → auto 落到 base；其余默认应为 staffing + producers
        r = self.calc.compute()
        self.assertEqual(r.price_basis, "base")
        self.assertEqual(r.scale, "staffing")
        self.assertEqual(r.building_filter, "producers")
        self.assertAlmostEqual(r.annual, 113462652, delta=100)

    def test_price_modes_differ(self):
        a = self.calc.compute(price="base", scale="staffing")
        b = self.calc.compute(price="market", scale="staffing")
        self.assertNotAlmostEqual(a.annual, b.annual, places=0)

    def test_auto_price_falls_back_to_base_in_save_mode(self):
        r = self.calc.compute(price="auto", scale="staffing")
        self.assertEqual(r.price_basis, "base")

    def test_exclude_goods(self):
        a = self.calc.compute(price="base", scale="staffing")
        b = self.calc.compute(price="base", scale="staffing",
                              exclude_goods={"services"})
        svc = next((g.net_value for g in a.goods if g.good == "services"), 0.0)
        self.assertAlmostEqual((a.weekly - svc) * 52, b.annual, places=3)

    # -- PM -----------------------------------------------------------
    def test_all_active_pms_are_allowed(self):
        ck = PMChecker(self.gd, self.snap)
        seen = set()
        for b in self.snap.buildings:
            for name in b.production_methods:
                if (b.type, name) in seen:
                    continue
                seen.add((b.type, name))
                for g in ck.pm_groups.get(name, []):
                    if g not in self.gd.buildings[b.type].production_method_groups:
                        continue
                    st = ck.evaluate(self.gd.pms[name], g, set(b.production_methods))
                    self.assertNotEqual(st.status, STATUS_LOCKED,
                                        "%s@%s 被判 locked: %s" % (b.type, name, st.reasons))

    def test_tech_gate(self):
        ck = PMChecker(self.gd, self.snap)
        rep = ck.building_report("building_food_industry")
        base = {s.pm: s for g in rep.groups for s in g.statuses}
        self.assertEqual(base["pm_baking_powder"].status, STATUS_LOCKED)
        self.assertIn("baking_powder", base["pm_baking_powder"].missing_techs)

        ck2 = PMChecker(self.gd, self.snap, extra_techs={"baking_powder"})
        rep2 = ck2.building_report("building_food_industry")
        base2 = {s.pm: s for g in rep2.groups for s in g.statuses}
        self.assertEqual(base2["pm_baking_powder"].status, STATUS_AVAILABLE)

    def test_pm_prerequisite(self):
        pm = self.gd.pms["pm_elastics"]
        ck = PMChecker(self.gd, self.snap, extra_techs={"vulcanization"})
        st = ck.evaluate(pm, "pmg_luxury_building_textile_mill", set())
        self.assertEqual(st.status, STATUS_LOCKED)
        st2 = ck.evaluate(pm, "pmg_luxury_building_textile_mill",
                          {"pm_sewing_machines"})
        self.assertNotEqual(st2.status, STATUS_LOCKED)

    def test_law_gate(self):
        snap = copy.deepcopy(self.snap)
        snap.laws.append(LawRow(name="law_command_economy",
                                group="lawgroup_economic_system"))
        ck = PMChecker(self.gd, snap)
        st = ck.evaluate(self.gd.pms["pm_company_headquarter_privately_owned"],
                         "pmg_company_headquarter", set())
        self.assertEqual(st.status, STATUS_LOCKED)
        self.assertIn("law_command_economy", st.blocked_laws)

    def test_geographic_gate_is_conditional_without_mapping(self):
        ck = PMChecker(self.gd, self.snap)
        st = ck.evaluate(self.gd.pms["pm_herring_meal_farming"],
                         "pmg_base_building_fishing_wharf", set())
        self.assertIn(st.status, ("conditional", "locked"))

    # -- 方案 ---------------------------------------------------------
    def test_scenario_delta_and_no_mutation(self):
        before = [list(b.production_methods) for b in self.snap.buildings]
        base = gdp_of(self.gd, self.snap, price="base", scale="staffing")
        d = pm_delta(self.gd, self.snap, "building_tea_plantation",
                     "pmg_tea_exploitation", "worker_exploitation_tea",
                     price="base", scale="staffing", base=base)
        self.assertNotEqual(d.delta, 0.0)
        after = [list(b.production_methods) for b in self.snap.buildings]
        self.assertEqual(before, after)      # 原快照不能被改

        # 从零重算一遍，确认增量一致
        work = copy.deepcopy(self.snap)
        for b in work.buildings:
            if b.type == "building_tea_plantation":
                b.production_methods = [
                    "worker_exploitation_tea" if p == "default_labour" else p
                    for p in b.production_methods]
        new = gdp_of(self.gd, work, price="base", scale="staffing")
        self.assertAlmostEqual(new.annual - base.annual, d.delta, places=3)


class TestWatchOffline(unittest.TestCase):
    """不依赖游戏的 watch 循环测试：用假进程 + 固定快照跑 2 轮。"""

    def test_watch_writes_json_and_history(self):
        import argparse
        import tempfile
        from unittest import mock

        from v3calc import cli as vcli
        from v3calc.model import Budget, MarketRow

        snap = Snapshot(
            country=Country(id=1, tag="TST", population=1000, treasury=100,
                            budget=Budget(balance=5.0)),
            states=[StateRow(id=1)],
            buildings=[BuildingRow(type="building_wheat_farm", state_id=1,
                                   levels=10, staffing=10, throughput=1.0,
                                   production_methods=["pm_simple_farming"])],
            market=MarketRow(),
        )

        class FakeProc:
            pid = 1

            def close(self):
                pass

        td = str(ROOT / "tests")
        jp = os.path.join(td, "_watch_test_snap.json")
        hp = os.path.join(td, "_watch_test_hist.csv")
        for p in (jp, hp):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            args = argparse.Namespace(
                game_dir=GAME, mod_dir=[], country=None, save=None, snapshot=None,
                json=jp, quiet=True, pid=None, interval=0.01, rounds=2,
                history=hp, no_gdp=False, no_prewarm=True, target=85100000,
                price="market", scale="staffing", buildings="producers")
            with mock.patch("v3live.proc.attach", return_value=FakeProc()), \
                    mock.patch("v3calc.live.LiveReader.read_with",
                               return_value=copy.deepcopy(snap)):
                rc = vcli.cmd_watch(args)
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(jp))
            with open(hp, encoding="utf-8") as f:
                lines = f.read().strip().split("\n")
            self.assertEqual(len(lines), 3)          # header + 2 rounds
            self.assertIn("gdp_annual", lines[0])
        finally:
            for p in (jp, hp):
                try:
                    os.remove(p)
                except OSError:
                    pass

class TestMenuCache(unittest.TestCase):
    """主菜单检测用的缓存计数/清理（不依赖游戏）。"""

    def test_empty_country_cache_is_not_loaded_and_purged(self):
        from v3calc import cli as vcli
        pid = 987654321
        path = os.path.join(vcli._cache_dir(),
                            "CCountry_p_%d_deadbeef.json" % pid)
        os.makedirs(vcli._cache_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"addrs": []}, f)
        try:
            self.assertEqual(vcli._cache_count(pid, "CCountry"), 0)
            vcli._purge_pid_caches(pid)
            self.assertIsNone(vcli._cache_count(pid, "CCountry"))
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


class TestFastScan(unittest.TestCase):
    """并行扫描与 v3live 单线程结果必须一致。"""

    def test_parallel_matches_serial(self):
        import struct

        from v3calc import fastscan
        from v3live import discover

        base = 0x10000000
        mem = bytearray(1 << 20)
        va = 0x7FF600001000
        vb = 0x7FF600002000
        mem[0x100:0x108] = struct.pack("<Q", va)
        mem[0x10008:0x10010] = struct.pack("<Q", vb)
        mem[0x80000:0x80008] = struct.pack("<Q", va)

        class FakeProc:
            def regions(self, private_only=False):
                return [(base, len(mem), 0x04, 0x20000)]

            def read(self, addr, size):
                off = addr - base
                return bytes(mem[off:off + size])

        mapping = {va: "A", vb: "B"}
        p = FakeProc()
        regions = p.regions()
        serial = discover._scan_numpy(p, mapping, regions, progress=False)
        fast = fastscan.scan_instances(p, mapping, regions, progress=False,
                                       workers=2)
        self.assertEqual(sorted(serial.get("A", [])), sorted(fast.get("A", [])))
        self.assertEqual(sorted(serial.get("B", [])), sorted(fast.get("B", [])))
        self.assertEqual(len(fast["A"]), 2)
        self.assertEqual(len(fast["B"]), 1)


class TestWorldRegistry(unittest.TestCase):
    """州注册表判定的内存布局测试（假进程）。"""

    def _proc(self, mem, base=0):
        import struct as _s

        class P:
            def read_u64(self, addr):
                return _s.unpack_from("<Q", mem, addr - base)[0]

            def read_u32(self, addr):
                return _s.unpack_from("<I", mem, addr - base)[0]

            def read(self, addr, n):
                off = addr - base
                return bytes(mem[off:off + n])

        return P()

    def test_state_registry_detected(self):
        import struct as _s

        from v3calc.worldstate import WorldProbe

        base = 0
        mem = bytearray(0x10000)
        # global -> registry -> entries
        _s.pack_into("<Q", mem, 0x1000, 0x2000)
        _s.pack_into("<Q", mem, 0x2000 + 0x20, 0x3000)
        _s.pack_into("<I", mem, 0x2000 + 0x2C, 4)
        # entry[1] -> state object whose vtable is 0x9000
        _s.pack_into("<Q", mem, 0x3000 + 16 + 8, 0x4000)
        _s.pack_into("<Q", mem, 0x4000, 0x9000)
        p = self._proc(mem)
        probe = WorldProbe([0x1000], [0x9000], 0, p)
        ws = probe.probe()
        self.assertTrue(ws.in_save)
        self.assertEqual(ws.capacity, 4)
        self.assertEqual(ws.states, 1)
        self.assertEqual(ws.first_state, 0x4000)

    def test_country_registry_is_ignored(self):
        import struct as _s

        from v3calc.worldstate import WorldProbe

        mem = bytearray(0x12000)
        # first global: a country-like registry (wrong vtable)
        _s.pack_into("<Q", mem, 0x1000, 0x2000)
        _s.pack_into("<Q", mem, 0x2000 + 0x20, 0x3000)
        _s.pack_into("<I", mem, 0x2000 + 0x2C, 4)
        _s.pack_into("<Q", mem, 0x3000 + 8, 0x4000)
        _s.pack_into("<Q", mem, 0x4000, 0x9100)       # not CState
        # second global: real state registry
        _s.pack_into("<Q", mem, 0x1008, 0x5000)
        _s.pack_into("<Q", mem, 0x5000 + 0x20, 0x6000)
        _s.pack_into("<I", mem, 0x5000 + 0x2C, 4)
        _s.pack_into("<Q", mem, 0x6000 + 16 + 8, 0x7000)
        _s.pack_into("<Q", mem, 0x7000, 0x9000)
        probe = WorldProbe([0x1000, 0x1008], [0x9000], 0, self._proc(mem))
        ws = probe.probe()
        self.assertTrue(ws.in_save)
        self.assertEqual(ws.first_state, 0x7000)

    def test_null_global_is_menu(self):
        from v3calc.worldstate import WorldProbe
        mem = bytearray(0x2000)
        ws = WorldProbe([0x1000], [0x9000], 0, self._proc(mem)).probe()
        self.assertFalse(ws.in_save)
        self.assertEqual(ws.capacity, 0)

    @unittest.skipUnless(
        GAME and os.path.isfile(os.path.join(
            os.path.dirname(GAME), "binaries", "victoria3.exe")),
        "没有 victoria3.exe")
    def test_resolver_and_vtable_discovery(self):
        from v3calc.worldstate import find_global_rvas, state_vt_rvas
        exe = os.path.join(os.path.dirname(GAME), "binaries", "victoria3.exe")
        self.assertGreater(len(find_global_rvas(exe)), 0)
        self.assertGreater(len(state_vt_rvas(exe)), 0)


class TestSynthetic(unittest.TestCase):
    """不依赖存档/游戏的纯逻辑测试。"""

    def _gd(self):
        if not GAME:
            self.skipTest("没有游戏目录")
        return GameData(GAME)

    def test_live_reader_cli_symbols_exist(self):
        """live.py 里引用的所有 cli.* 必须在 v3live 里存在（防偏移常量拼错）。"""
        import re
        from v3live import cli as live_cli
        src = (ROOT / "v3calc" / "live.py").read_text(encoding="utf-8")
        names = set(re.findall(r"cli\.([A-Za-z_]\w*)", src))
        missing = sorted(n for n in names if not hasattr(live_cli, n))
        self.assertEqual(missing, [], "live.py 引用了不存在的 cli 符号: %s" % missing)

    def test_live_reader_constructs(self):
        from v3calc.live import LiveReader
        r = LiveReader(country_id=157)
        self.assertEqual(r.country_id, 157)

    def test_empty_snapshot(self):
        gd = self._gd()
        snap = Snapshot(country=Country(id=1, tag="TEST", population=1000),
                        states=[StateRow(id=1)])
        snap.buildings = [BuildingRow(
            type="building_wheat_farm", state_id=1, levels=10, staffing=10,
            throughput=1.0, production_methods=["pm_simple_farming"])]
        calc = GDPCalculator(gd, snap)
        r = calc.compute(price="base", scale="throughput")
        self.assertGreater(r.weekly, 0)
        self.assertEqual(r.population, 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
