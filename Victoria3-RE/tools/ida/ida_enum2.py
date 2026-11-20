"""IDA 脚本 v2：用「唯一锚点」定位 CCountry 的预算数组访问器。

v1 失败原因：0x1D50 / 0x1E00 这些偏移在多个类里重复出现，只搜位移常量会
命中别的类（看到 unique_ptr 赋值、虚函数调用等，明显不是 i64 金额）。
名字匹配也不可靠（字符串 xref 中转到调用者会串到无关代码）。

v2 改用三个独立锚点，每个都只在 CCountry 上成立：

  A. 数据函数名：界面绑定的是 GetIncomeTaxIncome / PredictMinting 这类名字。
     如果二进制里存在这些串且有 xref，直接反编译就能看到它读哪个槽。
  B. 结构跨度：CCountry 大小 0x2638。分配它的代码会调用
     operator new(0x2638) 或 memset(..., 0x2638)，那里的函数就是构造/更新入口。
  C. 共现：同时访问 0x12F8（gold，已确认）与 0x1D50~0x1E78 区间的函数，
     基本只能是 CCountry 的预算代码。累计访问 >= 6 个不同槽的函数是聚合器，
     它逐个槽的写法直接暴露顺序与语义。

用法：IDA 里打开 victoria3.exe，等自动分析完成后
  File -> Script file...  选本文件
输出：tmp\\ida_enum2_result.txt
"""
import re

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
GOLD_OFF = 0x12F8          # CCountry+0x12F8 = 国库，已用存档对齐验证过
CC_STRIDE = 0x2638         # CCountry 大小
OUT = r"D:\Game\ai\Victoria3-RE\data\ida_enum2_result.txt"

FUNC_NAME_PAT = re.compile(
    r"^(Get|Predict|Can|Is|Has)[A-Za-z]*"
    r"(Income|Expense|Tax|Minting|Tariff|Wage|Subsid|Welfare|Treaty|Pact|"
    r"Interest|Construction|Ship|Supply|Lane|Slave|Dividend)[A-Za-z]*$")

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


def slots_in(text):
    out = set()
    for m in re.finditer(r"\b(\d{4,5})\b", text):
        s = slot_of(int(m.group(1)))
        if s:
            out.add(s)
    for m in re.finditer(r"0x([0-9A-Fa-f]{4,6})\b", text):
        s = slot_of(int(m.group(1), 16))
        if s:
            out.add(s)
    return out


def main():
    log("=" * 72)
    log("v2：CCountry 预算数组枚举反查（三锚点交叉验证）")
    log("ImageBase = 0x%X  HexRays=%s" % (idaapi.get_imagebase(), HAVE_HX))
    log("=" * 72)

    # ---------- A. 按界面数据函数名找 ----------
    log("\n[A] 按数据函数名找（界面实际绑定的名字）")
    name_hits = []
    for s in idautils.Strings():
        try:
            t = str(s)
        except Exception:
            continue
        if not t or not FUNC_NAME_PAT.match(t):
            continue
        xr = list(idautils.XrefsTo(s.ea, 0))
        if not xr:
            continue
        name_hits.append((t, s.ea, len(xr)))
    log("   命中名字串 %d 个" % len(name_hits))
    for t, ea, n in sorted(name_hits)[:60]:
        log("     %-40s xref %d" % (t, n))

    # 对每个名字，找注册/分发函数里的配对目标函数，再反编译看读哪个槽
    log("\n[A2] 名字 -> 目标函数 -> 读取的槽")
    mapped = {}
    for t, sea, _n in name_hits:
        for x in idautils.XrefsTo(sea, 0):
            f = ida_funcs.get_func(x.frm)
            if not f:
                continue
            code = decomp(f.start_ea)
            if not code:
                continue
            for m in re.finditer(r"(sub_[0-9A-Fa-f]+)\(", code):
                tgt = idc.get_name_ea_simple(m.group(1))
                if tgt == idc.BADADDR:
                    continue
                c2 = decomp(tgt)
                if not c2:
                    continue
                sl = slots_in(c2)
                if len(sl) == 1:
                    mapped.setdefault(next(iter(sl)), t + " -> " + m.group(1))
    for k in sorted(mapped):
        log("     %-8s 槽 %-2d  <-  %s" % (k[0], k[1], mapped[k]))

    # ---------- B. 结构跨度锚点 ----------
    log("\n[B] 结构跨度锚点 0x2638（%d）" % CC_STRIDE)
    span_funcs = []
    for fea in idautils.Functions():
        f = ida_funcs.get_func(fea)
        if not f or f.end_ea - f.start_ea > 0x2000:
            continue
        code = decomp(fea)
        if not code:
            continue
        if str(CC_STRIDE) in code or hex(CC_STRIDE) in code.lower() or "10680" in code:
            span_funcs.append(fea)
    log("   引用 0x2638 的函数 %d 个" % len(span_funcs))
    for fea in span_funcs[:20]:
        log("     sub_%X  %s" % (fea, idc.get_func_name(fea)))

    # ---------- C. 共现锚点 ----------
    log("\n[C] 同时访问 gold(0x12F8) 与预算区间的函数")
    agg = []
    for fea in idautils.Functions():
        f = ida_funcs.get_func(fea)
        if not f or f.end_ea - f.start_ea > 0x4000:
            continue
        code = decomp(fea)
        if not code:
            continue
        has_gold = (str(GOLD_OFF) in code) or (hex(GOLD_OFF) in code.lower()) or ("4856" in code)
        sl = slots_in(code)
        if has_gold and len(sl) >= 3:
            agg.append((fea, sl, code))
    agg.sort(key=lambda x: -len(x[1]))
    log("   命中 %d 个函数，按访问槽数排序：" % len(agg))
    for fea, sl, code in agg[:6]:
        log("\n   --- sub_%X  访问 %d 个槽 ---" % (fea, len(sl)))
        log("       槽: %s" % sorted(sl))
        for ln in code.splitlines():
            if any(str(INCOME_BASE + 8 * b) in ln or str(EXPENSE_BASE + 8 * b) in ln
                   for a, b in sl):
                log("       %s" % ln.strip()[:120])

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    log("\n已写入 %s" % OUT)


main()
