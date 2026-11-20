"""IDA 脚本：定位 GDP 的计算/读取代码。

思路（比按位移常量猜可靠）：
  1. 枚举所有含 "gdp" 的字符串（名字表、趋势字段、数据函数名都算）
  2. 用 IDA 的 xref 数据库找引用它们的代码（IDA 解析过的引用，
     比我之前按字节扫 RIP 相对 lea 更全）
  3. 反编译这些函数，把伪代码写到文件，人工判读哪些是 GDP 计算
  4. 额外：找出「同时算多个商品」的函数——GDP 必然遍历商品/建筑

在 IDA 里打开 victoria3.exe，File -> Script file... 选本文件。
输出：tmp\\ida_gdp\\*.c  与 tmp\\ida_gdp_result.txt
"""
import os
import re

import ida_funcs
import idautils
import idc

try:
    import ida_hexrays
    HAVE_HX = True
except Exception:
    HAVE_HX = False

OUTDIR = r"D:\Game\ai\Victoria3-RE\data\ida_gdp"
PAT = re.compile(r"gdp", re.I)
MAX_FUNCS = 40

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
    log("HexRays=%s" % HAVE_HX)

    # 1. 含 gdp 的字符串
    strs = []
    for s in idautils.Strings():
        try:
            t = str(s)
        except Exception:
            continue
        if t and PAT.search(t):
            strs.append((s.ea, t))
    log("\n[1] 含 gdp 的字符串 %d 个" % len(strs))
    for ea, t in strs[:60]:
        log("   0x%X  %s" % (ea, t))

    # 2. 找引用它们的函数
    funcs = {}
    for ea, t in strs:
        for xr in idautils.XrefsTo(ea, 0):
            f = ida_funcs.get_func(xr.frm)
            if not f:
                continue
            funcs.setdefault(f.start_ea, set()).add(t)
    log("\n[2] 引用这些字符串的函数 %d 个" % len(funcs))

    # 3. 反编译并输出
    log("\n[3] 反编译（按引用的字符串数排序）")
    ranked = sorted(funcs.items(), key=lambda kv: -len(kv[1]))
    for fea, names in ranked[:MAX_FUNCS]:
        code = decomp(fea)
        if not code:
            log("   sub_%X 反编译失败" % fea)
            continue
        path = os.path.join(OUTDIR, "sub_%X.c" % fea)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("// 引用字符串: %s\n\n" % ", ".join(sorted(names)))
            fh.write(code)
        log("   sub_%X  %-40s 引用 %d 个串  -> %s"
            % (fea, idc.get_func_name(fea)[:40], len(names), os.path.basename(path)))
        for ln in code.splitlines()[:6]:
            log("        %s" % ln.strip()[:110])

    with open(r"D:\Game\ai\Victoria3-RE\data\ida_gdp_result.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    log("\n汇总写入 tmp\\ida_gdp_result.txt，伪代码在 tmp\\ida_gdp\\")


main()
