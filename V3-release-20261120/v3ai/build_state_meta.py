# -*- coding: utf-8 -*-
"""重建州静态地理缓存（state_id -> region/resources/caps/coastal）。

数据来源（任选其一）：
  * 存档流水线产出的 ``v3_states.json``（推荐；含 region/resources/caps/incorporated）；
  * ``state_regions.json``（地图数据；补 coastal / arable_land / traits）。

用法：
  python -m v3ai.build_state_meta
  python -m v3ai.build_state_meta --from-v3-states "...\\v3_states.json" \
      --regions "...\\state_regions.json" --out v3ai/data/state_meta.json

注意：州归属（owner）**不写入**缓存，归属永远以实时快照为准。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATES = r"D:\Game\ai\Victoria3-RE\legacy\v3_states.json"
DEFAULT_REGIONS = r"D:\Game\ai\Victoria3-RE\legacy\state_regions.json"
DEFAULT_OUT = os.path.join(PKG_DIR, "data", "state_meta.json")


def build(states_path: str, regions_path: str, out_path: str) -> dict:
    with open(states_path, encoding="utf-8") as f:
        data = json.load(f)
    region_map = {}
    if regions_path and os.path.isfile(regions_path):
        with open(regions_path, encoding="utf-8") as f:
            region_map = json.load(f) or {}
    states: dict[str, dict] = {}
    for sid, s in (data.get("states") or {}).items():
        region = s.get("region")
        rr = region_map.get(region, {}) if region else {}
        states[str(sid)] = {
            "region": region,
            "coastal": bool(rr.get("coastal", False)),
            "arable_land": s.get("arable_land", rr.get("arable_land")),
            "resources": s.get("resources") or (
                list(rr.get("arable_resources") or [])
                + list((rr.get("capped_resources") or {}).keys())),
            "resource_caps": s.get("resource_caps") or rr.get("capped_resources") or {},
            "traits": s.get("traits") or rr.get("traits") or [],
            "incorporated": s.get("incorporated"),
        }
    meta = {
        "_meta": {
            "source": states_path,
            "region_source": regions_path,
            "save_date": (data.get("meta") or {}).get("date"),
            "save_version": (data.get("meta") or {}).get("save_version"),
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": "state_id -> static geography; ownership is NOT stored here.",
        },
        "states": states,
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
    return {"states": len(states), "out": out_path,
            "bytes": os.path.getsize(out_path),
            "save_date": meta["_meta"]["save_date"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="重建 v3ai 州静态地理缓存")
    ap.add_argument("--from-v3-states", default=DEFAULT_STATES)
    ap.add_argument("--regions", default=DEFAULT_REGIONS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    info = build(args.from_v3_states, args.regions, args.out)
    print("wrote %(out)s  states=%(states)d  bytes=%(bytes)d  save_date=%(save_date)s" % info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
