"""在运行中的进程里发现「某个类的存活实例」。

原理：C++ 多态对象的第一个指针就是 vtable。已知所有类的 vtable 地址后，
扫堆内存里等于这些值的 8 字节槽，槽地址就是对象起始地址。

性能：用 numpy 向量化比较；先用高 32 位（镜像基址所在段）粗筛，
再做一次 searchsorted 精确命中，避免对几十亿个 qword 做 Python 循环。
"""
import struct

try:
    import numpy as np
    _HAS_NP = True
except Exception:                                    # 没 numpy 时退化为纯 Python
    np = None
    _HAS_NP = False

CHUNK = 8 << 20


def _scan_numpy(proc, value_to_name, regions, progress):
    keys = np.array(sorted(value_to_name), dtype=np.uint64)
    hi_set = {int(v) >> 32 for v in value_to_name}
    hits = {}
    total = sum(r[1] for r in regions)
    done = 0
    next_mark = 0.0
    for base, size, prot, typ in regions:
        addr, end = base, base + size
        while addr < end:
            n = min(CHUNK, end - addr) & ~7
            if n <= 0:
                break
            buf = proc.read(addr, n)
            arr = np.frombuffer(buf, dtype="<u8")
            hi = arr >> np.uint64(32)
            mask = np.zeros(arr.shape, dtype=bool)
            for h in hi_set:
                mask |= (hi == np.uint64(h))
            if mask.any():
                sub = arr[mask]
                idx = np.searchsorted(keys, sub)
                np.clip(idx, 0, len(keys) - 1, out=idx)
                matched = keys[idx] == sub
                if matched.any():
                    positions = np.nonzero(mask)[0][matched]
                    for pos, v in zip(positions.tolist(), sub[matched].tolist()):
                        hits.setdefault(value_to_name.get(v, "?"), []).append(
                            addr + pos * 8)
            done += n
            addr += n
            if progress:
                pct = 100.0 * done / max(total, 1)
                if pct >= next_mark:
                    print("  %5.1f%%  %d 类有实例" % (pct, len(hits)), flush=True)
                    next_mark = pct + 10.0
    return hits


def _scan_pure(proc, value_to_name, regions, progress):
    """无 numpy：按「高 4 字节」分组做 memmem 预筛，再查表。"""
    groups = {}
    for v, name in value_to_name.items():
        prefix = struct.pack("<I", (v >> 32) & 0xFFFFFFFF)
        groups.setdefault(prefix, {})[v] = name
    hits = {}
    for base, size, prot, typ in regions:
        addr, end = base, base + size
        while addr < end:
            n = min(CHUNK, end - addr)
            buf = proc.read(addr, n)
            for prefix, table in groups.items():
                start = 0
                while True:
                    j = buf.find(prefix, start)
                    if j < 0:
                        break
                    start = j + 1
                    pos = j - 4
                    if pos < 0 or pos + 8 > len(buf) or ((addr + pos) & 7):
                        continue
                    v = struct.unpack_from("<Q", buf, pos)[0]
                    name = table.get(v)
                    if name:
                        hits.setdefault(name, []).append(addr + pos)
            addr += n
    return hits


def scan_instances(proc, value_to_name, regions=None, progress=True):
    """返回 {类名: [实例地址...]}。value_to_name 是 {vtable_va: 类名}。"""
    regions = regions if regions is not None else proc.regions(private_only=True)
    if _HAS_NP:
        return _scan_numpy(proc, value_to_name, regions, progress)
    return _scan_pure(proc, value_to_name, regions, progress)


def counts(hits):
    return {k: len(v) for k, v in hits.items()}


def cluster_count(addrs, window=0x10):
    """多继承对象的多个 vtable 指针彼此相邻（+0/+8/+0x10…）。

    把同一类里间距 < window 的命中合并成一个对象，返回 (对象数, 对象基址列表)。
    对象基址取簇内最小地址（主 vtable 通常在对象 +0）。
    """
    if not addrs:
        return 0, []
    s = sorted(addrs)
    bases = [s[0]]
    last = s[0]
    for a in s[1:]:
        if a - last > window:
            bases.append(a)
        last = a
    return len(bases), bases


