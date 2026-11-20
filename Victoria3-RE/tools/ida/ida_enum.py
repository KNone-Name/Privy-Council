"""IDA 脚本：反查收入/支出数组的枚举顺序（含从未非零的空槽）。

在 IDA 里打开 victoria3.exe，等自动分析完成（ImageBase 应为 0x140000000），
然后 File -> Script file... 选本文件。

命令行无界面跑（推荐，慢但省事）：
    "<IDA目录>\\idat.exe" -A -S"v3live\\ida_enum.py" "E:\\...\\victoria3.exe"

原理
----
每个预算项在 CCountry 里是固定偏移的 i64：
    收入  CCountry+0x1E00+8i   (i = 0..15)
    支出  CCountry+0x1D50+8i   (i = 0..21)
访问器的机器码里一定带这个常量位移。所以：

  1. 在 .text 里按 4 字节小端搜每个位移常量，定位候选指令
  2. 把候选地址归到函数，只反编译这些函数（不是全部几万个）
  3. 反编译文本里出现的偏移即该函数真正读的槽
  4. 用字符串 xref（cached_* 这类注册名）给槽位配上名字

输出：tmp\\ida_enum_result.txt
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

INCOME_BASE = 0x1E00
INCOME_N = 16
EXPENSE_BASE = 0x1D50
EXPENSE_N = 22
OUT = r"D:\Game\ai\Victoria3-RE\data\ida_enum_result.txt"
MAX_HITS_PER_SLOT = 400

NAME_PAT = re.compile(
    r"(cached_|^)(income_tax|poll_tax|dividend|consumption|mint|welfare|subsid|"
    r"subvention|interest|tariff|pact|treaty|slave|construction|wage|goods|"
    r"expense|income|ship|lane|naval|share)", re.I)

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
    """反编译；没有 Hex-Rays 时退回线性反汇编文本。"""
    if HAVE_HX:
        try:
            cf = ida_hexrays.decompile(ea)
            if cf:
                return str(cf)
        except Exception:
            pass
    # 兜底：逐条反汇编
    f = ida_funcs.get_func(ea)
    if not f:
        return None
    out = []
    for item in idautils.Heads(f.start_ea, f.end_ea):
        try:
            out.append(idc.generate_disasm_line(item, 0) or "")
        except Exception:
            continue
    return "\n".join(out)


def offsets_in(text):
    out = set()
    for m in re.finditer(r"\+\s*(\d+)", text):
        s = slot_of(int(m.group(1)))
        if s:
            out.add(s)
    for m in re.finditer(r"0x([0-9A-Fa-f]+)", text):
        s = slot_of(int(m.group(1), 16))
        if s:
            out.add(s)
    return out


def main():
    log("=" * 72)
    log("victoria3.exe 预算数组枚举反查")
    log("ImageBase = 0x%X" % idaapi.get_imagebase())
    log("=" * 72)

    text = None
    for seg in idautils.Segments():
        if idc.get_segm_name(seg).startswith(".text"):
            text = (seg, idc.get_segm_end(seg))
            break
    if not text:
        log("找不到 .text 段")
        return
    start, end = text
    body = ida_bytes.get_bytes(start, end - start)
    log("\n.text: 0x%X - 0x%X (%.1f MB)" % (start, end, len(body) / 1048576.0))

    # 1+2. 搜位移常量，归到函数
    funcs = {}
    for arr, base, n in (("income", INCOME_BASE, INCOME_N),
                         ("expense", EXPENSE_BASE, EXPENSE_N)):
        for i in range(n):
            off = base + 8 * i
            pat = struct.pack("<I", off)
            pos = 0
            cnt = 0
            while cnt < MAX_HITS_PER_SLOT:
                j = body.find(pat, pos)
                if j < 0:
                    break
                pos = j + 1
                cnt += 1
                ea = start + j
                f = ida_funcs.get_func(ea)
                if f:
                    funcs.setdefault(f.start_ea, set()).add((arr, i))
            log("  %-7s 槽 %-2d  位移 0x%X  指令候选 %d 处" % (arr, i, off, cnt))

    log("\n[1] 候选函数 %d 个，开始反编译" % len(funcs))

    # 3. 只反编译候选函数
    confirmed = {}
    for k, (fea, guess) in enumerate(sorted(funcs.items())):
        code = decomp(fea)
        if not code:
            continue
        found = offsets_in(code)
        if not found:
            continue
        for s in found:
            confirmed.setdefault(s, []).append((fea, code))

    log("[2] 确认读取预算偏移的函数：涉及 %d 个槽" % len(confirmed))

    # 4. 用字符串 xref 配名字
    name_of = {}
    strs = []
    for s in idautils.Strings():
        try:
            t = str(s)
        except Exception:
            continue
        if t and NAME_PAT.search(t):
            strs.append((s.ea, t))
    log("[3] 关注字符串 %d 个" % len(strs))
    for sea, txt in strs:
        for xr in idautils.XrefsTo(sea, 0):
            f = ida_funcs.get_func(xr.frm)
            if f:
                name_of.setdefault((f.start_ea, txt), 0)
    # 把「引用了该名字串的函数」和「读该槽的函数」建立联系：
    # 注册函数通常同时引用名字串和目标函数，所以看注册函数里调用了谁
    for sea, txt in strs:
        for xr in idautils.XrefsTo(sea, 0):
            f = ida_funcs.get_func(xr.frm)
            if not f:
                continue
            code = decomp(f.start_ea)
            if not code:
                continue
            for m in re.finditer(r"(sub_[0-9A-Fa-f]+)\(", code):
                tgt = idc.get_name_ea_simple(m.group(1))
                if tgt == idc.BADADDR or tgt not in funcs:
                    continue
                c2 = decomp(tgt)
                if not c2:
                    continue
                for s in offsets_in(c2):
                    name_of.setdefault(s, txt)

    # 5. 输出
    log("\n[4] 结果")
    for arr, base, n in (("income", INCOME_BASE, INCOME_N),
                         ("expense", EXPENSE_BASE, EXPENSE_N)):
        log("\n  ==== %s[%d]  ====" % (arr, n))
        for i in range(n):
            key = (arr, i)
            fns = confirmed.get(key, [])
            nm = name_of.get(key, "?")
            log("   槽 %-2d  位移 0x%04X  名字=%-42s 读取函数 %d 个"
                % (i, base + 8 * i, nm, len(fns)))
            for fea, code in fns[:2]:
                log("        sub_%X (%s)" % (fea, idc.get_func_name(fea)))
                for ln in code.splitlines():
                    if re.search(r"\+\s*(%d|0x%X)\b" % (base + 8 * i, base + 8 * i), ln):
                        log("           %s" % ln.strip()[:110])

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    log("\n已写入 %s" % OUT)


main()
