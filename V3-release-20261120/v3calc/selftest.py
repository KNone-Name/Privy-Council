# -*- coding: utf-8 -*-
"""决策计算层自带测试（不需要测试框架之外的东西）。

    python -m v3calc selftest -v
    python -m unittest v3calc.selftest -v
    python -m unittest discover -s tests -v      # 若把本文件并入 tests

覆盖：
  * 价格模型对真实快照的复现（54 商品，误差 0；gold 例外）；
  * 价格函数的边界（触底/触顶/无买卖单）；
  * 产业链覆盖全部商品；
  * 候选包：可建性、价格反馈方向、增量价值、部分均衡陷阱防护；
  * 逐步贪心：预算与去重约束；
  * 生产方式：只给允许项、建造力价值；
  * 信号：财政/价格异常能被识别；
  * 简报：字符预算、关键段落；
  * 台账：记账 → 对账 → 偏差；
  * 意图：权重与禁令。
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from .candidates import (CAPACITY_CHAIN, CONSTRUCTION_SECTOR, CandidateEngine,
                         Intent, unit_goods)
from .construction import ConstructionModel, load_state_meta
from .gamedata import GameData, discover_game_dir
from .ledger import Ledger
from .market import (CHAIN_GOODS, PRICE_MAX, PRICE_MIN, PriceModel,
                     price_multiplier, unknown_goods)
from .model import Snapshot
from .signals import scan

PROJ = Path(__file__).resolve().parent.parent
LIVE = PROJ / "snap_live.json"
EXAMPLE = PROJ / "examples" / "snapshot_chi_1836.json"
GAME = discover_game_dir()

_GD: GameData | None = None


def gd() -> GameData:
    global _GD
    if _GD is None:
        _GD = GameData(GAME)
    return _GD


class TestPriceFunction(unittest.TestCase):
    """价格公式本身（不依赖快照）。"""

    def test_balance_is_base(self):
        self.assertAlmostEqual(price_multiplier(100, 100), 1.0)

    def test_zero_both(self):
        self.assertAlmostEqual(price_multiplier(0, 0), 1.0)

    def test_no_demand_is_floor(self):
        self.assertAlmostEqual(price_multiplier(10, 0), PRICE_MIN)

    def test_no_supply_is_ceiling(self):
        self.assertAlmostEqual(price_multiplier(0, 10), PRICE_MAX)

    def test_clamped(self):
        self.assertAlmostEqual(price_multiplier(1, 1000), PRICE_MAX)
        self.assertAlmostEqual(price_multiplier(1000, 1), PRICE_MIN)

    def test_asymmetric_denominator(self):
        # 分母取较小的一方：供需 2:1 与 1:2 对称
        up = price_multiplier(100, 200)
        down = price_multiplier(200, 100)
        self.assertAlmostEqual(up + down, 2.0)


@unittest.skipUnless(GAME, "没有找到游戏安装目录")
class TestChains(unittest.TestCase):
    def test_all_goods_mapped(self):
        self.assertEqual(unknown_goods(gd()), [])

    def test_chain_partition(self):
        seen: dict[str, str] = {}
        for chain, goods in CHAIN_GOODS.items():
            for g in goods:
                self.assertNotIn(g, seen, "商品被分到两个链")
                seen[g] = chain
        self.assertEqual(len(seen), len(gd().goods))


@unittest.skipUnless(GAME and LIVE.exists(), "需要 snap_live.json 与游戏目录")
class TestPriceAgainstSnapshot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = Snapshot.load(str(LIVE))
        cls.model = PriceModel.from_snapshot(gd(), cls.snap)

    def test_reproduces_market_prices(self):
        """价格模型必须逐项复现快照里的市价（gold 除外）。"""
        bad = []
        snap_prices = self.snap.market.prices
        for g in gd().goods:
            if g.name == "gold":
                continue
            want = snap_prices.get(g.name)
            if not want:
                continue
            got = self.model.price(g.name)
            if abs(got - want) > 1e-3:
                bad.append((g.name, want, got))
        self.assertEqual(bad, [])

    def test_positions_have_orders(self):
        self.assertTrue(self.model.has_orders)
        pos = self.model.position("clothes")
        self.assertGreater(pos.supply, 0)
        self.assertGreater(pos.demand, 0)


@unittest.skipUnless(GAME, "没有找到游戏安装目录")
class TestUnitGoods(unittest.TestCase):
    def test_textile_mill(self):
        ug = unit_goods(gd(), "building_textile_mill")
        net = ug.net(1)
        self.assertAlmostEqual(net["clothes"], 45.0)
        self.assertAlmostEqual(net["fabric"], -40.0)

    def test_levels_scale(self):
        ug = unit_goods(gd(), "building_textile_mill")
        self.assertAlmostEqual(ug.net(3)["clothes"], 135.0)


@unittest.skipUnless(GAME and LIVE.exists(), "需要 snap_live.json 与游戏目录")
class TestCandidates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = Snapshot.load(str(LIVE))
        cls.geo = load_state_meta()
        cls.eng = CandidateEngine(gd(), cls.snap, geo=cls.geo, intent=Intent())

    def _a_state(self) -> int:
        return self.snap.states[0].id

    def test_price_feedback_direction(self):
        sid = self._a_state()
        opt = self.eng.evaluate_build(sid, "building_textile_mill", 1)
        self.assertTrue(opt.warnings is not None)
        # 建纺织厂：衣服价格被压下去，布料需求上升→价格上去
        self.assertLess(opt.price_changes.get("clothes", 0.0), 0.0)
        self.assertGreater(opt.price_changes.get("fabric", 0.0), 0.0)

    def test_values_and_weeks(self):
        opt = self.eng.evaluate_build(self._a_state(), "building_textile_mill", 1)
        self.assertGreater(opt.weekly_value, 0)
        self.assertGreater(opt.points, 0)
        self.assertGreater(opt.weeks or 0, 0)
        self.assertGreater(opt.per_point, 0)

    def test_constraint_blocks(self):
        # 不存在的州
        bad = self.eng.evaluate_build(999999, "building_textile_mill", 1)
        self.assertEqual(bad.score, float("-inf"))
        # 自给农场不可建
        sub = self.eng.evaluate_build(self._a_state(), "building_subsistence_farm", 1)
        self.assertEqual(sub.score, float("-inf"))
        # 没科技的建筑
        techs = self.snap.tech_set()
        locked = [b for b in self.eng._buildable_catalog()
                  if gd().buildings[b].unlocking_technologies
                  and not set(gd().buildings[b].unlocking_technologies) & techs]
        if locked:
            o = self.eng.evaluate_build(self._a_state(), locked[0], 1)
            self.assertEqual(o.score, float("-inf"))

    def test_plan_respects_budget(self):
        budget = 400.0
        plan = self.eng.plan(budget, max_items=4)
        self.assertLessEqual(plan.total_points, budget + 1e-6)
        self.assertLessEqual(len(plan.items), 4)
        counts: dict[tuple, int] = {}
        for o in plan.items:
            counts[(o.state_id, o.building)] = \
                counts.get((o.state_id, o.building), 0) + 1
        self.assertLessEqual(max(counts.values()), 2)

    def test_plan_reduces_second_pick_value(self):
        """同类第二座的价值必须低于第一座（价格反馈生效）。"""
        sid = self._a_state()
        u = self.eng.unit("building_textile_mill")
        m1 = self.eng.model
        first = self.eng.evaluate_build(sid, "building_textile_mill", 1, m1)
        m2 = m1.shifted({g: (o, i) for g, (o, i) in
                         self.eng._deltas(u, 1).items()})
        second = self.eng.evaluate_build(sid, "building_textile_mill", 1, m2)
        self.assertLess(second.annual_value, first.annual_value)

    def test_capacity_is_cost_only(self):
        """建造力不估值：只给成本与可控性判定。"""
        sid = self._a_state()
        opt = self.eng.evaluate_build(sid, CONSTRUCTION_SECTOR, 1)
        self.assertEqual(opt.chain, CAPACITY_CHAIN)
        self.assertEqual(opt.value_basis, "capacity_cost")
        self.assertEqual(opt.annual_value, 0.0)
        self.assertEqual(opt.score, 0.0)
        self.assertGreater(opt.capacity_add or 0, 0)
        self.assertGreater(opt.weekly_upkeep or 0, 0)
        self.assertIn("budget", opt.affordable)
        self.assertIn("cash", opt.affordable)
        self.assertIn("upkeep", opt.affordable)

    def test_capacity_not_in_ranked_candidates(self):
        rows = self.eng.build_options(limit=None)
        self.assertTrue(rows)
        self.assertNotIn(CONSTRUCTION_SECTOR, {o.building for o in rows})

    def test_capacity_affordability(self):
        sid = self._a_state()
        tight = self.eng.evaluate_capacity(sid, 1, budget_points=1.0)
        self.assertFalse(tight.affordable["budget"])
        self.assertFalse(tight.ok)
        loose = self.eng.evaluate_capacity(sid, 1, budget_points=10_000.0,
                                           balance=10_000_000.0)
        self.assertTrue(loose.affordable["budget"])
        self.assertTrue(loose.ok)

    def test_capacity_pm_ranking_is_cost_order(self):
        rows = self.eng.capacity_pm_options()
        self.assertTrue(rows)
        costs = [r["cost_per_capacity"] for r in rows]
        self.assertEqual(costs, sorted(costs))
        self.assertTrue(all(r["capacity_add"] > 0 for r in rows))

    def test_delta_value_blocks_contraction_trick(self):
        """减产不能因为"价格暴涨"而被算成增值（部分均衡陷阱）。"""
        model = PriceModel(gd(), {"steel": 50.0}, {"steel": 1000.0},
                           {"steel": 1000.0})
        after = model.shifted({"steel": (-500.0, 0.0)})
        self.assertGreater(after.price("steel"), model.price("steel"))
        d = CandidateEngine._delta_value(model, after, {"steel": -500.0}, {})
        self.assertLess(d, 0.0)

    def test_pm_options_allowed_only(self):
        pms = self.eng.pm_options(limit=10)
        for p in pms:
            self.assertTrue(p.pm)
            self.assertNotIn(p.pm, p.current)

    def test_construction_capacity_positive(self):
        cm = ConstructionModel(gd(), self.snap)
        self.assertGreater(cm.capacity_per_week, 0)
        self.assertGreater(cm.cost_points("building_textile_mill", 1), 0)
        self.assertGreater(cm.cost_points(CONSTRUCTION_SECTOR, 1), 0)


@unittest.skipUnless(GAME and LIVE.exists(), "需要 snap_live.json 与游戏目录")
class TestSignalsAndBrief(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = Snapshot.load(str(LIVE))
        cls.geo = load_state_meta()

    def test_signals_scan(self):
        sig = scan(gd(), self.snap)
        self.assertIsInstance(sig, list)
        for s in sig:
            self.assertIn(s.severity, (1, 2, 3))
            self.assertTrue(s.title)

    def test_deficit_detected(self):
        from .model import Budget, Country, Snapshot as S

        snap = S.load(str(LIVE))
        snap.country = Country(id=1, treasury=-1000.0, credit_limit=1000.0,
                               loan_principal=1000.0,
                               budget=Budget(balance=-500.0))
        kinds = {s.kind for s in scan(gd(), snap)}
        self.assertIn("deficit", kinds)

    def test_brief_char_budget(self):
        from .brief import build_brief, render_brief

        brief = build_brief(gd(), self.snap, geo=self.geo, top=6)
        text = render_brief(brief, 1500)
        self.assertLessEqual(len(text), 1500)
        self.assertIn("候选包", text)
        self.assertIn("市场", text)

    def test_brief_is_jsonable(self):
        from .brief import build_brief

        brief = build_brief(gd(), self.snap, geo=self.geo, top=4)
        json.dumps(brief, ensure_ascii=False)


@unittest.skipUnless(GAME, "没有找到游戏安装目录")
class TestSaveModeDegrades(unittest.TestCase):
    """存档模式没有买卖单：价格反馈关掉，但不能崩。"""

    @unittest.skipUnless(EXAMPLE.exists(), "缺少示例快照")
    def test_save_snapshot(self):
        snap = Snapshot.load(str(EXAMPLE))
        model = PriceModel.from_snapshot(gd(), snap)
        self.assertFalse(model.has_orders)
        self.assertGreater(model.price("clothes"), 0)
        eng = CandidateEngine(gd(), snap, geo=load_state_meta())
        opts = eng.build_options(limit=5)
        self.assertTrue(opts)
        for o in opts:
            self.assertNotEqual(o.score, float("-inf"))


@unittest.skipUnless(GAME, "没有找到游戏安装目录")
class TestLedger(unittest.TestCase):
    def setUp(self):
        # 直接写在已存在的 state 目录下：临时目录在沙箱里可能不可写
        import uuid
        self.dir = PROJ / "v3calc" / "state"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = str(self.dir / ("_selftest_%s.jsonl" % uuid.uuid4().hex[:10]))

    def tearDown(self):
        for p in (self.path, self.path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    @unittest.skipUnless(EXAMPLE.exists(), "缺少示例快照")
    def test_record_and_reconcile(self):
        snap = Snapshot.load(str(EXAMPLE))
        plan = _FakePlan(1000.0)
        led = Ledger(self.path)
        rec = led.record_plan(plan, snap, gd())
        self.assertEqual(rec["status"], "open")
        self.assertEqual(len(led.records()), 1)

        # 同一份快照对账：实际增量 0 → 偏差 0
        rep = led.reconcile(snap, gd())
        self.assertEqual(rep["n_open"], 1)
        self.assertAlmostEqual(rep["rows"][0]["bias"], 0.0)
        self.assertEqual(led.summary()["n_done"], 1)

    def test_corrupt_line_skipped(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{bad json\n")
        self.assertEqual(Ledger(self.path).records(), [])


class _FakeOption:
    def __init__(self, annual):
        self.state_id = 1
        self.building = "building_textile_mill"
        self.levels = 1
        self.points = 600.0
        self.annual_value = annual
        self.weekly_value = annual / 52.0
        self.price_changes = {"clothes": -0.5}


class _FakePlan:
    def __init__(self, annual):
        self.items = [_FakeOption(annual)]
        self.total_points = 600.0
        self.total_annual_value = annual
        self.budget_points = 600.0
        self.capacity_per_week = 20.0
        self.weeks = 30.0


@unittest.skipUnless(GAME, "没有找到游戏安装目录")
class TestIntent(unittest.TestCase):
    def test_weights_and_bans(self):
        it = Intent(priority_chains=["纺织"], ban_buildings=["building_barrack"],
                    ban_pm_patterns=["worker_exploitation"],
                    target_states=[1, 2])
        self.assertAlmostEqual(it.weight_for("纺织"), it.chain_weight)
        self.assertAlmostEqual(it.weight_for("矿产"), 1.0)
        self.assertFalse(it.allow_building("building_barrack"))
        self.assertTrue(it.allow_building("building_textile_mill"))
        self.assertFalse(it.allow_pm("worker_exploitation_tea"))
        self.assertTrue(it.allow_pm("pm_sewing_machines"))
        self.assertTrue(it.state_ok(1))
        self.assertFalse(it.state_ok(9))

    def test_from_dict_ignores_unknown(self):
        it = Intent.from_dict({"name": "x", "priority_chains": ["军需"],
                               "nonsense": 1})
        self.assertEqual(it.name, "x")
        self.assertEqual(it.priority_chains, ["军需"])

    def test_intent_filters_engine(self):
        if not LIVE.exists():
            self.skipTest("需要 snap_live.json")
        snap = Snapshot.load(str(LIVE))
        it = Intent(ban_buildings=["building_construction_sector"])
        eng = CandidateEngine(gd(), snap, geo=load_state_meta(), intent=it)
        for o in eng.build_options(limit=10):
            self.assertNotEqual(o.building, CONSTRUCTION_SECTOR)
