# -*- coding: utf-8 -*-
"""LLM 决策循环：概况 → 自主调工具 → 提议 → 审查 → 执行 → 反馈。

循环是"一轮一决策"：
  1. 刷新世界模型；
  2. 把 compact 概况 + 策略 + 上轮反馈交给 LLM；
  3. LLM 通过 function-calling 调工具（工具结果立即回灌）；
  4. LLM 用 propose_build / propose_pm 提交，审查层当场通过/拒绝；
  5. 回合结束，执行已通过的动作，把执行结果写进 journal；
  6. 下一轮概况里带上这些反馈。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .llm import LLMClient, LLMResponse, MockLLM, make_llm
from .review import Command
from .runtime import Runtime, RuntimeErrorV3
from .tools import ToolRegistry, build_registry
from .util import now_iso, truncate_json

INDUSTRY_CHAINS = {
    "building_arms_industry": {
        "outputs": ["small_arms", "artillery", "ammunition"],
        "inputs": ["iron", "coal", "tools"],
        "upstream": ["building_iron_mine", "building_coal_mine", "building_tooling_workshop"],
    },
    "building_steel_mill": {
        "outputs": ["steel"], "inputs": ["iron", "coal"],
        "upstream": ["building_iron_mine", "building_coal_mine"],
    },
    "building_tooling_workshop": {
        "outputs": ["tools"], "inputs": ["iron", "wood"],
        "upstream": ["building_iron_mine", "building_logging_camp"],
    },
    "building_textile_mill": {
        "outputs": ["clothes"], "inputs": ["fabric", "dye"],
        "upstream": ["building_cotton_plantation", "building_dye_plantation"],
    },
    "building_furniture_manufactory": {
        "outputs": ["furniture"], "inputs": ["wood", "fabric"],
        "upstream": ["building_logging_camp", "building_cotton_plantation"],
    },
    "building_glassworks": {
        "outputs": ["glass"], "inputs": ["coal", "lead"],
        "upstream": ["building_coal_mine", "building_lead_mine"],
    },
    "building_paper_mill": {
        "outputs": ["paper"], "inputs": ["wood"],
        "upstream": ["building_logging_camp"],
    },
    "building_chemical_plant": {
        "outputs": ["fertilizer", "explosives"], "inputs": ["sulfur", "coal"],
        "upstream": ["building_sulfur_mine", "building_coal_mine"],
    },
    "building_power_plant": {
        "outputs": ["electricity"], "inputs": ["coal", "oil"],
        "upstream": ["building_coal_mine", "building_oil_rig"],
    },
    "building_construction_sector": {
        "outputs": ["construction"], "inputs": ["wood", "iron", "tools"],
        "upstream": ["building_logging_camp", "building_iron_mine", "building_tooling_workshop"],
    },
    "building_shipyard": {
        "outputs": ["clippers", "steamers"], "inputs": ["wood", "iron", "coal"],
        "upstream": ["building_logging_camp", "building_iron_mine", "building_coal_mine"],
    },
    "building_food_industry": {
        "outputs": ["groceries"], "inputs": ["grain", "meat", "fish"],
        "upstream": ["building_wheat_farm", "building_livestock_ranch", "building_fishing_wharf"],
    },
    "building_distillery": {
        "outputs": ["liquor"], "inputs": ["grain", "sugar"],
        "upstream": ["building_wheat_farm", "building_sugar_plantation"],
    },
    "building_winery": {
        "outputs": ["wine"], "inputs": ["fruit"],
        "upstream": ["building_orchard"],
    },
}

CHAIN_ALIASES = {
    "武器厂": "building_arms_industry", "钢铁厂": "building_steel_mill",
    "工具厂": "building_tooling_workshop", "纺织厂": "building_textile_mill",
    "家具厂": "building_furniture_manufactory", "玻璃厂": "building_glassworks",
    "造纸厂": "building_paper_mill", "化工厂": "building_chemical_plant",
    "发电厂": "building_power_plant", "建造部门": "building_construction_sector",
    "造船厂": "building_shipyard", "食品厂": "building_food_industry",
    "酿酒厂": "building_distillery", "葡萄酒厂": "building_winery",
}

SYSTEM_PROMPT = """你是 Victoria 3 的 AI 统治者。你的任务不是"多建东西"，而是**在给定策略下，
用最少的动作让国家变得更好**。你可以自由调用工具收集信息，最后把动作提交给审查层。

