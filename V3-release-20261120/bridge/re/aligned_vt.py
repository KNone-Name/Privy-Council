"""Properly aligned disassembly around each reference to the request vtable."""
import bisect
import struct
import sys

sys.path.insert(0, r"D:\Game\ai\Victoria3-RE\legacy\pylibs")
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

EXE = r"D:\Game\ai\V3\_archive\victoria3_new_100221048.exe"
VT_RVA = 0x46B5048


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
            name = d[p:p + 8].rstrip(b"\0").decode()
            vs, va, rs, rp = struct.unpack_from("<IIII", d, p + 8)
            self.sections.append((name, va, vs, rs, rp))

    def off(self, rva):
        for n, va, vs, rs, rp in self.sections:
            if va <= rva < va + rs:
                return rp + (rva - va)
        return None

    def pdata(self):
        out = []
        for n, va, vs, rs, rp in self.sections:
            if n != ".pdata":
                continue
            for i in range(min(vs, rs) // 12):
                p = rp + i * 12
                a, b, u = struct.unpack_from("<III", self.data, p)
                if a and b > a:
                    out.append((a, b))
        out.sort()
        return out


pe = PE(EXE)
funcs = pe.pdata()
starts = [s for s, _ in funcs]
name, tv, tvs, trs, trp = [x for x in pe.sections if x[0] == ".text"][0]
blob = pe.data[trp:trp + trs]

# byte scan for lea rip-relative to the vtable
sites = []
for i in range(len(blob) - 8):
    if blob[i] != 0x48 or blob[i + 1] != 0x8D:
        continue
    if (blob[i + 2] & 0xC7) != 0x05:
        continue
    disp = struct.unpack_from("<i", blob, i + 3)[0]
    if tv + i + 7 + disp == VT_RVA:
        sites.append(tv + i)

md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True

print("引用请求虚表的 lea: %d 处" % len(sites))
for site in sites[:6]:
    i = bisect.bisect_right(starts, site) - 1
    if i < 0:
        continue
    s, e = funcs[i]
    if not (s <= site < e):
        print("  0x%08X: 不在 pdata 函数内" % site)
        continue
    print()
    print("=== lea 0x%08X 位于函数 0x%08X..0x%08X (%d 字节) ===" % (site, s, e, e - s))
    shown = 0
    for ins in md.disasm(pe.data[pe.off(s):pe.off(s) + (e - s)], pe.base + s):
        rva = ins.address - pe.base
        if rva < site - 24:
            continue
        if rva > site + 16:
            break
        mark = "  <== lea vtable" if rva == site else ""
        print("   0x%08X  %-42s%s" % (rva, ins.mnemonic + " " + ins.op_str, mark))
        shown += 1
