"""最小 PE 解析：RVA <-> 文件偏移、节表、ImageBase。"""
import struct


class Section:
    __slots__ = ("name", "rva", "vsize", "off", "rawsize")

    def __init__(self, name, rva, vsize, off, rawsize):
        self.name, self.rva, self.vsize = name, rva, vsize
        self.off, self.rawsize = off, rawsize

    @property
    def end(self):
        return self.rva + max(self.vsize, self.rawsize)

    def __repr__(self):
        return "<%s rva=0x%X size=0x%X>" % (self.name, self.rva, self.vsize)


class PE:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        e = struct.unpack_from("<I", d, 0x3C)[0]
        if d[e:e + 4] != b"PE\0\0":
            raise ValueError("not a PE file: %s" % path)
        nsec = struct.unpack_from("<H", d, e + 6)[0]
        optsz = struct.unpack_from("<H", d, e + 20)[0]
        self.image_base = struct.unpack_from("<Q", d, e + 24 + 0x18)[0]
        so = e + 24 + optsz
        self.sections = []
        for i in range(nsec):
            name = d[so + i * 40: so + i * 40 + 8].rstrip(b"\0").decode("latin-1")
            vsz, va, rsz, ro = struct.unpack_from("<IIII", d, so + i * 40 + 8)
            self.sections.append(Section(name, va, vsz, ro, rsz))

    def section_of(self, rva):
        for s in self.sections:
            if s.rva <= rva < s.end:
                return s
        return None

    def initialized(self, rva):
        """该 RVA 在文件里是否有真实字节（否则属 BSS，加载时清零）。"""
        s = self.section_of(rva)
        return s is not None and (rva - s.rva) < s.rawsize

    def off(self, rva):
        s = self.section_of(rva)
        if s is None:
            return -1
        delta = rva - s.rva
        return s.off + delta if delta < s.rawsize else -1

    def read(self, rva, n):
        o = self.off(rva)
        if o < 0 or o + n > len(self.data):
            return None
        return self.data[o:o + n]

    def u32(self, rva):
        b = self.read(rva, 4)
        return None if b is None else struct.unpack("<I", b)[0]

    def u64(self, rva):
        b = self.read(rva, 8)
        return None if b is None else struct.unpack("<Q", b)[0]

    def cstr(self, rva, n=256):
        o = self.off(rva)
        if o < 0:
            return None
        z = self.data.find(b"\0", o, o + n)
        return self.data[o:z if z >= 0 else o + n].decode("latin-1")
