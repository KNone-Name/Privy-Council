# -*- coding: utf-8 -*-
"""决策计算层（v3calc）与运行时（v3ai）之间的适配器。

职责只有一件事：把 ``Runtime`` 手里的快照/地理/策略/队列，翻译成
``v3calc.advisor.Advisor`` 需要的输入，并把结果**转回 v3ai 既有的键名**，
这样工具、审查层、one-shot 载荷都不用改协议就能拿到"算好账"的候选。

换算规则：
  * 州静态地理：``world.StateMeta`` → ``v3calc.construction.StateGeo``；
  * 队列 + 待执行：合成 ``{(state_id, building): levels}`` 传给计算层；
  * 策略：``constraints.priority_buildings`` 经"建筑→主商品→产业链"得到
    ``Intent.priority_chains``；``forbidden_buildings`` / ``block_pm_patterns``
    直接变成禁令。

缓存：``Advisor`` 按（快照对象, 待执行数量）缓存。待执行动作变化会改变
"已占用空间"，必须重建；同一回合内反复查同一份数据则复用。
"""
from __future__ import annotations

from typing import Any

from .util import safe_int


class AdvisorBridge:
    def __init__(self, snap, gd, state_meta, buildings_kb: dict | None = None,
                 strategy=None, queue: list[dict] | None = None,
                 pending: list[Any] | None = None, config=None):
        self.snap = snap
        self.gd = gd
        self.state_meta = state_meta or {}
        self.buildings_kb = buildings_kb or {}
        self.strategy = strategy
        self.queue = list(queue or [])
        self.pending = list(pending or [])
        self.config = config
        self._advisor = None
        self._sig: tuple | None = None
        self._memo: dict = {}
        self._gdp_annual: float | None = None
        self._failed = False

    # -- 输入换算 -----------------------------------------------------
    def _geo(self) -> dict:
        from v3calc.construction import StateGeo

        out = {}
        for sid, m in self.state_meta.items():
            out[int(sid)] = StateGeo(
                region=getattr(m, "region", "") or "",
                coastal=bool(getattr(m, "coastal", False)),
                arable_land=int(getattr(m, "arable_land", 0) or 0),
                resources=tuple(getattr(m, "resources", []) or ()),
                resource_caps=dict(getattr(m, "resource_caps", {}) or {}),
                traits=tuple(getattr(m, "traits", []) or ()),
                incorporated=getattr(m, "incorporated", None),
            )
        return out

    def _queued(self) -> dict[tuple[int, str], int]:
        q: dict[tuple[int, str], int] = {}
        for e in self.queue:
            sid = safe_int(e.get("state_id"), None)
            b = str(e.get("building") or "")
            if sid is None or not b:
                continue
            n = max(1, int(safe_int(e.get("levels"), 1) or 1))
            q[(sid, b)] = q.get((sid, b), 0) + n
        for c in self.pending:
            if str(getattr(c, "kind", "")) != "build":
                continue
            sid = safe_int(getattr(c, "state_id", None), None)
            b = str(getattr(c, "building", "") or "")
            if sid is None or not b:
                continue
            n = max(1, int(getattr(c, "levels", 1) or 1))
            q[(sid, b)] = q.get((sid, b), 0) + n
        return q

    def _intent(self):
        from v3calc.candidates import CAPACITY_CHAIN, Intent, unit_goods
        from v3calc.market import chain_of

        cons = dict((self.strategy.constraints if self.strategy else {}) or {})
        chains: list[str] = []
        for b in cons.get("priority_buildings") or []:
            b = str(b)
            if b == "building_construction_sector":
                chains.append(CAPACITY_CHAIN)
                continue
            try:
                good = unit_goods(self.gd, b).main_good
            except Exception:                          # noqa: BLE001
                continue
            if good:
                chains.append(chain_of(good))
        dev = dict((self.strategy.development if self.strategy else {}) or {})
        target: list[int] = []
        if str(dev.get("mode") or "") == "custom":
            target = [int(x) for x in (dev.get("priority_states") or [])
                      if safe_int(x) is not None]
        bans = [str(x) for x in (cons.get("forbidden_buildings") or [])]
        pm_bans = [str(x) for x in (cons.get("block_pm_patterns") or [])]
        return Intent(
            name=(self.strategy.name if self.strategy else "默认"),
            priority_chains=sorted(set(chains)),
            ban_buildings=bans,
            ban_pm_patterns=pm_bans + ["worker_exploitation"],
            target_states=target,
        )

    def _ledger_path(self) -> str:
        try:
            import os
            return os.path.join(self.config.path("state_dir"), "advise_ledger.jsonl")
        except Exception:                              # noqa: BLE001
            return ""

    # -- 构造/缓存 -----------------------------------------------------
    def advisor(self):
        if self._failed:
            return None
        key = (id(self.snap), len(self.pending))
        if self._advisor is not None and self._sig == key:
            return self._advisor
        try:
            from v3calc.advisor import Advisor

            self._advisor = Advisor(
                self.gd, self.snap, geo=self._geo(), intent=self._intent(),
                queued=self._queued(),
                ledger=(self._ledger_path() or None),
            )
            self._sig = key
        except Exception:                              # noqa: BLE001
            # 计算层出问题不能拖垮决策层：降级为"没有 advise"，旧逻辑继续跑
            self._advisor = None
            self._failed = True
        return self._advisor

    @property
    def available(self) -> bool:
        return self.advisor() is not None

    def _memo_get(self, key):
        row = self._memo.get(key)
        return row[1] if row and row[0] == self._sig else None

    def _memo_put(self, key, value):
        self._memo[key] = (self._sig, value)
        return value

    # -- 查询 ---------------------------------------------------------
    def brief_text(self, char_budget: int | None = None, top: int = 8) -> str:
        adv = self.advisor()
        if adv is None:
            return ""
        key = ("brief", int(char_budget or 2400), int(top))
        hit = self._memo_get(key)
        if hit is not None:
            return hit
        try:
            return self._memo_put(key, adv.text(top=top,
                                                char_budget=char_budget or 2400))
        except Exception:                              # noqa: BLE001
            return ""

    def signals(self) -> list[dict]:
        adv = self.advisor()
        if adv is None:
            return []
        hit = self._memo_get(("signals",))
        if hit is not None:
            return hit
        try:
            return self._memo_put(("signals",), [s.to_dict() for s in adv.signals()])
        except Exception:                              # noqa: BLE001
            return []

    def plan(self, weeks: float | None = None, max_items: int = 6) -> dict:
        adv = self.advisor()
        if adv is None:
            return {}
        key = ("plan", weeks, int(max_items))
        hit = self._memo_get(key)
        if hit is not None:
            return hit
        try:
            return self._memo_put(key, adv.plan(weeks=weeks, max_items=max_items).to_dict())
        except Exception:                              # noqa: BLE001
            return {}

    def capacity(self, weeks: float | None = None, limit: int = 3) -> dict:
        """建造力：成本 + 可控性 + 各档 PM 的每点容量成本（不做估值）。"""
        adv = self.advisor()
        if adv is None:
            return {}
        key = ("capacity", weeks, int(limit))
        hit = self._memo_get(key)
        if hit is not None:
            return hit
        try:
            out = {"capacity": adv.capacity(weeks=weeks, limit=limit),
                   "pms": adv.capacity_pms()[:6],
                   "note": "建造力不估值：只看建造点/现金/周维护是否可控；"
                           "pms 按每点容量的周维护从便宜到贵排"}
        except Exception:                              # noqa: BLE001
            return {}
        return self._memo_put(key, out)

    def gdp_annual(self) -> float | None:
        if self._gdp_annual is None:
            try:
                from v3calc.gdp import GDPCalculator
                self._gdp_annual = GDPCalculator(self.gd, self.snap).compute().annual
            except Exception:                          # noqa: BLE001
                self._gdp_annual = None
        return self._gdp_annual

    # -- 候选（转回旧键名 + 新字段） ------------------------------------
    @staticmethod
    def _row(opt) -> dict:
        return {
            # 旧协议（one-shot / 提示词里引用过）
            "state_id": opt.state_id, "building": opt.building,
            "remaining": opt.remaining, "suggest_levels": opt.levels,
            "gdp_per_level": round(opt.annual_value / max(1, opt.levels), 1),
            "score_gdp": round(opt.annual_value, 1),
            "cost_points_1": round(opt.points / max(1, opt.levels), 1),
            "weeks_1": opt.weeks,
            # 计算层新字段
            "annual_value": round(opt.annual_value, 1),
            "points": round(opt.points, 1), "weeks": opt.weeks,
            "per_point": round(opt.per_point, 2),
            "risk": round(opt.risk, 2), "risk_notes": opt.risk_notes,
            "chain": opt.chain, "value_basis": opt.value_basis,
            "price_changes": {k: round(v, 3) for k, v in opt.price_changes.items()},
            "cash_cost": (round(opt.cash_cost, 0) if opt.cash_cost else None),
            "roi": (round(opt.roi, 3) if opt.roi is not None else None),
            "score": round(opt.score, 2),
            "warnings": opt.warnings,
        }

    def candidates(self, top: int = 15, state_id: int | None = None,
                   building: str | None = None,
                   state_ids: list[int] | None = None) -> list[dict]:
        adv = self.advisor()
        if adv is None:
            return []
        eng = adv.engine
        try:
            if building and state_id is not None:
                opts = [eng.evaluate_build(int(state_id), building, 1)]
            elif building:
                opts = [o for o in eng.build_options(limit=None)
                        if o.building == building][:max(1, int(top))]
            elif state_id is not None:
                opts = eng.build_options(limit=top, states=[int(state_id)])
            elif state_ids:
                opts = eng.build_options(limit=top, states=[int(x) for x in state_ids])
            else:
                opts = eng.build_options(limit=top)
        except Exception:                              # noqa: BLE001
            return []
        rows = [self._row(o) for o in opts if o.score != float("-inf")]
        # 建造部门不进增值排序，但要能被提议：附在最后，标成"只算成本"
        if not building:
            try:
                for c in eng.capacity_options(
                        levels=1, budget_points=eng.cons.budget_points(12),
                        limit=2):
                    rows.append({
                        "state_id": c.state_id,
                        "building": c.building,
                        "remaining": None, "suggest_levels": c.levels,
                        "gdp_per_level": 0.0, "score_gdp": 0.0,
                        "cost_points_1": round(c.points, 1),
                        "weeks_1": c.weeks,
                        "annual_value": 0.0, "points": round(c.points, 1),
                        "weeks": c.weeks, "per_point": 0.0, "risk": 0.0,
                        "risk_notes": [], "chain": "建造",
                        "value_basis": "capacity_cost",
                        "price_changes": {},
                        "cash_cost": (round(c.cash_cost, 0) if c.cash_cost else None),
                        "roi": None,
                        "capacity_add": round(c.capacity_add, 2),
                        "weekly_upkeep": round(c.weekly_upkeep, 0),
                        "cost_per_capacity": (round(c.cost_per_capacity, 1)
                                              if c.cost_per_capacity else None),
                        "affordable": dict(c.affordable),
                        "score": 0.0,
                        "warnings": ["建造力不估值，只做成本校验"] + list(c.notes),
                    })
            except Exception:                          # noqa: BLE001
                pass
        return rows

    def estimate(self, state_id: int, building: str, levels: int = 1) -> dict:
        adv = self.advisor()
        if adv is None:
            return {}
        try:
            opt = adv.engine.evaluate_build(int(state_id), building, int(levels))
        except Exception:                              # noqa: BLE001
            return {}
        if opt.score == float("-inf"):
            return {}
        gdp_before = self.gdp_annual()
        gdp_after = (gdp_before + opt.annual_value) if gdp_before is not None else None
        out = {
            "building": building, "state_id": state_id, "levels": levels,
            "gdp_before": gdp_before,
            "gdp_after": gdp_after,
            "gdp_delta": round(opt.annual_value, 1),
            "gdp_delta_pct": (round(100.0 * opt.annual_value / gdp_before, 3)
                              if gdp_before else None),
            "annual_value": round(opt.annual_value, 1),
            "per_point": round(opt.per_point, 2),
            "risk": round(opt.risk, 2), "risk_notes": opt.risk_notes,
            "price_changes": {k: round(v, 3) for k, v in opt.price_changes.items()},
            "cash_cost": (round(opt.cash_cost, 0) if opt.cash_cost else None),
            "roi": (round(opt.roi, 3) if opt.roi is not None else None),
            "construction": {
                "cost_points": round(opt.points, 1),
                "capacity_per_week": adv.engine.cons.capacity_per_week,
                "weeks": opt.weeks,
            },
            "assumptions": {
                "pms": list(adv.engine.unit(building).pms),
                "throughput": 1.0, "staffing": "满员",
                "price_feedback": bool(adv.model.has_orders),
                "note": "年增值 = (产出−投入)×推演后价格×52；已含自价格反馈，"
                        "不含州建造效率/法令/临时修正。",
            },
        }
        if opt.value_basis == "capacity_cost":
            # 建造部门不做 GDP 估值：只回成本与可控性
            out["gdp_delta"] = 0.0
            out["gdp_after"] = gdp_before
            out["gdp_delta_pct"] = 0.0
            out["annual_value"] = 0.0
            out["per_point"] = 0.0
            out["value_basis"] = "capacity_cost"
            out["capacity_add"] = round(opt.capacity_add or 0.0, 2)
            out["weekly_upkeep"] = round(opt.weekly_upkeep or 0.0, 0)
            out["cost_per_capacity"] = (
                round(opt.cost_per_capacity, 1)
                if opt.cost_per_capacity is not None else None)
            out["affordable"] = dict(opt.affordable or {})
            out["assumptions"]["note"] = (
                "建造部门不估 GDP 价值：只算成本（建造点/现金/周维护），"
                "并判断是否在可控范围内。")
        return out

    # -- 可建性（单一事实来源：计算层 + 知识库补充） ---------------------
    def check_build(self, state_id: int, building: str, levels: int = 1) -> dict:
        """返回与旧 ``DecisionContext.check_build`` 同键名的判定结果。

        主体用 ``v3calc.candidates.CandidateEngine.check``（科技/可建/自给/
        唯一/上限/资源/耕地/队列），再补上知识库里才有的条件：
        区域限定、沿海、州特质、法律、不可扩建、唯一建筑跨州。
        """
        adv = self.advisor()
        if adv is None:
            return {}
        try:
            chk = adv.engine.check(int(state_id), building, int(levels))
        except Exception:                              # noqa: BLE001
            return {}
        reasons = list(chk.reasons)
        warnings = list(chk.warnings)
        meta = self.state_meta.get(int(state_id))
        bi = self.buildings_kb.get(building)
        if bi is not None:
            if not bi.buildable:
                reasons.append("%s 不是玩家可建造建筑" % building)
            if bi.subsistence:
                reasons.append("%s 是自给建筑，玩家不能主动建造" % building)
            missing_laws = [l for l in bi.laws
                            if l not in (self.snap.law_set() if self.snap else set())]
            if missing_laws:
                reasons.append("缺法律：" + ", ".join(missing_laws))
            if bi.allowed_regions:
                region = meta.region if meta else None
                if region and region not in bi.allowed_regions:
                    reasons.append("只能建在 %s" % ", ".join(bi.allowed_regions))
                elif not region:
                    warnings.append("州缺少区域映射，无法校验地理限制")
            if bi.requires_coastal and meta is not None and not meta.coastal:
                reasons.append("需要沿海州")
            if bi.requires_incorporated and meta is not None \
                    and meta.incorporated is False:
                warnings.append("静态地理显示该州未整合；实时整合状态未读，交由执行端裁决")
            if bi.requires_traits and meta is not None:
                if not (set(bi.requires_traits) & set(meta.traits or [])):
                    reasons.append("需要州特质：" + ", ".join(bi.requires_traits))
            if not bi.expandable and chk.existing > 0:
                reasons.append("%s 不可扩建（已存在 %d 级）" % (building, chk.existing))
            if bi.unique and chk.existing > 0:
                others = [b.state_id for b in (self.snap.buildings if self.snap else [])
                          if b.type == building and b.state_id != int(state_id)]
                if others:
                    reasons.append("唯一建筑，已存在于州 %s"
                                   % ", ".join(str(x) for x in others[:4]))

        cons = adv.engine.cons
        cost_points = cons.cost_points(building, int(levels))
        out = {
            "ok": not reasons,
            "code": "ok" if not reasons else "rejected",
            "building": building, "state_id": int(state_id),
            "levels": int(levels), "reasons": reasons, "warnings": warnings,
            "region": (meta.region if meta else None),
            "remaining": chk.remaining, "cap": chk.cap,
            "existing_levels": chk.existing, "queued_levels": chk.queued,
            "projected_levels": chk.projected,
            "cost_points": round(cost_points, 1),
            "cost_category": cons.cost_category(building),
            "source": "calc",
        }
        return out

    def pm_candidates(self, top: int = 10) -> list[dict]:
        adv = self.advisor()
        if adv is None:
            return []
        try:
            rows = adv.engine.pm_options(limit=top)
        except Exception:                              # noqa: BLE001
            return []
        out = []
        for o in rows:
            out.append({
                "building": o.building, "group": o.group,
                "candidate": o.pm, "pm": o.pm,
                "current": list(o.current),
                "status": "available",
                "delta": round(o.annual_value, 1),
                "annual_value": round(o.annual_value, 1),
                "risk": round(o.risk, 2), "risk_notes": o.risk_notes,
                "price_changes": {k: round(v, 3) for k, v in o.price_changes.items()},
                "non_gdp_effects": o.warnings,
                "score": round(o.score, 2),
            })
        return out

    # -- 台账 ---------------------------------------------------------
    def record(self, commands) -> dict | None:
        """把本轮真正执行出去的建造记成一条预测（供日后对账）。"""
        adv = self.advisor()
        if adv is None:
            return None
        from types import SimpleNamespace

        items = []
        for c in commands:
            if str(getattr(c, "kind", "")) != "build":
                continue
            sid = safe_int(getattr(c, "state_id", None), None)
            b = str(getattr(c, "building", "") or "")
            if sid is None or not b:
                continue
            try:
                opt = adv.engine.evaluate_build(
                    sid, b, max(1, int(getattr(c, "levels", 1) or 1)))
            except Exception:                          # noqa: BLE001
                continue
            if opt.score != float("-inf"):
                items.append(opt)
        if not items:
            return None
        plan = SimpleNamespace(
            items=items,
            total_points=sum(o.points for o in items),
            total_annual_value=sum(o.annual_value for o in items))
        return adv.ledger.record_plan(plan, self.snap, self.gd, note="cycle")

    def reconcile(self) -> dict:
        adv = self.advisor()
        if adv is None:
            return {}
        try:
            return adv.ledger.reconcile(self.snap, self.gd)
        except Exception:                              # noqa: BLE001
            return {}
