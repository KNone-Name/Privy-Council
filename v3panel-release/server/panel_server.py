#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3AI 御前 AI 面板：独立外部服务（不依赖 v3ai 包）。

游戏内 DLL 通过 127.0.0.1:8766 的 JSON-lines TCP 连接发送玩家输入，
本服务调用 OpenAI 兼容的 /v1/chat/completions，再把回复送回。

协议（每行一个 JSON）：
  客户端 -> 服务: {"t":"submit","id":"1","text":"..."}
  服务 -> 客户端: {"t":"reply","id":"1","text":"..."}
  客户端 -> 服务: {"t":"ping"}
  服务 -> 客户端: {"t":"pong"}

用法：
  python panel_server.py --echo
  python panel_server.py
  python panel_server.py --base-url http://127.0.0.1:18200/v1 --model kvmem-polystrata-qwen35
"""
from __future__ import annotations

import argparse
import json
import os
import socketserver
import sys
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "panel_server.json")
SNAP_PATH = os.path.join(HERE, "snap.json")

SYSTEM_PROMPT = (
    "你是《维多利亚3》里的“御前 AI”，替玩家分析国势并给出可执行建议。"
    "回答用简体中文，控制在 6 行以内；先给结论，再给 1-3 条具体动作"
    "（州/建筑/生产方式/法律）。不确定的数据不要编造。"
)


def log(msg: str) -> None:
    line = time.strftime("[%H:%M:%S] ") + msg
    print(line, flush=True)
    try:
        with open(os.path.join(HERE, "panel_server.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    cfg = {
        "base_url": "http://127.0.0.1:18200/v1",
        "model": "kvmem-polystrata-qwen35",
        "api_key": "",
        "temperature": 0.7,
        "max_tokens": 2048,
        "timeout_s": 300,
    }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def load_ctx() -> dict:
    """读取同目录 snap.json（可选），压缩成给 LLM 的局势上下文。"""
    try:
        mtime = os.path.getmtime(SNAP_PATH)
        with open(SNAP_PATH, "r", encoding="utf-8") as fh:
            snap = json.load(fh)
    except Exception:
        return {}
    age = max(0, int(time.time() - mtime))
    game = snap.get("game") or {}
    country = snap.get("country") or {}
    ctx = {
        "snapshot_age_s": age,
        "captured_at": game.get("captured_at"),
        "save_date": game.get("save_date"),
        "game_version": game.get("game_version"),
        "country": {
            k: country.get(k)
            for k in ("tag", "name", "treasury", "credit_limit",
                      "loan_principal", "population", "religion")
            if k in country
        },
        "tax": snap.get("tax"),
    }
    laws = snap.get("laws")
    if isinstance(laws, list):
        ctx["laws"] = [
            (item.get("key") or item.get("name") or item.get("id"))
            if isinstance(item, dict) else item
            for item in laws[:40]
        ]
    elif laws:
        ctx["laws"] = str(laws)[:2000]
    if age > 600:
        ctx["warning"] = "快照已过期，数据可能不是当前局势"
    return ctx


class Backend:
    def __init__(self, cfg: dict, echo: bool) -> None:
        self.cfg = cfg
        self.echo = echo
        self.name = "echo" if echo else "%s @ %s" % (cfg["model"], cfg["base_url"])

    def reply(self, text: str, ctx: dict | None = None) -> str:
        if self.echo:
            return "[echo] " + text
        if ctx is None:
            ctx = load_ctx()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if ctx:
            messages.append({
                "role": "system",
                "content": "当前游戏局势（JSON）：" + json.dumps(ctx, ensure_ascii=False),
            })
        messages.append({"role": "user", "content": text})
        payload = {
            "model": self.cfg["model"],
            "messages": messages,
            "temperature": self.cfg.get("temperature", 0.7),
            "max_tokens": self.cfg.get("max_tokens", 2048),
        }
        url = self.cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        key = self.cfg.get("api_key") or ""
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.cfg.get("timeout_s", 300)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data["choices"][0]["message"]["content"]


class Handler(socketserver.StreamRequestHandler):
    def send(self, obj: dict) -> None:
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    def handle(self) -> None:
        peer = "%s:%s" % self.client_address
        log("client connected " + peer)
        try:
            for raw in self.rfile:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    self.send({"t": "error", "text": "invalid json"})
                    continue
                kind = msg.get("t")
                if kind == "ping":
                    self.send({"t": "pong"})
                elif kind == "hello":
                    self.send({"t": "welcome", "v": 1, "backend": self.server.backend.name})
                elif kind == "submit":
                    mid = msg.get("id")
                    text = msg.get("text") or ""
                    if not text.strip():
                        self.send({"t": "error", "id": mid, "text": "empty input"})
                        continue
                    log("submit id=%s chars=%d" % (mid, len(text)))
                    try:
                        reply = self.server.backend.reply(text, msg.get("ctx"))
                    except Exception as exc:  # noqa: BLE001
                        log("backend error: %s" % exc)
                        log(traceback.format_exc())
                        self.send({"t": "error", "id": mid, "text": "LLM 调用失败：%s" % exc})
                        continue
                    log("reply id=%s chars=%d" % (mid, len(reply)))
                    self.send({"t": "reply", "id": mid, "text": reply})
                else:
                    self.send({"t": "error", "text": "unknown type %r" % kind})
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            log("client disconnected " + peer)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, backend) -> None:
        super().__init__(addr, Handler)
        self.backend = backend


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--echo", action="store_true")
    ap.add_argument("--base-url")
    ap.add_argument("--model")
    ap.add_argument("--api-key")
    ap.add_argument("--snap")
    args = ap.parse_args()

    cfg = load_config()
    if args.base_url:
        cfg["base_url"] = args.base_url
    if args.model:
        cfg["model"] = args.model
    if args.api_key is not None:
        cfg["api_key"] = args.api_key
    if args.snap:
        global SNAP_PATH
        SNAP_PATH = args.snap

    backend = Backend(cfg, args.echo)
    srv = Server((args.host, args.port), backend)
    log("V3AI panel service on %s:%d backend=%s" % (args.host, args.port, backend.name))
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        log("stopping")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
