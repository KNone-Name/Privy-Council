# -*- coding: utf-8 -*-
"""Paradox 脚本（Victoria 3 游戏数据文件）解析器。

与 ``v3live/gamedata.py`` 的最小解析器不同，这里保留：
  * 重复键（例如同一块里多个 ``if = { ... }``）——通过 ``Obj.getall()`` 取全部；
  * 裸值列表（``production_methods = { pm_a pm_b }``）与键值块的区别；
  * 引号字符串里的 ``#``、空格、转义；
  * ``?=`` / ``==`` / ``!=`` / ``<=`` / ``>=`` 等运算符（触发器里常见）。

值类型：
  * ``Obj``     —— 花括号块，是 dict 子类，另带 ``multi`` 保存重复键；
  * ``str``     —— 单个裸值/字符串；
  * ``list``    —— 裸值列表（如 PM 组的生产方式列表）。

所有数字保持字符串原样（日期 1836.1.1、前导零、``@var`` 都不会被破坏），
需要数值时用 ``as_float()`` / ``Obj.num()``。
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Iterator

__all__ = ["Obj", "parse_text", "parse_file", "parse_dir", "as_float", "as_bool"]

_ASSIGN = {"=", "?=", "==", "!=", "<", ">", "<=", ">="}
_SPECIAL = set("{}=")


class Obj(dict):
    """Paradox 块。dict 里保存每个键的最后一个值，``multi`` 保存全部值。"""

    __slots__ = ("multi", "values")

    def __init__(self) -> None:
        super().__init__()
        self.multi: dict[str, list] = {}
        self.values: list[str] = []          # 裸值列表

    # -- 写入 ---------------------------------------------------------
    def add(self, key: str, value) -> None:
        self.multi.setdefault(key, []).append(value)
        self[key] = value

    def add_value(self, value: str) -> None:
        self.values.append(value)

    # -- 读取 ---------------------------------------------------------
    def getall(self, key: str) -> list:
        return self.multi.get(key, [])

    def atoms(self, key: str) -> list[str]:
        """把 ``key = { a b c }`` 或 ``key = a`` 统一成字符串列表。"""
        out: list[str] = []
        for v in self.multi.get(key, []):
            if isinstance(v, Obj):
                out.extend(v.values)
            elif isinstance(v, list):
                out.extend(v)
            else:
                out.append(v)
        return out

    def block(self, key: str) -> "Obj | None":
        v = self.get(key)
        return v if isinstance(v, Obj) else None

    def blocks(self, key: str) -> list["Obj"]:
        return [v for v in self.multi.get(key, []) if isinstance(v, Obj)]

    def num(self, key: str, default: float = 0.0) -> float:
        v = self.get(key)
        if isinstance(v, Obj) or isinstance(v, list):
            return default
        f = as_float(v)
        return default if f is None else f

    def flag(self, key: str, default: bool = False) -> bool:
        v = self.get(key)
        if v is None:
            return default
        return as_bool(v)


def as_float(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def as_bool(v) -> bool:
    return str(v).strip().lower() in ("yes", "true", "1")


# --------------------------------------------------------------------------
# 词法
# --------------------------------------------------------------------------
def _tokenize(text: str) -> Iterator[tuple[str, str, int]]:
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n\ufeff":
            i += 1
            continue
        if c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n:
                ch = text[j]
                if ch == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if ch == '"':
                    j += 1
                    break
                buf.append(ch)
                j += 1
            yield ("str", "".join(buf), i)
            i = j
            continue
        if c in "{}":
            yield (c, c, i)
            i += 1
            continue
        if c == "=":
            yield ("=", "=", i)
            i += 1
            continue
        # 二/三字符运算符
        two = text[i:i + 2]
        if two in ("?=", "==", "!=", "<=", ">="):
            yield ("op", two, i)
            i += 2
            continue
        if c in "<>":
            yield ("op", c, i)
            i += 1
            continue
        # 裸词
        j = i
        while j < n and text[j] not in " \t\r\n\ufeff#{}=" and text[j] not in "<>" \
                and not (text[j] == "?" and j + 1 < n and text[j + 1] == "="):
            j += 1
        if j == i:                       # 防御：不认识的单字符
            j += 1
        yield ("atom", text[i:j], i)
        i = j


# --------------------------------------------------------------------------
# 语法
# --------------------------------------------------------------------------
class _Parser:
    def __init__(self, toks: list[tuple[str, str, int]]):
        self.t = toks
        self.i = 0

    def _peek(self, k: int = 0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else ("eof", "", -1)

    def parse_block(self) -> Obj:
        """当前 token 必须是 ``{``；返回块内容。"""
        assert self._peek()[0] == "{", self._peek()
        self.i += 1
        out = Obj()
        while True:
            kind, val, _pos = self._peek()
            if kind == "eof":
                return out
            if kind == "}":
                self.i += 1
                return out
            # key = value ?
            nk = self._peek(1)
            if kind in ("atom", "str") and nk[0] in ("=", "op"):
                key = val
                self.i += 2
                vk, vv, _ = self._peek()
                if vk == "{":
                    value = self.parse_block()
                elif vk in ("atom", "str"):
                    value = vv
                    self.i += 1
                elif vk in ("}", "eof"):
                    value = ""
                else:                     # 运算符作值（极少见）
                    value = vv
                    self.i += 1
                out.add(key, value)
                continue
            # 裸值（列表元素）
            if kind in ("atom", "str"):
                out.add_value(val)
                self.i += 1
                continue
            # 其它符号：跳过，保持健壮
            self.i += 1

    def parse_document(self) -> Obj:
        out = Obj()
        while self._peek()[0] != "eof":
            if self._peek()[0] != "{":
                self.i += 1
                continue
            for k, v in self.parse_block().items():
                out.add(k, v)
        return out


def parse_text(text: str) -> Obj:
    """解析整个文件。顶层通常是若干 ``name = { ... }``。"""
    toks = list(_tokenize(text))
    # 用一对虚拟花括号把顶层包成块，复用同一套语句解析
    toks = [("{", "{", 0)] + toks + [("}", "}", len(text))]
    return _Parser(toks).parse_block()


def parse_file(path: str) -> Obj:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return parse_text(f.read())


def parse_dir(path: str, pattern: str = "*.txt") -> Obj:
    out = Obj()
    if not os.path.isdir(path):
        return out
    for name in sorted(os.listdir(path)):
        if not name.endswith(".txt"):
            continue
        if not re.match(pattern.replace("*", ".*") + "$", name):
            continue
        try:
            part = parse_file(os.path.join(path, name))
        except Exception as exc:          # 单文件失败不影响整体
            print("  [pdx] 跳过 %s: %s" % (name, exc))
            continue
        for k, v in part.items():
            out.add(k, v)
        for v in part.values:
            out.add_value(v)
    return out
