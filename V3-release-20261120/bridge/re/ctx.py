"""Find BuildCommand call sites and the build-context global they load."""
import struct

NEW = r"D:\Game\ai\V3\_archive\victoria3_new_100221048.exe"
BUILD_CMD = 0x008C24B0


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
tvaddr, trawsz, trawptr = pe.text()
blob = pe.data[trawptr:trawptr + trawsz]

print("=== 谁调用了 BuildCommand (rva 0x%X) ===" % BUILD_CMD)
calls = []
i = 0
while True:
    i = blob.find(b"\xE8", i)
    if i < 0:
        break
    if i + 5 <= len(blob):
        rel = struct.unpack_from("<i", blob, i + 1)[0]
        tgt = tvaddr + i + 5 + rel
        if tgt == BUILD_CMD:
            calls.append(tvaddr + i)
    i += 1

print("  找到 %d 个调用点" % len(calls))
for c in calls[:10]:
    print("    rva=0x%08X" % c)

# In a Windows x64 call the 8th argument is [rsp+0x20] after the return address
# is pushed, i.e. the caller does "mov [rsp+0x20], reg" before the call. Look
# backwards for a RIP-relative load feeding it.
print()
print("=== 调用点前 64 字节里的 rip 引用（.data 目标）===")
for c in calls[:5]:
    off = pe.off(c)
    start = max(0, off - 96)
    chunk = pe.data[start:off]
    print("  调用点 rva=0x%08X" % c)
    j = 0
    hits = 0
    while j < len(chunk) - 7:
        # 48 8B 0D/15/05/0D ... = mov r64,[rip+disp32]
        if chunk[j] in (0x48, 0x4C) and chunk[j + 1] in (0x8B, 0x8D, 0x89):
            modrm = chunk[j + 2]
            if (modrm & 0xC7) == 0x05:
                disp = struct.unpack_from("<i", chunk, j + 3)[0]
                ins_rva = tvaddr + (start + j) - trawptr
                tgt = ins_rva + 7 + disp
                sec = pe.sec_of(tgt)
                print("      rva=0x%08X  ->  0x%08X (%s)"
                      % (ins_rva, tgt, sec))
                hits += 1
        j += 1
    if not hits:
        print("      （没有 rip 引用）")
