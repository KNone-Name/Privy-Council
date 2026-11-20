"""对象内 vector 容器的快照与差分。

用法：
  python v3live\\diffscan.py save 0x<对象地址> [长度] out.json
  python v3live\\diffscan.py diff a.json b.json
"""
import json
import struct
import sys

sys.path.insert(0, r"D:\Game\ai\Victoria3-RE")
from v3live import proc as proc_mod


def snapshot(p, base, length):
    buf = p.read(base, length)
    out = []
    off = 0
    while off + 24 <= len(buf):
        ptr = struct.unpack_from("<Q", buf, off)[0]
        n = struct.unpack_from("<I", buf, off + 8)[0]
        cap = struct.unpack_from("<I", buf, off + 12)[0]
        if 0 < n < 200000 and n == cap and 0x10000000000 < ptr < 0x7F0000000000:
            raw = p.read(ptr, min(8 * n, 0x400))
            head = list(struct.unpack_from("<%dq" % (len(raw) // 8), raw, 0)) if raw else []
            out.append({"off": off, "n": n, "ptr": "0x%X" % ptr, "head": head[:8]})
            off += 24
            continue
        off += 8
    return out


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    p = proc_mod.attach("victoria3.exe")
    try:
        if cmd == "save":
            base = int(argv[2], 0)
            length = int(argv[3], 0) if len(argv) > 3 and not argv[3].endswith(
                ".json") else 0x5000
            path = argv[-1]
            snap = snapshot(p, base, length)
            json.dump(snap, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print("已保存 %d 个容器到 %s" % (len(snap), path))
            for s in snap:
                print("  +0x%-6X n=%-6d %s" % (s["off"], s["n"], s["head"][:4]))
        elif cmd == "diff":
            a = {(s["off"]): s for s in json.load(open(argv[2], encoding="utf-8"))}
            b = {(s["off"]): s for s in json.load(open(argv[3], encoding="utf-8"))}
            print("新增容器：")
            for off in sorted(set(b) - set(a)):
                s = b[off]
                print("  +0x%-6X n=%-6d ptr=%s head=%s"
                      % (off, s["n"], s["ptr"], s["head"][:6]))
            print("消失容器：")
            for off in sorted(set(a) - set(b)):
                print("  +0x%-6X (原 n=%d)" % (off, a[off]["n"]))
            print("元素数变化：")
            for off in sorted(set(a) & set(b)):
                if a[off]["n"] != b[off]["n"]:
                    print("  +0x%-6X n: %d -> %d  ptr %s -> %s"
                          % (off, a[off]["n"], b[off]["n"],
                             a[off]["ptr"], b[off]["ptr"]))
            print("内容变化（元素数不变但数据变了）：")
            for off in sorted(set(a) & set(b)):
                if a[off]["n"] == b[off]["n"] and a[off]["head"] != b[off]["head"]:
                    print("  +0x%-6X n=%d  %s -> %s"
                          % (off, a[off]["n"], a[off]["head"][:4], b[off]["head"][:4]))
        else:
            print(__doc__)
    finally:
        p.close()


if __name__ == "__main__":
    main(sys.argv)
