# -*- coding: utf-8 -*-
"""一页纸简报：给 LLM（或人）看的**压缩态势**。

目标：**不超过 ~2500 字符**，但决策要用的数字全在。做法是三层压缩：

1. 54 个商品 → 10 条链（只给链级短缺/过剩与最紧的商品）；
2. 几千个可建组合 → Top N 候选包（每行 5 个数字，见 ``candidates.py``）；
3. 全部信号 → 只留最严重的几个（见 ``signals.py``）。

``render_brief`` 会按字符预算裁剪：先砍"变化对比"，再砍"生产方式"，
最后才动候选表——因为候选表是决策的主干。
"""
from __future__ import annotations

from .candidates import CandidateEngine, Intent
from .construction import StateGeo
from .market import MarketView, PriceModel
from .model import Snapshot
from .signals import scan

DEFAULT_CHAR_BUDGET = 2600


def fmt_money(v: float | None) -> str:
    if v is None:
        return "?"
    a = abs(v)
    if a >= 1e8:
        return "%.2f亿" % (v / 1e8)
    if a >= 1e4:
        return "%.2f万" % (v / 1e4)
    if a >= 100:
        return "%.0f" % v
    return ("%.1f" % v).rstrip("0").rstrip(".")


def _fmt_int(v: float | None) -> str:
    if v is None:
        return "?"
    v = float(v)
    if abs(v) >= 1e8:
        return "%.2f亿" % (v / 1e8)
    if abs(v) >= 1e4:
        return "%.2f万" % (v / 1e4)
    return f"{int(round(v)):,}"


def _short_building(name: str) -> str:
    return name.replace("building_", "")


def build_brief(gd, snap: Snapshot, prev: Snapshot | None = None,
                top: int = 8, geo: dict[int, StateGeo] | None = None,
                intent: Intent | None = None,
                model: PriceModel | None = None,
                weeks: float = 4.0,
                include_pm: bool = True) -> dict:
    """汇总成一个大 dict（可直接 JSON 化，也可交给 render_brief）。"""
    model = model or PriceModel.from_snapshot(gd, snap)
    view = MarketView(gd, model)
    engine = CandidateEngine(gd, snap, geo=geo, intent=intent)
    cons = engine.cons
    capacity = cons.capacity()
    budget = cons.budget_points(weeks)
    cands = _dedupe_by_building(engine.build_options(limit=None))[:top]
    pms = engine.pm_options(limit=6) if include_pm else []
    signals = scan(gd, snap, model=model, view=view, prev=prev,
                   capacity=capacity)
    plan = engine.plan(budget, max_items=min(6, top))
    caps = engine.capacity_options(levels=1, budget_points=budget, limit=2)

    c = snap.country
    meta = {
        "source": snap.game.source,
        "date": snap.game.save_date or snap.game.captured_at,
        "version": snap.game.game_version,
        "country": (c.tag or c.name or ("国家%s" % c.id)) if c else "?",
        "country_id": c.id if c else None,
        "population": c.population if c else None,
        "treasury": c.treasury if c else None,
        "credit_limit": c.credit_limit if c else None,
        "loan_principal": c.loan_principal if c else None,
        "balance": (c.budget.balance if c and c.budget else None),
        "tax_level": (c.budget.tax_level if c and c.budget else None),
        "states": len(snap.states),
        "buildings": len(snap.buildings),
        "gdp_annual": _gdp_annual(gd, snap),
    }
    return {
        "meta": meta,
        "market": {
            "summary": view.summary(),
            "chains": [r.to_dict() for r in view.chains()],
            "shortages": [p.to_dict() for p in view.shortages(6)],
            "gluts": [p.to_dict() for p in view.gluts(5)],
            "ceiling": [p.good for p in view.at_ceiling()],
        },
        "capacity": {
            **capacity,
            "weeks": weeks,
            "budget_points": budget,
            "cash_per_point": _r(cons.cash_per_point()),
            "cash_budget": _r(cons.cash_budget()),
            "queued": sum(engine.queued.values()) if engine.queued else 0,
        },
        "candidates": [o.to_dict() for o in cands],
        "capacity_options": [c.to_dict() for c in caps],
        "capacity_pms": engine.capacity_pm_options()[:4],
        "pms": [o.to_dict() for o in pms],
        "plan": plan.to_dict(),
        "signals": [s.to_dict() for s in signals],
        "changes": _changes(prev, snap, model, gd) if prev is not None else None,
    }


