# -*- coding: utf-8 -*-
"""主菜单 / 存档判定：直接读游戏的州注册表（g_regState）。

来自 v3bridge 的已验证布局（D:\\Game\\ai\\Victoria3-RE\\legacy\\v3bridge.cpp）：

    g_regState（全局，保存 registry 指针）
    registry +0x20  entries 数组指针
    registry +0x2C  容量（1024 → 2048 会翻倍；不是州数量）
    每个条目 16 字节，对象指针在 +8

判定“在世界里”必须找到一个首 qword 是 CState vtable 的真实对象；
只看容量/count 会在读档中途误判（数组已分配、条目还全空）。

resolver 的 prologue：

    48 83 EC 58        sub rsp, 58h
    48 8B 05 disp32    mov rax, [rip+disp32]   <- 全局
    8B 11              mov edx, [rcx]
    48 85 C0           test rax, rax
    75 67              jnz ...

所以从 exe 的 .text 里搜这个模式，取 disp32 得到全局的 RVA；运行期
global = module_base + rva。CState 的 vtable 由 RTTI 解析，不硬编码。
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from v3live.pe import PE
from v3live.rtti import RTTI

PROLOGUE = b"\x48\x83\xEC\x58\x48\x8B\x05"
AFTER = b"\x8B\x11\x48\x85\xC0"
REG_DATA = 0x20
REG_COUNT = 0x2C
MAX_NULLS = 256

_CACHE: dict[tuple, tuple[list[int], list[int]]] = {}


def find_global_rvas(exe_path: str) -> list[int]:
    """从 exe 里找所有 resolver 全局的 RVA（不区分 State/Country）。"""
    pe = PE(exe_path)
    text = next((s for s in pe.sections if s.name == ".text"), None)
    if text is None or not text.rawsize:
        return []
    blob = pe.data[text.off:text.off + text.rawsize]
    out: list[int] = []
    i = 0
    while True:
        i = blob.find(PROLOGUE, i)
        if i < 0:
            break
        if blob[i + 11:i + 16] == AFTER:
            disp = struct.unpack_from("<i", blob, i + 7)[0]
            rva = text.rva + i + 11 + disp
            if 0 < rva < 0x8000000 and rva not in out:
                out.append(rva)
        i += 1
    return out


def state_vt_rvas(exe_path: str) -> list[int]:
    r = RTTI(exe_path)
    cls = r.classes.get("CState")
    return list(cls.vtables) if cls else []


def resolve_static(exe_path: str) -> tuple[list[int], list[int]]:
    """返回 (resolver 全局 RVA 列表, CState vtable RVA 列表)，带缓存。"""
    try:
        st = os.stat(exe_path)
        key = (os.path.abspath(exe_path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (os.path.abspath(exe_path), 0, 0)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    globals_rva = find_global_rvas(exe_path)
    vt_rva = state_vt_rvas(exe_path)
    _CACHE[key] = (globals_rva, vt_rva)
    return globals_rva, vt_rva


@dataclass
class WorldState:
    in_save: bool = False
    capacity: int = 0
    states: int = 0
    loading: bool = False
    first_state: int | None = None
    registry: int | None = None
    global_rva: int | None = None
    source: str = "registry"

    @property
    def label(self) -> str:
        if self.in_save:
            return "in save (states=%d, capacity=%d)" % (self.states, self.capacity)
        if self.loading:
            return "loading (states=0, capacity=%d)" % self.capacity
        return "main menu (states=0, capacity=0)"


class WorldProbe:
    def __init__(self, globals_rva: list[int], vt_rvas: list[int],
                 module_base: int, proc):
        self.base = module_base
        self.globals = [module_base + r for r in globals_rva]
        self.state_vts = {module_base + r for r in vt_rvas}
        self.p = proc

    @classmethod
    def from_process(cls, p) -> "WorldProbe | None":
        exe = None
        for m in p.modules():
            if m["name"].lower() == "victoria3.exe":
                exe = m.get("path")
                break
        if not exe or not os.path.isfile(exe):
            return None
        globals_rva, vt_rva = resolve_static(exe)
        if not globals_rva or not vt_rva:
            return None
        base = p.module_base("victoria3.exe")
        if not base:
            return None
        return cls(globals_rva, vt_rva, base, p)

    # ------------------------------------------------------------------
    def _read_registry(self, global_va: int):
        reg = self.p.read_u64(global_va)
        if not reg:
            return None
        cap = self.p.read_u32(reg + REG_COUNT)
        if cap is None or not (0 < cap <= 100000):
            return None
        entries = self.p.read_u64(reg + REG_DATA)
        if not entries:
            return None
        size = cap * 16
        buf = self.p.read(entries, size)
        if not buf or len(buf) < size:
            return None
        first = None
        first_vt = None
        states = 0
        nulls = 0
        loading = False
        for i in range(cap):
            obj = struct.unpack_from("<Q", buf, i * 16 + 8)[0]
            if not obj:
                nulls += 1
                if first is None and nulls > MAX_NULLS:
                    loading = True
                    break
                continue
            if first is None:
                first = obj
                first_vt = self.p.read_u64(obj)
            if first_vt in self.state_vts:
                states += 1
        return {
            "registry": reg, "capacity": cap, "states": states,
            "first": first, "first_vt": first_vt, "loading": loading,
        }

    def probe(self) -> WorldState:
        best = None
        for g in self.globals:
            r = self._read_registry(g)
            if r is None:
                continue
            if r["states"] > 0:
                return WorldState(
                    in_save=True, capacity=r["capacity"], states=r["states"],
                    first_state=r["first"], registry=r["registry"],
                    global_rva=g - self.base,
                )
            # 首对象不是 CState：这是 country 注册表，不是我们要找的那个
            if r["first"] is not None and r["first_vt"] not in self.state_vts:
                continue
            if best is None or r["capacity"] > best["capacity"]:
                best = r
        if best is not None:
            return WorldState(
                in_save=False, capacity=best["capacity"], states=0,
                loading=bool(best["capacity"]) or best["loading"],
                registry=best["registry"],
                global_rva=None)
        return WorldState(in_save=False, capacity=0, states=0, loading=False)
