"""Reconstruct the BuildParam structure from every BuildCommand call site."""
import struct
import sys

sys.path.insert(0, r"D:\Game\ai\Victoria3-RE\legacy\pylibs")
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

EXE = r"D:\Game\ai\V3\_archive\victoria3_new_100221048.exe"
BUILD_CMD = 0x008C24B0
CALLS = [0x0088E765, 0x008C2482, 0x008DD204, 0x00AF730D, 0x014E5B1C]


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


pe = PE(EXE)
md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True

for call in CALLS:
    print("=" * 78)
    print("call site 0x%08X" % call)
    print("=" * 78)
    start = call - 160
    off = pe.off(start)
    # Disassemble a window ending just before the call.
    ins_list = list(md.disasm(pe.data[off:off + 220], pe.base + start))
    for ins in ins_list:
        rva = ins.address - pe.base
        note = ""
        # RIP-relative reference?
        for op in ins.operands:
            if op.type == 3 and op.mem.base == 41:
                tgt = ins.address + ins.size + op.mem.disp - pe.base
                note = "   ; -> 0x%X (%s)" % (tgt, pe.sec(tgt))
        marker = "  <== CALL" if rva == call else ""
        print("  0x%08X  %-40s%s%s" % (rva, ins.mnemonic + " " + ins.op_str, note, marker))
    print()
