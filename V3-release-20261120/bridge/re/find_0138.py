"""Find code that stores into [reg+0x138] - the field the reload fills in."""
import struct
import sys

sys.path.insert(0, r"D:\Game\ai\Victoria3-RE\legacy\pylibs")
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM

EXE = r"D:\Game\ai\V3\_archive\victoria3_new_100221048.exe"
DISP = 0x138


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

    def text(self):
        for name, vaddr, vsize, rawsize, rawptr in self.sections:
            if name == ".text":
                return vaddr, rawsize, rawptr
        return None

    def pdata(self):
        out = []
        for name, vaddr, vsize, rawsize, rawptr in self.sections:
            if name != ".pdata":
                continue
            for i in range(min(vsize, rawsize) // 12):
                p = rawptr + i * 12
                s, e, u = struct.unpack_from("<III", self.data, p)
                if s and e > s:
                    out.append((s, e))
        return out


pe = PE(EXE)
tvaddr, trawsz, trawptr = pe.text()
blob = pe.data[trawptr:trawptr + trawsz]
funcs = pe.pdata()


def func_of(rva):
    for s, e in funcs:
        if s <= rva < e:
            return s
    return None


# Encoding of [reg+disp32] with mod=10: modrm in 0x80..0xBF range (mod=10)
needle = struct.pack("<I", DISP)
md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True

hits = []
i = 0
while True:
    i = blob.find(needle, i)
    if i < 0:
        break
    # the displacement must be preceded by a modrm byte with mod=10
    if i >= 1:
        modrm = blob[i - 1]
        if 0x80 <= modrm <= 0xBF:
            ins_off = max(0, i - 8)
            for ins in md.disasm(blob[ins_off:i + 4], tvaddr + ins_off):
                rva = ins.address - tvaddr
                if rva + len(ins.bytes) != i + 4:
                    continue
                write = False
                for op in ins.operands:
                    if op.type == X86_OP_MEM and op.mem.disp == DISP:
                        if ins.mnemonic.startswith("mov") and ins.op_str.startswith("qword ptr [r"):
                            write = True
                        if ins.mnemonic in ("add", "or", "and", "sub", "inc", "dec"):
                            write = True
                if write:
                    hits.append((rva, func_of(rva), ins.mnemonic, ins.op_str))
    i += 1

print("找到 %d 处写 [reg+0x%X]" % (len(hits), DISP))
seen = set()
for rva, fn, mn, ops in hits[:40]:
    key = (fn, mn, ops)
    print("  0x%08X  函数 0x%08X   %s %s" % (rva, fn or 0, mn, ops))
    seen.add(fn)

print()
print("涉及的函数:", ", ".join("0x%08X" % f for f in sorted(x for x in seen if x)))
