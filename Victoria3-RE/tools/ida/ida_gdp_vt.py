"""IDA 脚本：反编译 GDP 相关的 RTTI 类方法，找出 GDP 计算过程。

线索来自 RTTI：这三个类的虚表地址已知，虚表里就是它们的实现：
    CTickTask_UpdateCachedRankingsAndGlobalGDP  vtable RVA 0x46B3EF0
    CGDPLink                                    vtable RVA 0x48FFEC8
    CGlobalGDPLink                              vtable RVA 0x4901140

做法：读虚表（RVA + ImageBase），把每个函数指针都反编译出来，
      伪代码写到 tmp\\ida_gdp\\ 下，并打印开头几行便于挑选。

在 IDA 里打开 victoria3.exe，File -> Script file... 选本文件。
"""
import os

import ida_bytes
import ida_funcs
import idaapi
import idautils
import idc

try:
    import ida_hexrays
    HAVE_HX = True
except Exception:
    HAVE_HX = False

OUTDIR = r"D:\Game\ai\Victoria3-RE\data\ida_gdp"
TARGETS = [
    ("TickTask_UpdateCachedRankingsAndGlobalGDP", 0x46B3EF0),
    ("CGDPLink", 0x48FFEC8),
    ("CGlobalGDPLink", 0x4901140),
]
MAX_SLOTS = 40

lines = []


def log(s=""):
    print(s)
    lines.append(s)


def decomp(ea):
    if HAVE_HX:
        try:
            cf = ida_hexrays.decompile(ea)
            if cf:
                return str(cf)
        except Exception:
            pass
    f = ida_funcs.get_func(ea)
    if not f:
        return None
    out = []
    for it in idautils.Heads(f.start_ea, f.end_ea):
        try:
            out.append(idc.generate_disasm_line(it, 0) or "")
        except Exception:
            continue
    return "\n".join(out)


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    base = idaapi.get_imagebase()
    log("ImageBase=0x%X  HexRays=%s" % (base, HAVE_HX))

    for name, rva in TARGETS:
        vt = base + rva
        log("\n" + "=" * 68)
        log("%s  vtable=0x%X" % (name, vt))
        ptrs = []
        for i in range(MAX_SLOTS):
            ea = vt + 8 * i
            try:
                ptr = ida_bytes.get_qword(ea)
            except Exception:
                break
            if not ptr or not (base <= ptr < base + 0x6000000):
                break
            f = ida_funcs.get_func(ptr)
            if not f:
                break
            ptrs.append((i, ptr))
        log("  虚表里有 %d 个有效函数指针" % len(ptrs))

        for slot, ptr in ptrs:
            code = decomp(ptr)
            if not code:
                log("   [%2d] 0x%X 反编译失败" % (slot, ptr))
                continue
            path = os.path.join(OUTDIR, "%s_slot%d_%X.c" % (name[:28], slot, ptr))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(code)
            first = [l.strip() for l in code.splitlines() if l.strip()][:1]
            log("   [%2d] 0x%-10X %-42s -> %s"
                % (slot, ptr, (first[0] if first else "")[:42],
                   os.path.basename(path)))

    with open(r"D:\Game\ai\Victoria3-RE\data\ida_gdp_vt.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    log("\n汇总写入 tmp\\ida_gdp_vt.txt，伪代码在 tmp\\ida_gdp\\")


main()
