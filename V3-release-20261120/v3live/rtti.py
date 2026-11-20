"""MSVC RTTI 解析：从 PE 里还原「类名 -> vtable」映射。

用到的结构（x64）：

    TypeDescriptor      +0x00 type_info_vftable, +0x08 spare, +0x10 char name[]
                        name 形如 ".?AVCState@@"
    CompleteObjectLocator
                        +0x00 signature(=1), +0x04 offset, +0x08 cdOffset,
                        +0x0C pTypeDescriptor(RVA), +0x10 pClassDescriptor(RVA)
    ClassHierarchyDescriptor
                        +0x00 signature(=0), +0x04 attributes,
                        +0x08 numBaseClasses, +0x0C pBaseClassArray(RVA)
    BaseClassArray       u32[numBaseClasses]，每项是 BaseClassDescriptor 的 RVA
    BaseClassDescriptor  +0x00 pTypeDescriptor(RVA), +0x04 numContainedBases, ...

vtable 的[-1]槽指向 COL（RVA）。所以：扫到某处 qword 指向合法 COL，
该 qword 后面 8 字节就是 vtable 起点。
"""
import re
import struct

from .pe import PE


class ClassInfo:
    __slots__ = ("name", "td", "vtables", "bases")

    def __init__(self, name, td):
        self.name, self.td = name, td
        self.vtables = []
        self.bases = []

    def __repr__(self):
        return "<%s vt=%d bases=%d>" % (self.name, len(self.vtables), len(self.bases))


def _decode_name(raw):
    """.?AVCState@@ -> CState；. ?AVNs::CFoo@@ -> Ns::CFoo"""
    if not raw or not raw.startswith(".?A"):
        return None
    s = raw[4:] if raw[:4] == ".?AV" else raw[4:]
    s = s.split("@@")[0]
    s = s.replace("?$", "").replace("?", "")
    s = s.replace("@", "::")
    return s or None


class RTTI:
    def __init__(self, path):
        self.pe = PE(path)
        self.classes = {}        # name -> ClassInfo
        self.by_vtable = {}      # vtable VA -> name
        self.td_rva = {}         # td rva -> name
        self._parse_tds()
        self._parse_locators()

    # ---------------------------------------------------------------- TD
    def _parse_tds(self):
        pe = self.pe
        d = pe.data
        for sec in pe.sections:
            if not sec.rawsize:
                continue
            blob = d[sec.off:sec.off + sec.rawsize]
            for m in re.finditer(rb"\.\?A[VU]", blob):
                name_rva = sec.rva + m.start()
                td_rva = name_rva - 0x10
                if td_rva < 0:
                    continue
                raw = pe.cstr(name_rva, 200)
                name = _decode_name(raw)
                if not name:
                    continue
                # TD 头部的 type_info vftable 必须指向镜像内部
                vt = pe.u64(td_rva)
                if vt is None or not (pe.image_base <= vt < pe.image_base + 0x8000000):
                    continue
                self.td_rva[td_rva] = name
                if name not in self.classes:
                    self.classes[name] = ClassInfo(name, td_rva)

    # ----------------------------------------------------------- COL
    def _parse_locators(self):
        pe = self.pe
        d = pe.data
        ib = pe.image_base
        img_end = ib + 0x8000000
        for sec in pe.sections:
            if sec.name not in (".rdata", ".data") or not sec.rawsize:
                continue
            blob = d[sec.off:sec.off + sec.rawsize]
            for i in range(0, len(blob) - 8, 8):
                q = struct.unpack_from("<Q", blob, i)[0]
                if not (ib <= q < img_end):
                    continue
                col_rva = q - ib
                if pe.u32(col_rva) != 1:            # signature
                    continue
                td = pe.u32(col_rva + 0x0C)
                if td is None or td not in self.td_rva:
                    continue
                name = self.td_rva[td]
                vt_rva = sec.rva + i + 8
                cls = self.classes[name]
                if vt_rva not in cls.vtables:
                    cls.vtables.append(vt_rva)
                self.by_vtable[ib + vt_rva] = name
                if not cls.bases:
                    cls.bases = self._bases(col_rva + 0x10)

    def _bases(self, chd_rva):
        pe = self.pe
        if pe.u32(chd_rva) != 0:
            return []
        n = pe.u32(chd_rva + 0x08)
        arr = pe.u32(chd_rva + 0x0C)
        if not n or n > 64 or arr is None:
            return []
        out = []
        for i in range(n):
            bcd = pe.u32(arr + 4 * i)
            if bcd is None:
                break
            td = pe.u32(bcd)
            nm = self.td_rva.get(td)
            if nm:
                out.append(nm)
        return out

    # ---------------------------------------------------------- query
    def vtable_set(self):
        return set(self.by_vtable)

    def find(self, pattern):
        """按子串找类名，返回 [(name, vtables)]。"""
        pat = pattern.lower()
        return [(n, c.vtables) for n, c in sorted(self.classes.items())
                if pat in n.lower()]

    def summary(self, top=20):
        with_vt = [c for c in self.classes.values() if c.vtables]
        return {
            "classes": len(self.classes),
            "with_vtable": len(with_vt),
            "vtables": len(self.by_vtable),
        }
