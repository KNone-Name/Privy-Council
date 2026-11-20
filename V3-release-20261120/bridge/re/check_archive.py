import struct

FILES = [
    (r"D:\Game\ai\V3\_archive\victoria3_old_99988600.exe", 99988600, 0x017B6FB764315747, 0x6AEA0F6D),
    (r"D:\Game\ai\V3\_archive\victoria3_new_100221048.exe", 100221048, 0xF8A08D788306BC2F, 0x6AF3094F),
]

for path, exp_size, exp_hash, exp_tds in FILES:
    data = open(path, "rb").read()
    e = struct.unpack_from("<I", data, 0x3C)[0]
    tds = struct.unpack_from("<I", data, e + 8)[0]
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    name = path.split("\\")[-1]
    print("%s" % name)
    print("   size %d  %s" % (len(data), "OK" if len(data) == exp_size else "MISMATCH"))
    print("   tds  0x%08X  %s" % (tds, "OK" if tds == exp_tds else "MISMATCH"))
    print("   hash 0x%016X  %s" % (h, "OK" if h == exp_hash else "MISMATCH"))
    print()
