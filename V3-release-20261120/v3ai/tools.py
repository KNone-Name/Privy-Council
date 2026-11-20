# -*- coding: utf-8 -*-
"""工具集：LLM 通过 OpenAI function-calling 或 MCP 调用的同一批工具。

工具只读"当前 Runtime"；``propose_*`` 会立刻走审查层，把拒绝原因返回给 LLM。
同一份注册表同时给：
  * ``v3ai.agent`` 的 in-process 工具循环；
  * ``v3ai.mcp_server`` 的 stdio MCP server（外部客户端也能用）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from .review import Command
from .strategy import Strategy
from .util import safe_int

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Runtime


def _schema(props: dict, required: list[str] | None = None, desc: str = "") -> dict:
    return {
        "type": "object",
        "description": desc,
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


def _p(desc: str, typ: str = "string", **kw) -> dict:
    d = {"type": typ, "description": desc}
    d.update(kw)
    return d


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[["Runtime", dict], Any]

    def openai(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.input_schema}}

    def mcp(self) -> dict:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def keep(self, names: set[str]) -> None:
        self._tools = {k: v for k, v in self._tools.items() if k in names}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def openai_tools(self) -> list[dict]:
        return [t.openai() for t in self._tools.values()]

    def mcp_tools(self) -> list[dict]:
        return [t.mcp() for t in self._tools.values()]

    def call(self, rt: "Runtime", name: str, args: dict | None = None) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": "未知工具：%s" % name}
        try:
            out = tool.handler(rt, args or {})
            if isinstance(out, dict):
                out.setdefault("ok", True)
                return out
            return {"ok": True, "result": out}
        except Exception as exc:  # 工具错误返回给 LLM，不终止循环
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def _state_row(rt: "Runtime", sid: int) -> dict | None:
    ctx = rt.ctx
    s = ctx.states.get(sid) if ctx else None
    if s is None:
        return None
    meta = ctx.state_meta.get(sid)
    rows = [b for b in ctx.snap.buildings if b.state_id == sid]
    return {
        "id": s.id, "owner": s.owner,
        "region": meta.region if meta else None,
        "population": s.population, "arable": s.arable_land,
        "infra": round(s.infrastructure or 0, 2),
        "infra_used": round(s.infrastructure_used or 0, 2),
        "infra_free": round((s.infrastructure or 0) - (s.infrastructure_used or 0), 2),
        "radicals": s.radicals, "unemployed": s.unemployed, "laborers": s.laborers,
        "buildings": len(rows), "levels": sum(b.levels for b in rows),
        "resources": (meta.resources[:10] if meta else []),
        "resource_caps": (meta.resource_caps if meta else {}),
        "traits": (meta.traits if meta else []),
        "coastal": (meta.coastal if meta else None),
        "incorporated": (meta.incorporated if meta else None),
    }


def t_overview(rt, args):
    return rt.overview()


def t_refresh(rt, args):
    if rt.cycle_active:
        return {"ok": False, "code": "cycle_frozen",
                "error": "本回合世界已冻结：LLM 思考期间不重新读数据；"
                         "执行反馈处理完、下一回合才会刷新"}
    rt.refresh()
    return rt.overview()


def t_query_states(rt, args):
    ctx = rt.ctx
    ids = args.get("state_ids") or []
    ids = [int(x) for x in ids if safe_int(x) is not None]
    region = str(args.get("region") or "").strip().upper()
    rows = []
    for s in ctx.snap.states:
        if ids and s.id not in ids:
            continue
        meta = ctx.state_meta.get(s.id)
        if region and (not meta or (meta.region or "").upper() != region):
            continue
        row = _state_row(rt, s.id)
        if row:
            rows.append(row)
    sort = str(args.get("sort") or "population")
    keys = {
        "population": lambda x: -(x["population"] or 0),
        "arable": lambda x: -(x["arable"] or 0),
        "radicals": lambda x: -(x["radicals"] or 0),
        "infrastructure_free": lambda x: -(x["infra_free"] or 0),
        "id": lambda x: x["id"],
    }
    rows.sort(key=keys.get(sort, keys["population"]))
    offset = max(0, int(safe_int(args.get("offset"), 0) or 0))
    limit = max(1, min(200, int(safe_int(args.get("limit"), 20) or 20)))
    return {"total": len(rows), "offset": offset, "limit": limit,
            "states": rows[offset:offset + limit]}


def t_state_detail(rt, args):
    sid = safe_int(args.get("state_id"), None)
    if sid is None:
        return {"ok": False, "error": "state_id 必填"}
    row = _state_row(rt, sid)
    if row is None:
        return {"ok": False, "error": "州 %s 不在当前快照" % sid}
    ctx = rt.ctx
    meta = ctx.state_meta.get(sid)
    built = [{"type": b.type, "levels": b.levels, "staffing": round(b.staffing, 2),
              "throughput": round(b.throughput, 2), "profit": b.profit,
              "pms": b.production_methods}
             for b in ctx.snap.buildings if b.state_id == sid]
    arable = []
    if meta:
        used = 0
        for r in meta.arable_resources():
            used += ctx.projected_levels(sid, r)
        arable = [{"building": r, "used_or_projected": ctx.projected_levels(sid, r)}
                  for r in meta.arable_resources()]
        arable.append({"arable_land": meta.arable_land, "used": used,
                       "free": (meta.arable_land - used) if meta.arable_land is not None else None})
    return {"state": row, "buildings": built,
            "arable": arable,
            "queue": ctx.queued_entries(state_id=sid),
            "pending": [{"id": c.id, "kind": c.kind, "building": c.building,
                         "levels": c.levels, "pm": c.pm}
                        for c in ctx.pending if getattr(c, "state_id", None) == sid]}


def t_query_buildings(rt, args):
    ctx = rt.ctx
    btype = str(args.get("building") or "").strip()
    sid = safe_int(args.get("state_id"), None)
    rows = []
    for b in ctx.snap.buildings:
        if btype and b.type != btype:
            continue
        if sid is not None and b.state_id != sid:
            continue
        meta = ctx.state_meta.get(b.state_id)
        rows.append({
            "type": b.type, "state_id": b.state_id,
            "region": meta.region if meta else None,
            "levels": b.levels, "staffing": round(b.staffing, 2),
            "throughput": round(b.throughput, 2),
            "profit": round(b.profit, 1) if b.profit is not None else None,
            "salary_rate": b.salary_rate, "hiring_rate": b.hiring_rate,
            "pms": b.production_methods,
        })
    sort = str(args.get("sort") or "levels")
    if sort == "profit":
        rows.sort(key=lambda x: (x["profit"] is None, x["profit"] or 0))
    elif sort == "staffing":
        rows.sort(key=lambda x: (x["staffing"] or 0) / max(1, x["levels"]))
    else:
        rows.sort(key=lambda x: -x["levels"])
    limit = max(1, min(500, int(safe_int(args.get("limit"), 50) or 50)))
    return {"total": len(rows), "buildings": rows[:limit]}


def t_building_detail(rt, args):
    ctx = rt.ctx
    btype = str(args.get("building") or "").strip()
    if not btype:
        return {"ok": False, "error": "building 必填"}
    bi = ctx.buildings_kb.get(btype)
    rows = [b for b in ctx.snap.buildings if b.type == btype]
    groups = []
    rep = ctx.pm_checker().building_report(btype)
    for g in rep.groups:
        groups.append({"group": g.group, "current": g.current,
                       "pms": [s.to_dict() for s in g.statuses]})
    return {
        "building": btype,
        "kb": bi.to_dict() if bi else None,
        "instances": [{"state_id": b.state_id, "levels": b.levels,
                       "staffing": b.staffing, "throughput": b.throughput,
                       "profit": b.profit, "pms": b.production_methods} for b in rows],
        "groups": groups,
        "warnings": rep.warnings,
    }


def t_query_market(rt, args):
    ctx = rt.ctx
    d = ctx.market_digest(top_n=int(safe_int(args.get("top_n"), 8) or 8))
    goods = args.get("goods") or []
    if goods and ctx.snap.market:
        wanted = set(str(g) for g in goods)
        base = {g.name: g.cost for g in ctx.gd.goods}
        d["selected"] = [
            {"good": g, "price": ctx.snap.market.prices.get(g),
             "base": base.get(g),
             "supply": ctx.snap.market.supply.get(g),
             "demand": ctx.snap.market.demand.get(g)}
            for g in wanted]
    return d


def t_query_budget(rt, args):
    return rt.ctx.budget_digest()


def t_query_politics(rt, args):
    return rt.ctx.politics_digest()


def t_query_techs(rt, args):
    ctx = rt.ctx
    researched = sorted(ctx.techs)
    avail = []
    for name, t in ctx.gd.techs.items():
        if name in ctx.techs or not t.can_research:
            continue
        if t.prerequisites and not all(p in ctx.techs for p in t.prerequisites):
            continue
        avail.append({"tech": name, "era": t.era, "category": t.category,
                      "prerequisites": list(t.prerequisites)})
    avail.sort(key=lambda x: (x["era"], x["tech"]))
    limit = max(1, min(100, int(safe_int(args.get("limit"), 30) or 30)))
    return {"researched_count": len(researched),
            "researched": researched if args.get("all") else researched[:60],
            "available_next": avail[:limit]}


def t_query_laws(rt, args):
    ctx = rt.ctx
    current = {l.name for l in ctx.snap.laws}
    groups: dict[str, list[str]] = {}
    for name, law in ctx.gd.laws.items():
        if law.group:
            groups.setdefault(law.group, []).append(name)
    alternatives = []
    current_by_group = {l.group: l.name for l in ctx.snap.laws if l.group}
    for g, name in current_by_group.items():
        for cand in groups.get(g, []):
            if cand in current:
                continue
            law = ctx.gd.laws.get(cand)
            if law is None:
                continue
            if law.unlocking_technologies and not all(t in ctx.techs for t in law.unlocking_technologies):
                continue
            if law.unlocking_laws and not all(l in current for l in law.unlocking_laws):
                continue
            alternatives.append({"group": g, "current": name, "candidate": cand,
                                 "progressiveness": law.progressiveness})
    return {"laws": [{"name": l.name, "group": l.group} for l in ctx.snap.laws],
            "alternatives": alternatives[:40]}


def t_calc_gdp(rt, args):
    ctx = rt.ctx
    price = str(args.get("price") or "auto")
    scale = str(args.get("scale") or "staffing")
    buildings = str(args.get("buildings") or "producers")
    g = ctx.gdp(price=price, scale=scale, buildings=buildings)
    out = g.to_dict()
    by = str(args.get("by") or "none")
    if by == "goods":
        out["goods"] = out.get("goods", [])[:20]
    elif by == "buildings":
        out["building_types"] = out.get("building_types", [])[:20]
    elif by == "states":
        out["states"] = out.get("states", [])[:20]
    else:
        out.pop("goods", None)
        out.pop("building_types", None)
        out.pop("states", None)
        out["top_goods"] = [{"good": x.good, "net_value": round(x.net_value, 1)}
                            for x in g.top_goods(8)]
        out["top_building_types"] = [{"type": x.type, "levels": x.levels,
                                      "net": round(x.net, 1)}
                                     for x in g.top_buildings(8)]
    return out


def t_pm_report(rt, args):
    ctx = rt.ctx
    btype = str(args.get("building") or "").strip()
    if not btype:
        return {"ok": False, "error": "building 必填"}
    rep = ctx.pm_checker().building_report(btype)
    only = str(args.get("status") or "all")
    out_groups = []
    for g in rep.groups:
        pms = []
        for s in g.statuses:
            if only != "all" and s.status != only:
                continue
            d = s.to_dict()
            d["effects"] = ctx.pm_effects(s.pm)
            pms.append(d)
        out_groups.append({"group": g.group, "current": g.current, "pms": pms})
    return {"building": btype, "building_unlocked": not rep.locked_by_tech,
            "missing_building_techs": rep.missing_building_techs,
            "groups": out_groups, "warnings": rep.warnings}


def t_pm_upgrades(rt, args):
    ctx = rt.ctx
    btype = args.get("building")
    if not btype:
        # 默认取等级最多的 5 类建筑，避免一次扫全国
        top = sorted(ctx.levels_by_type.items(), key=lambda kv: -kv[1])[:5]
        rows = []
        for t, _n in top:
            rows.extend(ctx.pm_candidates(t, state_id=safe_int(args.get("state_id"), None),
                                          top=10,
                                          include_conditional=bool(args.get("include_conditional"))))
        rows.sort(key=lambda x: -x["delta"])
        return {"building": None, "candidates": rows[:max(1, int(safe_int(args.get("top"), 20) or 20))]}
    rows = ctx.pm_candidates(str(btype), state_id=safe_int(args.get("state_id"), None),
                             top=max(1, int(safe_int(args.get("top"), 15) or 15)),
                             include_conditional=bool(args.get("include_conditional")))
    return {"building": str(btype), "candidates": rows}


def t_build_candidates(rt, args):
    ctx = rt.ctx
    return {"candidates": ctx.build_candidates(
        state_id=safe_int(args.get("state_id"), None),
        building=(str(args["building"]) if args.get("building") else None),
        top=max(1, int(safe_int(args.get("top"), 15) or 15)))}


def t_estimate_build(rt, args):
    ctx = rt.ctx
    sid = safe_int(args.get("state_id"), None)
    btype = str(args.get("building") or "").strip()
    if sid is None or not btype:
        return {"ok": False, "error": "state_id 和 building 必填"}
    levels = max(1, int(safe_int(args.get("levels"), 1) or 1))
    chk = ctx.check_build(sid, btype, levels)
    if not chk.get("ok"):
        return {"ok": False, "error": "不可建", "check": chk}
    est = ctx.estimate_build(sid, btype, levels)
    est["check"] = chk
    return est


def t_advise_brief(rt, args):
    """计算层的"一页纸简报"：市场按链、候选包（含价格反馈）、信号、贪心方案。"""
    payload = rt.advise_payload(char_budget=safe_int(args.get("char_budget"), None),
                                top=safe_int(args.get("top"), None))
    if not payload:
        return {"ok": False,
                "error": "决策计算层不可用：检查 advisor.enabled、游戏数据目录，"
                         "或先跑 python -m v3calc selftest"}
    return payload


def t_advise_plan(rt, args):
    if rt.bridge is None or not rt.bridge.available:
        return {"ok": False, "error": "决策计算层不可用"}
    weeks = args.get("weeks")
    plan = rt.bridge.plan(
        weeks=(float(weeks) if weeks not in (None, "") else None),
        max_items=max(1, int(safe_int(args.get("max_items"), 6) or 6)))
    if not plan:
        return {"ok": False, "error": "算不出方案"}
    return {"plan": plan}


def t_advise_signals(rt, args):
    if rt.bridge is None or not rt.bridge.available:
        return {"ok": False, "error": "决策计算层不可用"}
    sig = rt.bridge.signals()
    return {"signals": sig,
            "escalate": any(int(s.get("severity") or 0) >= 3 for s in sig),
            "note": "severity 3 = 会崩，必须先处理；空列表 = 市况平稳"}


def t_advise_capacity(rt, args):
    if rt.bridge is None or not rt.bridge.available:
        return {"ok": False, "error": "决策计算层不可用"}
    weeks = args.get("weeks")
    out = rt.bridge.capacity(
        weeks=(float(weeks) if weeks not in (None, "") else None),
        limit=max(1, int(safe_int(args.get("limit"), 3) or 3)))
    if not out:
        return {"ok": False, "error": "算不出建造力成本"}
    return out


def t_strategy_get(rt, args):
    return {"active": rt.strategy.summary(), "history_count": len(rt.strategy_history(50))}


def t_strategy_propose(rt, args):
    raw = args.get("strategy")
    if not isinstance(raw, dict):
        return {"ok": False, "error": "strategy 必须是一个完整策略对象"}
    try:
        st = rt.set_strategy(Strategy.from_dict(raw), source="ai")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "active": st.summary(),
            "note": "AI 已切换策略；审查层的硬护栏不会因策略切换而关闭"}


def _propose(rt, cmd: Command) -> dict:
    res = rt.propose(cmd)
    out = {"approved": res.approved, "code": res.code, "message": res.message,
           "reasons": res.reasons, "warnings": res.warnings,
           "command_id": cmd.id,
           "command": {"id": cmd.id, "kind": cmd.kind, "state_id": cmd.state_id,
                       "building": cmd.building, "levels": cmd.levels,
                       "pm": cmd.pm, "status": cmd.status}}
    if not res.approved:
        out["hint"] = ("看 reasons；不要重复同一被拒动作。"
                       "科技/法律/资源/上限问题换目标或换州，条件未知的 PM 需人工确认。")
    return out


def t_propose_build(rt, args):
    sid = safe_int(args.get("state_id"), None)
    btype = str(args.get("building") or "").strip()
    levels = max(1, int(safe_int(args.get("levels"), 1) or 1))
    cmd = Command(kind="build", state_id=sid, building=btype, levels=levels,
                  source=str(args.get("source") or "llm"),
                  rationale=str(args.get("rationale") or ""))
    return _propose(rt, cmd)


def t_propose_pm(rt, args):
    sid = safe_int(args.get("state_id"), None)
    btype = str(args.get("building") or "").strip()
    pm = str(args.get("pm") or "").strip()
    cmd = Command(kind="pm", state_id=sid, building=btype, pm=pm,
                  source=str(args.get("source") or "llm"),
                  rationale=str(args.get("rationale") or ""))
    return _propose(rt, cmd)


def t_pending(rt, args):
    return {"pending": [{"id": c.id, "kind": c.kind, "state_id": c.state_id,
                         "building": c.building, "levels": c.levels, "pm": c.pm,
                         "status": c.status, "message": (c.review or {}).get("message")}
                        for c in rt.pending],
            "count": len(rt.pending)}


def t_cancel(rt, args):
    cid = str(args.get("command_id") or "")
    return {"ok": rt.cancel(cid), "command_id": cid}


def t_execute_pending(rt, args):
    if rt.cycle_active:
        return {"ok": False, "code": "cycle_frozen",
                "error": "执行只在 LLM 决策完成后的回合末进行；请先 finish_cycle，"
                         "由运行时统一执行并处理 DLL 反馈"}
    results = rt.execute_pending()
    return {"executed": [r.to_dict() for r in results], "count": len(results)}


def t_queue(rt, args):
    caps = dict(rt._caps or {})
    if rt.cycle_active:
        q = list(rt.queue)
        caps["cycle_frozen"] = True
    else:
        try:
            q = rt.executor.queue()
        except Exception as exc:
            q = []
            caps = dict(caps)
            caps["queue_error"] = str(exc)
    return {"capabilities": caps, "queue": q[:40], "count": len(q),
            "cycle_frozen": rt.cycle_active}


def t_feedback(rt, args):
    return rt.feedback()


def t_history(rt, args):
    return {"cycles": rt.recent_history(int(safe_int(args.get("limit"), 10) or 10))}


def t_finish(rt, args):
    return {"ok": True, "finished": True,
            "summary": str(args.get("summary") or ""),
            "rationale": str(args.get("rationale") or "")}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
# 小模型/小上下文用的核心工具集
CORE_TOOLS = {
    "get_overview", "advise_brief", "query_states", "state_detail", "query_buildings",
    "query_market", "query_budget", "calc_gdp", "pm_report",
    "pm_upgrades", "build_candidates", "estimate_build", "strategy_get",
    "strategy_propose", "propose_build", "propose_pm", "pending_actions",
    "execution_feedback", "finish_cycle",
}


def build_registry(core_only: bool = False) -> ToolRegistry:
    r = ToolRegistry()
    r.register(Tool("get_overview",
                    "看当前国家概况：预算/GDP/市场/州/建筑/政治/队列/策略。决策第一步。",
                    _schema({}), t_overview))
    r.register(Tool("advise_brief",
                    "算好的简报：市场按产业链、候选包（年增值已扣价格反馈+风险）、"
                    "信号、贪心方案。决策优先看它，比 get_overview 更省 token。",
                    _schema({"char_budget": _p("字符上限（默认 2400）", "integer"),
                             "top": _p("候选条数（默认 8）", "integer")}),
                    t_advise_brief))
    r.register(Tool("advise_plan",
                    "按建造力预算做逐步贪心：选一项→按推演价格重算→再选。"
                    "返回方案与备选，不是命令。",
                    _schema({"weeks": _p("队列深度（周）", "number"),
                             "max_items": _p("最多几项（默认 6）", "integer")}),
                    t_advise_plan))
    r.register(Tool("advise_signals",
                    "触发信号：价格触顶/触底/短缺、赤字、基建饱和、激进派。"
                    "空列表 = 本轮不需要额外干预。",
                    _schema({}), t_advise_signals))
    r.register(Tool("advise_capacity",
                    "建造力成本：+1 级建造部门的建造点/现金/周维护，是否在可控范围；"
                    "各档建造方式按每点容量的周成本排序。不估价值。",
                    _schema({"weeks": _p("队列深度（周）", "number"),
                             "limit": _p("落点数量（默认 3）", "integer")}),
                    t_advise_capacity))
    r.register(Tool("refresh_world",
                    "重新读快照和建造队列（执行动作后用；普通查询不需要）。",
                    _schema({}), t_refresh))
    r.register(Tool("query_states",
                    "按州查询人口/耕地/基础设施/激进派/资源；可按人口/耕地/激进派/基建余量排序。",
                    _schema({
                        "state_ids": _p("州 id 列表；空=全部玩家州", "array",
                                        items={"type": "integer"}),
                        "region": _p("州区域名过滤，如 STATE_BEIJING", "string"),
                        "sort": _p("排序", "string",
                                   enum=["population", "arable", "radicals",
                                         "infrastructure_free", "id"]),
                        "offset": _p("偏移", "integer"), "limit": _p("返回条数", "integer"),
                    }), t_query_states))
    r.register(Tool("state_detail",
                    "单州详情：建筑、耕地占用、队列、待执行动作、资源/特质。",
                    _schema({"state_id": _p("州 id", "integer")}, ["state_id"]),
                    t_state_detail))
    r.register(Tool("query_buildings",
                    "查询建筑实例（等级/就业/吞吐/利润/当前 PM），可按建筑/州过滤。",
                    _schema({
                        "building": _p("建筑键，如 building_tea_plantation", "string"),
                        "state_id": _p("州 id", "integer"),
                        "sort": _p("排序", "string", enum=["levels", "profit", "staffing"]),
                        "limit": _p("返回条数", "integer"),
                    }), t_query_buildings))
    r.register(Tool("building_detail",
                    "单类建筑详情：知识库、实例、PM 组与当前/可用 PM。",
                    _schema({"building": _p("建筑键", "string")}, ["building"]),
                    t_building_detail))
    r.register(Tool("query_market",
                    "市场 54 商品价格/基础价/供需；最贵/最便宜，可指定商品。",
                    _schema({"top_n": _p("最贵/最便宜各取几条", "integer"),
                             "goods": _p("指定商品列表", "array",
                                         items={"type": "string"})}), t_query_market))
    r.register(Tool("query_budget",
                    "预算：收入/支出合计、结余、国库、信贷、税级、前几项收支。",
                    _schema({}), t_query_budget))
    r.register(Tool("query_politics",
                    "政治：法律、利益集团、运动、机构、法令、公司。",
                    _schema({}), t_query_politics))
    r.register(Tool("query_techs",
                    "科技：已研发 + 当前可研发的下一批（含前置）。",
                    _schema({"limit": _p("可研发科技条数", "integer"),
                             "all": _p("是否返回全部已研发", "boolean")}), t_query_techs))
    r.register(Tool("query_laws",
                    "法律：当前法律 + 同组可替换候选（已过滤科技/法律前置）。",
                    _schema({}), t_query_laws))
    r.register(Tool("calc_gdp",
                    "算 GDP；默认 market+staffing+producers。by=goods/buildings/states 给明细。",
                    _schema({
                        "price": _p("价格基准", "string", enum=["auto", "market", "base", "local"]),
                        "scale": _p("缩放", "string",
                                    enum=["staffing", "throughput", "levels", "staffing_only"]),
                        "buildings": _p("建筑范围", "string",
                                        enum=["producers", "all"]),
                        "by": _p("明细维度", "string",
                                 enum=["none", "goods", "buildings", "states"]),
                    }), t_calc_gdp))
    r.register(Tool("pm_report",
                    "某建筑的 PM 组：当前/可用/条件未知/不可用，含原因与非GDP效果。",
                    _schema({"building": _p("建筑键", "string"),
                             "status": _p("只看某状态", "string",
                                          enum=["all", "active", "available",
                                                "conditional", "locked"])},
                            ["building"]), t_pm_report))
    r.register(Tool("pm_upgrades",
                    "按全国年 GDP 增量排序的换 PM 候选；可指定建筑/州。",
                    _schema({
                        "building": _p("建筑键；空=自动取前 5 类", "string"),
                        "state_id": _p("只算某州", "integer"),
                        "top": _p("返回条数", "integer"),
                        "include_conditional": _p("是否包含条件未知", "boolean"),
                    }), t_pm_upgrades))
    r.register(Tool("build_candidates",
                    "可建候选：按州×建筑过滤科技/资源/上限后，按估算 GDP 排序。",
                    _schema({"state_id": _p("限定州", "integer"),
                             "building": _p("限定建筑", "string"),
                             "top": _p("返回条数", "integer")}), t_build_candidates))
    r.register(Tool("estimate_build",
                    "估算某州建 N 级的年 GDP 增量与就业（默认 PM/满员/吞吐1.0）。",
                    _schema({"state_id": _p("州 id", "integer"),
                             "building": _p("建筑键", "string"),
                             "levels": _p("等级", "integer")},
                            ["state_id", "building"]), t_estimate_build))
    r.register(Tool("strategy_get", "读取当前策略与历史数量。",
                    _schema({}), t_strategy_get))
    r.register(Tool("strategy_propose",
                    "AI 主动切换策略（目标权重/约束/优先级）。审查层硬护栏不会关闭。",
                    _schema({
                        "strategy": {"type": "object",
                                     "description": "完整策略对象：name/rationale/objectives/constraints/notes",
                                     "additionalProperties": True},
                        "rationale": _p("为什么现在切策略", "string"),
                    }, ["strategy"]), t_strategy_propose))
    r.register(Tool("propose_build",
                    "提议建造；立即经过审查层，返回 approved/reasons。",
                    _schema({
                        "state_id": _p("州 id", "integer"),
                        "building": _p("建筑键", "string"),
                        "levels": _p("等级 1..N", "integer"),
                        "rationale": _p("决策理由", "string"),
                    }, ["state_id", "building", "levels"]), t_propose_build))
    r.register(Tool("propose_pm",
                    "提议换生产方式；立即经过审查层（科技/法律/前置/条件未知/同组冲突）。",
                    _schema({
                        "state_id": _p("州 id", "integer"),
                        "building": _p("建筑键", "string"),
                        "pm": _p("PM 键", "string"),
                        "rationale": _p("决策理由", "string"),
                    }, ["state_id", "building", "pm"]), t_propose_pm))
    r.register(Tool("pending_actions", "查看本周期已通过审查、待执行的动作。",
                    _schema({}), t_pending))
    r.register(Tool("cancel_action", "取消一条待执行动作。",
                    _schema({"command_id": _p("指令 id", "string")},
                            ["command_id"]), t_cancel))
    r.register(Tool("execute_pending",
                    "手动执行待执行动作（自动模式下由循环在回合末执行，通常不用调）。",
                    _schema({}), t_execute_pending))
    r.register(Tool("queue_status",
                    "执行端状态：世界/玩家/capabilities + 建造队列。",
                    _schema({}), t_queue))
    r.register(Tool("execution_feedback", "上一回合的执行/被拒结果。",
                    _schema({}), t_feedback))
    r.register(Tool("history_recent", "最近几轮决策摘要。",
                    _schema({"limit": _p("轮数", "integer")}), t_history))
    r.register(Tool("finish_cycle",
                    "结束本回合并写总结；调用后不要再提交动作。",
                    _schema({"summary": _p("本轮决策摘要", "string"),
                             "rationale": _p("关键判断", "string")}), t_finish))
    if core_only:
        r.keep(CORE_TOOLS)
    return r
