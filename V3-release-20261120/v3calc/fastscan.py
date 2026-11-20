# -*- coding: utf-8 -*-
"""更快的 vtable 实例扫描（并行版）。

``v3live.discover.scan_instances`` 是单线程、8 MB 分块。实测一次全量扫描
约 90 秒，瓶颈主要是 1) 逐块 ``ReadProcessMemory`` 的往返，2) numpy 比较
只用一个核。这里保持完全相同的语义，但：

  * 把内存切成 16 MB 任务，用线程池并行读+比较（ctypes 读和 numpy 比较
    都会释放 GIL，所以多线程有效）；
  * 结果按类合并，地址顺序不重要（调用方会排序/聚类）；
  * 进度按完成字节数打印，不依赖任务顺序。

不修改 ``v3live`` 源码：``install()`` 只替换模块级的
``discover.scan_instances``，``prewarm`` / ``_instances_of`` 都会自动用上。
可用环境变量 ``V3_SCAN_WORKERS`` 控制线程数（默认 min(4, CPU)）。
"""
from __future__ import annotations

import ctypes
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import numpy as np
    _HAS_NP = True
except Exception:                                    # pragma: no cover
    np = None
    _HAS_NP = False

try:
    from v3live.proc import k32
except Exception:                                    # pragma: no cover
    k32 = None

CHUNK = 16 << 20
_DEFAULT_WORKERS = max(1, min(4, (os.cpu_count() or 4)))
_TL = threading.local()


def _read_buffer(proc, addr: int, n: int):
    """线程本地复用 16MB 缓冲，避免每块都分配/复制；失败时退回 proc.read。"""
    if k32 is None or not getattr(proc, "h", None):
        return np.frombuffer(proc.read(addr, n), dtype="<u8")
    buf = getattr(_TL, "buf", None)
    if buf is None or len(buf) < n:
        buf = ctypes.create_string_buffer(max(n, CHUNK))
        _TL.buf = buf
    got = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(proc.h, ctypes.c_void_p(addr), buf, n,
                               ctypes.byref(got))
    if not ok or got.value != n:
        return np.frombuffer(proc.read(addr, n), dtype="<u8")
    return np.frombuffer(buf, dtype="<u8", count=n // 8)


def _chunks(regions, chunk=CHUNK):
    for base, size, _prot, _typ in regions:
        addr, end = base, base + size
        while addr < end:
            n = min(chunk, end - addr) & ~7
            if n <= 0:
                break
            yield addr, n
            addr += n


def _scan_chunk(proc, addr, n, keys, hi_values):
    arr = _read_buffer(proc, addr, n)
    if arr is None or len(arr) == 0:
        return {}
    hi = arr >> np.uint64(32)
    mask = None
    for h in hi_values:
        m = (hi == np.uint64(h))
        mask = m if mask is None else (mask | m)
    out = {}
    if mask is not None and mask.any():
        sub = arr[mask]
        idx = np.searchsorted(keys, sub)
        np.clip(idx, 0, len(keys) - 1, out=idx)
        matched = keys[idx] == sub
        if matched.any():
            positions = np.nonzero(mask)[0][matched]
            for pos, v in zip(positions.tolist(), sub[matched].tolist()):
                out.setdefault(v, []).append(addr + pos * 8)
    return out


def scan_instances(proc, value_to_name, regions=None, progress=True,
                   workers: int | None = None):
    if not _HAS_NP:
        # 没有 numpy 就退回 v3live 的原实现
        from v3live import discover
        return discover._scan_pure(
            proc, value_to_name,
            regions if regions is not None else proc.regions(private_only=True),
            progress)

    if not value_to_name:
        return {}
    regions = regions if regions is not None else proc.regions(private_only=True)
    keys = np.array(sorted(value_to_name), dtype=np.uint64)
    hi_values = {int(v) >> 32 for v in value_to_name}
    tasks = list(_chunks(regions))
    total = sum(n for _a, n in tasks)
    raw_workers = workers if workers is not None else os.environ.get("V3_SCAN_WORKERS")
    try:
        workers = int(raw_workers) if raw_workers else _DEFAULT_WORKERS
    except (TypeError, ValueError):
        workers = _DEFAULT_WORKERS
    workers = max(1, min(16, workers))

    merged: dict[int, list[int]] = {}
    done = 0
    next_mark = 0.0
    lock = threading.Lock()

    def run(task):
        addr, n = task
        return n, _scan_chunk(proc, addr, n, keys, hi_values)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, t) for t in tasks]
        for fut in as_completed(futures):
            n, part = fut.result()
            with lock:
                done += n
                for v, addrs in part.items():
                    merged.setdefault(v, []).extend(addrs)
                if progress:
                    pct = 100.0 * done / max(total, 1)
                    if pct >= next_mark:
                        print("  %5.1f%%  %d 类有实例" % (pct, len(merged)),
                              flush=True)
                        next_mark = pct + 10.0

    hits: dict[str, list[int]] = {}
    for v, addrs in merged.items():
        name = value_to_name.get(v)
        if name:
            hits.setdefault(name, []).extend(addrs)
    return hits


def install(workers: int | None = None) -> bool:
    """把 v3live.discover.scan_instances 换成并行版；返回是否安装成功。"""
    if not _HAS_NP:
        return False
    try:
        from v3live import discover
        if getattr(discover, "_V3CALC_FAST", False):
            return True

        def _wrapper(proc, value_to_name, regions=None, progress=True):
            return scan_instances(proc, value_to_name, regions, progress, workers)

        discover.scan_instances = _wrapper
        discover._V3CALC_FAST = True
        return True
    except Exception:
        return False
