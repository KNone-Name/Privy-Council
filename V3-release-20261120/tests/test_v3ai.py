# -*- coding: utf-8 -*-
"""v3ai 离线测试。

思路：**这个项目服务的是"当前玩家国"，不是任何一个具体国家**，所以测试分两层：

1. **契约层（``TestContract``）**：不假设国名/州 id/科技年代，只断言"无论哪份快照
   都必须成立"的性质——国家 id 与快照一致、GDP 为正、候选必须落在真实州/真实建筑、
   不可建的必须被拒、advise 必须存在且可解析。默认跑**所有可用夹具**：
   大清 1836（存档模式，无买卖单）＋ 俄罗斯 live 快照（有买卖单，走价格反馈）。
2. **金标层（``TestQingGolden``）**：只针对 1836 大清这份固定夹具的具体断言
   （州 914/907 的资源、1836 年没有缝纫机科技……）。它验证的是"历史时点语义"，
   不是"代码只支持大清"。

夹具选择：``V3_SNAPSHOT`` 只影响**契约层**要跑哪些快照（金标层始终用大清示例，
因为它验证的是那个历史时点的语义）；不设时自动收集
``examples/snapshot_chi_1836.json`` 与根目录 ``snap_live.json``（存在才跑）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v3ai.agent import AgentLoop
from v3ai.config import Config
from v3ai.executor import BridgeExecutor, DryRunExecutor
from v3ai.llm import LLMClient, MockLLM, tool_call
from v3ai.mcp_server import MCPServer
from v3ai.review import Command
from v3ai.runtime import Runtime
from v3ai.strategy import Strategy, StrategyStore
from v3ai.tools import build_registry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QING = os.path.join(ROOT, "examples", "snapshot_chi_1836.json")
LIVE = os.path.join(ROOT, "snap_live.json")
TEST_TMP_ROOT = os.path.join(ROOT, "v3ai", "state", "_test_tmp")
os.makedirs(TEST_TMP_ROOT, exist_ok=True)


def fixtures() -> list[str]:
    """可用夹具：显式指定优先，否则自动收集（缺失就少跑，不假绿）。"""
    env = os.environ.get("V3_SNAPSHOT")
    if env:
        return [env] if os.path.isfile(env) else []
    out = []
    for p in (QING, LIVE):
        if os.path.isfile(p):
            out.append(p)
    return out


def _slug(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0][:40]


def _fixed_dir(name: str) -> str:
    # 不用 tempfile 子目录：DSH 沙箱对临时目录 ACL 可能拒绝写入。
    d = os.path.join(TEST_TMP_ROOT, name)
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    return d


_RUNTIMES: dict[tuple[str, str], Runtime] = {}


def runtime_for(path: str, mode: str = "dry_run") -> Runtime:
    """按（夹具, 执行模式）缓存 Runtime；每次复用前清空待执行。"""
    key = (path, mode)
    rt = _RUNTIMES.get(key)
    if rt is not None:
        rt.pending.clear()
        rt._sync_pending()
        return rt
    cfg = Config.load()
    cfg.set("agent.max_actions_per_cycle", 8)
    cfg.set("agent.max_levels_per_action", 5)
    cfg.set("agent.max_tool_rounds", 8)
    d = os.path.join(TEST_TMP_ROOT, _slug(path) + "_" + mode)
    os.makedirs(d, exist_ok=True)
    cfg.set("paths.state_dir", d)
    cfg.set("paths.journal", os.path.join(d, "journal.jsonl"))
    rt = Runtime(cfg, snapshot_path=path, source="file", mode=mode)
    _RUNTIMES[key] = rt
    return rt


def tearDownModule() -> None:
    for rt in _RUNTIMES.values():
        try:
            rt.close()
        except Exception:                              # noqa: BLE001
            pass
    _RUNTIMES.clear()


def _feasible(rt: Runtime) -> tuple[int, str]:
    """从当前快照里挑一个真的可建的目标（不写死州/建筑）。"""
    rows = rt.ctx.build_candidates(top=1)
    if not rows:
        raise unittest.SkipTest("该快照没有正收益候选")
    return int(rows[0]["state_id"]), str(rows[0]["building"])


def _subsistence(rt: Runtime) -> str:
    for b in rt.ctx.snap.building_types():
        if b.startswith("building_subsistence"):
            return b
    return "building_subsistence_farm"


# ---------------------------------------------------------------------------
# 1. 契约层：任何玩家国都必须满足
# ---------------------------------------------------------------------------
@unittest.skipUnless(fixtures(), "需要至少一份快照夹具")
class TestContract(unittest.TestCase):
    def test_overview_matches_snapshot(self):
        for path in fixtures():
            with self.subTest(fixture=_slug(path)):
                rt = runtime_for(path)
                snap = rt.snap
                ov = rt.overview()
                self.assertIsNotNone(snap.country, "玩家国解析失败")
                self.assertEqual(ov["meta"]["country_id"], snap.country.id)
                self.assertEqual(ov["meta"]["states"], len(snap.states))
                self.assertGreater(ov["gdp"]["annual"], 0)
                self.assertIn(ov["gdp"]["price_basis"], ("market", "base"))
                self.assertIn("budget", ov)
                self.assertIn("market", ov)

    def test_brief_and_advise(self):
        for path in fixtures():
            with self.subTest(fixture=_slug(path)):
                rt = runtime_for(path)
                b = rt.brief()
                adv = b.get("advise") or {}
                self.assertTrue(adv.get("text"), "advise.text 为空（计算层没接上）")
                self.assertLessEqual(len(adv["text"]), 2600)
                self.assertIn("候选包", adv["text"])
                self.assertIsInstance(adv.get("signals"), list)
                for it in (adv.get("plan") or {}).get("items") or []:
                    self.assertIn(it["state_id"], {s.id for s in rt.snap.states})
                    self.assertIn(it["building"], rt.gd.buildings)
                    self.assertGreater(it["annual_value"], 0)

    def test_candidates_are_grounded(self):
        """候选必须落在真实州、真实建筑上，且数字自洽。"""
        for path in fixtures():
            with self.subTest(fixture=_slug(path)):
                rt = runtime_for(path)
                states = {s.id for s in rt.snap.states}
                rows = rt.ctx.build_candidates(top=8)
                self.assertTrue(rows, "没有候选；检查可建性与数据")
                for r in rows:
                    self.assertIn(r["state_id"], states)
                    self.assertIn(r["building"], rt.gd.buildings)
                    self.assertGreaterEqual(r["risk"], 0.0)
                    self.assertLessEqual(r["risk"], 1.0)
                    self.assertGreater(r["points"], 0)
                    # 旧协议键必须保留（one-shot 提示词依赖）
                    self.assertIn("remaining", r)
                    self.assertIn("gdp_per_level", r)

    def test_estimate_on_real_candidate(self):
        for path in fixtures():
            with self.subTest(fixture=_slug(path)):
                rt = runtime_for(path)
                sid, btype = _feasible(rt)
                est = rt.ctx.estimate_build(sid, btype, 1)
                self.assertGreater(est["gdp_delta"], 0)
                self.assertIn("price_feedback", est.get("assumptions") or {})
                self.assertGreater(est["construction"]["cost_points"], 0)

    def test_infeasible_actions_rejected(self):
        for path in fixtures():
            with self.subTest(fixture=_slug(path)):
                rt = runtime_for(path)
                rt.pending.clear()
                rt._sync_pending()
                # 自给农场：任何国家都不能主动建
                bad = rt.propose({"kind": "build", "state_id": _feasible(rt)[0],
                                  "building": _subsistence(rt), "levels": 1})
                self.assertFalse(bad.approved)
                # 不存在的州
                bad2 = rt.propose({"kind": "build", "state_id": 999999,
                                   "building": "building_farm", "levels": 1})
                self.assertFalse(bad2.approved)

    def test_advise_tools(self):
        for path in fixtures():
            with self.subTest(fixture=_slug(path)):
                rt = runtime_for(path)
                reg = build_registry()
                brief = reg.call(rt, "advise_brief", {"top": 4})
                self.assertTrue(brief.get("ok"), brief.get("error"))
                self.assertTrue(brief.get("text"))
                sig = reg.call(rt, "advise_signals", {})
                self.assertTrue(sig.get("ok"))
                plan = reg.call(rt, "advise_plan", {"max_items": 3})
                self.assertTrue(plan.get("ok"))
                self.assertIn("plan", plan)

    def test_freeze_blocked(self):
        rt = runtime_for(fixtures()[0])
        reg = build_registry()
        rt.begin_cycle()
        try:
            self.assertTrue(rt.cycle_active)
            self.assertFalse(reg.call(rt, "refresh_world", {}).get("ok"))
            self.assertFalse(reg.call(rt, "execute_pending", {}).get("ok"))
        finally:
            rt.end_cycle()


# ---------------------------------------------------------------------------
# 2. 动态流程：建造 / 审查 / PM —— 不写死国家数据
# ---------------------------------------------------------------------------
@unittest.skipUnless(fixtures(), "需要至少一份快照夹具")
class TestFlows(unittest.TestCase):
    def setUp(self):
        self.path = fixtures()[0]
        self.rt = runtime_for(self.path)
        self.rt.pending.clear()
        self.rt._sync_pending()

    def test_build_approved_and_duplicate_rejected(self):
        sid, btype = _feasible(self.rt)
        ok = self.rt.propose({"kind": "build", "state_id": sid,
                              "building": btype, "levels": 1})
        self.assertTrue(ok.approved, ok.reasons)
        dup = self.rt.propose({"kind": "build", "state_id": sid,
                               "building": btype, "levels": 1})
        self.assertFalse(dup.approved)

    def test_level_cap_enforced(self):
        sid, btype = _feasible(self.rt)
        q = self.rt.propose({"kind": "build", "state_id": sid,
                             "building": btype, "levels": 99})
        self.assertFalse(q.approved)

    def test_pm_already_and_available(self):
        """用当前快照里真实存在的 PM：已启用的必须被拒，可用的必须放行。"""
        ctx = self.rt.ctx
        checker = ctx.pm_checker()
        available = None
        active = None
        exploitation = None
        for btype in ctx.snap.building_types():
            rep = checker.building_report(btype)
            rows = [b for b in ctx.snap.buildings if b.type == btype]
            if not rows:
                continue
            for g in rep.groups:
                for st in g.statuses:
                    if st.status == "active" and active is None:
                        active = (rows[0].state_id, btype, st.pm)
                    if st.status == "available" and st.pm.startswith("pm_") \
                            and available is None:
                        available = (rows[0].state_id, btype, st.pm)
                    if st.status == "available" \
                            and "worker_exploitation" in st.pm \
                            and exploitation is None:
                        exploitation = (rows[0].state_id, btype, st.pm)
            if available and active and exploitation:
                break
        self.assertTrue(active, "找不到已启用的 PM（夹具异常）")
        sid, btype, pm = active
        res = self.rt.propose({"kind": "pm", "state_id": sid, "building": btype,
                               "pm": pm})
        self.assertFalse(res.approved)
        self.assertIn("already", res.code)
        if available:
            sid2, btype2, pm2 = available
            ok = self.rt.propose({"kind": "pm", "state_id": sid2,
                                  "building": btype2, "pm": pm2})
            self.assertTrue(ok.approved, ok.reasons)
        if exploitation:
            # worker_exploitation_* 不以 pm_ 开头，但它是合法 PM：
            # 审查层应给"风险警告"，而不是 schema 拒绝。
            sid3, btype3, pm3 = exploitation
            self.rt.pending.clear()
            self.rt._sync_pending()
            r = self.rt.propose({"kind": "pm", "state_id": sid3,
                                 "building": btype3, "pm": pm3})
            self.assertNotEqual(r.code, "schema", r.reasons)

    def test_dry_run_executes(self):
        sid, btype = _feasible(self.rt)
        self.rt.propose({"kind": "build", "state_id": sid,
                         "building": btype, "levels": 1})
        out = self.rt.execute_pending()
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].ok)
        self.assertEqual(out[0].status, "dry_run")
        self.assertEqual(len(self.rt.pending), 0)

    def test_mcp_lists_advise_tools(self):
        s = MCPServer(runtime_factory=lambda: self.rt)
        init = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {}})
        self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")
        tools = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                          "params": {}})
        names = {t["name"] for t in tools["result"]["tools"]}
        self.assertGreater(len(names), 20)
        self.assertIn("advise_brief", names)
        call = s.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "get_overview", "arguments": {}}})
        self.assertFalse(call["result"]["isError"])


# ---------------------------------------------------------------------------
# 3. 决策循环：one-shot 与 tools 两种模式（目标动态选取）
# ---------------------------------------------------------------------------
@unittest.skipUnless(fixtures(), "需要至少一份快照夹具")
class TestAgentLoop(unittest.TestCase):
    def setUp(self):
        self.rt = runtime_for(fixtures()[0])
        self.rt.pending.clear()
        self.rt._sync_pending()
        self.sid, self.btype = _feasible(self.rt)

    def _restore_mode(self, old):
        if old is None:
            self.rt.config.data["agent"].pop("mode", None)
        else:
            self.rt.config.data["agent"]["mode"] = old

    def test_oneshot(self):
        old = self.rt.config.get("agent.mode")
        self.rt.config.data["agent"]["mode"] = "oneshot"
        try:
            llm = MockLLM([{"content": json.dumps({
                "summary": "one-shot build",
                "rationale": "test",
                "actions": [{"kind": "build", "state_id": self.sid,
                             "building": self.btype, "levels": 1,
                             "rationale": "test"}],
            }, ensure_ascii=False)}])
            res = AgentLoop(self.rt, llm=llm, max_rounds=4).run_cycle()
            self.assertIsNone(res.error)
            self.assertEqual(res.summary, "one-shot build")
            self.assertEqual(len(res.proposals), 1)
            self.assertTrue(res.proposals[0]["approved"])
            self.assertEqual(len(res.executions), 1)
        finally:
            self._restore_mode(old)

    def test_tools_with_advise(self):
        old = self.rt.config.get("agent.mode")
        self.rt.config.data["agent"]["mode"] = "tools"
        try:
            llm = MockLLM([
                tool_call("advise_brief", {}),
                tool_call("propose_build", {"state_id": self.sid,
                                            "building": self.btype,
                                            "levels": 1, "rationale": "test"}),
                tool_call("propose_build", {"state_id": self.sid,
                                            "building": _subsistence(self.rt),
                                            "levels": 1, "rationale": "bad"}),
                tool_call("finish_cycle", {"summary": "s", "rationale": "r"}),
            ])
            res = AgentLoop(self.rt, llm=llm, max_rounds=8).run_cycle()
            self.assertIsNone(res.error)
            self.assertEqual(res.summary, "s")
            self.assertEqual(len(res.proposals), 2)
            self.assertTrue(res.proposals[0]["approved"])
            self.assertFalse(res.proposals[1]["approved"])
            self.assertEqual(len(res.executions), 1)
            self.assertFalse(self.rt.cycle_active)
            self.assertEqual(res.tool_calls[0]["name"], "advise_brief")
        finally:
            self._restore_mode(old)


# ---------------------------------------------------------------------------
# 4. 金标层：1836 大清这份夹具的"历史时点语义"
#    （不是"只支持大清"；换夹具这些断言本来就不该成立）
# ---------------------------------------------------------------------------
@unittest.skipUnless(os.path.isfile(QING), "缺少 examples/snapshot_chi_1836.json")
class TestQingGolden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = runtime_for(QING)
        cls.ctx = cls.rt.ctx

    def test_identity(self):
        ov = self.rt.overview()
        self.assertEqual(ov["meta"]["country_id"], 157)
        self.assertEqual(ov["meta"]["states"], 43)
        self.assertGreater(ov["gdp"]["annual"], 80_000_000)
        self.assertIn(ov["gdp"]["price_basis"], ("market", "base"))

    def test_state_meta(self):
        self.assertEqual(self.ctx.state_meta[914].region, "STATE_BEIJING")
        self.assertEqual(self.ctx.state_meta[907].region, "STATE_SICHUAN")
        self.assertIn("building_tea_plantation", self.ctx.state_meta[907].resources)
        self.assertNotIn("building_tea_plantation", self.ctx.state_meta[914].resources)

    def test_buildability(self):
        self.assertFalse(self.ctx.check_build(914, "building_tea_plantation", 1)["ok"])
        self.assertTrue(self.ctx.check_build(907, "building_tea_plantation", 1)["ok"])
        # 1836 大清没有铁路科技
        self.assertFalse(self.ctx.check_build(914, "building_railway", 1)["ok"])
        camp = self.ctx.check_build(914, "building_logging_camp", 1)
        self.assertFalse(camp["ok"])
        self.assertEqual(camp["remaining"], 0)
        self.assertTrue(self.ctx.check_build(914, "building_wheat_farm", 1)["ok"])

    def test_pm_era(self):
        """1836 年：手工制衣在用、缝纫机还没解锁。"""
        already = self.ctx.check_pm(928, "building_textile_mill",
                                    "pm_handsewn_clothes")
        self.assertEqual(already["code"], "already")
        locked = self.ctx.check_pm(928, "building_textile_mill",
                                   "pm_sewing_machines")
        self.assertEqual(locked["code"], "locked")
        ok = self.ctx.check_pm(913, "building_government_administration",
                               "pm_simple_organization")
        self.assertTrue(ok["ok"])
        self.assertTrue(ok["warnings"])

    def test_review_messages(self):
        self.rt.pending.clear()
        self.rt._sync_pending()
        ok = self.rt.propose({"kind": "build", "state_id": 907,
                              "building": "building_tea_plantation", "levels": 2})
        self.assertTrue(ok.approved)
        bad = self.rt.propose({"kind": "build", "state_id": 914,
                               "building": "building_tea_plantation", "levels": 2})
        self.assertFalse(bad.approved)
        self.assertIn("没有该资源", "；".join(bad.reasons))


# ---------------------------------------------------------------------------
# 5. 与快照无关的单元测试
# ---------------------------------------------------------------------------
class TestStrategy(unittest.TestCase):
    def test_store(self):
        d = _fixed_dir("strategy")
        path = os.path.join(d, "strategy.json")
        store = StrategyStore(path)
        self.assertEqual(store.active().source, "default")
        st = store.set({"name": "工业优先", "objectives": [
            {"metric": "gdp_growth", "weight": 1.0, "direction": "max"}],
            "constraints": {"max_actions_per_cycle": 3}}, source="player")
        self.assertEqual(st.version, 2)
        self.assertEqual(store.active().name, "工业优先")
        self.assertEqual(len(store.history()), 1)
        ai = store.set({"name": "AI 策略", "constraints": {
            "allow_conditional_pm": True, "max_actions_per_cycle": 99}},
            source="ai")
        self.assertFalse(ai.constraints["allow_conditional_pm"])
        self.assertLessEqual(ai.constraints["max_actions_per_cycle"], 16)


class TestExecutor(unittest.TestCase):
    def test_dry_run(self):
        ex = DryRunExecutor()
        cmd = Command(kind="build", state_id=1, building="building_farm", levels=1)
        r = ex.execute(cmd)
        self.assertTrue(r.ok)
        self.assertEqual(r.status, "dry_run")

    def test_bridge_protocol(self):
        d = _fixed_dir("bridge")
        ex = BridgeExecutor(d, timeout_s=3, poll_s=0.02)
        stop = threading.Event()

        def watcher():
            while not stop.is_set():
                time.sleep(0.01)
                q = os.path.join(d, "queue.txt")
                qd = os.path.join(d, "queue.done.txt")
                if os.path.isfile(q) and not os.path.isfile(qd):
                    with open(os.path.join(d, "queue.csv"), "w", encoding="utf-8") as f:
                        f.write("queue,index,state_id,building,pts_total,pts_remaining\n")
                        f.write("government,0,914,building_farm,60000000,0\n")
                    with open(qd, "w", encoding="utf-8") as f:
                        f.write("x")
                rq = os.path.join(d, "requests.txt")
                rqd = os.path.join(d, "requests.done.txt")
                if os.path.isfile(rq) and not os.path.isfile(rqd):
                    with open(rqd, "w", encoding="utf-8") as f:
                        f.write("114 building_farm 1\n")
                    with open(os.path.join(d, "v3inject.log"), "a",
                              encoding="utf-8") as f:
                        f.write("build: owner=1 existed=0 rc=-1 queued=0->1\n")
                pm = os.path.join(d, "pmm.txt")
                pmd = os.path.join(d, "pmm.done.txt")
                if os.path.isfile(pm) and not os.path.isfile(pmd):
                    with open(os.path.join(d, "pmm.result.csv"), "w",
                              encoding="utf-8") as f:
                        f.write("state_id,building,pm,status,slot,old_pm\n")
                        f.write("914,building_farm,pm_a,ok,0,pm_b\n")
                    with open(pmd, "w", encoding="utf-8") as f:
                        f.write("x")

        t = threading.Thread(target=watcher, daemon=True)
        t.start()
        try:
            q = ex.queue()
            self.assertEqual(q[0]["building"], "building_farm")
            br = ex.execute(Command(kind="build", state_id=914,
                                    building="building_farm", levels=1))
            self.assertTrue(br.ok, br.message)
            pm = ex.execute(Command(kind="pm", state_id=914,
                                    building="building_farm", pm="pm_a"))
            self.assertTrue(pm.ok, pm.message)
        finally:
            stop.set()
            t.join(timeout=1)


class TestLLM(unittest.TestCase):
    def test_parse_openai_tool_call(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "tools" not in body:
                    self.send_error(400)
                    return
                resp = {"choices": [{"message": {
                    "content": "",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {
                        "name": "get_overview", "arguments": "{}"}}]}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
                data = json.dumps(resp).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            c = LLMClient({"name": "test",
                           "base_url": "http://127.0.0.1:%d/v1" % srv.server_port,
                           "model": "m", "api_key": "k"})
            r = c.chat([{"role": "user", "content": "hi"}],
                       tools=[{"type": "function"}])
            self.assertEqual(r.tool_calls[0].name, "get_overview")
            self.assertEqual(r.usage["prompt_tokens"], 1)
        finally:
            srv.shutdown()


@unittest.skipUnless(fixtures(), "需要至少一份快照夹具")
class TestWeb(unittest.TestCase):
    def test_payload_and_chat(self):
        from v3ai.web_server import WebApp
        cfg = Config.load()
        cfg.set("executor.bridge.dir", "")
        d = os.path.join(TEST_TMP_ROOT, "web")
        os.makedirs(d, exist_ok=True)
        cfg.set("paths.state_dir", d)
        app = WebApp(cfg, snapshot_path=fixtures()[0], live=False)
        try:
            p = app.data.payload()
            self.assertTrue(p.get("ok"))
            self.assertNotIn("states", p)
            self.assertIn("headline", p)
            self.assertIn("construction", p)
            self.assertGreater(p["headline"]["gdp_annual"], 0)
            ctx = app.data.chat_context()
            self.assertNotIn("states", ctx)
            app.chat.llm = MockLLM([{"content": "国库尚可。"}])
            out = app.chat.ask("国库如何？")
            self.assertTrue(out.get("ok"))
            self.assertEqual(out["reply"], "国库尚可。")
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main()
