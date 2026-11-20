# -*- coding: utf-8 -*-
"""触发信号：什么时候值得叫醒 LLM。

设计目标是**让多数轮次不需要 LLM**：代码每周扫一遍，一切正常就返回空列表；
只有越界的事才会变成 ``Signal``，再由上层决定是升给 LLM 还是直接按规则办。

信号分三类：

* **市场**——价格触顶/触底/短缺/过剩（含与上一份快照相比的突变）；
* **财政**——周结余为负、国库接近信贷线；
* **结构**——激进派比例、基建饱和、建造力为零。

``severity``：1=值得记一笔，2=该处理，3=会崩，必须处理。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .market import MarketView, PriceModel
from .model import Snapshot

SEV_LABEL = {1: "提示", 2: "警告", 3: "危急"}


@dataclass
class Signal:
    kind: str
    severity: int = 1
    title: str = ""
    message: str = ""
    evidence: dict = field(default_factory=dict)
    suggested: str = ""

    @property
    def label(self) -> str:
        return SEV_LABEL.get(self.severity, str(self.severity))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "severity": self.severity,
                "label": self.label, "title": self.title,
                "message": self.message, "evidence": self.evidence,
                "suggested": self.suggested}


def _good_signals(view: MarketView) -> list[Signal]:
    out: list[Signal] = []
    for p in view.pos.values():
        if p.base <= 0:
            continue
        if p.at_ceiling:
            out.append(Signal(
                "price_ceiling", 3, "%s 价格触顶" % p.good,
                "%s 已是基础价的 %.2f×，市场在配给；继续扩产会被原料卡住。"
                % (p.good, p.ratio),
                {"good": p.good, "ratio": round(p.ratio, 3),
                 "gap": round(p.gap, 1), "chain": p.chain},
                "补上游（%s 链）或进口；别再把下游产能往上堆" % p.chain))
        elif p.at_floor:
            out.append(Signal(
                "price_floor", 2, "%s 价格触底" % p.good,
                "%s 已跌到基础价的 %.2f×，再扩产只会产出不值钱的货。"
                % (p.good, p.ratio),
                {"good": p.good, "ratio": round(p.ratio, 3), "chain": p.chain},
                "停止扩产，或转做它的下游消费品"))
        elif p.shortage:
            sev = 3 if (p.ratio >= 1.5 and p.gap_value >= 20000) else 2
            out.append(Signal(
                "shortage", sev, "%s 短缺" % p.good,
                "%s 价格 %.2f× 基础价，缺口 %.0f/周（约 %.0f/周）。"
                % (p.good, p.ratio, p.gap, p.gap_value),
                {"good": p.good, "ratio": round(p.ratio, 3),
                 "gap": round(p.gap, 1), "chain": p.chain},
                "优先补 %s 的产能或用替代品" % p.chain))
        elif p.glut:
            out.append(Signal(
                "glut", 1, "%s 过剩" % p.good,
                "%s 价格 %.2f× 基础价，供给多出 %.0f/周。"
                % (p.good, p.ratio, -p.gap),
                {"good": p.good, "ratio": round(p.ratio, 3), "chain": p.chain},
                "可考虑用它的下游建筑消化，或出口"))
    return out


def _budget_signals(snap: Snapshot) -> list[Signal]:
    c = snap.country
    if c is None:
        return []
    b = c.budget
    out: list[Signal] = []
    balance = b.balance
    treasury = float(c.treasury or 0.0)
    credit = float(c.credit_limit or 0.0)
    if balance is not None and balance < 0:
        sev = 2
        headroom = max(0.0, credit - float(c.loan_principal or 0.0))
        if treasury < 0 or (headroom and headroom < abs(balance) * 26):
            sev = 3
        out.append(Signal(
            "deficit", sev, "周结余为负",
            "每周 %.0f，国库 %.0f，剩余信贷 %.0f。" % (balance, treasury, headroom),
            {"balance": round(balance, 1), "treasury": round(treasury, 1),
             "credit_headroom": round(headroom, 1)},
            "先别加建造；检查军费/补贴/行政，或提高税级"))
    if credit and treasury < credit * 0.1 and treasury >= 0:
        headroom = max(0.0, credit - float(c.loan_principal or 0.0))
        if headroom < credit * 0.25:
            out.append(Signal(
                "treasury_low", 2, "国库偏低",
                "国库 %.0f，剩余信贷 %.0f，缓冲不够。"
                % (treasury, headroom),
                {"treasury": round(treasury, 1), "credit_limit": round(credit, 1),
                 "credit_headroom": round(headroom, 1)},
                "留出缓冲，避免利息滚雪球"))
    return out


def _structure_signals(snap: Snapshot) -> list[Signal]:
    out: list[Signal] = []
    pop = sum(s.population or 0 for s in snap.states)
    rad = sum(s.radicals or 0 for s in snap.states)
    if pop and rad / pop >= 0.15:
        out.append(Signal(
            "radicals", 2, "激进派比例偏高",
            "全国激进派 %.1f%%（%s / %s）。" % (100.0 * rad / pop, f"{rad:,}", f"{pop:,}"),
            {"radicals": rad, "population": pop,
             "share": round(rad / pop, 3)},
            "提生活水平/降税/少用剥削性 PM；激进派会拉低吞吐"))
    hot = []
    for s in snap.states:
        if s.infrastructure and s.infrastructure_used and \
                s.infrastructure_used / s.infrastructure >= 0.9:
            hot.append(s.id)
    if hot:
        out.append(Signal(
            "infra", 1, "基建饱和州 %d 个" % len(hot),
            "这些州再建厂会吃惩罚：%s" % ", ".join(str(x) for x in hot[:8]),
            {"states": hot[:20]},
            "先修铁路/港口提基建，或把新厂放到别的州"))
    return out


def _change_signals(model: PriceModel, prev, **kw) -> list[Signal]:
    if prev is None:
        return []
    from .market import PriceModel as _PM
    try:
        old = prev if isinstance(prev, _PM) else _PM.from_snapshot(model.gd, prev)
    except Exception:                                  # noqa: BLE001
        return []
    out: list[Signal] = []
    for g in model.base:
        a, b = old.multiplier(g), model.multiplier(g)
        if a <= 0 or b <= 0:
            continue
        if abs(b - a) >= 0.15:
            sev = 2 if abs(b - a) >= 0.3 else 1
            out.append(Signal(
                "price_shift", sev, "%s 价格突变" % g,
                "%s 从 %.2f× 变为 %.2f× 基础价。" % (g, a, b),
                {"good": g, "from": round(a, 3), "to": round(b, 3)},
                "确认是供需变化还是贸易/战争导致的"))
    return out


def scan(gd, snap: Snapshot, model: PriceModel | None = None,
         view: MarketView | None = None, prev=None, top: int = 12,
         capacity: dict | None = None) -> list[Signal]:
    """扫一遍，按严重度返回信号（正常时为空列表）。"""
    model = model or PriceModel.from_snapshot(gd, snap)
    view = view or MarketView(gd, model)
    out: list[Signal] = []
    if model.has_orders:
        out.extend(_good_signals(view))
    else:
        # 存档快照只有世界市场趋势价，没有买卖单：逐商品的短缺/过剩判断不可靠，
        # 只报一条"数据不足"，别用一堆假信号淹掉真信号。
        out.append(Signal(
            "no_orders", 1, "存档模式：没有买卖单",
            "这份快照没有 market.supply/demand，价格反馈与逐商品缺口都算不了；"
            "市况判断请用 live 快照（python v3.py read --json snap.json）。",
            {"source": snap.game.source}, "切 live 快照再做市场决策"))
    out.extend(_budget_signals(snap))
    out.extend(_structure_signals(snap))
    out.extend(_change_signals(model, prev))
    if capacity is not None and not capacity.get("capacity_per_week"):
        out.append(Signal("no_capacity", 3, "建造力为 0",
                          "算不出建造力，检查建造部门与生产方式。",
                          capacity, "先建/修建造部门"))
    out.sort(key=lambda s: (-s.severity, -abs(float(
        s.evidence.get("gap_value") or s.evidence.get("gap") or 0))))
    return out[:top]


def should_escalate(signals: list[Signal]) -> bool:
    """是否值得把控制权交给 LLM（而不是让规则自己处理）。"""
    return any(s.severity >= 3 for s in signals) or \
        sum(1 for s in signals if s.severity >= 2) >= 2
