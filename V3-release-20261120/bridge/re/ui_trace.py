"""Disassemble whole functions that call BuildCommand (function bounds from .pdata)."""
import struct
import sys

sys.path.insert(0, r"D:\Game\ai\Victoria3-RE\legacy\pylibs")
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

EXE = r"D:\Game\ai\V3\_archive\victoria3_new_100221048.exe"
CALLS = [0x0088E765, 0x008DD204, 0x00AF730D, 0x014E5B1C]


class PE:
    def __init__(self, path):
        self.data = open(path, "rb").read()
        d = self.data
        e = struct.unpack_from("<I", d, 0x3C)[0]
        nsec = struct.unpack_from("<H", d, e + 6)[0]
        optsz = struct.unpack_from("<H", d, e + 20)[0]
        opt = e + 24
        self.base = struct.unpack_from("<Q", d, opt + 24)[0]
        self.sections = []
        s = opt + optsz
        for i in range(nsec):
            p = s + i * 40
            name = d[p:p + 8].rstrip(b"\0").decode("ascii", "replace")
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", d, p + 8)
            self.sections.append((name, vaddr, vsize, rawsize, rawptr))

    def off(self, rva):
        for name, vaddr, vsize, rawsize, rawptr in self.sections:
            if vaddr <= rva < vaddr + rawsize:
                return rawptr + (rva - vaddr)
        return None

    def sec(self, rva):
        for name, vaddr, vsize, rawsize, rawptr in self.sections:
            if vaddr <= rva < vaddr + vsize:
                return name
        return None

    def pdata(self):
        out = []
        for name, vaddr, vsize, rawsize, rawptr in self.sections:
            if name != ".pdata":
                continue
            n = min(vsize, rawsize) // 12
            for i in range(n):
                p = rawptr + i * 12
                start, end, unwind = struct.unpack_from("<III", self.data, p)
                if start and end > start:
                    out.append((start, end))
        return out


pe = PE(EXE)
funcs = pe.pdata()
print("pdata functions: %d" % len(funcs))


def containing(rva):
    for s, e in funcs:
        if s <= rva < e:
            return s, e
    return None, None


md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True

for call in CALLS:
    s, e = containing(call)
    print()
    print("=" * 78)
    if not s:
        print("call 0x%08X: no function record" % call)
        continue
    print("call 0x%08X inside function 0x%08X..0x%08X (%d bytes)"
          % (call, s, e, e - s))
    print("=" * 78)
    off = pe.off(s)
    code = pe.data[off:off + (e - s)]
    for ins in md.disasm(code, pe.base + s):
        rva = ins.address - pe.base
        if rva > call + 8:
            break
        note = ""
        for op in ins.operands:
            if op.type == 3 and op.mem.base == 41:
                tgt = ins.address + ins.size + op.mem.disp - pe.base
                note = "   ; -> 0x%X (%s)" % (tgt, pe.sec(tgt))
        marker = "  <== CALL BuildCommand" if rva == call else ""
        print("  0x%08X  %-42s%s%s" % (rva, ins.mnemonic + " " + ins.op_str, note, marker))
