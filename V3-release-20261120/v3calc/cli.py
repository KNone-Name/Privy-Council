# -*- coding: utf-8 -*-
"""v3calc 命令行：读取快照 / 算 GDP / 查生产方式 / 枚举升级方案。

    python v3.py read                      # 实时读取，落盘 + 摘要
    python v3.py read --save save.txt      # 从文本存档读取（离线可用）
    python v3.py gdp                       # 实时快照 + GDP
    python v3.py gdp --save ... --calibrate
    python v3.py pm --building building_food_industry
    python v3.py pm --all-buildings --json pm.json
    python v3.py upgrades --top 20
    python v3.py info
    python v3.py doctor
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from . import __version__
from .gamedata import GameData, discover_game_dir
from .gdp import GDPCalculator
from .model import Snapshot
from .pm import PMChecker, STATUS_ACTIVE, STATUS_AVAILABLE, STATUS_CONDITIONAL
from .scenario import best_upgrade_per_group, rank_pm_upgrades

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)


def _p(msg="", **kw):
    print(msg, **kw)


def _err(msg: str) -> int:
    print("错误: " + msg, file=sys.stderr)
    return 2


def _game_dir(args) -> str | None:
    return args.game_dir or discover_game_dir()


def _gd(args) -> GameData:
    d = _game_dir(args)
    if not d:
        raise RuntimeError("找不到游戏目录，请用 --game-dir 指定 ...\\Victoria 3\\game")
    mods = list(getattr(args, "mod_dir", None) or [])
    return GameData(d, extra_dirs=mods)


def _load_snapshot(args, gd: GameData | None = None) -> Snapshot:
    if getattr(args, "snapshot", None):
        return Snapshot.load(args.snapshot)
    if getattr(args, "save", None):
        from .savefile import SaveReader
        return SaveReader(args.save, country_id=args.country,
                          game_dir=(gd.game_dir if gd else _game_dir(args)),
                          game_data=gd,
                          include_pops=getattr(args, "pops", False)).read()
    if not getattr(args, "_prewarmed", False) and not getattr(args, "no_prewarm", False):
        ok, pid, state = _prewarm_and_check(args, refresh=False)
        if not ok and state == "loading":
            print("[加载中] 检测到半成品缓存，重扫一次…", file=sys.stderr)
            ok, pid, state = _prewarm_and_check(args, refresh=True)
        if not ok:
            from .live import MainMenuError
            if pid is None:
                raise RuntimeError("游戏进程不存在，无法实时读取")
            raise MainMenuError("当前在游戏主菜单/正在加载存档（%s），请稍后重试" % state)
        args._prewarmed = True
        if pid:
            args.pid = pid
    from .live import LiveReader
    reader = LiveReader(
        pid=getattr(args, "pid", None),
        country_id=getattr(args, "country", None),
        include_local_prices=not getattr(args, "no_local_prices", False),
        include_modifiers=not getattr(args, "no_modifiers", False),
        include_politics=not getattr(args, "no_politics", False),
        include_orgs=not getattr(args, "no_orgs", False),
        refresh=getattr(args, "refresh", False),
        game_data=gd,
        progress=(None if getattr(args, "quiet", False)
                  else (lambda m: print(m, file=sys.stderr))),
    )
    return reader.read()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_read(args) -> int:
    # live 首次读取：先 prewarm。否则每个类各扫一遍全内存（本机实测 15 类 ≈ 17 分钟）；
    # prewarm 一次扫完所有类（≈1.5 分钟），之后全走缓存。
    if not getattr(args, "save", None) and not getattr(args, "snapshot", None) \
            and not getattr(args, "no_prewarm", False):
        ok, pid, state = _prewarm_and_check(args, refresh=False)
        if not ok and state == "loading":
            print("[加载中] 检测到半成品缓存（CState 数量不完整），重扫一次…",
                  file=sys.stderr)
            ok, pid, state = _prewarm_and_check(args, refresh=True)
        if not ok:
            if pid is None:
                return _err("游戏进程不存在，或 prewarm 失败")
            print("[主菜单/加载中] 未检测到完整存档（%s）。"
                  "请进入游戏后再执行 read；本次缓存已清理。" % state,
                  file=sys.stderr)
            return 2
        if pid:
            args.pid = pid
        args._prewarmed = True
    gd = None
    if _game_dir(args):
        try:
            gd = GameData(_game_dir(args))
        except Exception as exc:                      # noqa: BLE001
            print("警告: 游戏数据解析失败: %s" % exc, file=sys.stderr)
    t0 = time.time()
    snap = _load_snapshot(args, gd)
    dt = time.time() - t0
    summary = snap.summary()
    print("== 快照 ==")
    print("  来源      %s%s" % (snap.game.source,
                               ("  " + os.path.basename(snap.game.save_path))
                               if snap.game.save_path else ""))
    if snap.game.save_date:
        print("  存档日期  %s   版本 %s" % (snap.game.save_date, snap.game.game_version or "?"))
    print("  国家      %s%s" % (summary["country"],
                               (" / " + summary["tag"]) if summary.get("tag") else ""))
    print("  州 / 建筑 %d / %d（总等级 %d）" %
          (summary["states"], summary["buildings"], summary["levels"]))
    print("  人口      %s" % (f"{summary['population']:,}" if summary["population"] else "?"))
    if summary.get("gdp_save"):
        print("  存档 GDP %s /年（界面值，仅用于校验）" % f"{summary['gdp_save']:,.2f}")
    print("  市场价    %d 种商品" % summary["market_prices"])
    print("  法律/科技 %d / %d" % (summary["laws"], summary["techs"]))
    print("  法令/机构 %d / %d" % (summary["decrees"], summary["institutions"]))
    print("  公司/IG   %d / %d" % (summary["companies"], summary["interest_groups"]))
    print("  政治运动  %d   临时修正 %d" % (summary["movements"], summary["timed_modifiers"]))
    if summary.get("pops") is not None:
        print("  人口聚合  %d 条（州×职业）" % summary["pops"])
    print("  耗时      %.2fs" % dt)
    if snap.diagnostics.warnings:
        print("\n  == 警告 ==")
        for w in snap.diagnostics.warnings:
            print("    - " + w)
    out = args.json
    if out:
        snap.save(out, indent=2)
        print("\n已写出快照: %s" % out)
    elif args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        out = os.path.join(args.out_dir,
                           "snapshot_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        snap.save(out, indent=2)
        print("\n已写出快照: %s" % out)
    return 0


def cmd_gdp(args) -> int:
    gd = _gd(args)
    snap = _load_snapshot(args, gd)
    calc = GDPCalculator(gd, snap)
    res = calc.compute(price=args.price, scale=args.scale, buildings=args.buildings,
                       exclude_goods=_split(args.exclude_goods))
    print("== GDP ==")
    print("  国家        %s%s" % (snap.country_id, (" / " + snap.country.tag) if snap.country.tag else ""))
    print("  口径        价格=%s  缩放=%s  建筑=%s" %
          (res.price_basis, res.scale, res.building_filter))
    print("  周 GDP      %16s" % f"{res.weekly:,.2f}")
    print("  年 GDP      %16s" % f"{res.annual:,.0f}")
    print("  年产出      %16s" % f"{res.annual_output:,.0f}")
    print("  年投入      %16s" % f"{res.annual_input:,.0f}")
    if res.per_capita is not None:
        print("  人均 GDP    %16.3f" % res.per_capita)
    target = snap.country.gdp if snap.country and snap.country.gdp else args.target
    if target:
        diff = 100.0 * (res.annual / target - 1.0)
        label = "存档界面值" if (snap.country and snap.country.gdp) else "指定目标"
        print("  %-10s  %16s   偏差 %+.2f%%" % (label, f"{target:,.0f}", diff))
    if args.by_good:
        print("\n  == 商品贡献（周，按绝对值降序）==")
        print("  %-20s %12s %12s %12s" % ("商品", "产出值", "投入值", "净贡献"))
        for g in res.top_goods(args.top):
            print("  %-20s %12.0f %12.0f %12.0f" %
                  (g.good, g.output_value, g.input_value, g.net_value))
    if args.by_building:
        print("\n  == 建筑贡献（周）==")
        print("  %-42s %4s %7s %12s %12s" %
              ("建筑", "栋", "等级", "产出值", "净贡献"))
        for b in res.top_buildings(args.top):
            print("  %-42s %4d %7d %12.0f %12.0f" %
                  (b.type, b.count, b.levels, b.output, b.net))
    if args.calibrate:
        rows = calc.calibrate(args.target)
        if not rows:
            print("\n（没有存档 GDP 目标值；用 --target 指定）")
        else:
            print("\n  == 口径校准（对比界面 GDP）==")
            print("  %-7s %-13s %-9s %14s %9s" %
                  ("价格", "缩放", "建筑", "年 GDP", "偏差"))
            for r in rows[:args.top]:
                print("  %-7s %-13s %-9s %14.0f %+8.2f%%" %
                      (r["price"], r["scale"], r["buildings"], r["annual"], r["diff_pct"]))
    if res.warnings:
        print("\n  == 计算警告 ==")
        for w in res.warnings[:20]:
            print("    - " + w)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res.to_dict(), f, ensure_ascii=False, indent=2)
        print("\n已写出: %s" % args.json)
    return 0


def _print_pm_report(rep, show_locked: bool):
    tag = "" if not rep.locked_by_tech else "  [建筑本身未解锁: %s]" % ", ".join(rep.missing_building_techs)
    print("\n%s  %d 栋 / %d 级%s" % (rep.building, rep.count, rep.levels, tag))
    for gr in rep.groups:
        cur = ", ".join(gr.current) if gr.current else "（无）"
        print("  %s   当前: %s" % (gr.group, cur))
        for st in gr.statuses:
            if st.status == STATUS_ACTIVE:
                print("    [已启用] %s" % st.pm)
            elif st.status == STATUS_AVAILABLE:
                print("    [可用]   %s" % st.pm)
            elif st.status == STATUS_CONDITIONAL:
                print("    [条件?]  %s   %s" % (st.pm, "；".join(st.conditional)))
            elif show_locked:
                print("    [不可用] %s   %s" % (st.pm, "；".join(st.reasons)))
    for w in rep.warnings:
        print("    ! " + w)


def cmd_pm(args) -> int:
    gd = _gd(args)
    snap = _load_snapshot(args, gd)
    extra_techs = _split(args.tech)
    extra_laws = _split(args.law)
    checker = PMChecker(gd, snap, extra_techs=extra_techs, extra_laws=extra_laws)
    if extra_techs or extra_laws:
        print("== 假设 ==")
        if extra_techs:
            print("  额外科技: " + ", ".join(sorted(extra_techs)))
        if extra_laws:
            print("  额外法律: " + ", ".join(sorted(extra_laws)))
    if args.building:
        reps = [checker.building_report(args.building)]
    else:
        reps = checker.country_report(only_present=not args.all_buildings)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in reps], f, ensure_ascii=False, indent=2)
        print("已写出: %s" % args.json)
        return 0
    print("== 生产方式可用性 ==")
    print("  国家 %s%s   法律 %d   科技 %d" %
          (snap.country_id, (" / " + snap.country.tag) if snap.country.tag else "",
           len(checker.laws), len(checker.techs)))
    for rep in reps:
        _print_pm_report(rep, args.show_locked)
    return 0


def _split(text: str | None) -> set[str]:
    if not text:
        return set()
    return {x.strip() for x in text.replace(";", ",").split(",") if x.strip()}


def cmd_upgrades(args) -> int:
    gd = _gd(args)
    snap = _load_snapshot(args, gd)
    if args.best:
        rows = best_upgrade_per_group(gd, snap, price=args.price, scale=args.scale,
                                      buildings=args.buildings,
                                      include_conditional=args.conditional)
    else:
        rows = rank_pm_upgrades(gd, snap, building_type=args.building,
                                group=args.group, price=args.price, scale=args.scale,
                                buildings=args.buildings,
                                include_conditional=args.conditional,
                                state_id=args.state)
    rows = [r for r in rows if (r.delta > 0 or args.all)]
    rows = rows[:args.top]
    print("== PM 升级方案（按年 GDP 增量）==")
    print("  口径 价格=%s 缩放=%s 建筑=%s" % (args.price, args.scale, args.buildings))
    if not rows:
        print("  （没有正收益且当前允许的替换）")
        return 0
    print("  %-34s %-24s %-24s %12s %8s" %
          ("建筑", "当前 PM", "候选 PM", "年增量", "增量%"))
    for r in rows:
        cur = ",".join(r.current) if r.current else "-"
        print("  %-34s %-24s %-24s %12.0f %+7.2f%%" %
              (r.building, cur[:24], r.candidate[:24], r.delta, r.delta_pct))
    print("\n  注：年增量只按商品产出−投入计算，不含行政力/就业/死亡率等非商品效果；"
          "换 PM 前请用 `pm --building` 核对。")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in rows], f, ensure_ascii=False, indent=2)
        print("\n已写出: %s" % args.json)
    return 0


def _cache_dir() -> str:
    from v3live import cli as live_cli
    return live_cli.CACHE_DIR


def _cache_files(pid: int, cls: str) -> list[str]:
    import glob
    pat = os.path.join(_cache_dir(), "%s_*_%d_*.json" % (cls, pid))
    return sorted(glob.glob(pat))


def _cache_count(pid: int, cls: str) -> int | None:
    best = None
    for path in _cache_files(pid, cls):
        try:
            with open(path, encoding="utf-8") as f:
                n = len(json.load(f).get("addrs") or [])
        except Exception:
            continue
        best = n if best is None else max(best, n)
    return best


def _cache_sample_ok(p, cls: str) -> bool:
    """抽查缓存地址的首 8 字节是否仍是该类的 vtable（判断是否已退出到主菜单）。"""
    from v3live import cli as live_cli
    addrs = []
    for path in _cache_files(p.pid, cls):
        try:
            with open(path, encoding="utf-8") as f:
                addrs = json.load(f).get("addrs") or []
        except Exception:
            continue
        if addrs:
            break
    if not addrs:
        return False
    base = p.module_base("victoria3.exe")
    try:
        vs = live_cli._selected("^%s$" % cls, base)
    except SystemExit:
        return False
    if not vs:
        return False
    n = len(addrs)
    idx = sorted({i * n // 8 for i in range(min(8, n))})
    return all(p.read_u64(addrs[i]) in vs for i in idx)


def _game_loaded(p) -> bool:
    """判断“已进入游戏且扫描完整”。

    只看 CCountry>0 不够：在“正在加载存档”的瞬间扫描，会出现
    CCountry=735 但 CState 只有 132（应为 1000+）的半成品。这里用
    州数/国家数、CJominiPlayer、CBuilding 三个条件一起卡。
    """
    c = _cache_count(p.pid, "CCountry") or 0
    if not c:
        return False
    if not _cache_sample_ok(p, "CCountry"):
        return False
    states = _cache_count(p.pid, "CState") or 0
    if states < max(8, int(c * 0.75)):
        return False
    if (_cache_count(p.pid, "CJominiPlayer") or 0) < 1:
        return False
    if (_cache_count(p.pid, "CBuilding") or 0) < 1:
        return False
    return True


def _purge_pid_caches(pid: int) -> None:
    for path in _cache_files(pid, "*"):
        try:
            os.remove(path)
        except OSError:
            pass


def _ensure_decree_cache(p) -> None:
    # 没有法令实例（开局常见）就不做第二次全内存扫描
    if not _cache_count(p.pid, "CDecree"):
        return
    # 已有 all-vtable 缓存就不重复
    if any("_a_" in os.path.basename(x) for x in _cache_files(p.pid, "CDecree")):
        return
    from v3live import cli as live_cli
    try:
        live_cli._DEDUPE_BY = None
        live_cli._DEDUPE_KEEP = "auto"
        live_cli._instances_of(p, "CDecree", 0, verbose=False, all_vtables=True)
    except (Exception, SystemExit):                    # noqa: BLE001
        pass


def _prewarm_and_check(args, refresh: bool = False):
    """先读州注册表判世界状态，只有确认在存档里才做全量扫描。

    返回 (loaded, pid, state)；state = loaded / menu / loading / no_process。
    """
    from v3live import cli as live_cli
    from v3live import proc as proc_mod
    from .worldstate import WorldProbe

    ns = argparse.Namespace(pid=getattr(args, "pid", None), refresh=bool(refresh))
    try:
        p = proc_mod.attach("victoria3.exe", pid=ns.pid)
    except Exception as exc:                          # noqa: BLE001
        print("[等待游戏] %s" % exc, file=sys.stderr)
        return False, None, "no_process"
    try:
        pid = p.pid
        probe = None
        ws = None
        try:
            probe = WorldProbe.from_process(p)
            if probe is not None:
                ws = probe.probe()
        except Exception as exc:                      # noqa: BLE001
            print("[世界判定] 注册表探测不可用: %s" % exc, file=sys.stderr)

        if ws is not None and not ws.in_save:
            # 主菜单/加载中：绝不扫描，清掉旧缓存，直接等待
            _purge_pid_caches(pid)
            return False, pid, ("loading" if ws.loading or ws.capacity else "menu")

        before = ws.states if ws is not None else 0
        # 确认在存档里（或探测不可用退回旧逻辑）才扫描
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            live_cli.cmd_prewarm(ns)
        text = buf.getvalue()
        if "需要扫描的类" in text or "正在一次性扫描" in text:
            print(text, end="")          # 真扫描时保留进度；全缓存时保持安静

        if probe is not None:
            ws2 = probe.probe()
            if not ws2.in_save:
                _purge_pid_caches(pid)
                return False, pid, ("loading" if ws2.loading or ws2.capacity else "menu")
            if before and ws2.states + 8 < before:
                _purge_pid_caches(pid)
                return False, pid, "loading"
        if _game_loaded(p):
            _ensure_decree_cache(p)
            return True, pid, "loaded"
        _purge_pid_caches(pid)
        return False, pid, "loading"
    finally:
        p.close()


def _wait_for_game(args, refresh: bool = False, max_attempts: int = 0):
    """等待进程出现 / 等待用户加载存档；返回 (ok, pid)。"""
    import time as _time
    attempts = 0
    while True:
        attempts += 1
        loaded, pid, state = _prewarm_and_check(args, refresh=refresh)
        if loaded:
            return True, pid
        if max_attempts and attempts >= max_attempts:
            return False, pid
        if pid is None:
            # 进程还没起来：不必每次重扫，直接等进程出现
            print("[等待游戏] 请先启动 Victoria 3…", file=sys.stderr)
            _time.sleep(max(getattr(args, "interval", 10) or 10, 2.0))
            continue
        # 进程在，但 CCountry=0（主菜单）或 CState 数量不完整（加载中）
        wait = float(getattr(args, "wait_load", 0) or 0)
        if wait > 0:
            print("[主菜单/加载中] 未检测到完整存档；%.0f 秒后重试（会重新扫描）…" % wait,
                  file=sys.stderr)
            _time.sleep(wait)
        else:
            try:
                input("[主菜单] 请在游戏里加载存档，然后按 Enter 继续（Ctrl+C 退出）…")
            except (EOFError, KeyboardInterrupt):
                return False, pid
        refresh = True


def cmd_prewarm(args) -> int:
    """调用 v3live 的 prewarm：一次扫完常用类，写进缓存。

    主菜单（CCountry=0）时不会保留缓存，返回 2，避免之后进游戏误用空缓存。
    """
    loaded, pid, state = _prewarm_and_check(
        args, refresh=bool(getattr(args, "refresh", False)))
    if not loaded:
        if pid is None:
            print("[错误] 游戏进程不存在，或 prewarm 失败。")
            return 1
        print("[主菜单/加载中] 未检测到完整存档（%s）；本次缓存已清理。"
              % ("主菜单" if state == "menu" else "CState 数量不完整"))
        return 2
    return 0


def cmd_watch(args) -> int:
    """实时循环：复用同一个进程句柄，按 interval 重读并打印/落盘。"""
    import csv
    import time as _time
    from v3live import proc as proc_mod

    if getattr(args, "save", None) or getattr(args, "snapshot", None):
        return _err("watch 只支持实时读取，不能配 --save/--snapshot")
    gd = _gd(args)

    def _attach():
        return proc_mod.attach("victoria3.exe", pid=getattr(args, "pid", None))

    # 先确认已经进入游戏（主菜单 CCountry=0 时等待 Enter/--wait-load）
    p = None
    if not args.no_prewarm:
        ok, pid = _wait_for_game(args, refresh=False,
                                 max_attempts=getattr(args, "rounds", 0))
        if not ok:
            return 2
        if pid:
            args.pid = pid
        try:
            p = _attach()
        except Exception as exc:                       # noqa: BLE001
            print("[等待游戏] %s" % exc, file=sys.stderr)
            return 1

    from .gdp import GDPCalculator
    from .live import LiveReader

    reader = LiveReader(
        pid=getattr(args, "pid", None),
        country_id=args.country,
        game_data=gd,
        progress=None,
        include_local_prices=True,
        include_modifiers=True,
        include_politics=True,
        include_orgs=True,
    )
    hist_f = None
    hist_w = None
    prev_gdp = None
    prev_buildings = None
    n = 0
    attempts = 0
    t_start = _time.time()
    try:
        if args.history:
            new = not os.path.exists(args.history) or os.path.getsize(args.history) == 0
            hist_f = open(args.history, "a", newline="", encoding="utf-8")
            hist_w = csv.writer(hist_f)
            if new:
                hist_w.writerow(["captured_at", "country", "tag", "population",
                                 "treasury", "weekly_balance", "gdp_annual",
                                 "gdp_per_capita", "buildings", "levels", "states",
                                 "market_prices"])
        while True:
            attempts += 1
            t0 = _time.time()
            if p is None:
                if args.no_prewarm:
                    try:
                        p = _attach()
                    except Exception as exc:           # noqa: BLE001
                        print("[等待游戏] %s" % exc, file=sys.stderr)
                        if args.rounds and attempts >= args.rounds:
                            break
                        _time.sleep(max(args.interval, 1.0))
                        continue
                else:
                    ok, pid = _wait_for_game(args, refresh=bool(n),
                                             max_attempts=getattr(args, "rounds", 0))
                    if not ok:
                        break
                    if pid:
                        args.pid = pid
                    try:
                        p = _attach()
                        if n:
                            print("已重新连接游戏进程 pid=%d" % p.pid)
                    except Exception as exc:           # noqa: BLE001
                        print("[等待游戏] %s" % exc, file=sys.stderr)
                        if args.rounds and attempts >= args.rounds:
                            break
                        _time.sleep(max(args.interval, 1.0))
                        continue
            try:
                snap = reader.read_with(p)
            except Exception as exc:                   # noqa: BLE001
                print("[读取失败] %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
                try:
                    p.close()
                except Exception:
                    pass
                p = None
                if args.rounds and attempts >= args.rounds:
                    break
                _time.sleep(max(args.interval, 1.0))
                continue
            if snap.country is None or (prev_buildings and not snap.buildings):
                print("[读取失败] 数据为空（进程可能已退出）", file=sys.stderr)
                try:
                    p.close()
                except Exception:
                    pass
                p = None
                if args.rounds and attempts >= args.rounds:
                    break
                _time.sleep(max(args.interval, 1.0))
                continue
            prev_buildings = len(snap.buildings) or prev_buildings

            res = None
            if not args.no_gdp:
                try:
                    res = GDPCalculator(gd, snap).compute(
                        price=args.price, scale=args.scale, buildings=args.buildings)
                except Exception as exc:               # noqa: BLE001
                    print("[GDP 失败] %s" % exc, file=sys.stderr)
            c = snap.country
            gdp = res.annual if res else None
            delta = "" if (gdp is None or prev_gdp is None) else \
                " Δ%+d" % round(gdp - prev_gdp)
            target = args.target or (c.gdp if c else None)
            diff = (" %+.2f%%" % (100.0 * (gdp / target - 1.0))) if (gdp and target) else ""
            line = "[%s] " % datetime.now().strftime("%H:%M:%S")
            if gdp is not None:
                line += "GDP %s%s%s | " % (f"{gdp:,.0f}", delta, diff)
                if res.per_capita is not None:
                    line += "人均 %.3f | " % res.per_capita
            line += "国库 %s | 周结余 %s | 建筑 %d/%d | %d 州 | %.2fs" % (
                f"{c.treasury:,.0f}" if c and c.treasury is not None else "?",
                f"{c.budget.balance:+,.0f}" if c and c.budget.balance is not None else "?",
                len(snap.buildings), sum(b.levels for b in snap.buildings),
                len(snap.states), _time.time() - t0)
            print(line, flush=True)
            prev_gdp = gdp

            if args.json:
                tmp = args.json + ".tmp"
                snap.save(tmp, indent=None)
                os.replace(tmp, args.json)
            if hist_w:
                hist_w.writerow([
                    snap.game.captured_at, c.id if c else "", c.tag if c else "",
                    c.population if c else "", c.treasury if c else "",
                    c.budget.balance if c and c.budget else "",
                    round(gdp, 2) if gdp is not None else "",
                    round(res.per_capita, 6) if res and res.per_capita is not None else "",
                    len(snap.buildings), sum(b.levels for b in snap.buildings),
                    len(snap.states), len(snap.market.prices) if snap.market else 0,
                ])
                hist_f.flush()
            n += 1
            if args.rounds and n >= args.rounds:
                break
            elapsed = _time.time() - t0
            _time.sleep(max(0.0, args.interval - elapsed))
    except KeyboardInterrupt:
        print("\n已停止（共 %d 轮，%.1fs）" % (n, _time.time() - t_start), file=sys.stderr)
        return 0
    finally:
        if p is not None:
            p.close()
        if hist_f is not None:
            hist_f.close()
    return 0


def cmd_info(args) -> int:
    gd = _gd(args)
    s = gd.summary()
    print("== 游戏数据 ==")
    for k, v in s.items():
        print("  %-18s %s" % (k, v))
    print("\n  == 建筑（按 PM 组数）==")
    rows = sorted(gd.buildings.values(), key=lambda b: -len(b.production_method_groups))
    for b in rows[:args.top]:
        print("  %-42s %d 组  %s" %
              (b.name, len(b.production_method_groups),
               " ".join(b.production_method_groups)))
    return 0


def cmd_doctor(args) -> int:
    ok = True
    print("== 环境检查 ==")

    def check(name, fn):
        nonlocal ok
        try:
            msg = fn()
            print("  [PASS] %-22s %s" % (name, msg or ""))
        except Exception as exc:                       # noqa: BLE001
            ok = False
            print("  [FAIL] %-22s %s" % (name, exc))

    check("Python", lambda: sys.version.split()[0])
    check("v3live", lambda: __import__("v3live").__version__)
    check("游戏目录", lambda: _game_dir(args))
    check("游戏数据", lambda: str(GameData(_game_dir(args)).summary()))
    try:
        from v3live import proc as proc_mod
        pids = proc_mod.find_pids("victoria3.exe")
        print("  [%s] %-22s %s" % ("PASS" if pids else "WARN", "游戏进程",
                                   pids or "未运行（live 模式不可用，可用 --save）"))
    except Exception as exc:                           # noqa: BLE001
        print("  [FAIL] 游戏进程            %s" % exc)
    if args.save:
        def _save():
            from .savefile import SaveReader
            s = SaveReader(args.save).read()
            return "%s  %s  %d 栋建筑" % (s.game.save_date, s.country.tag, len(s.buildings))
        check("存档", _save)
    else:
        print("  [SKIP] 存档                  未指定 --save")
    print("\n结论: %s" % ("全部通过" if ok else "有失败项，见上"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 决策计算层（市场视图 / 候选包 / 简报 / 信号 / 台账）
# ---------------------------------------------------------------------------
def cmd_advise(args) -> int:
    from .advisor import Advisor
    from .brief import fmt_money, render_brief
    from .candidates import Intent

    gd = _gd(args)
    snap = _load_snapshot(args, gd)
    prev = Snapshot.load(args.prev) if getattr(args, "prev", None) else None
    intent = Intent.load(args.intent) if getattr(args, "intent", None) else None
    adv = Advisor(gd, snap, prev=prev, intent=intent,
                  state_meta=args.state_meta, ledger=args.ledger)

    what = args.what
    payload: dict | list | None = None

    if what in ("brief", "all"):
        brief = adv.brief(top=args.top, weeks=args.weeks,
                          include_pm=not args.no_pm)
        text = render_brief(brief, args.char_budget)
        print(text)
        payload = brief
        if what == "all":
            print("\n== 规则快通道 ==")
            for i, q in enumerate(adv.quick(max_items=4), 1):
                print("  %d) [%s] %s — %s" % (i, q["kind"], q["action"], q["reason"]))
        if args.record:
            rec = adv.record(note="cli advise")
            print("\n已记入台账: %s（预测年增值 %s）"
                  % (rec["id"], fmt_money(rec["predicted_annual_value"])))
    elif what == "market":
        view = adv.view
        print("== 市场（按产业链）==")
        print("  链        商品 短缺 过剩 最紧（比价）        缺口/周")
        for r in view.chains():
            tight = ("%s %.2f" % (r.tightest, r.tightest_ratio)
                     if r.tightest else "-")
            print("  %-9s %3d %4d %4d %-20s %s" % (
                r.chain, r.n_goods, r.n_shortage, r.n_glut, tight,
                fmt_money(r.shortage_value) if r.shortage_value else "-"))
        print("\n  短缺 Top:")
        for p in view.shortages(8):
            print("    %-16s %.2f× 缺口 %+.0f/周（%s/周）"
                  % (p.good, p.ratio, p.gap, fmt_money(p.gap_value)))
        print("  过剩 Top:")
        for p in view.gluts(6):
            print("    %-16s %.2f×" % (p.good, p.ratio))
        payload = adv.market()
    elif what == "candidates":
        rows = adv.candidates(args.top)
        from .brief import _candidate_table
        print("\n".join(_candidate_table(rows)))
        payload = rows
    elif what == "plan":
        plan = adv.plan(weeks=args.weeks, max_items=max(1, args.top),
                        budget_points=args.budget)
        print("== 逐步贪心方案 ==")
        print("  预算 %.0f 建造点（建造力 %.0f/周，约 %.1f 周），现金 %.0f"
              % (plan.budget_points, plan.capacity_per_week,
                 plan.weeks or 0, sum(x.cash_cost or 0 for x in plan.items)))
        for i, o in enumerate(plan.items, 1):
            print("  %d) 州 %-5s %-32s +%d  %s/年  每点 %.1f  %.1f 周  风险 %.2f"
                  % (i, o.state_id, o.building, o.levels,
                     fmt_money(o.annual_value), o.per_point, o.weeks or 0, o.risk))
            for n in (o.risk_notes + o.warnings)[:2]:
                print("       · " + str(n))
        if not plan.items:
            print("  （没有超过阈值的正收益候选）")
        print("  " + plan.note)
        payload = plan.to_dict()
    elif what == "capacity":
        rows = adv.capacity(weeks=args.weeks, limit=max(1, args.top))
        print("== 建造力（只算成本，不估值）==")
        if not rows:
            print("  （没有可建的落点）")
        for c in rows:
            verdict = "可承受" if c["ok"] else "超限：" + "、".join(
                k for k, v in (c.get("affordable") or {}).items() if not v)
            print("  州 %-5s +%d 级：%s 点 / %.1f 周 / 现金 %s / +%.1f 容量/周"
                  % (c["state_id"], c["levels"], fmt_money(c["points"]),
                     c["weeks"] or 0, fmt_money(c.get("cash_cost")),
                     c.get("capacity_add") or 0))
            print("      周维护 %s，每点容量 %s → %s"
                  % (fmt_money(c.get("weekly_upkeep")),
                     fmt_money(c.get("cost_per_capacity")), verdict))
            for n in c.get("notes") or []:
                print("      · " + n)
        pms = adv.capacity_pms()
        if pms:
            print("  建造方式（按每点容量周成本，便宜到贵）：")
            for p in pms:
                print("      %-34s +%.0f/级  维护 %s  每点 %s%s"
                      % (p["pm"], p["capacity_add"],
                         fmt_money(p["weekly_upkeep"]),
                         fmt_money(p["cost_per_capacity"]),
                         "  ← 当前" if p["current"] else ""))
        payload = {"capacity": rows, "pms": pms}
    elif what == "signals":
        sig = adv.signals()
        if not sig:
            print("（无异常信号：本轮不需要升级给 LLM）")
        for s in sig:
            print("[S%d %s] %s — %s" % (s.severity, s.label, s.title, s.message))
            if s.suggested:
                print("        → " + s.suggested)
        payload = [s.to_dict() for s in sig]
    elif what == "quick":
        for i, q in enumerate(adv.quick(max_items=args.top), 1):
            print("%d) [%s] %s\n   %s" % (i, q["kind"], q["action"], q["reason"]))
        payload = adv.quick(max_items=args.top)
    elif what == "ledger":
        if args.record:
            rec = adv.record(note="cli advise --record")
            print("已记入台账: %s（%d 项，预测年增值 %s）"
                  % (rec["id"], len(rec["items"]),
                     fmt_money(rec["predicted_annual_value"])))
            payload = rec
        elif args.reconcile:
            rep = adv.reconcile()
            print("== 对账 ==")
            for row in rep["rows"]:
                print("  %s  预测 %+.0f  实际 %+.0f  偏差 %s"
                      % (row["id"], row["predicted"],
                         row["actual_delta"] or 0, row["bias"]))
            print("  中位偏差 %s  建议缩放 %s" % (rep["median_bias"],
                                                 rep["suggested_scale"]))
            print("  " + rep["note"])
            payload = rep
        else:
            summary = adv.ledger.summary()
            print("== 台账 ==")
            print("  文件 %s" % summary["path"])
            print("  共 %d 条（未对账 %d / 已对账 %d），中位偏差 %s"
                  % (summary["n"], summary["n_open"], summary["n_done"],
                     summary["median_bias"]))
            payload = summary
    else:
        return _err("未知动作: %s" % what)

    if args.json and payload is not None:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print("\n已写出: %s" % args.json)
    return 0


def cmd_selftest(args) -> int:
    import unittest
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("v3calc.selftest")
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="v3", description="Victoria 3 实时数据读取 + 计算层（GDP / 生产方式）")
    p.add_argument("--version", action="version", version="v3calc " + __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--game-dir", help="...\\Victoria 3\\game")
        sp.add_argument("--mod-dir", action="append", default=[],
                        help="mod 根目录（其下含 common\\），可重复；后加载覆盖先加载")
        sp.add_argument("--country", type=int, help="国家 id（默认玩家）")
        sp.add_argument("--save", help="文本存档 save.txt（离线模式）")
        sp.add_argument("--snapshot", help="已落盘的 snapshot.json")
        sp.add_argument("--json", help="输出 JSON 路径")
        sp.add_argument("--quiet", action="store_true")

    sp = sub.add_parser("read", help="读取实时/存档快照")
    common(sp)
    sp.add_argument("--pid", type=int)
    sp.add_argument("--out-dir", help="快照输出目录（默认不落盘）")
    sp.add_argument("--refresh", action="store_true", help="重新扫描内存（慢）")
    sp.add_argument("--no-local-prices", action="store_true")
    sp.add_argument("--no-modifiers", action="store_true")
    sp.add_argument("--no-politics", action="store_true")
    sp.add_argument("--no-orgs", action="store_true")
    sp.add_argument("--pops", action="store_true", help="解析并聚合人口（慢，存档模式）")
    sp.add_argument("--no-prewarm", action="store_true",
                    help="跳过首次自动 prewarm（会慢很多）")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("gdp", help="计算 GDP")
    common(sp)
    sp.add_argument("--price", default="auto",
                    choices=["auto", "local", "market", "base"])
    sp.add_argument("--scale", default="staffing",
                    choices=["throughput", "staffing", "levels", "staffing_only"])
    sp.add_argument("--buildings", default="producers",
                    help="producers（默认）/ all / 逗号分隔的建筑类型")
    sp.add_argument("--exclude-goods", help="排除的商品（逗号分隔），如 services")
    sp.add_argument("--by-good", action="store_true")
    sp.add_argument("--by-building", action="store_true")
    sp.add_argument("--calibrate", action="store_true")
    sp.add_argument("--target", type=float, help="校准目标（默认用存档界面值）")
    sp.add_argument("--top", type=int, default=12)
    sp.set_defaults(func=cmd_gdp)

    sp = sub.add_parser("pm", help="当前科技/法律下允许的生产方式")
    common(sp)
    sp.add_argument("--building", help="只看某建筑类型")
    sp.add_argument("--all-buildings", action="store_true", help="包含玩家没有的建筑")
    sp.add_argument("--show-locked", action="store_true", help="显示不可用项及原因")
    sp.add_argument("--tech", help="假设已研发的科技（逗号分隔），用于规划")
    sp.add_argument("--law", help="假设已生效的法律（逗号分隔），用于规划")
    sp.set_defaults(func=cmd_pm)

    sp = sub.add_parser("upgrades", help="按 GDP 增量枚举 PM 替换方案")
    common(sp)
    sp.add_argument("--building")
    sp.add_argument("--group")
    sp.add_argument("--state", type=int, help="只改某个州的建筑")
    sp.add_argument("--best", action="store_true", help="每组只取最优")
    sp.add_argument("--conditional", action="store_true", help="包含条件未知项")
    sp.add_argument("--all", action="store_true", help="包含负收益")
    sp.add_argument("--price", default="auto",
                    choices=["auto", "local", "market", "base"])
    sp.add_argument("--scale", default="staffing",
                    choices=["throughput", "staffing", "levels", "staffing_only"])
    sp.add_argument("--buildings", default="producers",
                    help="producers（默认）/ all / 逗号分隔的建筑类型")
    sp.add_argument("--top", type=int, default=20)
    sp.set_defaults(func=cmd_upgrades)

    sp = sub.add_parser("watch", help="实时循环读取（默认每 10 秒刷新）")
    common(sp)
    sp.add_argument("--pid", type=int)
    sp.add_argument("--interval", type=float, default=10.0,
                    help="刷新间隔秒数（默认 10；可 1~60）")
    sp.add_argument("--rounds", type=int, default=0, help="只跑 N 轮（0=无限）")
    sp.add_argument("--wait-load", type=float, default=0,
                    help="主菜单时每隔 N 秒自动重试（0=等待按 Enter）")
    sp.add_argument("--history", help="追加 CSV 历史（每轮一行）")
    sp.add_argument("--no-gdp", action="store_true", help="只看快照，不算 GDP")
    sp.add_argument("--no-prewarm", action="store_true")
    sp.add_argument("--target", type=float, help="对照 GDP 目标")
    sp.add_argument("--price", default="auto",
                    choices=["auto", "local", "market", "base"])
    sp.add_argument("--scale", default="staffing",
                    choices=["throughput", "staffing", "levels", "staffing_only"])
    sp.add_argument("--buildings", default="producers",
                    help="producers（默认）/ all / 逗号分隔的建筑类型")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("prewarm", help="预热 v3live 内存扫描缓存（游戏重启后跑一次）")
    sp.add_argument("--pid", type=int)
    sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(func=cmd_prewarm)

    sp = sub.add_parser("info", help="游戏数据概览")
    sp.add_argument("--game-dir")
    sp.add_argument("--top", type=int, default=30)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("doctor", help="环境检查")
    sp.add_argument("--game-dir")
    sp.add_argument("--save")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser(
        "advise", help="决策计算层：市场视图/候选包/简报/信号/台账")
    common(sp)
    sp.add_argument("what", nargs="?", default="brief",
                    choices=["brief", "market", "candidates", "plan",
                             "capacity", "signals", "quick", "ledger", "all"],
                    help="默认 brief（一页纸简报）")
    sp.add_argument("--top", type=int, default=8, help="候选/条目数量")
    sp.add_argument("--weeks", type=float, default=None,
                    help="本轮预算按几周建造力算（默认取 intent.budget_weeks）")
    sp.add_argument("--budget", type=float, default=None,
                    help="直接给建造点预算（覆盖 --weeks）")
    sp.add_argument("--char-budget", type=int, default=2600,
                    help="简报字符上限（默认 2600）")
    sp.add_argument("--state-meta", help="state_meta.json（默认 v3ai/data）")
    sp.add_argument("--prev", help="上一份快照（算变化/对账）")
    sp.add_argument("--intent", help="意图 JSON（priority_chains / bans / 预算周数）")
    sp.add_argument("--ledger", help="台账路径（默认 v3calc/state/ledger.jsonl）")
    sp.add_argument("--no-pm", action="store_true", help="简报不含生产方式")
    sp.add_argument("--record", action="store_true", help="把本轮方案记入台账")
    sp.add_argument("--reconcile", action="store_true", help="对账（配合 ledger）")
    sp.set_defaults(func=cmd_advise)

    sp = sub.add_parser("selftest", help="跑决策计算层自带测试")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_selftest)
    return p


def main(argv=None) -> int:
    # 并行扫描（语义与 v3live 相同，只换实现）；失败就退回原版
    try:
        from .fastscan import install
        install()
    except Exception:                                  # noqa: BLE001
        pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as exc:                           # noqa: BLE001
        try:
            from .live import MainMenuError
        except Exception:
            MainMenuError = ()                         # type: ignore
        if MainMenuError and isinstance(exc, MainMenuError):
            print("[主菜单] %s" % exc, file=sys.stderr)
            print("提示：先加载存档；或运行 python v3.py watch 让它自动等待。",
                  file=sys.stderr)
            return 2
        if os.environ.get("V3CALC_DEBUG"):
            raise
        print("错误: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        print("（设置 V3CALC_DEBUG=1 可看堆栈）", file=sys.stderr)
        return 1
