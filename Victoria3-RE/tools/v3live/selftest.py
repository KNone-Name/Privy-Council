"""v3live 自测：不依赖游戏进程，验证 RTTI 解析与实例发现链路。

用法：python -m v3live.selftest
"""
import ctypes
import struct
import sys

from . import discover, proc as proc_mod
from .rtti import RTTI

EXE = r"E:\SteamLibrary\steamapps\common\Victoria 3\binaries\victoria3.exe"
_fails = 0


def check(cond, label, detail=""):
    global _fails
    print(("[ OK ] " if cond else "[FAIL] ") + label + " " + detail)
    if not cond:
        _fails += 1


def test_rtti():
    r = RTTI(EXE)
    s = r.summary()
    check(s["classes"] > 20000, "RTTI 类数量", "%d" % s["classes"])
    check(s["vtables"] > 10000, "vtable 数量", "%d" % s["vtables"])
    for name, min_vt in (("CState", 1), ("CCountry", 1), ("CPop", 1),
                         ("CBuilding", 1), ("CMarket", 1), ("CBuildingType", 1)):
        cls = r.classes.get(name)
        ok = cls is not None and len(cls.vtables) >= min_vt
        check(ok, "类 %s 有 vtable" % name,
              str([hex(v) for v in (cls.vtables if cls else [])]))
    # vtable[-1] 应指向 COL（RVA 落在镜像内且 signature==1）
    cls = r.classes["CState"]
    vt_va = r.pe.image_base + cls.vtables[0]
    col = r.pe.u64(vt_va - 8 - r.pe.image_base)
    ok = col is not None and r.pe.u32(col - r.pe.image_base) == 1
    check(ok, "vtable[-1] 指向合法 COL", "col=0x%X" % (col or 0))


def test_discovery():
    pid = ctypes.windll.kernel32.GetCurrentProcessId()
    FAKE_VT = 0x7FF6D2080770          # 假的 vtable 值，只在本进程里存在
    with proc_mod.Proc(pid) as p:
        buf = ctypes.create_string_buffer(0x1000)
        base = ctypes.addressof(buf)
        # 在 buf + 0x100 处伪造一个「对象」：首 8 字节 = vtable
        struct.pack_into("<Q", buf, 0x100, FAKE_VT)
        struct.pack_into("<Q", buf, 0x200, FAKE_VT)
        # 只扫缓冲区这 4KB：扫自身进程时，读缓冲里会残留被扫内容的副本，会造成误报
        region = (base, 0x1000, 0x04, 0x20000)
        hits = discover.scan_instances(p, {FAKE_VT: "CFakeObject"},
                                       regions=[region], progress=False)
        found = sorted(hits.get("CFakeObject", []))
        check(base + 0x100 in found and base + 0x200 in found,
              "按 vtable 值发现实例",
              "找到 %d 个" % len(found))
        # 差分
        a = p.read(base + 0x100, 0x200)
        struct.pack_into("<I", buf, 0x140, 12345)
        b = p.read(base + 0x100, 0x200)
        offs = discover.diff_offsets(a, b)
        check(0x40 in offs, "差分定位到变化的字段",
              str(["+0x%X" % o for o in offs]))
        # 非 vtable 值不应误报
        check(len(found) == 2, "无误报", "%d 个命中" % len(found))


def main():
    print("=== v3live 自测 ===")
    test_rtti()
    print()
    test_discovery()
    print("\n结果：%s" % ("全部通过" if _fails == 0 else "%d 项失败" % _fails))
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