## 工作方式
1. 先读 get_overview 的 world；需要细节再调 query_* / calc_* / pm_* / build_candidates。
2. 决策依据必须是工具返回的事实，不要编造州 id、建筑键、PM 键。
3. 用 propose_build / propose_pm 提交动作；每次提交会立刻经过脚本审查。
   - approved=false 时读 reasons / warnings，调整目标或换州；**不要重复同一被拒动作**。
   - conditional PM 不能自动执行；locked PM 不可执行。
4. 本回合动作提交完，调用 finish_cycle 写 summary/rationale 并结束。
5. 执行由运行时在回合末统一做；执行结果会在下一轮 execution_feedback 里出现。
6. 回合内世界是冻结的：不要尝试 refresh_world / execute_pending（会被拒绝），
   也不要在同一回合里反复看"执行后的新数据"。

## 先看 advise（算好的账）
- `brief.advise.text` 是计算层算好的简报：市场按产业链、候选包（年增值已扣**价格反馈**）、信号。
- 决策优先依据 `advise.text` 与 `build_candidates` 的 `annual_value` / `per_point` / `risk` /
  `price_changes`；不要自己心算"多建几级会怎样"，那是算过的。
- `advise.signals` 里 `severity>=3` 的条目先处理；空列表说明市况平稳，不必强行救火。
- `advise.plan` 是逐步贪心的参考方案（不是命令）：可采纳、可修改、可拒绝。
- 建造部门（建造力）**不参与增值排序**：它只算成本（建造点/现金/周维护），
  看 `advise.capacity` 或 `advise_capacity` 的 `affordable` 判定；不要给它编一个"值多少"。
- 换建造方式（木构→铁构等）也只看"每点容量的周维护"，别按 GDP 增量比。

## 决策质量要求
- 建造前先看 build_candidates / estimate_build；注意科技、法律、资源、耕地、唯一建筑、
  州上限、已有队列。GDP 增量只是排序，不是唯一目标。
- 看 construction.capacity_per_week 与 weeks：一次排太多等级会把队列拉得很长；
  优先选"GDP/建造周"高、且不会拖垮预算的动作。
- 建造是发展的主要手段：只要审查层允许、国库/信用可承受，认为有必要就应建，不要因为保守而空过。
- 先看 queue.pressure：如果 overloaded=true（队列超过在建项目的 3 倍），本轮不要铺开建造，
  但应保留最多 1 条你认为必要的关键建造（优先 PM 或 priority_buildings），不是 0 条。
- build_candidates 里 remaining=null 只表示没有解析到该建筑的资源/建筑上限，不代表不能建；
  以 propose_build 的审查结果为准，不要在 summary 里写成“资源未满足”。
- 换 PM 前先看 pm_report 的完整修正；worker_exploitation_* 会推高死亡率，
  pm_simple_organization 可能降低行政力——除非策略明确允许，不要为了 GDP 自动切。
- 周结余为负是发展期常态，不等于不能建；除非策略明确设置了 max_weekly_deficit，或国库低于 min_treasury，不要仅因负结余放弃关键建造。
  不要在 summary 里用“赤字扩大”作为不建的理由；这不是审查拒绝项。
- 不要为了"花掉预算"而建造；国库/周结余/建造力都是约束。
- 同一州同一建筑不要重复排队；同一 PM 组不要在同一回合提交两个互斥 PM。
- 动作数量遵守当前策略与审查层配额；被配额拒绝时不要硬塞。

## 当前策略优先项
- 只以 world.strategy.constraints.priority_buildings 为准；如果为空，不要声称某个建筑是“策略优先建筑”，也不要引用历史策略里的优先项。

