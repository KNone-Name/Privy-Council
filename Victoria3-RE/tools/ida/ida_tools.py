"""IDA 辅助脚本（IDAPython 7/8/9）——把重复劳动自动化。

用法：
  1. 用 IDA 打开 victoria3.exe，等自动分析跑完（ImageBase 应为 0x140000000）
  2. File -> Script file... 选本文件
  3. 在 Output window / Python console 里调用下面的函数

    v3_decomp(0x1409B5B80)      # 反编译一个函数，直接把伪代码打到输出窗口
    v3_trace(0x1401941180)      # 从某函数起沿 call 逐层展开，每层列出
                                #   深层字段偏移 + 引用到的字符串
    v3_offset(0x1E08)           # 谁在访问这个字段偏移（按函数分组）
    v3_strings("gold")          # 列出含关键字的字符串及其引用点

设计说明：V3 的数据绑定名（GetGoldReserves 之类）在镜像里没有字符串引用，
按名字追会一无所获；能追的是脚本关键字/提示文本，以及"字段偏移 -> 访问者"
这个方向。本脚本就是为这两条路准备的。
"""
try:
    import idaapi
    import ida_bytes
    import ida_funcs
    import idautils
    import idc
    import ida_kernwin
except ImportError:
    print("[v3] 这不是 IDA 的 Python 环境")
    raise SystemExit

MIN_DISP = 0x400        # 只看"对象深层"字段，跳过 vtable/头部
MAX_DISP = 0x40000


def _msg(s):
    print(s)
    ida_kernwin.msg(s + "\n")


def _func(ea):
    return ida_funcs.get_func(ea)


def _heads(start, end):
    if start == idc.BADADDR:
        return
    ea = start
    while ea < end and ea != idc.BADADDR:
        yield ea
        ea = idc.next_head(ea, end)


def _disp(insn_ea):
    """取指令里 [reg+disp] 的 disp（o_displ 操作数）。"""
    out = []
    for i in range(3):
        if idc.get_operand_type(insn_ea, i) == idc.o_displ:
            v = idc.get_operand_value(insn_ea, i)
            if v is not None and 0 < v <= MAX_DISP:
                out.append(v)
    return out


def _ref_strings(func_ea, limit=8):
    """函数里引用到的字符串字面量。"""
    f = _func(func_ea)
    if not f:
        return []
    out = []
    for ea in _heads(f.start_ea, f.end_ea):
        for i in range(3):
            if idc.get_operand_type(ea, i) == idc.o_mem:
                addr = idc.get_operand_value(ea, i)
                s = ida_bytes.get_strlit_contents(addr, -1, 0)
                if s and len(s) >= 3:
                    try:
                        txt = s.decode("ascii", "ignore")
                    except Exception:
                        continue
                    if txt and txt not in out:
                        out.append(txt)
                        if len(out) >= limit:
                            return out
    return out


def _field_offsets(func_ea, min_disp=MIN_DISP):
    f = _func(func_ea)
    if not f:
        return {}
    acc = {}
    for ea in _heads(f.start_ea, f.end_ea):
        for d in _disp(ea):
            if d >= min_disp:
                acc.setdefault(d, []).append(ea)
    return acc


def _call_targets(func_ea):
    f = _func(func_ea)
    if not f:
        return []
    out = []
    for ea in _heads(f.start_ea, f.end_ea):
        if idc.print_insn_mnem(ea) == "call" and idc.get_operand_type(ea, 0) in (idc.o_near, idc.o_far):
            t = idc.get_operand_value(ea, 0)
            if t and t not in out:
                out.append(t)
    return out


# ---------------------------------------------------------------- 对外接口
def v3_decomp(ea):
    """反编译并打印伪代码（需要 Hex-Rays）。"""
    try:
        import ida_hexrays
    except ImportError:
        _msg("[v3] 没有 Hex-Rays")
        return None
    if not ida_hexrays.init_hexrays_plugin():
        _msg("[v3] Hex-Rays 未就绪")
        return None
    cf = ida_hexrays.decompile(ea)
    if cf is None:
        _msg("[v3] 反编译失败 0x%X" % ea)
        return None
    text = str(cf)
    _msg("===== decompile 0x%X =====" % ea)
    _msg(text)
    return text


def v3_trace(ea, depth=2, max_callees=6):
    """从 ea 起沿 call 展开若干层，打印每层的深层偏移与字符串。"""
    seen = set()

    def walk(func_ea, level):
        if level > depth or func_ea in seen:
            return
        seen.add(func_ea)
        f = _func(func_ea)
        if not f:
            return
        name = idc.get_func_name(func_ea)
        strs = _ref_strings(func_ea)
        offs = _field_offsets(func_ea)
        top = sorted(offs.items(), key=lambda kv: -len(kv[1]))[:8]
        _msg("%s0x%X %s  (%d 字节)" % ("  " * level, func_ea, name, f.end_ea - f.start_ea))
        if strs:
            _msg("%s  strings: %s" % ("  " * level, strs))
        if top:
            _msg("%s  offsets: %s" % ("  " * level,
                                      ["+0x%X(x%d)" % (d, len(v)) for d, v in top]))
        for t in _call_targets(func_ea)[:max_callees]:
            walk(t, level + 1)

    walk(ea, 0)


def v3_offset(offset, min_disp=None):
    """列出所有以 offset 为字段偏移的指令，按所在函数分组。"""
    from collections import defaultdict
    target = offset
    hits = defaultdict(list)
    for seg in idautils.Segments():
        if idc.get_segm_name(seg) != ".text":
            continue
        end = idc.get_segm_end(seg)
        for ea in _heads(idc.get_segm_start(seg), end):
            if idc.get_operand_type(ea, 0) not in (idc.o_displ, idc.o_phrase, idc.o_mem):
                pass
            if target in _disp(ea):
                f = _func(ea)
                hits[f.start_ea if f else idc.BADADDR].append(ea)
    _msg("===== 访问 +0x%X 的位置：%d 个函数 =====" % (target, len(hits)))
    for fstart, eas in sorted(hits.items()):
        if fstart == idc.BADADDR:
            continue
        strs = _ref_strings(fstart, limit=5)
        _msg("0x%X %s  命中 %d 处  strings=%s"
             % (fstart, idc.get_func_name(fstart), len(eas), strs))
    return hits


def v3_strings(pattern, limit=20):
    """列出包含 pattern 的字符串及其代码引用。"""
    import idautils as _x
    found = 0
    for s in _x.Strings():
        txt = idc.get_strlit_contents(s.ea, -1, 0)
        if not txt:
            continue
        try:
            txt = txt.decode("ascii", "ignore")
        except Exception:
            continue
        if pattern.lower() not in txt.lower():
            continue
        _msg('"%s"  @0x%X' % (txt, s.ea))
        for ref in _x.XrefsTo(s.ea):
            f = _func(ref.frm)
            _msg("    ref 0x%X  in %s" % (ref.frm,
                                          idc.get_func_name(f.start_ea) if f else "?"))
        found += 1
        if found >= limit:
            break
    if not found:
        _msg("[v3] 没有包含 %r 的字符串" % pattern)
