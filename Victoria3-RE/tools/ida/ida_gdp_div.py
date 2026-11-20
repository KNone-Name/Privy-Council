"""IDA 脚本：用 2000000 这个常量作锚点，找读取 GDP 的函数。

思路
----
`country_gdp_construction` 的系数公式是
    mult = 0.2 + 0.8 * (GDP / 2000000)     上限 1.0
对应 defines：
    COUNTRY_GDP_CONSTRUCTION_MODIFIER_DIVISOR = 2000000
    COUNTRY_GDP_CONSTRUCTION_MODIFIER_BASE_MULTIPLIER = 0.2
    COUNTRY_GDP_CONSTRUCTION_MODIFIER_SCALED_MULTIPLIER = 0.8

所以代码里必然出现 2000000（整数立即数或双精度常量），
以及 0.2 / 0.8 两个双精度常量。找到那个函数，
它里面读 GDP 的那一行就暴露了 GDP 的存储位置。

在 IDA 里打开 victoria3.exe，File -> Script file... 选本文件。
输出 tmp\\ida_gdp_div\\*.c  与 tmp\\ida_gdp_div.txt
"""
import os
import struct

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

OUTDIR = r"D:\Game\ai\Victoria3-RE\data\ida_gdp_div"
PATTERNS = {
    "int_2000000": struct.pack("<I", 2000000),
    "dbl_2000000": struct.pack("<d", 2000000.0),
    "dbl_0.2": struct.pack("<d", 0.2),
    "dbl_0.8": struct.pack("<d", 0.8),
    "flt_2000000": struct.pack("<f", 2000000.0),
}
MAX_FUNCS = 30

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
    log("ImageBase=0x%X HexRays=%s" % (idaapi.get_imagebase(), HAVE_HX))

    seg = None
    for s in idautils.Segments():
        if idc.get_segm_name(s).startswith(".text"):
            seg = (s, idc.get_segm_end(s))
            break
    start, end = seg
    body = ida_bytes.get_bytes(start, end - start)
    log(".text 0x%X-0x%X %.1f MB" % (start, end, len(body) / 1048576.0))

    funcs = {}
    for name, pat in PATTERNS.items():
        pos = 0
        cnt = 0
        while True:
            j = body.find(pat, pos)
            if j < 0:
                break
            pos = j + 1
            cnt += 1
            ea = start + j
            f = ida_funcs.get_func(ea)
            if f:
                funcs.setdefault(f.start_ea, set()).add(name)
        log("  %-12s 出现 %d 次" % (name, cnt))

    log("\n命中函数 %d 个" % len(funcs))
    # 优先「同时出现 2000000 与 0.2/0.8」的函数
    def score(names):
        return (2 if "int_2000000" in names or "dbl_2000000" in names else 0) \
             + (1 if "dbl_0.2" in names else 0) + (1 if "dbl_0.8" in names else 0)
    ranked = sorted(funcs.items(), key=lambda kv: -score(kv[1]))
    for fea, names in ranked[:MAX_FUNCS]:
        code = decomp(fea)
        if not code:
            log("  sub_%X 反编译失败 (%s)" % (fea, ",".join(sorted(names))))
            continue
        path = os.path.join(OUTDIR, "sub_%X.c" % fea)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("// 命中常量: %s\n\n" % ", ".join(sorted(names)))
            fh.write(code)
        log("  sub_%X  常量 %-42s 命中 %d  -> %s"
            % (fea, ",".join(sorted(names))[:42], len(names), os.path.basename(path)))

    with open(r"D:\Game\ai\Victoria3-RE\data\ida_gdp_div.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    log("\n汇总写入 tmp\\ida_gdp_div.txt，伪代码在 tmp\\ida_gdp_div\\")


main()
