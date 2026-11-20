"""Climb the caller chain upward from the UI-path function."""
import struct
import sys

sys.path.insert(0, r"D:\Game\ai\Victoria3-RE\legacy\pylibs")

EXE = r"D:\Game\ai\V3\_archive\victoria3_new_100221048.exe"


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

    def sec(self, rva):
        for name, vaddr, vsize, rawsize, rawptr in self.sections:
            if vaddr <= rva < vaddr + vsize:
                return name
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
            n = min(vsize, rawsize) // 12
            for i in range(n):
                p = rawptr + i * 12
                start, end, unwind = struct.unpack_from("<III", self.data, p)
                if start and end > start:
                    out.append((start, end))
        return out


pe = PE(EXE)
tvaddr, trawsz, trawptr = pe.text()
blob = pe.data[trawptr:trawptr + trawsz]
funcs = pe.pdata()
fstarts = {s: e for s, e in funcs}


def func_of(rva):
    for s, e in funcs:
        if s <= rva < e:
            return s
    return None


def callers_of(target):
    """All direct `call rel32` sites pointing at target."""
    out = []
    i = 0
    while True:
        i = blob.find(b"\xE8", i)
        if i < 0:
            break
        if i + 5 <= len(blob):
            rel = struct.unpack_from("<i", blob, i + 1)[0]
            if tvaddr + i + 5 + rel == target:
                out.append(tvaddr + i)
        i += 1
    return out


level = {0x0088DC80: "UI-path (builds param, calls BuildCommand)",
         0x008C2410: "virtual-dispatch wrapper",
         0x008C24B0: "BuildCommand"}

print("=== 直接调用者（一层层往上）===")
frontier = [0x0088DC80, 0x008C2410]
seen = set(frontier)
for depth in range(1, 4):
    nxt = []
    print()
    print("---- 第 %d 层上溯 ----" % depth)
    for t in frontier:
        cs = callers_of(t)
        label = level.get(t, "")
        print("  0x%08X%s  被 %d 处直接调用" % (t, (" (" + label + ")") if label else "", len(cs)))
        for c in cs[:8]:
            f = func_of(c)
            print("      from 0x%08X  (函数 0x%08X)" % (c, f if f else 0))
            if f and f not in seen:
                seen.add(f)
                nxt.append(f)
    frontier = nxt[:12]
    if not frontier:
        print("  （没有更多直接调用者 —— 上面那层是虚调用或入口）")
        break
