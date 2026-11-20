"""静态正向定位：脚本关键字字符串 -> 交叉引用 -> 所在函数 -> 字段偏移。

思路：V3 的字段偏移虽然不导出，但代码里读字段的指令是
`mov reg, [rbx+disp32]` / `add [rdi+disp32], reg` 这种形态。
只要先找到"处理某个业务概念的函数"（靠它引用的字符串定位），
再把这个函数反汇编，把里面所有 `[reg+disp32]` 的 disp 收集起来，
偏移就自然浮出来了——不用扫内存、不用猜数值。

依赖 capstone（可放在工作区 pylibs 里，见 get_capstone.py）。
"""
import re
import struct
import sys

from .pe import PE

try:
    import capstone as _cs
except ImportError:                                  # 允许放在工作区 pylibs
    import os
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pylibs")
    if os.path.isdir(_p):
        sys.path.insert(0, _p)
    try:
        import capstone as _cs
    except ImportError:
        _cs = None


class Static:
    def __init__(self, path):
        self.pe = PE(path)
        self.text = self._sec(".text")
        self.rdata = self._sec(".rdata")
        self.data = self._sec(".data")
        self.pdata = self._sec(".pdata")
        self._funcs = None

    def _sec(self, name):
        for s in self.pe.sections:
            if s.name == name:
                return s
        return None

    def blob(self, sec):
        return self.pe.data[sec.off:sec.off + sec.rawsize]

    # ---------------------------------------------------------- 字符串
    def strings(self, pattern, secs=(".rdata", ".data"), maxlen=120):
        rx = re.compile(pattern.encode() if isinstance(pattern, str) else pattern, re.I)
        out = []
        for name in secs:
            sec = self._sec(name)
            if not sec or not sec.rawsize:
                continue
            b = self.blob(sec)
            for m in re.finditer(rb"[\x20-\x7e]{%d,%d}" % (len(pattern) if isinstance(pattern, bytes) else 3, maxlen), b):
                s = m.group()
                if rx.search(s):
                    out.append((sec.rva + m.start(), s.decode("ascii", "ignore")))
        return out

    # ---------------------------------------------------------- 函数表
    def functions(self):
        if self._funcs is not None:
            return self._funcs
        b = self.blob(self.pdata)
        n = len(b) // 12
        rows = []
        for i in range(n):
            begin, end, unwind = struct.unpack_from("<III", b, i * 12)
            if begin and end > begin:
                rows.append((begin, end))
        rows.sort()
        self._funcs = rows
        return rows

    def func_of(self, rva):
        lo, hi = 0, len(self._funcs) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            b, e = self._funcs[mid] if self._funcs is not None else (0, 0)
            if b <= rva < e:
                return (b, e)
            if rva < b:
                hi = mid - 1
            else:
                lo = mid + 1
        return None

    # -------------------------------------------------------- 交叉引用
    _RIP_MODRM = bytes([0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D])
    # REX 前缀 0x48..0x4F 都可能出现（r8-r15 用法），别只匹配 0x48/0x4C
    _RIP_RX = re.compile(rb"[\x48-\x4F][\x8D\x8B][\x05\x0D\x15\x1D\x25\x2D\x35\x3D]")
    _RIP_RX_NO_REX = re.compile(rb"[\x8D\x8B][\x05\x0D\x15\x1D\x25\x2D\x35\x3D]")

    def xrefs_to(self, target_rvas):
        """扫 .text 里 RIP 相对的 lea/mov，找指向目标字符串的指令。"""
        targets = set(target_rvas)
        b = self.blob(self.text)
        base = self.text.rva
        hits = []
        for rx, ln in ((self._RIP_RX, 7), (self._RIP_RX_NO_REX, 6)):
            for m in rx.finditer(b):
                i = m.start()
                if i + ln > len(b):
                    continue
                disp = struct.unpack_from("<i", b, i + ln - 4)[0]
                tgt = base + i + ln + disp
                if tgt in targets:
                    hits.append(base + i)
        return sorted(set(hits))

    # ---------------------------------------------------------- 反汇编
    def field_accesses(self, fn_begin, fn_end, min_disp=0x8, max_disp=0x20000):
        """反汇编一个函数，收集 [reg+disp] 形态的偏移。"""
        if _cs is None:
            raise RuntimeError("需要 capstone：python v3live/get_capstone.py")
        md = _cs.Cs(_cs.CS_ARCH_X86, _cs.CS_MODE_64)
        md.detail = True
        off = self.pe.off(fn_begin)
        size = fn_end - fn_begin
        if off < 0 or off + size > len(self.pe.data):
            return []
        code = self.pe.data[off:off + size]
        out = []
        for insn in md.disasm(code, fn_begin):
            for op in insn.operands:
                if op.type != _cs.x86.X86_OP_MEM:
                    continue
                mem = op.mem
                if mem.base in (0, _cs.x86.X86_REG_RSP, _cs.x86.X86_REG_RBP,
                                _cs.x86.X86_REG_RIP):
                    continue
                d = mem.disp
                if min_disp <= d <= max_disp:
                    out.append((d, insn.mnemonic, insn.op_str))
        return out

    def abs_refs(self, rva, secs=(".rdata", ".data")):
        """按绝对指针反查：数据表里常存 image_base+rva 的完整 VA。"""
        pat = struct.pack("<Q", self.pe.image_base + rva)
        out = []
        for name in secs:
            sec = self._sec(name)
            if not sec or not sec.rawsize:
                continue
            b = self.blob(sec)
            start = 0
            while True:
                j = b.find(pat, start)
                if j < 0:
                    break
                out.append(sec.rva + j)
                start = j + 1
        return out

    def rva_refs(self, rva, secs=(".rdata", ".data")):
        """有些表存 32 位 RVA 而不是完整 VA。"""
        pat = struct.pack("<I", rva)
        out = []
        for name in secs:
            sec = self._sec(name)
            if not sec or not sec.rawsize:
                continue
            b = self.blob(sec)
            start = 0
            while True:
                j = b.find(pat, start)
                if j < 0:
                    break
                out.append(sec.rva + j)
                start = j + 1
        return out

    def qwords_around(self, rva, before=0x40, count=24):
        """把 rva 附近按 qword 展开，标出哪些指向 .text（很可能是函数指针）。"""
        start = rva - before
        o = self.pe.off(start)
        if o < 0:
            return []
        rows = []
        tb, te = self.text.rva, self.text.rva + self.text.vsize
        for i in range(count):
            v = struct.unpack_from("<Q", self.pe.data, o + i * 8)[0]
            r = v - self.pe.image_base if self.pe.image_base <= v < self.pe.image_base + 0x8000000 else None
            tag = ""
            if r is not None and tb <= r < te:
                tag = "  <- .text (函数?)"
            elif r is not None:
                tag = "  <- 镜像内"
            rows.append((start + i * 8, v, tag))
        return rows

    # ------------------------------------------------------------ 反查
    def find_disp(self, disp, limit=400):
        """找 .text 里所有以 disp 作为内存操作数偏移的指令。

        做法：先按 4 字节小端模式定位候选字节，再在附近尝试几种指令边界
        反汇编，确认该指令的内存操作数 disp 确实等于目标值。
        比全量反汇编（72 MB）快得多，且足够准。
        """
        if _cs is None:
            raise RuntimeError("需要 capstone：python v3live/get_capstone.py")
        pat = struct.pack("<I", disp & 0xFFFFFFFF)
        b = self.blob(self.text)
        base = self.text.rva
        md = _cs.Cs(_cs.CS_ARCH_X86, _cs.CS_MODE_64)
        md.detail = True
        out, seen = [], set()
        start = 0
        while True:
            j = b.find(pat, start)
            if j < 0:
                break
            start = j + 1
            if len(out) >= limit:
                break
            for k in range(1, 11):
                pos = j - k
                if pos < 0:
                    continue
                try:
                    insns = list(md.disasm(b[pos:pos + 16], base + pos, count=1))
                except Exception:
                    continue
                if not insns:
                    continue
                insn = insns[0]
                # 指令必须覆盖到 disp 字段
                if not (insn.address <= base + j < insn.address + insn.size):
                    continue
                for op in insn.operands:
                    if op.type != _cs.x86.X86_OP_MEM:
                        continue
                    if op.mem.disp != disp:
                        continue
                    if op.mem.base in (0, _cs.x86.X86_REG_RSP,
                                       _cs.x86.X86_REG_RBP, _cs.x86.X86_REG_RIP):
                        continue
                    key = insn.address
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append((insn.address, insn.mnemonic, insn.op_str))
                    break
                else:
                    continue
                break
        return out

    def function_strings(self, fn_begin, fn_end, maxlen=90, limit=12):
        """一个函数里引用了哪些字符串（RIP 相对 lea/mov 解析）。"""
        off = self.pe.off(fn_begin)
        size = fn_end - fn_begin
        if off < 0:
            return []
        b = self.pe.data[off:off + size]
        base = fn_begin
        out = []
        for m in re.finditer(rb"[\x48\x4C][\x8D\x8B][\x05\x0D\x15\x1D\x25\x2D\x35\x3D]", b):
            i = m.start()
            if i + 7 > len(b):
                continue
            disp = struct.unpack_from("<i", b, i + 3)[0]
            tgt = base + i + 7 + disp
            s = self.cstr_at(tgt, maxlen)
            if s and len(s) >= 3:
                out.append(s)
                if len(out) >= limit:
                    break
        return out

    def cstr_at(self, rva, maxlen=90):
        sec = self.pe.section_of(rva)
        if not sec:
            return None
        o = self.pe.off(rva)
        if o < 0:
            return None
        raw = self.pe.data[o:o + maxlen]
        z = raw.find(b"\0")
        raw = raw[:z] if z >= 0 else raw
        if not raw or any(c < 0x20 or c > 0x7E for c in raw):
            return None
        return raw.decode("ascii", "ignore")

    # ------------------------------------------------------------ 总流程
    def trace(self, pattern, max_strings=4, max_funcs=4):
        strs = self.strings(pattern)
        if not strs:
            return {"strings": [], "funcs": [], "offsets": {}}
        self.functions()
        picked = strs[:max_strings]
        xrefs = self.xrefs_to([rva for rva, _ in picked])
        funcs = []
        for x in xrefs:
            f = self.func_of(x)
            if f and f not in funcs:
                funcs.append(f)
        funcs = funcs[:max_funcs]
        counts = {}
        samples = {}
        for b, e in funcs:
            for disp, mnem, opstr in self.field_accesses(b, e):
                counts[disp] = counts.get(disp, 0) + 1
                samples.setdefault(disp, [])
                if len(samples[disp]) < 3:
                    samples[disp].append("%s %s" % (mnem, opstr))
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        return {"strings": picked, "xrefs": xrefs, "funcs": funcs,
                "offsets": ranked, "samples": samples}