def _r(v, n=1):
    return round(v, n) if isinstance(v, (int, float)) else None


def _gdp_annual(gd, snap: Snapshot) -> float | None:
    try:
        from .gdp import GDPCalculator
        return GDPCalculator(gd, snap).compute().annual
    except Exception:                                  # noqa: BLE001
        return None


def _dedupe_by_building(options: list) -> list:
    seen: set[str] = set()
    out = []
    for o in options:
        if o.building in seen:
            continue
        seen.add(o.building)
        out.append(o)
    return out


def _changes(prev: Snapshot | None, snap: Snapshot,
             model: PriceModel, gd) -> dict:
    if prev is None:
        return {}
    out: dict = {}
    try:
        old = PriceModel.from_snapshot(gd, prev)
        moved = []
        for g in model.base:
            a, b = old.multiplier(g), model.multiplier(g)
            if a > 0 and b > 0 and abs(b - a) >= 0.15:
                moved.append({"good": g, "from": round(a, 3), "to": round(b, 3)})
        out["price_moves"] = sorted(moved, key=lambda x: -abs(
            x["to"] - x["from"]))[:6]
    except Exception:                                  # noqa: BLE001
        pass
    a = (prev.country.budget.balance if prev.country and prev.country.budget
         else None)
    b = (snap.country.budget.balance if snap.country and snap.country.budget
         else None)
    if a is not None and b is not None:
        out["balance_delta"] = round(b - a, 1)
    out["state_delta"] = len(snap.states) - len(prev.states)
    out["levels_delta"] = (sum(x.levels for x in snap.buildings) -
                           sum(x.levels for x in prev.buildings))
    return out


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _chain_table(market: dict) -> list[str]:
    rows = market.get("chains") or []
    lines = ["链        商品 短缺 过剩 最紧（比价）        缺口/周"]
    for r in rows:
        tight = ("%s %.2f" % (r["tightest"], r["tightest_ratio"])
                 if r["tightest"] else "-")
        lines.append("%-9s %3d %4d %4d %-20s %s" % (
            r["chain"], r["n"], r["shortage"], r["glut"], tight,
            fmt_money(r["shortage_value"]) if r["shortage_value"] else "-"))
    s = market.get("summary") or {}
    lines.append("合计：短缺 %d / 过剩 %d / 触顶 %d / 触底 %d" % (
        s.get("n_shortage", 0), s.get("n_glut", 0),
        s.get("n_ceiling", 0), s.get("n_floor", 0)))
    if not s.get("has_orders", True):
        lines.append("（存档模式无买卖单：「短缺/过剩」只是世界价/基础价的估计）")
    return lines


def _candidate_table(cands: list[dict]) -> list[str]:
    lines = ["#  州    建筑                    级  点   周   年增值    每点   风险 链"]
    for i, c in enumerate(cands, 1):
        lines.append("%-2d %-5s %-23s %-2d %-4.0f %-4s %-9s %-6.1f %-4.2f %s" % (
            i, c["state_id"], _short_building(c["building"]), c["levels"],
            c["points"], ("%.1f" % c["weeks"]) if c["weeks"] is not None else "?",
            fmt_money(c["annual_value"]), c["per_point"], c["risk"], c["chain"]))
        notes = (c.get("risk_notes") or []) + (c.get("warnings") or [])
        if notes:
            lines.append("      · " + "；".join(str(x) for x in notes[:2])[:90])
    return lines


def _pm_table(pms: list[dict]) -> list[str]:
    lines = ["建筑 → 生产方式                          年增值    风险 备注"]
    for p in pms:
        note = "；".join((p.get("risk_notes") or []) + (p.get("warnings") or []))
        lines.append("%-22s → %-34s %-9s %-4.2f %s" % (
            _short_building(p["building"])[:22], str(p["pm"])[:34],
            fmt_money(p["annual_value"]), p["risk"], note[:36]))
    return lines


def _signal_table(signals: list[dict]) -> list[str]:
    lines = []
    for s in signals:
        lines.append("[S%d] %s — %s" % (s["severity"], s["title"], s["suggested"]))
    return lines


