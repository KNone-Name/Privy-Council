# -*- coding: utf-8 -*-
"""v3ai 命令行入口。

常用：
  python -m v3ai doctor
  python -m v3ai overview --snapshot snap.json
  python -m v3ai run --snapshot snap.json --mode dry_run --once
  python -m v3ai plan --snapshot snap.json
  python -m v3ai review --file actions.json --snapshot snap.json
  python -m v3ai config use deepseek
  python -m v3ai mcp
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from .agent import AgentLoop, CycleResult
from .config import Config
from .executor import make_executor
from .llm import MockLLM, parse_tool_call, tool_call
from .review import Command
from .runtime import Runtime
from .strategy import Strategy
from .tools import build_registry
from .util import now_iso, read_json, read_jsonl, safe_int


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _p(msg: str = "") -> None:
    print(msg)


def _err(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


def _json(obj: Any, indent: int = 2) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=indent, default=str))


def _print_cycle(res: CycleResult) -> None:
    _p("")
    _p("[回合 %d] %s / %s  %.2fs" % (res.cycle, res.provider or "?", res.model or "?", res.latency_s))
    if res.error:
        _p("  错误：%s" % res.error)
    _p("  摘要：%s" % (res.summary or "（空）"))
    if res.rationale:
        _p("  理由：%s" % res.rationale[:800])
    if res.proposals:
        ok = sum(1 for x in res.proposals if x.get("approved"))
        _p("  提议：%d 条（通过 %d / 拒绝 %d）" % (len(res.proposals), ok, len(res.proposals) - ok))
        for x in res.proposals[:10]:
            mark = "✓" if x.get("approved") else "✗"
            _p("    %s %s %s" % (mark, x.get("code"), (x.get("message") or "")[:180]))
    else:
        _p("  提议：0 条")
    if res.executions:
        _p("  执行：")
        for x in res.executions[:10]:
            _p("    %s %s %s" % ("✓" if x.get("ok") else "✗",
                                x.get("status"), (x.get("message") or "")[:180]))
    if res.llm_usage:
        _p("  tokens：%s" % ", ".join("%s=%s" % (k, v) for k, v in res.llm_usage.items()))


# ---------------------------------------------------------------------------
# 运行时
# ---------------------------------------------------------------------------
def _make_runtime(args) -> Runtime:
    cfg = Config.load(getattr(args, "config", None))
    if getattr(args, "provider", None):
        os.environ["V3AI_PROVIDER"] = args.provider
    source = None
    if getattr(args, "live", False):
        source = "live"
    elif getattr(args, "snapshot", None):
        source = "file"
    return Runtime(cfg, snapshot_path=getattr(args, "snapshot", None),
                   source=source, mode=getattr(args, "mode", None))


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    cfg = Config.load(args.config)
    _p("v3ai doctor")
    _p("  config      : %s" % cfg.file)
    _p("  provider    : %s" % cfg.provider_name)
    prof = cfg.provider()
    _p("  base_url    : %s" % prof.get("base_url"))
    _p("  model       : %s" % prof.get("model"))
    _p("  api_key     : %s" % ("已配置" if prof.get("api_key") else "空"))
    _p("  snapshot    : %s" % cfg.path("snapshot"))
    _p("  state_meta  : %s" % cfg.path("state_meta"))
    _p("  bridge dir  : %s" % (cfg.executor.get("bridge") or {}).get("dir"))
    _p("  executor    : %s" % (getattr(args, "mode", None) or cfg.executor.get("mode") or "dry_run"))
    ok = True
    try:
        rt = _make_runtime(args)
        _p("  game_dir    : %s" % rt.game_dir)
        _p("  gamedata    : %d 商品 / %d PM / %d 建筑 / %d 科技 / %d 法律"
           % (len(rt.gd.goods), len(rt.gd.pms), len(rt.gd.buildings),
              len(rt.gd.techs), len(rt.gd.laws)))
        _p("  state_meta  : %d 州（%s）" % (len(rt.state_meta),
                                          rt._state_meta_info.get("save_date") or "?"))
        _p("  snapshot    : source=%s captured=%s states=%d buildings=%d"
           % (rt.snap.game.source, rt.snap.game.captured_at,
              len(rt.snap.states), len(rt.snap.buildings)))
        _p("  building_kb : %d 个建筑" % len(rt.buildings_kb))
        caps = rt.executor.capabilities()
        _p("  executor    : %s" % json.dumps(caps, ensure_ascii=False)[:400])
        if caps.get("mode") == "bridge":
            exe = os.path.join(os.path.dirname(rt.game_dir), "binaries", "victoria3.exe")
            expected = int((cfg.executor.get("bridge") or {}).get("expected_exe_size")
                           or 100221048)
            try:
                size = os.path.getsize(exe)
                if expected and size != expected:
                    _p("  [WARN] 当前 victoria3.exe = %d 字节；配置期望 %d 字节，"
                       "bridge 指纹可能不匹配" % (size, expected))
                else:
                    _p("  [PASS] victoria3.exe = %d 字节，与 bridge 0.2 指纹一致" % size)
            except OSError:
                _p("  [WARN] 找不到 victoria3.exe：%s" % exe)
            if caps.get("fingerprint") == "UNKNOWN":
                _p("  [WARN] bridge 日志显示指纹不匹配（当前 exe 版本与 bridge 不一致）")
            if caps.get("world") == "1":
                _p("  [PASS] bridge world=1 player=%s capabilities=%s"
                   % (caps.get("player"), caps.get("capabilities")))
            elif caps.get("world") == "0":
                _p("  [WARN] 游戏在主菜单/加载中")
        rt.close()
    except Exception as exc:
        ok = False
        _p("  [FAIL] %s: %s" % (type(exc).__name__, exc))
    if args.ping:
        try:
            from .llm import LLMClient
            okp, msg = LLMClient(prof).ping()
            _p("  [%s] LLM ping: %s" % ("PASS" if okp else "WARN", msg))
        except Exception as exc:
            _p("  [WARN] LLM ping: %s" % exc)
    _p("")
    _p("结论：%s" % ("基本可用" if ok else "有阻塞，先看上面的 FAIL"))
    return 0 if ok else 1


def cmd_overview(args) -> int:
    rt = _make_runtime(args)
    try:
        _json(rt.overview(), indent=2 if not args.compact else None)
    finally:
        rt.close()
    return 0


def _mock_llm(script_path: str | None) -> MockLLM:
    """把 JSON 脚本变成 MockLLM；用于离线跑通/回放，不联网。"""
    if not script_path:
        return MockLLM([
            tool_call("get_overview", {}),
            tool_call("build_candidates", {"top": 5}),
            tool_call("finish_cycle", {"summary": "离线冒烟：只看概况与候选，不提交动作",
                                       "rationale": "mock 脚本"}),
        ])
    raw = read_json(script_path, default=None)
    if raw is None:
        raise SystemExit("读不到 mock 脚本：%s" % script_path)
    if isinstance(raw, dict):
        raw = raw.get("responses") or raw.get("steps") or []
    if not isinstance(raw, list):
        raise SystemExit("mock 脚本必须是列表，或 {\"responses\": [...]}")
    from .llm import LLMResponse
    responses = []
    for step in raw:
        if not isinstance(step, dict):
            continue
        if step.get("tool"):
            responses.append(tool_call(str(step["tool"]), step.get("arguments") or {},
                                       content=str(step.get("content") or "")))
        elif step.get("tool_calls"):
            responses.append(LLMResponse(
                content=str(step.get("content") or ""),
                tool_calls=[parse_tool_call(x, i) for i, x in enumerate(step["tool_calls"])],
                model="mock"))
        else:
            responses.append(LLMResponse(content=str(step.get("content") or ""), model="mock"))
    if not any(r.tool_calls and r.tool_calls[0].name == "finish_cycle" for r in responses):
        responses.append(tool_call("finish_cycle", {"summary": "mock 脚本结束",
                                                    "rationale": "scripted"}))
    return MockLLM(responses)


def cmd_run(args) -> int:
    rt = _make_runtime(args)
    try:
        llm = _mock_llm(args.mock_script) if (args.mock or args.mock_script) else None
        agent = AgentLoop(rt, llm=llm)
        cycles = args.cycles
        if args.once:
            cycles = 1
        if cycles < 0:
            cycles = 0
        n = 0
        while True:
            n += 1
            if args.confirm:
                res = agent.run_cycle(plan_only=True)
                _print_cycle(res)
                if rt.pending:
                    _p("")
                    _p("待执行：")
                    for c in rt.pending:
                        _p("  %s  %s" % (c.id, c.label()))
                    try:
                        ans = input("执行这批动作？[y/N] ").strip().lower()
                    except EOFError:
                        ans = "n"
                    if ans in ("y", "yes"):
                        out = agent.commit_pending()
                        _p("  执行结果：%s" % json.dumps(out, ensure_ascii=False)[:1200])
            else:
                res = agent.run_cycle(plan_only=args.plan_only)
                _print_cycle(res)
            if cycles and n >= cycles:
                break
            if args.interval > 0:
                time.sleep(args.interval)
        return 0
    finally:
        rt.close()


def cmd_plan(args) -> int:
    args.plan_only = True
    args.confirm = False
    args.once = True
    for k, v in (("cycles", 1), ("interval", 0.0), ("mock", False),
                 ("mock_script", None)):
        if not hasattr(args, k):
            setattr(args, k, v)
    return cmd_run(args)


def _load_actions(path: str) -> list[Command]:
    d = read_json(path, default=None)
    if d is None:
        raise SystemExit("读不到动作文件：%s" % path)
    raw = d.get("actions") if isinstance(d, dict) else d
    if not isinstance(raw, list):
        raise SystemExit("动作文件必须是列表，或 {\"actions\": [...]}")
    out = []
    for i, x in enumerate(raw):
        if not isinstance(x, dict):
            raise SystemExit("actions[%d] 不是对象" % i)
        out.append(Command.from_dict(x))
    return out


def cmd_review(args) -> int:
    cmds = _load_actions(args.file)
    rt = _make_runtime(args)
    try:
        results = []
        for c in cmds:
            r = rt.propose(c)
            results.append({"command": c.to_dict(), "review": r.to_dict()})
        _json(results)
        if args.apply:
            approved = [c for c in rt.pending if c.status == "approved"]
            if not approved:
                _p("没有通过审查的动作")
                return 1
            if not args.yes:
                _p("将执行：")
                for c in approved:
                    _p("  %s" % c.label())
                _p("加 --yes 才会真正执行")
                return 0
            out = rt.execute_pending()
            _json([x.to_dict() for x in out])
        return 0
    finally:
        rt.close()


def cmd_config(args) -> int:
    cfg = Config.load(args.config)
    if args.action == "show":
        d = json.loads(json.dumps(cfg.data))
        for p in (d.get("providers") or {}).values():
            if p.get("api_key"):
                p["api_key"] = "***"
        _json(d)
        return 0
    if args.action == "providers":
        for name, p in (cfg.data.get("providers") or {}).items():
            mark = "*" if name == cfg.data.get("active_provider") else " "
            _p("%s %-10s %-36s %s" % (mark, name, p.get("base_url", ""), p.get("model", "")))
        return 0
    if args.action == "use":
        name = (args.rest or [None])[0]
        if not name:
            return _err("config use 需要名字")
        cfg.use_provider(name)
        _p("active_provider = %s" % name)
        return 0
    if args.action == "set":
        if len(args.rest or []) < 2:
            return _err("config set 需要 key value")
        key, raw_value = args.rest[0], args.rest[1]
        try:
            val = json.loads(raw_value)
        except ValueError:
            val = raw_value
        cfg.set(key, val)
        cfg.save()
        _p("%s = %s" % (key, json.dumps(val, ensure_ascii=False)))
        return 0
    if args.action == "ping":
        name = (args.rest or [None])[0]
        from .llm import LLMClient
        ok, msg = LLMClient(cfg.provider(name)).ping()
        _p(("PASS " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    return _err("未知 config 动作：%s" % args.action)


def cmd_strategy(args) -> int:
    cfg = Config.load(args.config)
    if args.action == "show":
        rt = _make_runtime(args)
        try:
            _json({"active": rt.strategy.summary(),
                   "history": rt.strategy_history(5)})
        finally:
            rt.close()
        return 0
    if args.action == "reset":
        rt = _make_runtime(args)
        try:
            st = rt.strategy_store.reset()
            _p("已重置为默认策略：%s" % st.name)
        finally:
            rt.close()
        return 0
    if args.action == "set":
        if not args.file:
            return _err("strategy set 需要 JSON 文件")
        raw = read_json(args.file, default=None)
        if not isinstance(raw, dict):
            return _err("策略文件必须是 JSON 对象")
        rt = _make_runtime(args)
        try:
            st = rt.set_strategy(Strategy.from_dict(raw), source="player")
            _json(st.summary())
        finally:
            rt.close()
        return 0
    return _err("未知 strategy 动作：%s" % args.action)


def cmd_tools(args) -> int:
    reg = build_registry()
    if args.json:
        _json(reg.mcp_tools())
        return 0
    for name in reg.names():
        t = reg.get(name)
        _p("%-22s %s" % (name, t.description if t else ""))
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import MCPServer
    MCPServer().serve()
    return 0


def cmd_web(args) -> int:
    from .web_server import serve
    cfg = Config.load(getattr(args, "config", None))
    if getattr(args, "provider", None):
        os.environ["V3AI_PROVIDER"] = args.provider
    snapshot = getattr(args, "snapshot", None) or cfg.path("snapshot")
    return serve(cfg, host=args.host, port=args.port,
                 snapshot_path=snapshot, live=bool(args.live),
                 open_browser=not args.no_browser,
                 queue_interval=args.queue_interval)


def cmd_journal(args) -> int:
    cfg = Config.load(args.config)
    rows = read_jsonl(cfg.journal_path, tail=args.tail)
    for r in rows:
        if r.get("type") == "cycle":
            _p("[%s] cycle=%s %s" % (r.get("ts"), r.get("cycle"), (r.get("summary") or "")[:160]))
        else:
            _p("[%s] %s %s" % (r.get("ts"), r.get("type"), json.dumps(
                {k: v for k, v in r.items() if k not in ("ts", "type")},
                ensure_ascii=False)[:300]))
    return 0


def cmd_bridge(args) -> int:
    cfg = Config.load(args.config)
    ex = make_executor(cfg, "bridge")
    caps = ex.capabilities()
    _json(caps)
    if args.action == "queue":
        try:
            _json({"queue": ex.queue()})
        except Exception as exc:
            return _err("读队列失败：%s" % exc)
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def _common(sp, snapshot=True):
    sp.add_argument("--config", help="配置文件路径（默认 v3ai/config.json）")
    sp.add_argument("--provider", help="临时覆盖 provider 档案名")
    if snapshot:
        sp.add_argument("--snapshot", help="用指定快照文件（离线/回放）")
        sp.add_argument("--live", action="store_true", help="强制 live 读取")
    sp.add_argument("--mode", choices=["dry_run", "bridge", "mock"],
                    help="执行模式（默认取配置，当前 dry_run）")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="v3ai", description="Victoria 3 AI 决策层")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("doctor", help="环境/数据/执行端自检")
    _common(sp)
    sp.add_argument("--ping", action="store_true", help="同时测试 LLM 端点")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("overview", help="打印 compact 概况")
    _common(sp)
    sp.add_argument("--compact", action="store_true")
    sp.set_defaults(func=cmd_overview)

    sp = sub.add_parser("run", help="LLM 决策循环")
    _common(sp)
    sp.add_argument("--once", action="store_true", help="只跑一轮")
    sp.add_argument("--cycles", type=int, default=1, help="轮数；0=无限")
    sp.add_argument("--interval", type=float, default=0.0, help="轮间隔秒")
    sp.add_argument("--confirm", action="store_true", help="每轮人工确认后执行")
    sp.add_argument("--plan-only", action="store_true", help="只决策不执行")
    sp.add_argument("--mock", action="store_true", help="用内置离线脚本代替 LLM（不联网）")
    sp.add_argument("--mock-script", help="离线脚本 JSON：步骤列表")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("plan", help="只跑一轮决策，不执行")
    _common(sp)
    sp.add_argument("--mock", action="store_true", help="用内置离线脚本代替 LLM")
    sp.add_argument("--mock-script", help="离线脚本 JSON：步骤列表")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("review", help="审查一个动作 JSON 文件（可 --apply）")
    _common(sp)
    sp.add_argument("--file", required=True)
    sp.add_argument("--apply", action="store_true", help="审查后执行通过的动作")
    sp.add_argument("--yes", action="store_true", help="配合 --apply，跳过确认")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("config", help="配置/provider 管理")
    sp.add_argument("--config", help="配置文件路径")
    sp.add_argument("action", choices=["show", "providers", "use", "set", "ping"])
    sp.add_argument("rest", nargs="*", help="use NAME / set KEY VALUE / ping [NAME]")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("strategy", help="策略查看/切换/重置")
    _common(sp)
    sp.add_argument("action", choices=["show", "set", "reset"])
    sp.add_argument("--file")
    sp.set_defaults(func=cmd_strategy)

    sp = sub.add_parser("tools", help="列出工具")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_tools)

    sp = sub.add_parser("mcp", help="以 stdio MCP server 运行")
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("web", help="启动御前会议 Web 控制台")
    _common(sp)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    sp.add_argument("--queue-interval", type=float, default=5.0,
                    help="建造队列轮询秒数")
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("journal", help="查看决策日志")
    sp.add_argument("--config", help="配置文件路径")
    sp.add_argument("--tail", type=int, default=20)
    sp.set_defaults(func=cmd_journal)

    sp = sub.add_parser("bridge", help="执行端状态/队列")
    sp.add_argument("--config", help="配置文件路径")
    sp.add_argument("action", choices=["status", "queue"], nargs="?", default="status")
    sp.set_defaults(func=cmd_bridge)

    return ap


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        _p("\n已中断")
        return 130
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return _err("%s: %s" % (type(exc).__name__, exc))


if __name__ == "__main__":
    raise SystemExit(main())
