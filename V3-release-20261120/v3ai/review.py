# -*- coding: utf-8 -*-
"""审查层：在决策层和执行端之间逐条判断"这条指令现在能不能执行"。

它只做确定性检查，不调用 LLM：
  * 结构：州/建筑/PM/等级是否合法；
  * 科技/法律/地理/资源/唯一性/上限/队列；
  * PM 的 locked / conditional / already / 同组冲突；
  * 策略与配置的配额、国库、赤字、禁用 PM 模式；
  * 通过 → 放行；不通过 → 带原因返回给 LLM 重新决策。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .util import now_iso, safe_int


def _new_id(prefix: str) -> str:
    return "%s_%s_%s" % (prefix, time.strftime("%H%M%S"), uuid.uuid4().hex[:6])


@dataclass
class Command:
    kind: str                                  # build | pm
    state_id: int | None = None
    building: str = ""
    levels: int = 1
    pm: str | None = None
    source: str = "llm"
    rationale: str = ""
    id: str = field(default_factory=lambda: _new_id("cmd"))
    created_at: str = field(default_factory=now_iso)
    status: str = "proposed"                   # proposed | approved | rejected | executed | failed
    review: dict = field(default_factory=dict)
    execution: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "state_id": self.state_id,
            "building": self.building, "levels": self.levels, "pm": self.pm,
            "source": self.source, "rationale": self.rationale,
            "created_at": self.created_at, "status": self.status,
            "review": self.review, "execution": self.execution,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Command":
        c = cls(
            kind=str(d.get("kind") or ""),
            state_id=safe_int(d.get("state_id"), None),
            building=str(d.get("building") or ""),
            levels=int(safe_int(d.get("levels"), 1) or 1),
            pm=(str(d["pm"]) if d.get("pm") else None),
            source=str(d.get("source") or "llm"),
            rationale=str(d.get("rationale") or ""),
            id=str(d.get("id") or _new_id("cmd")),
        )
        c.status = str(d.get("status") or "proposed")
        c.review = dict(d.get("review") or {})
        c.execution = dict(d.get("execution") or {})
        return c

    def label(self) -> str:
        if self.kind == "build":
            return "建造 %s @州%s +%d级" % (self.building, self.state_id, self.levels)
        return "换PM %s @州%s %s" % (self.building, self.state_id, self.pm)


@dataclass
class ReviewResult:
    approved: bool
    code: str
    message: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved, "code": self.code, "message": self.message,
            "reasons": self.reasons, "warnings": self.warnings,
            "details": self.details,
        }


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return []


def _cap(a, b) -> int:
    """0 = 不限制；两个都非 0 时取更严格的那个。"""
    a, b = int(a), int(b)
    if a <= 0 and b <= 0:
        return 0
    if a <= 0:
        return b
    if b <= 0:
        return a
    return min(a, b)


def effective_limits(config, strategy) -> dict:
    """配置是硬顶，策略只能更保守，不能更宽松。

    配置里 0 表示不限制；此时忽略策略的同项上限（玩家在设置里明确放开）。
    """
    cfg_agent = config.agent if config else {}
    cfg_review = config.review if config else {}
    c = {}
    if strategy is not None:
        c.update(strategy.constraints or {})
    cfg_actions = int(cfg_agent.get("max_actions_per_cycle", 8) or 0)
    cfg_levels = int(cfg_agent.get("max_levels_per_action", 5) or 0)
    if cfg_actions <= 0:
        max_actions = 0
    else:
        max_actions = _cap(c.get("max_actions_per_cycle", cfg_actions), cfg_actions)
    if cfg_levels <= 0:
        max_levels = 0
    else:
        max_levels = _cap(c.get("max_levels_per_action", cfg_levels), cfg_levels)
    c["max_actions_per_cycle"] = max_actions
    c["max_levels_per_action"] = max_levels
    if max_actions <= 0:
        c["max_build_actions_per_cycle"] = 0
        c["max_pm_actions_per_cycle"] = 0
    else:
        c["max_build_actions_per_cycle"] = _cap(
            c.get("max_build_actions_per_cycle", max_actions), max_actions)
        c["max_pm_actions_per_cycle"] = _cap(
            c.get("max_pm_actions_per_cycle", max_actions), max_actions)
    c["allow_conditional_pm"] = bool(c.get("allow_conditional_pm",
                                           cfg_review.get("allow_conditional_pm", False)))
    c["min_treasury"] = float(c.get("min_treasury", cfg_review.get("min_treasury", 0.0)) or 0.0)
    c["max_weekly_deficit"] = c.get("max_weekly_deficit", None)
    c["block_pm_patterns"] = (_as_list(c.get("block_pm_patterns")) +
                              _as_list(cfg_review.get("block_pm_patterns")))
    c["warn_pm_patterns"] = (_as_list(c.get("warn_pm_patterns")) +
                             _as_list(cfg_review.get("warn_pm_patterns")))
    c["forbidden_buildings"] = _as_list(c.get("forbidden_buildings"))
    c["priority_buildings"] = _as_list(c.get("priority_buildings"))
    c["max_pending"] = int(cfg_review.get("max_pending", 16) or 16)
    c["development"] = dict((strategy.development if strategy is not None else {}) or {})
    return c


class Reviewer:
    def __init__(self, config, strategy):
        self.config = config
        self.strategy = strategy
        self.limits = effective_limits(config, strategy)

    def _budget_guard(self, cmd: Command, ctx) -> tuple[list[str], list[str]]:
        reasons: list[str] = []
        warnings: list[str] = []
        country = ctx.snap.country
        if country is None:
            return reasons, warnings
        treasury = float(country.treasury or 0.0)
        min_t = float(self.limits.get("min_treasury") or 0.0)
        if cmd.kind == "build":
            cost = ctx.cost_of(cmd.building, cmd.levels)
            if treasury < min_t:
                reasons.append("国库 %.0f 低于策略下限 %.0f" % (treasury, min_t))
            elif treasury - cost < min_t:
                warnings.append("国库 %.0f 付完估算建造成本 %.0f 后低于下限 %.0f（建造是分期付款）"
                                % (treasury, cost, min_t))
            max_def = self.limits.get("max_weekly_deficit")
            balance = float((country.budget.balance if country.budget else 0.0) or 0.0)
            if max_def is not None and balance < -abs(float(max_def)):
                reasons.append("周结余 %.0f 超过允许赤字 %.0f" % (balance, abs(float(max_def))))
        return reasons, warnings

    def review(self, cmd: Command, ctx) -> ReviewResult:
        limits = self.limits
        reasons: list[str] = []
        warnings: list[str] = []
        details: dict = {"limits": {k: limits.get(k) for k in (
            "max_actions_per_cycle", "max_levels_per_action",
            "max_build_actions_per_cycle", "max_pm_actions_per_cycle",
            "allow_conditional_pm", "min_treasury")}}

        # -- 结构 -----------------------------------------------------
        if cmd.kind not in ("build", "pm"):
            return ReviewResult(False, "schema", "未知指令类型", ["kind 必须是 build 或 pm"], [], details)
        if cmd.state_id is None:
            return ReviewResult(False, "schema", "缺少 state_id", ["state_id 必填"], [], details)
        if not cmd.building.startswith("building_"):
            return ReviewResult(False, "schema", "建筑键不合法",
                                ["building 必须以 building_ 开头"], [], details)
        if cmd.kind == "build":
            max_levels = int(limits["max_levels_per_action"])
            if max_levels > 0 and not (1 <= int(cmd.levels) <= max_levels):
                return ReviewResult(
                    False, "schema", "等级超限",
                    ["levels 必须在 1..%d" % max_levels], [], details)
            if int(cmd.levels) < 1:
                return ReviewResult(False, "schema", "等级不合法",
                                    ["levels 必须 >= 1"], [], details)
            dev = limits.get("development") or {}
            if str(dev.get("mode") or "") == "custom":
                allowed = {int(x) for x in (dev.get("priority_states") or [])}
                if allowed and int(cmd.state_id) not in allowed:
                    return ReviewResult(
                        False, "strategy", "州不在自定义建设范围",
                        ["策略限定省份：%s" % ", ".join(str(x) for x in sorted(allowed))],
                        [], details)
        else:
            # 游戏里并非所有 PM 都以 pm_ 开头（例如 worker_exploitation_*），
            # 所以先查游戏数据，再退回前缀规则（数据不可用时）。
            pm_name = str(cmd.pm or "")
            known = getattr(getattr(ctx, "gd", None), "pms", {}) or {}
            if not pm_name or (pm_name not in known
                               and not pm_name.startswith("pm_")):
                return ReviewResult(False, "schema", "PM 键不合法",
                                    ["PM 必须存在于游戏数据（或以 pm_ 开头）"],
                                    [], details)

        # -- 本周期配额 -----------------------------------------------
        approved = [c for c in ctx.pending if getattr(c, "status", "") in ("proposed", "approved")]
        n_all = len(approved)
        n_build = sum(1 for c in approved if getattr(c, "kind", "") == "build")
        n_pm = sum(1 for c in approved if getattr(c, "kind", "") == "pm")
        if n_all >= int(limits["max_actions_per_cycle"]) > 0:
            return ReviewResult(False, "quota", "本周期动作已满",
                                ["最多 %d 个动作/周期" % int(limits["max_actions_per_cycle"])], [], details)
        if cmd.kind == "build" and int(limits["max_build_actions_per_cycle"]) > 0 \
                and n_build >= int(limits["max_build_actions_per_cycle"]):
            return ReviewResult(False, "quota", "本周期建造动作已满",
                                ["最多 %d 个建造/周期" % int(limits["max_build_actions_per_cycle"])], [], details)
        if cmd.kind == "pm" and int(limits["max_pm_actions_per_cycle"]) > 0 \
                and n_pm >= int(limits["max_pm_actions_per_cycle"]):
            return ReviewResult(False, "quota", "本周期 PM 动作已满",
                                ["最多 %d 个 PM/周期" % int(limits["max_pm_actions_per_cycle"])], [], details)
        if n_all >= int(limits.get("max_pending", 16)) > 0:
            return ReviewResult(False, "quota", "待执行队列已满",
                                ["最多 %d 条待执行" % int(limits.get("max_pending", 16))], [], details)
        # -- 预算 -----------------------------------------------------
        b_reasons, b_warnings = self._budget_guard(cmd, ctx)
        reasons.extend(b_reasons)
        warnings.extend(b_warnings)

        if cmd.kind == "build":
            chk = ctx.check_build(int(cmd.state_id), cmd.building, int(cmd.levels))
            details["build_check"] = chk
            remaining = chk.get("remaining")
            if remaining is not None and int(remaining) < int(cmd.levels):
                cap = chk.get("cap")
                return ReviewResult(
                    False, "resource_cap", "超过该州资源/建筑上限",
                    ["州 %s 的 %s 最多还能建 %s 级（资源/建筑上限 %s，已/待建 %s，本次 %s 级）"
                     % (cmd.state_id, cmd.building, remaining, cap,
                        chk.get("projected_levels"), int(cmd.levels))],
                    warnings, details)
            reasons.extend(chk.get("reasons") or [])
            warnings.extend(chk.get("warnings") or [])
            if cmd.building in limits.get("forbidden_buildings", []):
                reasons.append("策略禁建：%s" % cmd.building)
            if cmd.building in limits.get("priority_buildings", []):
                warnings.append("策略优先建筑：%s" % cmd.building)
            for other in ctx.pending:
                if getattr(other, "kind", "") == "build" \
                        and getattr(other, "state_id", None) == cmd.state_id \
                        and getattr(other, "building", "") == cmd.building:
                    reasons.append("本周期已有同州同建筑指令：%s" % getattr(other, "id", ""))
                    break
            if chk.get("ok") and not reasons:
                msg = "通过：%s（剩余空间 %s，估算成本 %.0f 建造点）" % (
                    cmd.label(), chk.get("remaining"), chk.get("cost_points") or 0)
                return self._result(True, "ok", msg, reasons, warnings, details)
            code = "budget" if b_reasons else (chk.get("code") or "rejected")
            return self._result(False, code,
                                "拒绝：%s" % "；".join(reasons or ["未知原因"]),
                                reasons, warnings, details)

        # -- PM -------------------------------------------------------
        chk = ctx.check_pm(int(cmd.state_id), cmd.building, str(cmd.pm))
        details["pm_check"] = chk
        reasons.extend(chk.get("reasons") or [])
        warnings.extend(chk.get("warnings") or [])
        status = chk.get("status")
        if chk.get("code") == "already":
            reasons.append("已经是该 PM，无需切换")
        elif status == "locked":
            reasons.append("PM 被科技/法律/前置条件锁住")
        elif status == "conditional" and not limits.get("allow_conditional_pm"):
            reasons.append("PM 条件未知（公司/原则/地理等），策略不允许自动执行")
        elif status == "conditional" and limits.get("allow_conditional_pm"):
            warnings.append("PM 条件未知，但策略允许执行；执行后需人工/快照复核")
        pm = str(cmd.pm)
        for pat in limits.get("block_pm_patterns", []):
            if pat and pat in pm:
                reasons.append("策略禁用 PM 模式：%s" % pat)
        for pat in limits.get("warn_pm_patterns", []):
            if pat and pat in pm:
                warnings.append("PM 命中风险模式：%s（注意死亡率/行政力/生活水平）" % pat)
        for other in ctx.pending:
            if getattr(other, "kind", "") != "pm":
                continue
            if getattr(other, "state_id", None) != cmd.state_id \
                    or getattr(other, "building", "") != cmd.building:
                continue
            if getattr(other, "pm", None) == cmd.pm:
                reasons.append("本周期已提议同一 PM：%s" % getattr(other, "id", ""))
            else:
                reasons.append("同州同建筑已有另一个 PM 动作（同组互斥）：%s"
                               % getattr(other, "id", ""))
            break
        if chk.get("ok") and not reasons:
            eff = chk.get("effects") or {}
            if eff:
                warnings.append("非GDP效果：%s" % ", ".join(sorted(eff))[:300])
            return self._result(True, "ok", "通过：%s" % cmd.label(),
                                reasons, warnings, details)
        return self._result(False, "pm_" + str(chk.get("code") or "rejected"),
                            "拒绝：%s" % "；".join(reasons or ["未知原因"]),
                            reasons, warnings, details)

    @staticmethod
    def _result(ok: bool, code: str, message: str, reasons: list[str],
                warnings: list[str], details: dict) -> ReviewResult:
        def uniq(xs: list[str]) -> list[str]:
            seen = set()
            out = []
            for x in xs:
                if x and x not in seen:
                    seen.add(x)
                    out.append(x)
            return out
        return ReviewResult(ok, code, message, uniq(reasons), uniq(warnings), details)
