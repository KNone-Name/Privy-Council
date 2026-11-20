# -*- coding: utf-8 -*-
"""最小 MCP server（stdio，JSON-RPC 2.0），把 v3ai 工具暴露给外部客户端。

不依赖 ``mcp`` 包；实现的是 MCP 的工具子集：
  initialize / notifications/initialized / ping / tools/list / tools/call。

用法：
  python -m v3ai mcp
然后在支持 MCP 的客户端里把命令配成上面的命令即可。
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable

from .config import Config
from .runtime import Runtime
from .tools import ToolRegistry, build_registry

PROTOCOL_VERSION = "2024-11-05"


class MCPServer:
    def __init__(self, registry: ToolRegistry | None = None,
                 runtime_factory: Callable[[], Runtime] | None = None,
                 name: str = "v3ai", version: str = "0.1.0"):
        self.registry = registry or build_registry()
        self._runtime_factory = runtime_factory
        self._runtime: Runtime | None = None
        self.name = name
        self.version = version

    # -- runtime ------------------------------------------------------
    def runtime(self) -> Runtime:
        if self._runtime is None:
            if self._runtime_factory is not None:
                self._runtime = self._runtime_factory()
            else:
                self._runtime = Runtime(Config.load())
        return self._runtime

    # -- JSON-RPC -----------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        if not isinstance(msg, dict):
            return self._error(None, -32600, "Invalid Request")
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if mid is None and method and method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                return self._ok(mid, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": self.version},
                })
            if method == "ping":
                return self._ok(mid, {})
            if method == "tools/list":
                return self._ok(mid, {"tools": self.registry.mcp_tools()})
            if method == "tools/call":
                name = str(params.get("name") or "")
                args = params.get("arguments") or {}
                rt = self.runtime()
                out = self.registry.call(rt, name, args)
                text = json.dumps(out, ensure_ascii=False, default=str)
                return self._ok(mid, {
                    "content": [{"type": "text", "text": text}],
                    "isError": not bool(out.get("ok", True)),
                })
            if method in ("resources/list", "prompts/list"):
                key = "resources" if method.startswith("resources") else "prompts"
                return self._ok(mid, {key: []})
            if method == "shutdown":
                return self._ok(mid, {})
            return self._error(mid, -32601, "Method not found: %s" % method)
        except Exception as exc:
            return self._error(mid, -32603, "%s: %s" % (type(exc).__name__, exc))

    @staticmethod
    def _ok(mid, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _error(mid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": code, "message": message}}

    # -- stdio --------------------------------------------------------
    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                resp = self._error(None, -32700, "Parse error")
            else:
                resp = self.handle(msg)
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                stdout.flush()


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    MCPServer().serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