# ---------------------------------------------------------------- 数值反查
DTYPES = {"i8": ("<i1", 1), "u8": ("<u1", 1), "i16": ("<i2", 2), "u16": ("<u2", 2),
          "i32": ("<i4", 4), "u32": ("<u4", 4), "i64": ("<i8", 8), "u64": ("<u8", 8),
          "f32": ("<f4", 4), "f64": ("<f8", 8)}

# numpy dtype 和 struct 格式串是两套东西，别混用
STRUCT = {"i8": "<b", "u8": "<B", "i16": "<h", "u16": "<H",
          "i32": "<i", "u32": "<I", "i64": "<q", "u64": "<Q",
          "f32": "<f", "f64": "<d"}


def find_value(proc, kind, value, regions=None, progress=True):
    """在内存里找等于 value 的位置，返回地址列表。"""
    dt, sz = DTYPES[kind]
    specs = [(kind, _bits(kind, value), "<u%d" % sz, sz)]
    return find_multi(proc, specs, regions=regions, progress=progress)[kind]


def _bits(kind, value):
    """把值转成「按位比较」用的无符号整数：同 bit 即同值（含 -0.0/两个 0）。"""
    dt, sz = DTYPES[kind]
    if kind.startswith("f"):
        return struct.unpack("<I" if sz == 4 else "<Q",
                             struct.pack("<f" if sz == 4 else "<d", value))[0]
    return int(value) & ((1 << (sz * 8)) - 1)


def find_multi(proc, specs, regions=None, progress=True):
    """一趟扫描同时匹配多种类型。

    specs: [(名字, 目标位模式, numpy dtype, 字节数)]
    返回 {名字: [地址...]}
    """
    regions = regions if regions is not None else proc.regions()
    out = {name: [] for name, _, _, _ in specs}
    total = sum(r[1] for r in regions)
    done, next_mark = 0, 0.0
    for base, size, prot, typ in regions:
        addr, end = base, base + size
        while addr < end:
            n = min(CHUNK, end - addr)
            buf = proc.read(addr, n)
            for name, bits, ndt, sz in specs:
                cut = len(buf) // sz * sz
                if not cut:
                    continue
                if _HAS_NP:
                    arr = np.frombuffer(buf[:cut], dtype=ndt)
                    pos = np.nonzero(arr == np.array(bits, dtype=ndt))[0]
                    out[name].extend((addr + p * sz) for p in pos.tolist())
                else:
                    pat = struct.pack("<I" if sz == 4 else "<Q", bits)
                    s = 0
                    while True:
                        j = buf.find(pat, s)
                        if j < 0:
                            break
                        out[name].append(addr + j)
                        s = j + 1
            done += len(buf)
            addr += n
            if progress:
                pct = 100.0 * done / max(total, 1)
                if pct >= next_mark:
                    print("  %5.1f%%  %s" % (pct, " ".join(
                        "%s=%d" % (k, len(v)) for k, v in out.items())), flush=True)
                    next_mark = pct + 20.0
    return out


def find_range(proc, kind, lo, hi, regions=None, progress=True):
    """找落在 [lo, hi] 区间里的数值（用于界面上四舍五入显示的数字）。"""
    dt, sz = DTYPES[kind]
    return find_range_multi(proc, [(kind, dt, sz, lo, hi)],
                            regions=regions, progress=progress)[kind]


def find_range_multi(proc, specs, regions=None, progress=True):
    """一趟扫描同时做多种类型的区间匹配。

    specs: [(名字, numpy dtype, 字节数, lo, hi)] -> {名字: [地址...]}
    """
    regions = regions if regions is not None else proc.regions()
    out = {name: [] for name, _, _, _, _ in specs}
    total = sum(r[1] for r in regions)
    done, next_mark = 0, 0.0
    for base, size, prot, typ in regions:
        addr, end = base, base + size
        while addr < end:
            n = min(CHUNK, end - addr)
            buf = proc.read(addr, n)
            for name, dt, sz, lo, hi in specs:
                cut = len(buf) // sz * sz
                if not cut:
                    continue
                if _HAS_NP:
                    arr = np.frombuffer(buf[:cut], dtype=dt)
                    pos = np.nonzero((arr >= lo) & (arr <= hi))[0]
                    out[name].extend((addr + p * sz) for p in pos.tolist())
                else:
                    for i in range(0, cut, sz):
                        v = struct.unpack_from(STRUCT[kind], buf, i)[0]
                        if lo <= v <= hi:
                            out[name].append(addr + i)
            done += len(buf)
            addr += n
            if progress:
                pct = 100.0 * done / max(total, 1)
                if pct >= next_mark:
                    print("  %5.1f%%  %s" % (pct, " ".join(
                        "%s=%d" % (k, len(v)) for k, v in out.items())), flush=True)
                    next_mark = pct + 20.0
    return out


