#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小测试客户端：验证 server.py 的链路。"""
from __future__ import annotations

import argparse
import json
import socket


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--text", default="现在国库吃紧，下一步该建什么？")
    args = ap.parse_args()

    with socket.create_connection((args.host, args.port), timeout=10) as sock:
        f = sock.makefile("rwb")

        def send(obj):
            f.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            f.flush()

        send({"t": "hello", "v": 1})
        print("<-", f.readline().decode("utf-8").strip())
        send({"t": "submit", "id": "test-1", "text": args.text})
        print("<-", f.readline().decode("utf-8").strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
