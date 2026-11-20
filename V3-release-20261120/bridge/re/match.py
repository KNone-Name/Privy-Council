"""Match the 5 known functions from the old exe (1.15.0) inside the new exe (1.15.1).

Strategy that tolerates shifted rel32 bytes: take several 8-byte anchors spread over
the old function, find their hits in the new image, vote on the implied start
offset, then score candidates by byte equality over a long window.
"""
import struct

OLD = r"D:\Game\ai\V3\_archive\victoria3_old_99988600.exe"
NEW = r"D:\Game\ai\V3\_archive\victoria3_new_100221048.exe"

TARGETS = {
    "ResolveCountry": 0x0097B8D0,
    "ResolveState":   0x0097B530,
    "CountLevels":    0x00954B40,
    "BuildCommand":   0x0008BA770,
    "BldLookup":      0x000DC0140,
}


class PE:
    def __init__(self, path):
        self.path = path
        self.data = open(path, "rb").read()
        d = self.data
        e = struct.unpack_from("<I", d, 0x3C)[0]
        assert d[e:e + 4] == b"PE\0\0", "bad PE"
        nsec = struct.unpack_from("<H", d, e + 6)[0]
        optsz = struct.unpack_from("<H", d, e + 20)[0]
        opt = e + 24
        self.image_base = struct.unpack_from("<Q", d, opt + 24)[0]
        self.sections = []
        s = opt + optsz
        for i in range(nsec):
            p = s + i * 40
            name = d[p:p + 8].rstrip(b"\0").decode("ascii", "replace")
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", d, p + 8)
            self.sections.append((name, vaddr, rawsize, rawptr))

    def off(self, rva):
        for name, vaddr, rawsize, rawptr in self.sections:
            if vaddr <= rva < vaddr + rawsize:
                return rawptr + (rva - vaddr)
        return None

    def rva(self, off):
        for name, vaddr, rawsize, rawptr in self.sections:
            if rawptr <= off < rawptr + rawsize:
                return vaddr + (off - rawptr)
        return None

    def text(self):
        for name, vaddr, rawsize, rawptr in self.sections:
            if name == ".text":
                return vaddr, rawptr, rawsize
        return None


old = PE(OLD)
new = PE(NEW)
print("old base 0x%X  new base 0x%X" % (old.image_base, new.image_base))
print("new sections:", ", ".join(n for n, *_ in new.sections))
print()

SPAN = 224
ANCHOR = 8


def match(name, old_rva):
    o = old.off(old_rva)
    if o is None:
        print("%-16s old rva 0x%X not mapped" % (name, old_rva))
        return None
    old_bytes = old.data[o:o + SPAN]

    votes = {}
    for a_off in range(0, SPAN - ANCHOR + 1, ANCHOR):
        anchor = old_bytes[a_off:a_off + ANCHOR]
        if anchor.count(b"\x00") == ANCHOR:
            continue
        hits = 0
        pos = 0
        while hits < 400:
            i = new.data.find(anchor, pos)
            if i < 0:
                break
            start = i - a_off
            votes[start] = votes.get(start, 0) + 1
            pos = i + 1
            hits += 1

    scored = []
    for start, v in votes.items():
        if start < 0 or start + SPAN > len(new.data):
            continue
        chunk = new.data[start:start + SPAN]
        score = sum(1 for x, y in zip(old_bytes, chunk) if x == y)
        scored.append((score, v, start))
    scored.sort(reverse=True)

    print("%-16s old rva=0x%08X  候选 %d" % (name, old_rva, len(scored)))
    if not scored:
        print("    no candidate")
        return None
    for score, v, start in scored[:3]:
        rva = new.rva(start)
        print("    score %3d/%d (%.0f%%, votes=%d)  rva=0x%08X  off=0x%08X"
              % (score, SPAN, 100.0 * score / SPAN, v, rva if rva else 0, start))
    best = scored[0]
    if best[0] / SPAN < 0.75:
        print("    ** low confidence **")
        return None
    return new.rva(best[2])


results = {}
for name, rva in TARGETS.items():
    r = match(name, rva)
    if r:
        results[name] = r
    print()

print("=== 结果 ===")
for name, old_rva in TARGETS.items():
    new_rva = results.get(name)
    if new_rva:
        print("  %-16s 0x%08X -> 0x%08X   (delta %+d)" % (name, old_rva, new_rva, new_rva - old_rva))
    else:
        print("  %-16s 0x%08X -> ?" % (name, old_rva))

# also show what's now at the old RVAs, for the record
print()
print("=== 旧 RVA 在新 exe 上现在是什么 ===")
for name, old_rva in TARGETS.items():
    o = new.off(old_rva)
    if o is None:
        print("  %-16s 0x%08X  不在节内" % (name, old_rva))
    else:
        b = new.data[o:o + 8]
        print("  %-16s 0x%08X  %s" % (name, old_rva, " ".join("%02X" % x for x in b)))
