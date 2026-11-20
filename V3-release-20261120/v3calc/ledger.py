# -*- coding: utf-8 -*-
"""预测台账：把"当时为什么这么判断"和"后来到底怎样"对上账。

V3 的反馈延迟是 5~50 个游戏周，LLM 决策最容易死在这里：做完了不知道对不对，
下一轮又从头猜。台账做三件事：

1. **记账**：每次给出计划时，把预测（年增值、价格影响）和市场基线一起落盘；
2. **对账**：下一次拿到快照时，比较"实际 GDP 变化"与"当初预测的增值"；
3. **校准**：把偏差聚合成一个系数，反馈给候选打分（或至少让决策层知道
   "我们的估算系统性高估 40%"）。

文件是 JSONL（一行一条），可追加、可人读、坏行自动跳过。
默认落在 ``v3calc/state/ledger.jsonl``，可用 ``V3CALC_STATE`` 覆盖目录。
"""
from __future__ import annotations

import json
import os
import statistics
import time
import uuid
from dataclasses import dataclass

from .model import Snapshot

HERE = os.path.dirname(os.path.abspath(__file__))

OPEN = "open"
DONE = "done"


def _default_path() -> str:
    root = os.environ.get("V3CALC_STATE") or os.path.join(HERE, "state")
    return os.path.join(root, "ledger.jsonl")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _gdp_annual(snap: Snapshot, gd) -> float | None:
    try:
        from .gdp import GDPCalculator
        return GDPCalculator(gd, snap).compute().annual
    except Exception:                                  # noqa: BLE001
        return None


class Ledger:
    def __init__(self, path: str | None = None):
        self.path = path or _default_path()

    # -- 读写 ---------------------------------------------------------
    def records(self) -> list[dict]:
        if not os.path.isfile(self.path):
            return []
        out: list[dict] = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def append(self, record: dict) -> dict:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def rewrite(self, records: list[dict]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    # -- 记账 ---------------------------------------------------------
    def record_plan(self, plan, snap: Snapshot, gd,
                    note: str = "") -> dict:
        """把一次 :class:`~v3calc.candidates.PlanResult` 记成一条预测。"""
        from .market import PriceModel
        model = PriceModel.from_snapshot(gd, snap)
        goods: set[str] = set()
        for o in plan.items:
            goods |= set(o.price_changes)
        prices = {g: round(model.multiplier(g), 4) for g in model.base}
        rec = {
            "id": uuid.uuid4().hex[:12],
            "created_at": _now(),
            "game_date": snap.game.save_date or snap.game.captured_at,
            "kind": "build",
            "items": [{"state_id": o.state_id, "building": o.building,
                       "levels": o.levels, "points": round(o.points, 1),
                       "annual_value": round(o.annual_value, 1)}
                      for o in plan.items],
            "predicted_annual_value": round(plan.total_annual_value, 1),
            "predicted_points": round(plan.total_points, 1),
            "baseline": {
                "gdp_annual": _round(_gdp_annual(snap, gd)),
                "balance": _round(snap.country.budget.balance
                                  if snap.country and snap.country.budget else None),
                "levels": sum(b.levels for b in snap.buildings),
                "prices": prices,
                "touched_goods": sorted(goods),
            },
            "status": OPEN,
            "note": note,
        }
        return self.append(rec)

    # -- 对账 ---------------------------------------------------------
    def reconcile(self, snap: Snapshot, gd, mark: bool = True) -> dict:
        """用当前快照校验所有 open 记录，返回偏差报告。"""
        recs = self.records()
        base_gdp = _gdp_annual(snap, gd)
        changed = False
        rows = []
        for r in recs:
            if r.get("status") != OPEN:
                continue
            b = r.get("baseline") or {}
            pred = float(r.get("predicted_annual_value") or 0.0)
            old = b.get("gdp_annual")
            actual = None
            bias = None
            if base_gdp is not None and old:
                actual = base_gdp - float(old)
                if pred:
                    bias = actual / pred
            row = {
                "id": r.get("id"), "game_date": r.get("game_date"),
                "predicted": pred, "actual_delta": _round(actual),
                "bias": _round(bias), "items": len(r.get("items") or []),
            }
            rows.append(row)
            if mark and (actual is not None):
                r["status"] = DONE
                r["reconciled_at"] = _now()
                r["actual"] = {"gdp_delta_annual": _round(actual),
                               "balance": _round(snap.country.budget.balance
                                                 if snap.country and snap.country.budget
                                                 else None)}
                r["bias"] = _round(bias)
                changed = True
        if changed:
            self.rewrite(recs)
        biases = [x["bias"] for x in rows if x["bias"] is not None]
        return {
            "n_open": len(rows),
            "rows": rows[:20],
            "median_bias": _round(statistics.median(biases)) if biases else None,
            "mean_bias": _round(sum(biases) / len(biases)) if biases else None,
            "suggested_scale": (_round(1.0 / statistics.median(biases))
                                if biases and statistics.median(biases) else None),
            "note": ("bias = 实际年 GDP 增量 / 预测年增值。>1 说明低估，<1 说明高估；"
                     "suggested_scale 可直接乘回候选的 annual_value 做校准。"),
        }

    # -- 概览 ---------------------------------------------------------
    def summary(self) -> dict:
        recs = self.records()
        open_ = [r for r in recs if r.get("status") == OPEN]
        done = [r for r in recs if r.get("status") == DONE]
        biases = [r["bias"] for r in done if r.get("bias") is not None]
        return {
            "path": self.path,
            "n": len(recs), "n_open": len(open_), "n_done": len(done),
            "last": recs[-1] if recs else None,
            "median_bias": _round(statistics.median(biases)) if biases else None,
        }


def _round(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) else None
