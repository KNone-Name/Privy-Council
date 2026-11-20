# -*- coding: utf-8 -*-
"""小工具：原子写、JSON、时间、金额格式、截断。"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import time
import tempfile
from typing import Any


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    try:
        v = s.replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(v)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d
    except Exception:
        return None


def age_seconds(s: str | None) -> float | None:
    d = parse_iso(s)
    if d is None:
        return None
    return max(0.0, (_dt.datetime.now().astimezone() - d).total_seconds())


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
    d = os.path.dirname(os.path.abspath(path)) or "."
    # 不用 tempfile.mkstemp：某些沙箱里它对 .tmp-* 的 open 会被反复拒绝，
    # 而 mkstemp 内部是 10000 次重试，表现为"卡住"。这里用确定性临时名。
    tmp = os.path.join(d, ".%s.tmp-%d-%d" % (
        os.path.basename(path), os.getpid(), time.time_ns()))
    try:
        with open(tmp, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def atomic_write_json(path: str, obj: Any, indent: int | None = 2) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent,
                                      separators=(",", ":") if indent is None else None))


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def append_jsonl(path: str, obj: Any) -> None:
    ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: str, tail: int | None = None) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    if tail is not None:
        return out[-tail:]
    return out


def tail_text(path: str, n: int = 40) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [x.rstrip("\n") for x in lines[-n:]]
    except OSError:
        return []


def stable_hash(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def truncate_text(s: str, max_chars: int) -> str:
    if max_chars <= 0 or len(s) <= max_chars:
        return s
    return s[: max_chars - 3] + "..."


def truncate_json(obj: Any, max_chars: int) -> Any:
    """把工具结果截断成 LLM 可消化的字符串（保持 JSON 可读性）。"""
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= max_chars:
        return obj
    return {"_truncated": True, "_chars": len(s), "preview": s[: max_chars - 80] + "..."}


def fmt_money(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v:,.0f}"


def fmt_pct(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "?"
    return f"{v:+.{digits}f}%"


def safe_float(v: Any, default: float | None = None) -> float | None:
    try:
        if v is None or isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def safe_int(v: Any, default: int | None = None) -> int | None:
    try:
        if v is None or isinstance(v, bool):
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