## PM 评估（每轮必做）
- 每轮不能只看建造候选；至少检查一次 `pm_candidates`，必要时再看 `pm_report`。
- 对 priority_buildings 和当前主要产业，按 `available` / `conditional` / `locked` 分类。
- `available` 且有正的 GDP/产出提升、不会明显恶化投入或工资的，优先 `propose_pm`；PM 是补产业链效率、缓解输入短缺的手段，不要只靠新建。
- `locked` 只说明这一组不可用，不代表所有建筑都没有 PM；要继续看其它建筑/组的 available 候选。
- `conditional` 默认不可自动执行；除非策略 `allow_conditional_pm=true`，不要提交。
- 只有在确实没有任何 available 候选时，才写“无可行 PM 动作”。

## 配额语义
- `strategy.constraints.max_actions_per_cycle = 0` 表示**不限制**，不是“禁止提交动作”；`max_levels_per_action = 0` 同理。
- 只有大于 0 的配额才是上限；0 永远不能作为拒绝/不建的理由。
- 是否真的被拒绝，以 `propose_*` 返回的 `approved=false` 和 `reasons` 为准；不要自己宣布“策略硬约束禁止动作”。

## 产业链与物价
- 先看 market_signals 或 query_market：ratio = 价格/基准价；>1.25 视为短缺，<0.8 视为过剩。
- 依据 strategy.priority_buildings / objectives / development 的重点方向，先补关键产业链的上游输入
  （铁、煤、工具、木材、染料、硫磺等），不要只堆最终加工厂。
- 输入品短缺：优先建上游资源/原料建筑，或改用不依赖短缺输入的生产方式；
  输出品过剩：不要继续扩产，优先换 PM、补基础设施或转向短缺品。
- 每轮只推进 1–2 个关键链环节，不要在多个产业链同时铺开。

## 策略
world.strategy 是当前策略（objectives 权重 + constraints 硬约束）。如果局势明显变化，
可以用 strategy_propose 提出新策略（AI 策略不能关闭审查层硬护栏），并在 rationale 说明原因。
玩家也可以在外部切换策略；以 world.strategy 为准。

## 数据口径（必须记住）
- GDP 绝对值误差约 ±0.5%（live + market + staffing + producers）；save 模式只看相对。
- upgrades/estimate 的增量只算商品产出−投入，不含行政力/就业/死亡率/生活水平。
- 快照可能来自暂停的游戏；数值不变是正常的。执行后下一轮快照才可见。
- 读不到就是 null，不要自己补 0。

