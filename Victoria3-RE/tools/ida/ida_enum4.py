"""IDA 脚本 v4：把候选函数的伪代码/汇编整段导出，供人工判读。

v3 的问题：大函数 Hex-Rays 反编译失败会退回汇编，而输出过滤器只认十进制，
所以什么都没打印。

v4 做法：
  1. 对固定候选列表逐个尝试反编译，成功就把**完整伪代码**写入独立文件
  2. 无论成功与否，都定位函数内每个预算位移常量的出现位置，
     并反汇编该位置前后各 6 条指令（汇编里能看到它累加进哪个变量、
     调用了哪个计算函数）
  3. 汇总每个函数实际触碰的槽（十进制与十六进制两种写法都认）

在 IDA 里打开 victoria3.exe，File -> Script file... 选本文件。
输出目录：tmp\\ida_funcs\\
"""
import os
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
OUTDIR = r"D:\Game\ai\Victoria3-RE\data\ida_funcs"

CANDIDATES = [
    0x141D95250,   # 支出 18 槽 + 收入 2 槽
    0x141DAB890,   # 收入 13 槽
    0x1429C33E0,   # 收入 13 槽
    0x140DA17E0,   # 收入 9 + 支出 8
    0x140DB1780,   # 收入 3 + 支出 7
    0x140DB0C40,   # 收入 3 + 支出 6
    0x140C00170,   # 收入 9
    0x140DA2980,   # 收入 10 + 支出 1
]

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


def slots_in_text(text):
    out = set()
    for m in re.finditer(r"\b(\d{4,5})\b", text):
        s = slot_of(int(m.group(1)))
        if s:
            out.add(s)
    for m in re.finditer(r"\b([0-9A-Fa-f]{3,6})h\b", text):
        s = slot_of(int(m.group(1), 16))
        if s:
            out.add(s)
    for m in re.finditer(r"0x([0-9A-Fa-f]{3,6})\b", text):
        s = slot_of(int(m.group(1), 16))
        if s:
            out.add(s)
    return out


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    log("HexRays=%s  输出目录=%s" % (HAVE_HX, OUTDIR))

    for fea in CANDIDATES:
        f = ida_funcs.get_func(fea)
        if not f:
            log("\n0x%X: 不是函数" % fea)
            continue
        log("\n" + "=" * 70)
        log("函数 0x%X  范围 0x%X-0x%X  大小 %d"
            % (fea, f.start_ea, f.end_ea, f.end_ea - f.start_ea))

        # ---- 伪代码 ----
        code, ok = None, False
        if HAVE_HX:
            try:
                cf = ida_hexrays.decompile(fea)
                if cf:
                    code = str(cf)
                    ok = True
            except Exception as e:
                log("   Hex-Rays 失败: %s" % e)
        if ok:
            sl = slots_in_text(code)
            log("   伪代码成功，触碰槽 %s" % sorted(sl))
            path = os.path.join(OUTDIR, "sub_%X.c" % fea)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(code)
            log("   已写出 %s (%d 字符)" % (path, len(code)))
            # 只打印含预算偏移的行
            n = 0
            for ln in code.splitlines():
                if slots_in_text(ln):
                    log("      %s" % ln.strip()[:140])
                    n += 1
                    if n > 80:
                        log("      ...")
                        break
        else:
            log("   伪代码不可用，只用汇编")

        # ---- 汇编上下文（始终做）----
        body = ida_bytes.get_bytes(f.start_ea, f.end_ea - f.start_ea) or b""
        hits = []
        for i in range(INCOME_N):
            off = INCOME_BASE + 8 * i
            pat = struct.pack("<I", off)
            pos = 0
            while True:
                j = body.find(pat, pos)
                if j < 0:
                    break
                pos = j + 1
                hits.append((f.start_ea + j, ("income", i)))
        for i in range(EXPENSE_N):
            off = EXPENSE_BASE + 8 * i
            pat = struct.pack("<I", off)
            pos = 0
            while True:
                j = body.find(pat, pos)
                if j < 0:
                    break
                pos = j + 1
                hits.append((f.start_ea + j, ("expense", i)))
        log("   函数内常量出现 %d 处" % len(hits))
        for ea, key in hits[:40]:
            log("      --- %s 槽 %d @0x%X ---" % (key[0], key[1], ea))
            cur = ea
            for _ in range(6):
                cur = idc.prev_head(cur)
                if cur == idc.BADADDR or cur < f.start_ea:
                    break
            start = cur if cur != idc.BADADDR else ea
            c = start
            for _ in range(14):
                if c == idc.BADADDR or c >= f.end_ea:
                    break
                try:
                    log("         %08X  %s" % (c, idc.generate_disasm_line(c, 0) or ""))
                except Exception:
                    pass
                c = idc.next_head(c)
        if len(hits) > 40:
            log("      （只显示前 40 处，共 %d）" % len(hits))

    with open(r"D:\Game\ai\Victoria3-RE\data\ida_enum4_result.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    log("\n汇总已写入 tmp\\ida_enum4_result.txt")


main()