def hunt_in_instances(proc, insts, kinds, lo, hi, length=0x800):
    """在一个类的所有实例里，找值落在 [lo,hi] 的偏移。

    返回 {(类型, 偏移): {"count": 命中实例数, "addr": 样例实例, "value": 样例值}}
    """
    res = {}
    for a in insts:
        buf = proc.read(a, length)
        for kind in kinds:
            dt, sz = DTYPES[kind]
            cut = len(buf) // sz * sz
            if not cut:
                continue
            if _HAS_NP:
                arr = np.frombuffer(buf[:cut], dtype=dt)
                pos = np.nonzero((arr >= lo) & (arr <= hi))[0]
                for p in pos.tolist():
                    key = (kind, p * sz)
                    r = res.get(key)
                    if r is None:
                        res[key] = {"count": 1, "addr": a, "value": float(arr[p])}
                    else:
                        r["count"] += 1
            else:
                for i in range(0, cut, sz):
                    v = struct.unpack_from(STRUCT[kind], buf, i)[0]
                    if lo <= v <= hi:
                        key = (kind, i)
                        r = res.get(key)
                        if r is None:
                            res[key] = {"count": 1, "addr": a, "value": float(v)}
                        else:
                            r["count"] += 1
    return res


def locate_objects(proc, hit_addrs, vt_map, back=0x40000, max_hits=20000):
    """把"数值地址"归到它所属的对象：向前找最近的一个 vtable 指针。

    返回 [(类名, 对象内偏移, 对象地址)]。注意这是启发式——对象基址只要求
    "前面最近的 qword 恰好是已知 vtable"，所以聚合统计比单条更有意义。
    """
    if _HAS_NP:
        keys = np.array(sorted(vt_map), dtype=np.uint64)
        hi_set = {k >> 32 for k in vt_map}
    res = []
    for hit in hit_addrs[:max_hits]:
        start = max(0, hit - back) & ~7
        buf = proc.read(start, hit - start + 8)
        nq = len(buf) // 8
        if nq <= 0:
            continue
        found = None
        if _HAS_NP:
            arr = np.frombuffer(buf[:nq * 8], dtype="<u8")
            hi = arr >> np.uint64(32)
            mask = np.zeros(arr.shape, dtype=bool)
            for h in hi_set:
                mask |= (hi == np.uint64(h))
            if mask.any():
                sub = arr[mask]
                idx = np.searchsorted(keys, sub)
                np.clip(idx, 0, len(keys) - 1, out=idx)
                m = keys[idx] == sub
                if m.any():
                    positions = np.nonzero(mask)[0][m]
                    if len(positions):
                        found = int(positions[-1])          # 最近的
                        obj = start + found * 8
                        cls = vt_map[int(arr[found])]
                        res.append((cls, hit - obj, obj))
                        continue
        if found is None:
            for i in range(nq - 1, -1, -1):
                v = struct.unpack_from("<Q", buf, i * 8)[0]
                cls = vt_map.get(v)
                if cls:
                    res.append((cls, hit - start - i * 8, start + i * 8))
                    break
    return res


def aggregate_located(located):
    """{(类名, 偏移): 次数}，按次数降序。"""
    agg = {}
    for cls, off, _ in located:
        agg[(cls, off)] = agg.get((cls, off), 0) + 1
    return sorted(agg.items(), key=lambda kv: -kv[1])


def dump_fields(proc, obj, length=0x200):
    return proc.read(obj, length)


def diff_offsets(a, b, length=None):
    """两个字节串里不同的偏移（按 8 字节分组，便于看字段）。"""
    n = min(len(a), len(b)) if length is None else length
    return [i for i in range(0, n, 8) if a[i:i + 8] != b[i:i + 8]]