def render_brief(brief: dict, char_budget: int = DEFAULT_CHAR_BUDGET) -> str:
    """把 brief 渲染成给 LLM 的纯文本，控制在 ``char_budget`` 内。"""
    m = brief["meta"]
    cap = brief["capacity"]
    parts: list[str] = []
    when = m.get("date") or "?"
    if "T" in str(when):
        when = str(when)[:16].replace("T", " ")
    parts.append("== 国势 ==")
    parts.append("%s %s %s  人口 %s  年GDP %s" % (
        when, m.get("source") or "?",
        m.get("country") or "?", _fmt_int(m.get("population")),
        fmt_money(m.get("gdp_annual"))))
    parts.append("国库 %s（信贷 %s，已借 %s）  周结余 %s  税级 %s" % (
        fmt_money(m.get("treasury")), fmt_money(m.get("credit_limit")),
        fmt_money(m.get("loan_principal")), fmt_money(m.get("balance")),
        m.get("tax_level")))
    cash_pp = cap.get("cash_per_point")
    parts.append("建造力 %s/周  本轮预算 %s 点（%s 周）  现金 %s/点 余量 %s" % (
        cap.get("capacity_per_week"), fmt_money(cap.get("budget_points")),
        cap.get("weeks"),
        ("%.0f" % cash_pp) if cash_pp else "?",
        fmt_money(cap.get("cash_budget"))))
    plan = brief.get("plan") or {}
    if plan.get("items"):
        parts.append("贪心方案：%d 项 / %s 点 ≈ %s 周，年增值 +%s（明细见下表前几行）" % (
            len(plan["items"]), fmt_money(plan.get("total_points")),
            plan.get("weeks"), fmt_money(plan.get("total_annual_value"))))
    caps = brief.get("capacity_options") or []
    if caps:
        c = caps[0]
        verdict = "可承受" if c["ok"] else "超限：" + "、".join(
            k for k, v in (c.get("affordable") or {}).items() if not v)
        parts.append(
            "建造力（只算成本，不估值）：+1 级 = %s 点 / 现金 %s / +%.1f 容量/周；"
            "周维护 %s，每点容量 %s → %s" % (
                fmt_money(c["points"]), fmt_money(c.get("cash_cost")),
                c.get("capacity_add") or 0, fmt_money(c.get("weekly_upkeep")),
                fmt_money(c.get("cost_per_capacity")), verdict))

    sections: list[tuple[str, list[str]]] = []
    sections.append(("market", ["", "== 市场（按产业链）=="] +
                     _chain_table(brief["market"])))
    if brief.get("signals"):
        sections.append(("signals", ["", "== 信号 =="] +
                         _signal_table(brief["signals"][:6])))
    sections.append(("candidates", ["", "== 候选包（年增值已扣价格反馈；每点=年增值/建造点）=="] +
                     _candidate_table(brief["candidates"])))
    if brief.get("pms"):
        sections.append(("pms", ["", "== 生产方式（无建造成本，优先做）=="] +
                         _pm_table(brief["pms"])))
    if brief.get("changes"):
        ch = brief["changes"]
        bits = []
        if ch.get("balance_delta"):
            bits.append("结余 %+.0f" % ch["balance_delta"])
        if ch.get("levels_delta"):
            bits.append("等级 %+d" % ch["levels_delta"])
        for mv in (ch.get("price_moves") or [])[:4]:
            bits.append("%s %.2f→%.2f" % (mv["good"], mv["from"], mv["to"]))
        if bits:
            sections.append(("changes", ["", "== 变化 ==", "；".join(bits)]))

    text = "\n".join(parts)
    secs = {name: lines for name, lines in sections}
    # 入选优先级：候选表是决策主干，市场是上下文，其余可牺牲
    priority = ["candidates", "market", "signals", "pms", "changes"]
    # 展示顺序：市场 → 信号 → 候选 → 生产方式 → 变化
    display = ["market", "signals", "candidates", "pms", "changes"]
    chosen: dict[str, list[str]] = {}
    used = len(text)
    for name in priority:
        lines = secs.get(name)
        if not lines:
            continue
        n = len(lines)
        # 先试全量，放不下就逐级截断（至少留表头 + 2 行）
        for keep in (n, n - 2, 6, 4, 3):
            keep = max(3, min(keep, n))
            cut = lines[:keep]
            if keep < n:
                cut = cut + ["…（截断）"]
            add = len("\n" + "\n".join(cut))
            if used + add <= char_budget:
                chosen[name] = cut
                used += add
                break
    out = [text]
    for name in display:
        if name in chosen:
            out.append("\n".join(chosen[name]))
    return "\n".join(out)


def brief_text(gd, snap: Snapshot, **kw) -> str:
    budget = kw.pop("char_budget", DEFAULT_CHAR_BUDGET)
    return render_brief(build_brief(gd, snap, **kw), budget)