用中文写 summary/rationale。只提交动作，不要输出大段计划文本。
本地/小模型请尽量少写解释：先调工具，再提交，最后 finish_cycle。"""


def _int(v, default: int | None = None) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_json_object(text: str) -> dict | None:
    if not text:
        return None
    candidates = []
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        candidates.append(m.group(1))
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        candidates.append(s)
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        candidates.append(s[i:j + 1])
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


@dataclass
class CycleResult:
    cycle: int = 0
    summary: str = ""
    rationale: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    executions: list[dict] = field(default_factory=list)
    llm_usage: dict = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    error: str | None = None
    started_at: str = field(default_factory=now_iso)
    latency_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "cycle": self.cycle, "summary": self.summary,
            "rationale": self.rationale, "tool_calls": self.tool_calls,
            "proposals": self.proposals, "executions": self.executions,
            "llm_usage": self.llm_usage, "provider": self.provider,
            "model": self.model, "error": self.error,
            "started_at": self.started_at,
            "latency_s": round(self.latency_s, 2),
        }


class AgentLoop:
    def __init__(self, runtime: Runtime, llm=None,
                 registry: ToolRegistry | None = None,
                 max_rounds: int | None = None, logger=print):
        self.rt = runtime
        self.cfg: Config = runtime.config
        self.llm = llm or self._make_llm()
        self.registry = registry or build_registry(
            core_only=(str(self.cfg.agent.get("toolset") or "full").lower() == "core"))
        self.max_rounds = int(max_rounds or self.cfg.agent.get("max_tool_rounds", 24) or 0)
        if self.max_rounds <= 0:
            self.max_rounds = 200
        self.max_result_chars = int(self.cfg.agent.get("max_tool_result_chars", 16000) or 0)
        self.max_context_chars = int(self.cfg.agent.get("max_context_chars", 0) or 0)
        self.logger = logger

    def _make_llm(self):
        prof = self.cfg.provider()
        if prof.get("name") == "mock":
            return MockLLM([])
        return LLMClient(prof)

    # -- 一轮 ---------------------------------------------------------
    def run_cycle(self, plan_only: bool = False) -> CycleResult:
        mode = str(self.cfg.agent.get("mode") or "tools").lower()
        if mode == "oneshot":
            return self._run_oneshot(plan_only)
        t0 = time.time()
        self.rt.cycle_no += 1
        res = CycleResult(cycle=self.rt.cycle_no,
                          provider=getattr(self.llm, "name", ""),
                          model=getattr(self.llm, "model", ""))
        try:
            self.rt.begin_cycle()
        except Exception as exc:
            res.error = "刷新世界失败：%s" % exc
            res.latency_s = time.time() - t0
            self.rt.end_cycle()
            self._journal(res)
            return res

        brief = self.rt.brief()
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(brief, ensure_ascii=False)},
        ]
        tools = self.registry.openai_tools()
        final_text = ""
        summary = ""
        rationale = ""
        finished = False
        usage: dict[str, int] = {}

        for _round in range(self.max_rounds):
            try:
                messages = self._fit_context(messages)
                resp: LLMResponse = self.llm.chat(
                    messages, tools=tools,
                    temperature=self.cfg.agent.get("temperature"),
                    max_tokens=self.cfg.agent.get("max_tokens"),
                )
            except Exception as exc:
                res.error = "LLM 调用失败：%s" % exc
                break
            u = resp.usage or {}
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if u.get(k):
                    usage[k] = usage.get(k, 0) + int(u[k])
            msg: dict[str, Any] = {"role": "assistant", "content": resp.content or ""}
            if resp.tool_calls:
                msg["tool_calls"] = [tc.to_openai() for tc in resp.tool_calls]
            messages.append(msg)
            if not resp.tool_calls:
                # 本地模型可能把正文放在 reasoning_content，content 为 null
                final_text = resp.content or resp.reasoning or ""
                break
            for tc in resp.tool_calls:
                if plan_only and tc.name == "execute_pending":
                    out = {"ok": False, "error": "plan-only 模式禁止执行；请只提议，由外部确认"}
                else:
                    out = self.registry.call(self.rt, tc.name, tc.arguments or {})
                ok = bool(out.get("ok", True))
                rec = {"name": tc.name, "args": tc.arguments or {},
                       "ok": ok, "code": out.get("code")}
                res.tool_calls.append(rec)
                if tc.name in ("propose_build", "propose_pm"):
                    res.proposals.append({
                        "tool": tc.name, "args": tc.arguments or {},
                        "approved": out.get("approved"),
                        "code": out.get("code"),
                        "message": out.get("message"),
                        "reasons": out.get("reasons") or [],
                        "warnings": out.get("warnings") or [],
                        "command_id": out.get("command_id"),
                    })
                payload = truncate_json(out, self.max_result_chars)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                })
                if tc.name == "finish_cycle":
                    summary = str((tc.arguments or {}).get("summary") or out.get("summary") or "")
                    rationale = str((tc.arguments or {}).get("rationale") or out.get("rationale") or "")
                    finished = True
                    break
            if finished:
                break
            # 防止无限工具循环：接近上限时提示收尾
            if _round == self.max_rounds - 2:
                messages.append({"role": "user", "content":
                                 "工具调用次数接近上限，请现在调用 finish_cycle 收尾。"})

        if not summary:
            # 模型没有调用 finish_cycle 时，不要把整段英文内心推理塞进决议卡片
            approved_n = sum(1 for p in res.proposals if p.get("approved"))
            if approved_n:
                summary = "本轮已提交 %d 个动作（工具轮次用尽，未调用 finish_cycle）" % approved_n
            else:
                summary = "本轮工具轮次用尽，未提交动作"
            if not rationale:
                rationale = ("模型未在工具轮次内完成收尾；请检查工具结果或减少工具调用后重试。"
                             if not approved_n else
                             "动作已提交并通过审查，但模型未调用 finish_cycle 写收尾说明。")

        # 执行
        if not plan_only and self.cfg.agent.get("auto_execute", True):
            try:
                results = self.rt.execute_pending()
                res.executions = [r.to_dict() for r in results]
            except Exception as exc:
                res.error = (res.error + " | " if res.error else "") + "执行失败：%s" % exc
        res.summary = summary
        res.rationale = rationale
        res.llm_usage = usage
        res.latency_s = time.time() - t0
        self.rt.last_cycle = res.to_dict()
        self._journal(res)
        # 决策 + DLL 反馈处理完，才允许下一回合重新读取世界
        self.rt.end_cycle()
        return res

    # -- one-shot 快速路径（本地小模型用） -------------------------------
    def _oneshot_payload(self) -> dict:
        rt, ctx = self.rt, self.rt.ctx
        brief = rt.brief()
        world = brief["world"]
        queue_pressure = ((brief.get("world") or {}).get("queue") or {}).get("pressure") or {}
        market = ctx.market_digest(top_n=60)
        price_rows = {r["good"]: r for r in (market.get("most_expensive") or [])}
        for r in (market.get("cheapest") or []):
            price_rows.setdefault(r["good"], r)
        chain_notes = []
        priority_buildings = []
        try:
            priority_buildings = list(((rt.strategy.constraints if rt.strategy else {}) or {}).get("priority_buildings") or [])
        except Exception:
            priority_buildings = []
        for p in priority_buildings:
            key = CHAIN_ALIASES.get(str(p), str(p))
            chain = INDUSTRY_CHAINS.get(key)
            if not chain:
                continue
            def _prices(goods):
                return [{"good": g, "ratio": (price_rows.get(g) or {}).get("ratio")}
                        for g in goods]
            chain_notes.append({"building": key, "outputs": _prices(chain["outputs"]),
                                "inputs": _prices(chain["inputs"]),
                                "upstream": chain["upstream"]})
        market_signals = {
            "expensive": [{"good": r["good"], "ratio": r["ratio"], "supply": r["supply"], "demand": r["demand"]}
                          for r in (market.get("most_expensive") or [])[:6]],
            "cheap": [{"good": r["good"], "ratio": r["ratio"], "supply": r["supply"], "demand": r["demand"]}
                      for r in (market.get("cheapest") or [])[:6]],
        }
        dev = dict((rt.strategy.development if rt.strategy else {}) or {})
        focus = ctx.focus_states(dev.get("mode") or "nationwide",
                                 int(dev.get("max_states_per_cycle") or 0),
                                 dev.get("priority_states") or [])
        builds = ctx.build_candidates(top=12, state_ids=focus)
        pm_rows: list[dict] = []
        top_types = sorted(ctx.levels_by_type.items(), key=lambda kv: -kv[1])[:3]
        for btype, _n in top_types:
            states = sorted({b.state_id for b in ctx.snap.buildings if b.type == btype})
            if focus:
                states = [s for s in states if s in focus]
            states = states[:4]
            for sid in states:
                for row in ctx.pm_candidates(btype, state_id=sid, top=2):
                    pm_rows.append({
                        "state_id": sid, "building": btype,
                        "group": row.get("group"),
                        "candidate": row.get("candidate"),
                        "delta": row.get("delta"),
                        "status": row.get("status"),
                        "check": row.get("check"),
                        "non_gdp_effects": row.get("non_gdp_effects"),
                    })
        limits = {
            "max_actions": int(self.cfg.agent.get("max_actions_per_cycle", 4) or 0),
            "max_levels": int(self.cfg.agent.get("max_levels_per_action", 5) or 0),
            "note": "0 = 不限制；max_actions 是每轮动作数，max_levels 是单条建造等级",
        }
        advise = rt.advise_payload()
        return {
            "world": world,
            "advise_text": advise.get("text"),
            "advise_signals": advise.get("signals"),
            "advise_plan": advise.get("plan"),
            "strategy": rt.strategy.summary() if rt.strategy else None,
            "strategy_constraints": dict((rt.strategy.constraints if rt.strategy else {}) or {}),
            "zero_means_unlimited": ["max_actions_per_cycle", "max_levels_per_action"],
            "development": dev,
            "focus_states": focus,
            "queue_pressure": queue_pressure,
            "market_signals": market_signals,
            "priority_chains": chain_notes,
            "current_priority_buildings": priority_buildings,
            "build_candidates_note": "remaining=null 只表示上限未解析，不代表不能建；以审查结果为准",
            "build_candidates": builds,
            "pm_candidates": pm_rows[:20],
            "pm_note": "每轮至少评估一次；available 可提交，conditional 默认不可，locked 只跳该组，不要只看建造",
            "feedback": rt.feedback(),
            "recent_cycles": rt.recent_history(3),
            "limits": limits,
            "instruction": (
                "只输出一个 JSON 对象，不要 markdown。格式："
                '{"summary":"...","rationale":"...","actions":['
                '{"kind":"build","state_id":910,"building":"building_xxx",'
                '"levels":1,"rationale":"..."},'
                '{"kind":"pm","state_id":914,"building":"building_xxx",'
                '"pm":"pm_xxx","rationale":"..."}]}。'
                "每轮至少评估一次 pm_candidates：available 可提交，conditional 默认不可，locked 只跳过该组；不要只看建造；"
                "先读 advise_text（算好的账：年增值已扣价格反馈、risk 已计入）与 advise_signals；"
                "建造力只看成本（advise_capacity 的 affordable），不估值；"
                "建造优先从 build_candidates 里选 annual_value 高、risk 低、price_changes 不致命的；"
                "先看 market_signals 与 priority_chains：ratio>1.25 短缺，<0.8 过剩；"
                "按重点方向优先补上游输入，不要只堆最终厂；输出过剩不要扩产；"
                "周结余为负不等于不能建；除非策略 max_weekly_deficit 超限或国库低于 min_treasury，不要仅因负结余放弃关键建造；"
                "建造是发展主要手段：审查/国库允许且有必要就建；"
                "若 queue_pressure.overloaded=true，不要铺开，但仍保留最多 1 条必要建造（优先 PM 或 priority_buildings），不是 0 条；"
                "build 只能从 build_candidates 里选 state_id+building；"
                "pm 只能从 pm_candidates 里选 state_id+building+candidate；"
                "limits.max_actions / limits.max_levels 为 0 时不限制，不是禁止提交动作；0 不能作为不建理由；"
                "没有合适动作就 actions=[]。"
            ),
        }

    def _run_oneshot(self, plan_only: bool = False) -> CycleResult:
        t0 = time.time()
        self.rt.cycle_no += 1
        res = CycleResult(cycle=self.rt.cycle_no,
                          provider=getattr(self.llm, "name", ""),
                          model=getattr(self.llm, "model", ""))
        try:
            self.rt.begin_cycle()
        except Exception as exc:
            res.error = "刷新世界失败：%s" % exc
            res.latency_s = time.time() - t0
            self.rt.end_cycle()
            self._journal(res)
            return res
        try:
            payload = self._oneshot_payload()
            messages = [
                {"role": "system", "content": (
                    "你是 Victoria 3 的决策器。根据给定概况、策略、建造候选和 PM 候选，"
                    "选最多 limits.max_actions 个动作。只输出严格 JSON，不要解释，不要 markdown。"
                    "summary 和 rationale 必须中文；constraints 中 0 表示不限制。"
                    "不要编造候选之外的 state_id/building/pm。")},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            resp = self.llm.chat(messages, tools=None,
                                 temperature=self.cfg.agent.get("temperature"),
                                 max_tokens=self.cfg.agent.get("max_tokens"))
            res.llm_usage = resp.usage or {}
            text = resp.content or resp.reasoning or ""
            obj = _parse_json_object(text)
            if not isinstance(obj, dict):
                res.error = "one-shot 模型没有返回可解析 JSON：%s" % (text[:300] or "(empty)")
                res.summary = "（one-shot 解析失败）"
            else:
                res.summary = str(obj.get("summary") or "one-shot 决策")[:4000]
                res.rationale = str(obj.get("rationale") or "")[:4000]
                actions = obj.get("actions") or []
                if not isinstance(actions, list):
                    actions = []
                for a in actions:
                    if not isinstance(a, dict):
                        continue
                    kind = str(a.get("kind") or "")
                    if kind == "build":
                        cmd = Command(kind="build",
                                      state_id=_int(a.get("state_id")),
                                      building=str(a.get("building") or ""),
                                      levels=_int(a.get("levels"), 1) or 1,
                                      source="llm-oneshot",
                                      rationale=str(a.get("rationale") or ""))
                    elif kind == "pm":
                        cmd = Command(kind="pm",
                                      state_id=_int(a.get("state_id")),
                                      building=str(a.get("building") or ""),
                                      pm=(str(a.get("pm")) if a.get("pm") else None),
                                      source="llm-oneshot",
                                      rationale=str(a.get("rationale") or ""))
                    else:
                        res.proposals.append({"tool": "oneshot", "args": a,
                                              "approved": False, "code": "schema",
                                              "message": "未知 kind"})
                        continue
                    review = self.rt.propose(cmd)
                    res.tool_calls.append({"name": "oneshot_propose",
                                           "args": a, "ok": review.approved,
                                           "code": review.code})
                    res.proposals.append({
                        "tool": "oneshot", "args": a,
                        "approved": review.approved, "code": review.code,
                        "message": review.message,
                        "reasons": review.reasons, "warnings": review.warnings,
                        "command_id": cmd.id,
                    })
            if not plan_only and self.cfg.agent.get("auto_execute", True):
                results = self.rt.execute_pending()
                res.executions = [r.to_dict() for r in results]
        except Exception as exc:
            res.error = "one-shot 失败：%s" % exc
        finally:
            res.latency_s = time.time() - t0
            self.rt.last_cycle = res.to_dict()
            self._journal(res)
            self.rt.end_cycle()
        return res

    def commit_pending(self) -> list[dict]:
        results = self.rt.execute_pending()
        out = [r.to_dict() for r in results]
        if self.rt.last_cycle is not None:
            self.rt.last_cycle["executions"] = out
        return out

    def _fit_context(self, messages: list[dict]) -> list[dict]:
        """近似上下文预算：优先裁最长工具结果，再裁最长正文。

        OpenAI 的 tool_call_id 配对必须保留，所以这里不删除 tool 消息，只截断内容。
        """
        limit = int(self.max_context_chars or 0)
        if limit <= 0:
            return messages
        out = [dict(m) for m in messages]

        def clen(m):
            c = m.get("content")
            if isinstance(c, str):
                return len(c)
            if c is None:
                return 0
            return len(json.dumps(c, ensure_ascii=False))

        for _ in range(60):
            total = sum(clen(m) + 80 for m in out)
            if total <= limit:
                break
            # 1) 最长 tool 结果
            idx, best = -1, 0
            for i, m in enumerate(out):
                if m.get("role") == "tool":
                    n = clen(m)
                    if n > best:
                        idx, best = i, n
            if idx >= 0 and best > 300:
                c = out[idx].get("content") or ""
                keep = max(220, min(len(c) // 2, limit // 3))
                out[idx] = dict(out[idx], content=c[:keep] + "\n…（上下文预算已截断）")
                continue
            # 2) 最长 assistant 正文（保留 system 与最后一条 user）
            idx, best = -1, 0
            for i, m in enumerate(out):
                if m.get("role") == "assistant" and i != len(out) - 1:
                    n = clen(m)
                    if n > best:
                        idx, best = i, n
            if idx >= 0 and best > 400:
                c = out[idx].get("content") or ""
                out[idx] = dict(out[idx], content=c[:max(300, len(c) // 2)] + "\n…（已截断）")
                continue
            break
        return out

    def _journal(self, res: CycleResult) -> None:
        rec = res.to_dict()
        rec["type"] = "cycle"
        try:
            rec["country_id"] = self.rt.snap.country_id if self.rt.snap else None
        except Exception:
            rec["country_id"] = None
        rec["strategy"] = self.rt.strategy.summary() if self.rt.strategy else None
        self.rt.journal_event(rec)
