"""Find the table entry that references the add_building string."""
import struct

EXE = r"D:\Game\ai\V3\_archive\victoria3_new_100221048.exe"
TARGETS = {
    "add_building": 0x4958128,
    "build_building": 0x469A468,
    "create_building": 0x46A48C8,
    "queue_building": 0x48A785A,
}

d = open(EXE, "rb").read()
e = struct.unpack_from("<I", d, 0x3C)[0]
nsec = struct.unpack_from("<H", d, e + 6)[0]
optsz = struct.unpack_from("<H", d, e + 20)[0]
opt = e + 24
base = struct.unpack_from("<Q", d, opt + 24)[0]
secs = []
s = opt + optsz
for i in range(nsec):
    p = s + i * 40
    name = d[p:p + 8].rstrip(b"\0").decode()
    vs, va, rs, rp = struct.unpack_from("<IIII", d, p + 8)
    secs.append((name, va, vs, rs, rp))


def sec_of(rva):
    for n, va, vs, rs, rp in secs:
        if va <= rva < va + vs:
            return n
    return None


def off(rva):
    for n, va, vs, rs, rp in secs:
        if va <= rva < va + rs:
            return rp + (rva - va)
    return None


def rva_of_fileoff(o):
    for n, va, vs, rs, rp in secs:
        if rp <= o < rp + rs:
            return va + (o - rp)
    return None


for label, rva in TARGETS.items():
    print("=== %s (rva 0x%X, section %s) ===" % (label, rva, sec_of(rva)))
    # 32-bit RVA reference
    pat32 = struct.pack("<I", rva)
    pat64 = struct.pack("<Q", base + rva)
    hits = []
    i = 0
    while True:
        i = d.find(pat32, i)
        if i < 0:
            break
        r = rva_of_fileoff(i)
        if r is not None:
            hits.append((r, 4))
        i += 1
    i = 0
    while True:
        i = d.find(pat64, i)
        if i < 0:
            break
        r = rva_of_fileoff(i)
        if r is not None:
            hits.append((r, 8))
        i += 1
    print("   引用点 %d 处" % len(hits))
    for r, w in hits[:8]:
        # dump a window around the reference: the handler is usually adjacent
        o = off(r)
        lo = o - 0x18
        if lo < 0:
            continue
        vals = struct.unpack_from("<6Q", d, lo)
        rv = rva_of_fileoff(lo)
        print("     at rva 0x%X (%d bytes) 附近: %s"
              % (r, w, " ".join("0x%X" % v for v in vals)))
