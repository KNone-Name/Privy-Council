"""Final: signatures with branch displacements wildcarded, plus BuildCtx candidates."""
import struct
import sys

sys.path.insert(0, r"D:\Game\ai\Victoria3-RE\legacy\pylibs")
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_REG_RIP

NEW = r"D:\Game\ai\V3\_archive\victoria3_new_100221048.exe"

MATCH = {
    "ResolveCountry": 0x00983A00,
    "ResolveState":   0x00983660,
    "CountLevels":    0x0095CB60,
    "BuildCommand":   0x008C24B0,
    "BldLookup":      0x00DCBD70,
}
GLOBALS = {"regState": 0x05C0DCB8, "regCountry": 0x05C0C460}


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

    def sec_of(self, rva):
        for name, vaddr, vsize, rawsize, rawptr in self.sections:
            if vaddr <= rva < vaddr + vsize:
                return name
        return None

    def text(self):
        for name, vaddr, vsize, rawsize, rawptr in self.sections:
            if name == ".text":
                return vaddr, rawsize, rawptr
        return None


pe = PE(NEW)
md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True


def branch_tail_len(b):
    """Number of trailing bytes that encode a relative branch target."""
    if not b:
        return 0
    if b[0] in (0xE8, 0xE9):
        return 4
    if b[0] == 0x0F and len(b) >= 2 and 0x80 <= b[1] <= 0x8F:
        return 4
    if 0x70 <= b[0] <= 0x7F or b[0] in (0xEB, 0xE3):
        return 1
    return 0


def build_sig(rva, max_len=44):
    off = pe.off(rva)
    code = pe.data[off:off + 160]
    sig = []
    ins_list = []
    for ins in md.disasm(code, pe.base + rva):
        b = bytearray(ins.bytes)
        mask = [False] * len(b)

        tail = branch_tail_len(b)
        for i in range(len(b) - tail, len(b)):
            if i >= 0:
                mask[i] = True

        for op in ins.operands:
            if op.type == X86_OP_IMM and op.size == 4:
                for i in range(len(b) - 4, len(b)):
                    if i >= 0 and not mask[i]:
                        mask[i] = True
            if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
                for i in range(len(b) - 4, len(b)):
                    if i >= 0:
                        mask[i] = True

        for i, byte in enumerate(b):
            sig.append(None if mask[i] else byte)
        ins_list.append((ins, b, mask))
        if len(sig) >= max_len:
            break
    return ins_list, sig


print("=== 稳定签名（??=位移/跳转/立即数）===")
out_lines = []
for name, rva in MATCH.items():
    ins_list, sig = build_sig(rva)
    wild = sum(1 for v in sig if v is None)
    hexs = " ".join("??" if v is None else "%02X" % v for v in sig)
    print()
    print("%s  rva=0x%08X  %d 字节, 通配 %d" % (name, rva, len(sig), wild))
    for ins, b, mask in ins_list:
        print("    %-34s %s" % (ins.mnemonic + " " + ins.op_str,
                                " ".join("%02X" % x for x in b)))
    cpp = "static const uint8_t kSig%s[%d] = { %s };" % (name, len(sig), hexs)
    print("    " + cpp)
    out_lines.append((name, rva, sig, cpp))

print()
print("=== resolver 全局 ===")
for name, g in GLOBALS.items():
    print("  %-12s rva=0x%08X  节=%s" % (name, g, pe.sec_of(g)))

print()
print("=== 在 regState 附近被代码引用的 .data 目标（找 build ctx）===")
tvaddr, trawsz, trawptr = pe.text()
blob = pe.data[trawptr:trawptr + trawsz]
lo, hi = 0x05C0DCB8 - 0x200, 0x05C0DCB8 + 0x200

cands = {}
i = 0
while True:
    i = blob.find(b"\x8B\x05", i)          # mov reg, [rip+disp32]
    if i < 0:
        break
    if i >= 3 and 0x48 <= blob[i - 3] <= 0x4F and blob[i + 2] == 0x00:
        pass
    # generic: assume prefix+opcode at i, disp at i+2
    disp = struct.unpack_from("<i", blob, i + 2)[0]
    tgt = tvaddr + i + 6 + disp
    if lo <= tgt <= hi:
        cands[tgt] = cands.get(tgt, 0) + 1
    i += 1

for tgt in sorted(cands):
    mark = ""
    if tgt == GLOBALS["regState"]:
        mark = "  <- regState"
    if tgt == GLOBALS["regCountry"]:
        mark = "  <- regCountry"
    print("  0x%08X  被引用 %d 次%s" % (tgt, cands[tgt], mark))
