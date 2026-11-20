"""IDA 脚本 v3：快速版。只反编译极少数候选函数。

v2 的致命问题：为筛选而反编译了全部几十万个函数，耗时不可接受。
v3 改成——先在 .text 的原始字节里按「位移常量」做归属统计，
筛出候选函数（几十个）后再反编译，几秒到几十秒即可完成。

三个锚点（都只在 CCountry 上成立）：
  gold  0x12F8  国库，已用 735 国存档精确对齐验证
  span  0x2638  CCountry 的结构大小
  预算区 0x1D50+8i / 0x1E00+8i

重点输出：**访问预算槽最多的函数** = 预算聚合器，
它逐个槽累加的写法直接暴露枚举顺序与语义。

在 IDA 里打开 victoria3.exe（数据库已存在，无需重新分析），
File -> Script file... 选本文件。输出 tmp\\ida_enum3_result.txt
"""
import re
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

INCOME_BASE, INCOME_N = 0x1E00, 16
EXPENSE_BASE, EXPENSE_N = 0x1D50, 22
GOLD_OFF = 0x12F8
CC_STRIDE = 0x2638
OUT = r"D:\Game\ai\Victoria3-RE\data\ida_enum3_result.txt"

lines = []


def log(s=""):
    print(s)
    lines.append(s)


def slot_of(off):
    if INCOME_BASE <= off <= INCOME_BASE + 8 * (INCOME_N - 1) and (off - INCOME_BASE) % 8 == 0:
        return ("income", (off - INCOME_BASE) // 8)
    if EXPENSE_BASE <= off <= EXPENSE_BASE + 8 * (EXPENSE_N - 1) and (off - EXPENSE_BASE) % 8 == 0:
        return ("expense", (off - EXPENSE_BASE) // 8)
    return None


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
    log("=" * 72)
    log("v3 快速版：CCountry 预算数组枚举反查")
    log("ImageBase = 0x%X  HexRays=%s" % (idaapi.get_imagebase(), HAVE_HX))
    log("=" * 72)

    seg = None
    for s in idautils.Segments():
        if idc.get_segm_name(s).startswith(".text"):
            seg = (s, idc.get_segm_end(s))
            break
    start, end = seg
    body = ida_bytes.get_bytes(start, end - start)
    log("\n.text 0x%X-0x%X  %.1f MB" % (start, end, len(body) / 1048576.0))

    # ---- 一次扫描，把每个位移常量归到函数 ----
    fmap = {}        # func_start -> set of (arr, slot)
    gold_funcs = set()
    span_funcs = set()

    pats = []
    for i in range(INCOME_N):
        pats.append((struct.pack("<I", INCOME_BASE + 8 * i), ("income", i)))
    for i in range(EXPENSE_N):
        pats.append((struct.pack("<I", EXPENSE_BASE + 8 * i), ("expense", i)))
    gold_pat = struct.pack("<I", GOLD_OFF)
    span_pats = [struct.pack("<I", CC_STRIDE), struct.pack("<I", CC_STRIDE + 8)]

    for pat, key in pats:
        pos = 0
        while True:
            j = body.find(pat, pos)
            if j < 0:
                break
            pos = j + 1
            f = ida_funcs.get_func(start + j)
            if f:
                fmap.setdefault(f.start_ea, set()).add(key)

    for pat in [gold_pat]:
        pos = 0
        while True:
            j = body.find(pat, pos)
            if j < 0:
                break
            pos = j + 1
            f = ida_funcs.get_func(start + j)
            if f:
                gold_funcs.add(f.start_ea)

    for pat in span_pats:
        pos = 0
        while True:
            j = body.find(pat, pos)
            if j < 0:
                break
            pos = j + 1
            f = ida_funcs.get_func(start + j)
            if f:
                span_funcs.add(f.start_ea)

    log("\n含预算位移的函数 %d 个；含 gold 的函数 %d 个；含 0x2638 的 %d 个"
        % (len(fmap), len(gold_funcs), len(span_funcs)))

    # ---- 候选：访问槽数最多的 ----
    ranked = sorted(fmap.items(), key=lambda kv: -len(kv[1]))
    log("\n[1] 访问预算槽最多的函数（候选聚合器）")
    for fea, sl in ranked[:10]:
        inc = sum(1 for a, b in sl if a == "income")
        exp = sum(1 for a, b in sl if a == "expense")
        log("   sub_%X  %-28s  收入槽 %2d  支出槽 %2d%s%s"
            % (fea, idc.get_func_name(fea)[:28], inc, exp,
               "  [含 gold]" if fea in gold_funcs else "",
               "  [含 0x2638]" if fea in span_funcs else ""))

    # ---- 只反编译前几个，输出触碰预算槽的行 ----
    log("\n[2] 候选函数里触碰预算槽的语句")
    targets = [fea for fea, sl in ranked[:4]]
    # 再补上同时含 gold 的函数
    for fea in list(gold_funcs):
        if fea in fmap and fea not in targets and len(fmap[fea]) >= 3:
            targets.append(fea)
    targets = targets[:8]
    log("   反编译 %d 个函数" % len(targets))
    for fea in targets:
        code = decomp(fea)
        if not code:
            log("\n   --- sub_%X: 反编译失败 ---" % fea)
            continue
        log("\n   --- sub_%X (%s) 槽 %s ---"
            % (fea, idc.get_func_name(fea), sorted(fmap.get(fea, []))))
        for ln in code.splitlines():
            nums = [int(x) for x in re.findall(r"\b\d{4,5}\b", ln)]
            if any(slot_of(n) for n in nums):
                log("      %s" % ln.strip()[:130])

    # ---- 0x2638 的引用者 ----
    log("\n[3] 引用 CCountry 大小 0x2638 的函数")
    for fea in sorted(span_funcs)[:15]:
        log("   sub_%X  %s  预算槽 %d 个"
            % (fea, idc.get_func_name(fea), len(fmap.get(fea, []))))

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    log("\n已写入 %s" % OUT)


main()
