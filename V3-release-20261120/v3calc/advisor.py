# -*- coding: utf-8 -*-
"""决策计算层的门面：一次构造，导出市场/候选/方案/信号/简报/对账。

这一层**不调用 LLM、不写游戏**，只把"算账"做扎实，让决策层拿到的是
最多一两千字的结论，而不是几千行 JSON。

    from v3calc.advisor import Advisor
    a = Advisor(gd, snap, prev=old_snap)
    a.text()            # 一页纸简报（给 LLM）
    a.candidates(8)     # 候选包（结构化）
    a.signals()         # 触发信号
    a.plan()            # 逐步贪心方案
    a.quick()           # 纯规则能直接办的事
"""
from __future__ import annotations

from .brief import build_brief, render_brief
from .candidates import CandidateEngine, Intent, PlanResult
from .construction import StateGeo, load_state_meta
from .ledger import Ledger
from .market import MarketView, PriceModel
from .model import Snapshot
from .signals import Signal, scan, should_escalate


class Advisor:
    def __init__(self, gd, snap: Snapshot, prev: Snapshot | None = None,
                 geo: dict[int, StateGeo] | None = None,
                 intent: Intent | None = None,
                 state_meta: str | None = None,
                 queued: dict[tuple[int, str], int] | None = None,
                 ledger: str | None = None):
        self.gd = gd
        self.snap = snap
        self.prev = prev
        self.geo = geo if geo is not None else load_state_meta(state_meta)
        self.intent = intent or Intent()
        self.model = PriceModel.from_snapshot(gd, snap)
        self.view = MarketView(gd, self.model)
        self.engine = CandidateEngine(gd, snap, geo=self.geo,
                                      intent=self.intent, queued=queued)
        self.ledger = Ledger(ledger)

    # -- 单品 ---------------------------------------------------------
    def market(self) -> dict:
        return {
            "summary": self.view.summary(),
            "chains": [r.to_dict() for r in self.view.chains()],
            "shortages": [p.to_dict() for p in self.view.shortages(12)],
            "gluts": [p.to_dict() for p in self.view.gluts(12)],
            "ceiling": [p.to_dict() for p in self.view.at_ceiling()],
        }

    def candidates(self, top: int = 10, dedupe: bool = True) -> list[dict]:
        opts = self.engine.build_options(limit=None if dedupe else top)
        if dedupe:
            seen: set[str] = set()
            picked = []
            for o in opts:
                if o.building in seen:
                    continue
                seen.add(o.building)
                picked.append(o)
                if len(picked) >= top:
                    break
            opts = picked
        return [o.to_dict() for o in opts[:top]]

    def pm_candidates(self, top: int = 8) -> list[dict]:
        return [o.to_dict() for o in self.engine.pm_options(limit=top)]

    def capacity(self, weeks: float | None = None, limit: int = 3) -> list[dict]:
        """建造力：只给成本与可控性判定，不做增值估值。"""
        weeks = weeks if weeks is not None else self.intent.budget_weeks
        budget = self.engine.cons.budget_points(weeks)
        return [c.to_dict() for c in
                self.engine.capacity_options(levels=1, budget_points=budget,
                                             limit=limit)]

    def capacity_pms(self, top: int = 6) -> list[dict]:
        return self.engine.capacity_pm_options()[:top]

    def plan(self, weeks: float | None = None, max_items: int = 6,
             budget_points: float | None = None) -> PlanResult:
        if budget_points is None:
            weeks = weeks if weeks is not None else self.intent.budget_weeks
            budget_points = self.engine.cons.budget_points(weeks)
        return self.engine.plan(budget_points, max_items=max_items)

    def signals(self) -> list[Signal]:
        return scan(self.gd, self.snap, model=self.model, view=self.view,
                    prev=self.prev, capacity=self.engine.cons.capacity())

    def escalate(self) -> bool:
        return should_escalate(self.signals())

    # -- 汇总 ---------------------------------------------------------
    def brief(self, top: int = 8, weeks: float | None = None,
              include_pm: bool = True) -> dict:
        return build_brief(self.gd, self.snap, prev=self.prev, top=top,
                           geo=self.geo, intent=self.intent,
                           model=self.model,
                           weeks=weeks if weeks is not None
                           else self.intent.budget_weeks,
                           include_pm=include_pm)

    def text(self, top: int = 8, char_budget: int = 2600,
             weeks: float | None = None) -> str:
        return render_brief(self.brief(top=top, weeks=weeks),
                            char_budget=char_budget)

    # -- 规则快通道 ---------------------------------------------------
    def quick(self, weeks: float | None = None, max_items: int = 4) -> list[dict]:
        """不需要 LLM 就能下的结论：财政护栏 + PM 升级 + 队列补位。

        返回建议列表（不执行）；每条都带 ``reason``，可直接进 journal。
        """
        out: list[dict] = []
        sig = self.signals()
        sev3 = [s for s in sig if s.severity >= 3]
        c = self.snap.country
        balance = (c.budget.balance if c and c.budget else None)
        treasury = float(c.treasury or 0.0) if c else 0.0

        if balance is not None and balance < 0 and treasury <= 0:
            c2 = self.snap.country
            headroom = max(0.0, float(c2.credit_limit or 0.0) -
                           float(c2.loan_principal or 0.0)) if c2 else 0.0
            if headroom < abs(balance) * 26:
                out.append({
                    "kind": "hold", "action": "暂停新建造",
                    "reason": "周结余 %.0f、国库见底且剩余信贷 %.0f 撑不过半年；"
                              "先止血再加产能" % (balance, headroom),
                    "evidence": {"balance": balance, "treasury": treasury,
                                 "credit_headroom": headroom},
                })

        pms = self.engine.pm_options(limit=4)
        for p in pms:
            if p.annual_value <= 0 or p.risk > 0.3:
                continue
            out.append({
                "kind": "pm", "action": "换 %s → %s" % (p.building, p.pm),
                "reason": "年增值 %+.0f（无建造成本），风险 %.2f%s" % (
                    p.annual_value, p.risk,
                    ("；" + "；".join(p.risk_notes)) if p.risk_notes else ""),
                "evidence": p.to_dict(),
            })

        if not any(x["kind"] == "hold" for x in out):
            plan = self.plan(weeks=weeks, max_items=max_items)
            for o in plan.items:
                out.append({
                    "kind": "build", "action": "州 %s 建 %s +%d" % (
                        o.state_id, o.building, o.levels),
                    "reason": "年增值 %+.0f（每点 %.1f），%.1f 周%s" % (
                        o.annual_value, o.per_point, o.weeks or 0,
                        ("；" + "；".join(o.risk_notes)) if o.risk_notes else ""),
                    "evidence": o.to_dict(),
                })
        if sev3:
            out.append({
                "kind": "flag", "action": "有 %d 个危急信号，建议升级给 LLM" % len(sev3),
                "reason": "；".join(s.title for s in sev3[:3]),
                "evidence": [s.to_dict() for s in sev3[:3]],
            })
        return out

    # -- 台账 ---------------------------------------------------------
    def record(self, plan: PlanResult | None = None, note: str = "") -> dict:
        plan = plan or self.plan()
        return self.ledger.record_plan(plan, self.snap, self.gd, note=note)

    def reconcile(self) -> dict:
        return self.ledger.reconcile(self.snap, self.gd)
