"""v3live CLI —— 不改游戏内存，只读取并报告。

用法：
    python -m v3live classes                 # 静态：exe 里所有可实例化的类（不需要开游戏）
    python -m v3live classes --match Building
    python -m v3live scan                    # 实时：核心类各有多少存活实例
    python -m v3live scan --match CPop
    python -m v3live dump CCountry --limit 3 --len 0x100
    python -m v3live watch CCountry --interval 2 --rounds 3

EXE 路径默认取 VICTORIA3_EXE 环境变量，其次用下面的常量。
"""
import argparse
import json
import os
import re
import struct
import sys
import time

try:
    from . import discover, proc as proc_mod
    from .rtti import RTTI
except ImportError:                      # 直接 python run.py / python cli.py 运行时
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from v3live import discover, proc as proc_mod
    from v3live.rtti import RTTI

STRUCT = discover.STRUCT

EXE = os.environ.get(
    "VICTORIA3_EXE",
    r"E:\SteamLibrary\steamapps\common\Victoria 3\binaries\victoria3.exe")

# 默认关注的「有业务含义」的类前缀；纯名字，不含任何地址
CORE = ("CState", "CCountry", "CPop", "CBuilding", "CMarket", "CCharacter",
        "CInterestGroup", "CWar", "CFront", "CArmy", "CNavy", "CLaw",
        "CPoliticalMovement", "CProvince", "CGoods", "CTradeRoute", "CCompany")

_rtti = None


def rtti():
    global _rtti
    if _rtti is None:
        _rtti = RTTI(EXE)
    return _rtti


def _selected(pattern, base=None):
    """{运行时 vtable VA: 类名}。base 必须传运行中的模块基址（ASLR）。"""
    r = rtti()
    if base is None:
        base = r.pe.image_base
    out = {}
    for name, cls in r.classes.items():
        if not cls.vtables:
            continue
        if pattern:
            if not re.search(pattern, name):
                continue
        elif not name.startswith(CORE):
            continue
        for vt in cls.vtables:
            out[base + vt] = name
    return out


def _attach(args):
    p = proc_mod.attach("victoria3.exe", pid=args.pid)
    return p


# ---------------------------------------------------------------- classes
def cmd_classes(args):
    r = rtti()
    s = r.summary()
    print("exe=%s" % EXE)
    print("类 %d 个，其中可实例化（有 vtable）%d 个，vtable 共 %d 张"
          % (s["classes"], s["with_vtable"], s["vtables"]))
    rows = []
    for name, cls in r.classes.items():
        if args.match:
            if not re.search(args.match, name):
                continue
        if not cls.vtables:
            continue
        rows.append((name, len(cls.vtables)))
    rows.sort()
    print("匹配 %d 个：" % len(rows))
    for name, n in rows[:args.limit]:
        print("  %-56s %d vt" % (name, n))
    return 0


# ------------------------------------------------------------------- scan
# 每类的"数据合理性"校验：读档/重启后新旧两套对象会并存，
# 靠 keep=first/last 猜不可靠，改用字段范围来判断哪套是真的。
_SANITY = {
    "CState": [(0x1218, "u32", 0, 1_000_000),      # 耕地
               (0x1258, "i64", 0, 10 ** 15),        # 下层人口
               (0xEC8, "u32", 0, 100_000)],         # 归属国家
    "CCountry": [(0x18, "u32", 0, 100_000),          # 国家 id
                 (0x1308, "i64", -10 ** 15, 10 ** 15)],  # 国库
    "CBuilding": [(0xDC, "u32", 0, 100_000),         # 州 id
                  (0xE0, "u32", 0, 100_000)],        # 等级
    "CMarket": [(0x10, "u32", 0, 100_000),           # 市场 id
                (0x838, "u32", 0, 100_000)],         # owner
}


def _sane(p, cls_name, addr):
    checks = _SANITY.get(cls_name)
    if not checks:
        return True
    for off, kind, lo, hi in checks:
        v = p.read_i64(addr + off) if kind == "i64" else p.read_u32(addr + off)
        if v is None or not (lo <= v <= hi):
            return False
    return True


def cmd_scan(args):
    p = _attach(args)
    try:
        base = p.module_base("victoria3.exe")
        print("pid=%d  base=0x%X" % (p.pid, base))
        r = rtti()
        if base != r.pe.image_base:
            print("ASLR：静态基址 0x%X，运行时基址 0x%X，vtable 值按运行时换算"
                  % (r.pe.image_base, base))
        vs = _selected(args.match, base)
        print("关注 %d 张 vtable，开始扫描…" % len(vs))
        t0 = time.time()
        hits = discover.scan_instances(p, vs)
        dt = time.time() - t0
        rows = sorted(((n, a) for n, a in hits.items()), key=lambda kv: -len(kv[1]))
        print("\n扫描用时 %.1fs，命中 %d 个类" % (dt, len(rows)))
        print("%-44s %8s %8s" % ("类", "命中", "对象数"))
        tot_raw = tot_obj = 0
        for name, addrs in rows:
            n_obj, _ = discover.cluster_count(addrs)
            tot_raw += len(addrs)
            tot_obj += n_obj
            if n_obj < args.min:
                continue
            note = "" if n_obj == len(addrs) else "  (多继承去重)"
            print("  %-42s %8d %8d%s" % (name, len(addrs), n_obj, note))
            if args.verbose:
                for a in addrs[:args.limit]:
                    print("     0x%X" % a)
        print("\n合计：命中 %d 处 -> 对象 %d 个" % (tot_raw, tot_obj))
    finally:
        p.close()
    return 0


# ----------------------------------------------------------------- states
OFF_STATE_ID = 0x10
OFF_STATE_ARABLE = 0x1218      # 总耕地，已与存档 arable_land 逐州对齐（914=340, 915=75）
OFF_STATE_UNKNOWN_AC4 = 0xAC4  # 数值呈 17k+1 规律，含义待定
OFF_STATE_CAPITAL = 0xEC0
OFF_STATE_UNKNOWN = 0xEC4
OFF_STATE_OWNER = 0xEC8
OFF_STATE_PREV_OWNER = 0xECC
# pop_statistics（i64，每项 8 字节；已与存档逐项核对）
OFF_POP_LOWER = 0x1258
OFF_POP_MIDDLE = 0x1260
OFF_POP_UPPER = 0x1268
OFF_POP_RADICALS = 0x1270
OFF_POP_SALARIED = 0x1288
OFF_POP_UNEMPLOYED = 0x1298
OFF_POP_LABORER = 0x12A0
OFF_POP_GOVERNMENT = 0x12B0
OFF_POP_MILITARY = 0x12B8
OFF_POP_POLITICAL = 0x12C0


OFF_STATE_INFRA_USED = 0x1DE0  # i64 定点 ÷100000，已用基础设施（913=60.0）
OFF_STATE_INFRA = 0x1DB0     # i64 定点 ÷100000，基础设施总量（北京 93.0）


def cmd_states(args):
    """按州列出：id / 归属 / 首府 / 耕地。用于和游戏界面核对。"""
    global _DEDUPE_BY
    if _DEDUPE_BY is None:
        _DEDUPE_BY = OFF_STATE_ID      # 州对象自带唯一 id，默认按它去重
    p = _attach(args)
    try:
        addrs = _instances_of(p, "CState", 0, verbose=False)
        rows = []
        for a in addrs:
            sid = p.read_u32(a + OFF_STATE_ID)
            if sid is None:
                continue
            rows.append((
                sid,
                p.read_u32(a + OFF_STATE_OWNER),
                p.read_u32(a + OFF_STATE_PREV_OWNER),
                p.read_u32(a + OFF_STATE_CAPITAL),
                p.read_u32(a + OFF_STATE_UNKNOWN),
                p.read_u32(a + OFF_STATE_ARABLE),
                (p.read_i64(a + OFF_POP_LOWER) or 0),
                (p.read_i64(a + OFF_POP_MIDDLE) or 0),
                (p.read_i64(a + OFF_POP_UPPER) or 0),
                (p.read_i64(a + OFF_POP_RADICALS) or 0),
                (p.read_i64(a + OFF_POP_UNEMPLOYED) or 0),
                (p.read_i64(a + OFF_POP_LABORER) or 0),
                (p.read_i64(a + OFF_STATE_INFRA_USED) or 0) / FIXED,
                (p.read_i64(a + OFF_STATE_INFRA) or 0) / FIXED,
                a,
            ))
        if args.owner is not None:
            rows = [r for r in rows if r[1] == args.owner]
        if args.id is not None:
            rows = [r for r in rows if r[0] == args.id]
        if getattr(args, "max_id", 0):
            rows = [r for r in rows if r[0] <= args.max_id]
        rows.sort()
        if getattr(args, "ids_only", False):
            print(" ".join(str(r[0]) for r in rows))
            return 0
        print("CState %d 个%s" % (len(rows),
                                "（owner=%d）" % args.owner if args.owner is not None else ""))
        print("  %-6s %-7s %-9s %-8s %10s %8s %12s %10s %10s %10s %s"
              % ("id", "owner", "capital", "arable", "infra_used", "infra", "pop", "radicals",
                 "unemployed", "laborers", "addr"))
        for (sid, ow, pv, cap, unk, ar, pl, pm, pu, rad, unemp, lab, used, infra, a) in \
                rows[:args.limit or len(rows)]:
            print("  %-6d %-7s %-9s %-8s %10.2f %8.2f %12d %10d %10d %10d 0x%X"
                  % (sid, ow, cap, ar, used, infra, pl + pm + pu, rad, unemp, lab, a))
        if args.csv:
            import csv
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["state_id", "owner", "prev_owner", "capital",
                            "unknown_0xEC4", "arable_land", "pop_lower", "pop_middle",
                            "pop_upper", "radicals", "unemployed", "laborers", "infrastructure", "infra_used", "addr"])
                for r in rows:
                    w.writerow(list(r[:14]) + ["0x%X" % r[14]])
            print("\n已写出 %s（%d 行）" % (args.csv, len(rows)))
        return 0
    finally:
        p.close()


# ------------------------------------------------------------ professions
OFF_PROF_WORKFORCE = 0x1308
OFF_PROF_POP = 0x1320
OFF_PROF_QUALS = 0x1368


def _read_i64_vec(p, addr):
    ptr = p.read_u64(addr)
    n = p.read_u32(addr + 8)
    if not ptr or not n or n > 200:
        return []
    raw = p.read(ptr, 8 * n)
    if not raw:
        return []
    return list(struct.unpack_from("<%dq" % n, raw, 0))


def cmd_quals(args):
    """按州打印各职业的人口 / 劳动力 / 资质人数。"""
    global _DEDUPE_BY
    if _DEDUPE_BY is None:
        _DEDUPE_BY = OFF_STATE_ID
    p = _attach(args)
    try:
        addrs = _instances_of(p, "CState", 0, verbose=False)
        target = None
        for a in addrs:
            if p.read_u32(a + OFF_STATE_ID) == args.state:
                target = a
                break
        if target is None:
            print("找不到州 %d" % args.state)
            return 1
        wf = _read_i64_vec(p, target + OFF_PROF_WORKFORCE)
        pp = _read_i64_vec(p, target + OFF_PROF_POP)
        ql = _read_i64_vec(p, target + OFF_PROF_QUALS)
        print("州 %d（对象 0x%X）各职业数据" % (args.state, target))
        print("  %-6s %14s %14s %14s" % ("职业id", "人口", "劳动力", "资质人数"))
        for i in range(max(len(wf), len(pp), len(ql))):
            a1 = pp[i] if i < len(pp) else 0
            a2 = wf[i] if i < len(wf) else 0
            a3 = ql[i] if i < len(ql) else 0
            if not (a1 or a2 or a3):
                continue
            print("  %-6d %14d %14d %14d" % (i, a1, a2, a3))
        if args.csv:
            import csv
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["profession_id", "population", "workforce", "qualifications"])
                for i in range(max(len(wf), len(pp), len(ql))):
                    w.writerow([i,
                                pp[i] if i < len(pp) else 0,
                                wf[i] if i < len(wf) else 0,
                                ql[i] if i < len(ql) else 0])
            print("\n已写出 %s" % args.csv)
        return 0
    finally:
        p.close()


def _player_country_id(p):
    addrs = _instances_of(p, "CJominiPlayer", 0, verbose=False)
    for a in addrs:
        cid = p.read_u32(a + 0x58)
        if cid is not None:
            return cid
    return None


def cmd_report(args):
    """一体化快照：玩家国家 + 财政 + 州汇总 + 建筑汇总。"""
    global _DEDUPE_BY
    p = _attach(args)
    try:
        cid = args.country if args.country is not None else _player_country_id(p)
        print("== 玩家 ==")
        print("  国家 id  %s" % cid)

        _DEDUPE_BY = OFF_COUNTRY_ID
        caddrs = _instances_of(p, "CCountry", 0, verbose=False)
        caddr = None
        for a in caddrs:
            if p.read_u32(a + OFF_COUNTRY_ID) == cid:
                caddr = a
                break
        if caddr:
            gold = p.read_i64(caddr + OFF_GOLD)
            inc = [p.read_i64(caddr + OFF_INCOME + 8 * i) for i in range(N_INCOME)]
            exp = [p.read_i64(caddr + OFF_EXPENSES + 8 * i) for i in range(N_EXPENSES)]
            print("\n== 财政 ==")
            print("  黄金储备  %16.2f" % _pounds(gold))
            print("  周收入    %16.2f" % _pounds(sum(inc)))
            print("  周支出    %16.2f" % _pounds(sum(exp)))
            print("  周结余    %16.2f" % _pounds(sum(inc) - sum(exp)))

        _DEDUPE_BY = OFF_STATE_ID
        saddrs = _instances_of(p, "CState", 0, verbose=False)
        n_state = pop = rad = unemp = lab = 0
        arable = 0
        for a in saddrs:
            if p.read_u32(a + OFF_STATE_OWNER) != cid:
                continue
            n_state += 1
            pop += ((p.read_i64(a + OFF_POP_LOWER) or 0)
                    + (p.read_i64(a + OFF_POP_MIDDLE) or 0)
                    + (p.read_i64(a + OFF_POP_UPPER) or 0))
            rad += p.read_i64(a + OFF_POP_RADICALS) or 0
            unemp += p.read_i64(a + OFF_POP_UNEMPLOYED) or 0
            lab += p.read_i64(a + OFF_POP_LABORER) or 0
            arable += p.read_u32(a + OFF_STATE_ARABLE) or 0
        print("\n== 州 ==")
        print("  州数 %d   总人口 %d   耕地 %d" % (n_state, pop, arable))
        print("  激进派 %d   失业 %d   劳工 %d" % (rad, unemp, lab))
        if pop:
            print("  激进派占比 %.2f%%" % (100.0 * rad / pop))
            if pop > 10 ** 12 or arable > 10 ** 6:
                print("\n  [警告] 数值异常（读到了失效地址）。请加 --refresh 重扫一次。")

        _DEDUPE_BY = OFF_BLD_ID
        baddrs = _instances_of(p, "CBuilding", 0, verbose=False)
        n_bld = lv = 0
        profit = 0
        # 建筑按「所在州是否属于该国」筛选
        own_states = set()
        for a in saddrs:
            if p.read_u32(a + OFF_STATE_OWNER) == cid:
                own_states.add(p.read_u32(a + OFF_STATE_ID))
        for a in baddrs:
            if p.read_u32(a + OFF_BLD_STATE) not in own_states:
                continue
            n_bld += 1
            lv += p.read_u32(a + OFF_BLD_LEVELS) or 0
            profit += p.read_i64(a + OFF_BLD_PROFIT) or 0
        print("\n== 建筑 ==")
        print("  建筑数 %d   总等级 %d   周利润合计 %.2f" % (n_bld, lv, _pounds(profit)))
        return 0
    finally:
        p.close()


# ------------------------------------------------------------------ verify
def cmd_verify(args):
    """不依赖存档的自检：用 id 列的唯一性和上界判断基址是否偏移。

    注意不能用"严格连续"当判据——国家被吞并、新国家建立都会让 id 出现空洞。
    只用两条：唯一性 >= 95%，以及最大值不超过对象数的 10 倍（或 4096）。
    """
    global _DEDUPE_BY
    p = _attach(args)
    specs = [
        ("CState", 0x10),
        ("CCountry", OFF_COUNTRY_ID),
        ("CBuilding", OFF_BLD_ID),
        ("CMarket", OFF_MKT_ID),
    ]
    bad = 0
    try:
        for cls, off in specs:
            _DEDUPE_BY = off          # 先去重，排除残留/子对象
            try:
                addrs = _instances_of(p, cls, 0, verbose=False)
            except SystemExit:
                print("%-10s 跳过（没有这个类）" % cls)
                continue
            if not addrs:
                print("%-10s 无实例" % cls)
                continue
            ids = [v for v in (p.read_u32(a + off) for a in addrs) if v is not None]
            n = len(ids)
            uniq = len(set(ids))
            mn, mx = (min(ids), max(ids)) if ids else (0, 0)
            limit = max(4096, 10 * n)
            outliers = sum(1 for v in ids if v > limit)
            # 容忍极少量残留对象（<=1% 且至少 1 个），但多数越界就说明基址错了
            ok = (uniq >= 0.95 * n) and outliers <= max(1, int(0.01 * n))
            print("%-10s 去重后 %-6d id偏移 +0x%-4X  唯一 %-6d 范围 %d~%d  越界 %d 个  %s"
                  % (cls, n, off, uniq, mn, mx, outliers,
                     "OK" if ok else "!! 基址可能偏移"))
            if not ok:
                bad += 1
        if bad:
            print("\n有 %d 个类的 id 列不正常：基址可能整体偏了（常见 ±0x10）。" % bad)
            print("用存档真值锚定一次即可，例如 hunt CCountry --value <国库值> --tol 0 --kind i64 --len 0x2000")
        else:
            print("\n全部通过：id 列唯一且上界合理，基址正确。")
        return 0 if not bad else 1
    finally:
        p.close()


# ------------------------------------------------------------------ market
OFF_MKT_ID = 0x10
OFF_MKT_OWNER = 0x838
OFF_MKT_DEMAND = 0x1D0      # 与价格倍率正相关 +0.995
OFF_MKT_SUPPLY = 0x220      # 与需求配对的供给
OFF_MKT_DEMAND2 = 0xE0      # 需求的另一份（数值几乎相同）
OFF_MKT_SUPPLY2 = 0x40      # 供给的另一份


def _vec_i64(p, base, off):
    ptr = p.read_u64(base + off)
    n = p.read_u32(base + off + 8)
    if not ptr or not n or n > 400:
        return []
    raw = p.read(ptr, 8 * n)
    if not raw:
        return []
    return list(struct.unpack_from("<%dq" % n, raw, 0))


def _qty_raw(x):
    """原始人数按 K/M/B 显示（不做定点换算）。"""
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return "%.1f%s" % (x / div, unit)
    return "%d" % x


def _qty(v):
    """把定点数量 ÷1e5 后按 K/M/B 显示。"""
    x = v / FIXED
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return "%.1f%s" % (x / div, unit)
    return "%.0f" % x


def cmd_market(args):
    """按商品列出：价格、倍率、需求、供给、缺口。"""
    global _DEDUPE_BY
    p = _attach(args)
    try:
        _DEDUPE_BY = None
        if args.id is None:
            cid = _player_country_id(p)
            addrs = _instances_of(p, "CMarket", 0, verbose=False)
            owner = None
            for a in addrs:
                if p.read_u32(a + OFF_MKT_OWNER) == cid:
                    owner = a
                    break
            mid = p.read_u32(owner + OFF_MKT_ID) if owner else None
        else:
            mid = args.id
            addrs = _instances_of(p, "CMarket", 0, verbose=False)
            owner = None
            for a in addrs:
                if p.read_u32(a + OFF_MKT_ID) == mid:
                    owner = a
                    break
        if owner is None:
            print("找不到市场 %s" % mid)
            return 1
        prices = _vec_i64(p, owner, OFF_MARKET_PRICES)
        demand = _vec_i64(p, owner, OFF_MKT_DEMAND)
        supply = _vec_i64(p, owner, OFF_MKT_SUPPLY)
        goods = goods_table()
        print("市场 %d @ 0x%X   商品 %d   价格项 %d"
              % (mid, owner, len(goods), len(prices)))
        rows = []
        for i, g in enumerate(goods):
            name, base = g[0], g[1]
            pr = prices[i] / FIXED if i < len(prices) else 0.0
            dm = demand[i] if i < len(demand) else 0
            sp = supply[i] if i < len(supply) else 0
            rows.append((name, pr, base, dm, sp, dm - sp))
        if args.sort == "gap":
            rows.sort(key=lambda r: r[5], reverse=True)
        print("  %-20s %8s %6s %10s %10s %10s"
              % ("商品", "价格", "倍率", "供给", "需求", "缺口"))
        for name, pr, base, dm, sp, gap in rows[:args.limit or len(rows)]:
            print("  %-20s %8.2f %6.2f %10s %10s %10s"
                  % (name, pr, (pr / base if base else 0),
                     _qty(sp), _qty(dm), _qty(gap)))
        if args.csv:
            import csv
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["good", "price", "base", "demand", "supply", "gap"])
                for r in rows:
                    w.writerow([r[0], "%.2f" % r[1], r[2], r[3], r[4], r[5]])
            print("\n已写出 %s" % args.csv)
        return 0
    finally:
        p.close()


# ----------------------------------------------------------------- budget
FIXED = 100000.0          # 财政数值的定点除数（debugcountrybudgets 里 qword_144ADC980）

# 由 debugcountrybudgets 反推的 CCountry 布局
OFF_COUNTRY_ID = 0x08
OFF_BUDGET = 0x12D8
OFF_GOLD = 0x12F8         # budget+0x20   （已实测验证）
OFF_CREDIT_LIMIT = 0x2198  # budget+0xEC0
OFF_PRINCIPAL = 0x21A0     # budget+0xEC8
OFF_EXPENSES = 0x1D50      # budget+0xA78, 22 × i64
N_EXPENSES = 22
OFF_INCOME = 0x1E00        # budget+0xB28, 16 × i64
N_INCOME = 16
OFF_CONSTRUCTION_PTR = 0x9F0


def _pounds(v):
    return v / FIXED


def cmd_budget(args):
    """打印一个国家的完整预算（数值已按定点换算成镑）。"""
    p = _attach(args)
    try:
        if args.addr:
            addr = int(args.addr, 0)
        else:
            _set_dedupe(OFF_COUNTRY_ID, _DEDUPE_KEEP)
            addrs = _instances_of(p, "CCountry", 0)
            addr = None
            for a in addrs:
                if p.read_u32(a + OFF_COUNTRY_ID) == args.id:
                    addr = a
                    break
            if addr is None:
                print("没找到 id=%d 的国家" % args.id)
                return 1
        cid_read = p.read_u32(addr + OFF_COUNTRY_ID)
        print("CCountry @ 0x%X  id=%s" % (addr, cid_read))
        if not (0 <= (cid_read or -1) < 100000):
            print("  [警告] id 不在合理范围，布局可能偏移，请用国库真值重新锚定")

        gold = p.read_i64(addr + OFF_GOLD)
        print("\n== 财政 ==")
        print("  黄金储备  %20s  (raw %d)" % ("%.2f" % _pounds(gold), gold))
        print("  信贷额度  %20.2f" % _pounds(p.read_i64(addr + OFF_CREDIT_LIMIT)))
        print("  贷款本金  %20.2f" % _pounds(p.read_i64(addr + OFF_PRINCIPAL)))

        inc = [p.read_i64(addr + OFF_INCOME + 8 * i) for i in range(N_INCOME)]
        exp = [p.read_i64(addr + OFF_EXPENSES + 8 * i) for i in range(N_EXPENSES)]
        inc = [x if x else 0 for x in inc]
        exp = [x if x else 0 for x in exp]
        print("\n== 收入分项（16 项，+0x%X）==" % OFF_INCOME)
        for i, v in enumerate(inc):
            if v:
                print("  [%2d] %18.2f" % (i, _pounds(v)))
        print("  合计 %20.2f" % _pounds(sum(inc)))
        print("\n== 支出分项（22 项，+0x%X）==" % OFF_EXPENSES)
        for i, v in enumerate(exp):
            if v:
                print("  [%2d] %18.2f" % (i, _pounds(v)))
        print("  合计 %20.2f" % _pounds(sum(exp)))
        print("\n== 平衡 ==\n  收入-支出 %16.2f" % _pounds(sum(inc) - sum(exp)))
        return 0
    finally:
        p.close()


# ------------------------------------------------------------------- dump
def _hexdump(proc, obj, length):
    data = proc.read(obj, length)
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexpart = " ".join("%02x" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print("  +%04X  %-47s  %s" % (i, hexpart, asc))


CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cache")
_REFRESH = False
_ALL_VTABLES = False
_DEDUPE_BY = None
_DEDUPE_KEEP = "auto"


def _set_refresh(flag):
    global _REFRESH
    _REFRESH = bool(flag)


def _set_dedupe(offset, keep="auto"):
    global _DEDUPE_BY, _DEDUPE_KEEP
    _DEDUPE_BY = offset
    _DEDUPE_KEEP = keep or "auto"


def _set_all_vtables(flag):
    global _ALL_VTABLES
    _ALL_VTABLES = bool(flag)


def _cache_path(cls_name, pid, base, start=0):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except OSError:
        return None
    return os.path.join(CACHE_DIR, "%s_%d_%X_%X.json" % (cls_name, pid, base, start))


def _load_cache(path, p, vset):
    """读缓存并校验：抽查 32 个均匀分布的位置，首 8 字节必须仍是本类 vtable。

    读档/换局后对象会被释放重建，地址全部失效；只查前 8 个容易蒙混过关，
    所以这里均匀取样并要求全部命中。若启用了按 id 去重，还额外检查
    抽样对象的 id 是否在合理范围（防止读到已释放但 vtable 尚存的对象）。
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            addrs = json.load(f).get("addrs") or []
    except Exception:
        return None
    if not addrs:
        return None
    n = len(addrs)
    idx = sorted({i * n // 32 for i in range(min(32, n))})
    for i in idx:
        if p.read_u64(addrs[i]) not in vset:
            return None
    if _DEDUPE_BY is not None:
        good = 0
        for i in idx:
            v = p.read_u32(addrs[i] + _DEDUPE_BY)
            if v is not None and 0 <= v < (1 << 20):
                good += 1
        if good < len(idx):
            return None
    return addrs


def _save_cache(path, addrs):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"addrs": addrs, "time": time.time()}, f)
    except OSError:
        pass


def _instances_of(p, cls_name, limit=0, vs=None, verbose=False, refresh=False,
                  all_vtables=None):
    """取某个类的对象基址。

    多继承对象在 +0/+8/+0x10 各有一个 vtable 指针，扫描会把它们都算成命中。
    判定基址的办法：若 A-8 或 A-0x10 处的 8 字节仍是本类的 vtable，
    说明 A 是子对象而不是对象起点，跳过。
    """
    if vs is None:
        vs = _selected("^%s$" % re.escape(cls_name), p.module_base("victoria3.exe"))
    if not vs:
        raise SystemExit("没有这个类：%s" % cls_name)
    vset = {v for v, n in vs.items() if n == cls_name}
    # 默认只扫主 vtable：多继承/内嵌子对象会让同一对象出现多处命中，
    # 用全部 vtable 会把对象数算多（CState 会变成 2330 而不是 1156）。
    # 但有些类（如 CDecree）多数实例挂在次要 vtable 上，只扫主表只能找到极少数，
    # 这种类要显式传 all_vtables=True。
    use_all = _ALL_VTABLES if all_vtables is None else bool(all_vtables)
    if not use_all:
        primary = next(iter(vset))
        vset = {primary}
        vs = {primary: cls_name}
    base = p.module_base("victoria3.exe")
    cpath = _cache_path(cls_name + ("_a" if use_all else "_p")
                        + (("_d%X%s" % (_DEDUPE_BY, _DEDUPE_KEEP[0])) if _DEDUPE_BY is not None else ""),
                        p.pid, base, getattr(p, "start_time", lambda: 0)())
    refresh = refresh or _REFRESH
    bases = None if refresh else _load_cache(cpath, p, vset)
    if bases is not None:
        if verbose:
            print("  使用缓存：%d 个对象（跳过扫描）" % len(bases))
    else:
        if refresh or verbose:
            print("  正在扫描内存定位 %s 实例（约 60~120 秒，请等待）…" % cls_name, flush=True)
        hits = discover.scan_instances(p, vs, progress=False)
        addrs = sorted(set(hits.get(cls_name, [])))
        # 多继承对象在 +0/+8/+0x10 各有一个 vtable 指针；同一对象的命中彼此间距很小。
        # 统一按「0x10 内的命中归为一个对象，取最小地址」来定基址，
        # 比逐个检查 A-8/A-0x10 更稳（后者对 CState 会算重）。
        n_obj, bases = discover.cluster_count(addrs, window=0x10)
        if verbose:
            print("  vtable %d 张，原始命中 %d 处 -> 对象 %d 个"
                  % (len(vset), len(addrs), n_obj))
            if len(addrs) > 1:
                gaps = sorted({addrs[i + 1] - addrs[i] for i in range(min(len(addrs) - 1, 400))})
                print("  命中间距分布(前8种):", ["0x%X" % g for g in gaps[:8]])
        _save_cache(cpath, bases)
    if _DEDUPE_BY is not None:
        groups = {}
        for a in bases:
            v = p.read_u32(a + _DEDUPE_BY)
            if v is None:
                continue
            groups.setdefault(v, []).append(a)
        uniq = []
        bad = 0
        for v, cands in groups.items():
            if _DEDUPE_KEEP == "auto":
                # 优先挑通过合理性校验的那个；都不通过则退回 last
                pick = next((c for c in cands if _sane(p, cls_name, c)), cands[-1])
                if not _sane(p, cls_name, pick):
                    bad += 1
            elif _DEDUPE_KEEP == "last":
                pick = cands[-1]
            else:
                pick = cands[0]
            uniq.append(pick)
        uniq.sort()
        if verbose:
            extra = "" if _DEDUPE_KEEP != "auto" else "，不合理 %d" % bad
            print("  按 +0x%X 去重(keep=%s)：%d -> %d%s"
                  % (_DEDUPE_BY, _DEDUPE_KEEP, len(bases), len(uniq), extra))
        bases = uniq
    return bases[:limit] if limit and limit > 0 else bases


def cmd_dump(args):
    p = _attach(args)
    try:
        if args.cls.startswith("0x") or args.cls.startswith("0X") or args.cls.isdigit():
            addrs = [int(args.cls, 0)]
            print("直接地址 0x%X" % addrs[0])
        else:
            addrs = _instances_of(p, args.cls, args.limit)
            print("%s 实例 %d 个（显示前 %d）" % (args.cls, len(addrs), len(addrs)))
        for a in addrs:
            print("\n实例 0x%X" % a)
            _hexdump(p, a + args.offset, args.len)
    finally:
        p.close()
    return 0


# ------------------------------------------------------------------- find
def cmd_find(args):
    """给定游戏里看到的一个数字，反查它属于哪个类的哪个偏移。"""
    p = _attach(args)
    try:
        base = p.module_base("victoria3.exe")
        # 反查容器要覆盖所有类：金额可能躺在 CCountryBudget 之类里，不在核心名单中
        vt = _selected(args.classes or ".*", base)
        print("pid=%d base=0x%X  已知 vtable %d 张" % (p.pid, base, len(vt)))
        kinds = (["i32", "i64", "f32", "f64"] if args.kind == "auto" else [args.kind])
        tol = args.tol
        located = []
        t0 = time.time()
        if tol:
            specs = [(k, discover.DTYPES[k][0], discover.DTYPES[k][1],
                      args.value - tol, args.value + tol) for k in kinds]
            print("\n一趟扫描，区间匹配：%s ∈ [%s, %s]"
                  % ("/".join(kinds), args.value - tol, args.value + tol))
            found = discover.find_range_multi(p, specs)
            print("命中 %s" % ("  ".join("%s=%d" % (k, len(v)) for k, v in found.items())))
            for kind, hits in found.items():
                if hits:
                    located += [(kind,) + x for x in
                                discover.locate_objects(p, hits, vt, max_hits=args.max_hits)]
        else:
            specs = [(k, discover._bits(k, args.value), "<u%d" % discover.DTYPES[k][1],
                      discover.DTYPES[k][1]) for k in kinds]
            print("\n一趟扫描，精确匹配：%s = %s" % ("/".join(kinds), args.value))
            found = discover.find_multi(p, specs)
            print("命中 %s" % ("  ".join("%s=%d" % (k, len(v)) for k, v in found.items())))
            for kind, hits in found.items():
                if hits:
                    located += [(kind,) + x for x in
                                discover.locate_objects(p, hits, vt, max_hits=args.max_hits)]
        print("总计用时 %.1fs" % (time.time() - t0))
        agg = {}
        for kind, cls, off, _obj in located:
            agg[(kind, cls, off)] = agg.get((kind, cls, off), 0) + 1
        rows = sorted(agg.items(), key=lambda kv: -kv[1])
        print("\n按 (类型, 类, 对象内偏移) 聚合，取前 %d：" % args.top)
        print("  %-6s %-34s %-10s %s" % ("类型", "类", "偏移", "命中数"))
        for (kind, cls, off), n in rows[:args.top]:
            print("  %-6s %-34s +0x%-8X %d" % (kind, cls, off, n))
        if not rows:
            print("  （没有命中落在已知类对象里——用 --classes 放宽，或换个数值）")
        print("\n下一步：用 read 验证，例如")
        if rows:
            kind, cls, off = rows[0][0]
            print("  python -m v3live read %s --offset 0x%X --kind %s" % (cls, off, kind))
    finally:
        p.close()
    return 0


# ------------------------------------------------------------------- peek
def cmd_peek(args):
    """直接盯住一个地址的某个字段（不再重复扫描），用来验证字段是否"活"。"""
    p = _attach(args)
    try:
        addr = int(args.addr, 0) if isinstance(args.addr, str) else args.addr
        kind = args.kind
        fmt = {"i32": "<i", "u32": "<I", "i64": "<q", "u64": "<Q",
               "f32": "<f", "f64": "<d"}[kind]
        sz = struct.calcsize(fmt)
        prev = None
        for rnd in range(1, args.rounds + 1):
            raw = p.read(addr + args.offset, sz)
            v = struct.unpack(fmt, raw)[0]
            delta = "" if prev is None else "   Δ=%s" % (round(v - prev, 4) if kind.startswith("f") else v - prev)
            print("  #%d  0x%X+0x%X = %s%s" % (rnd, addr, args.offset, v, delta))
            prev = v
            if rnd < args.rounds:
                time.sleep(args.interval)
    finally:
        p.close()
    return 0


# ----------------------------------------------------------------- findid
def cmd_findid(args):
    """按国家 id 找出该对象，并定位"id 字段"的偏移。

    判据很强：某个偏移上恰好一个实例等于目标 id，且该偏移在全部实例上
    取值互不相同（id 是唯一键，而 flag/enum 会大量重复）。
    """
    p = _attach(args)
    try:
        base = p.module_base("victoria3.exe")
        vs = _selected("^%s$" % re.escape(args.cls), base)
        if not vs:
            print("没有这个类：%s" % args.cls)
            return 1
        vset = {v for v, n in vs.items() if n == args.cls}
        hits = discover.scan_instances(p, vs, progress=False)
        raw = sorted(set(hits.get(args.cls, [])))
        if args.raw:
            addrs = raw
        else:
            addrs = [a for a in raw if p.u64(a - 8) not in vset and p.u64(a - 0x10) not in vset]
        print("%s：原始命中 %d，候选对象 %d（--raw=%s）"
              % (args.cls, len(raw), len(addrs), args.raw))
        bufs = [p.read(a, args.len) for a in addrs]
        found = []
        for off in range(0, args.len - 4 + 1, 4):
            vals = [struct.unpack_from("<i", b, off)[0] for b in bufs]
            if vals.count(args.id) != 1:
                continue
            uniq = len(set(vals))
            found.append((uniq, off, addrs[vals.index(args.id)]))
        found.sort(reverse=True)
        if not found:
            print("  没有找到恰好一个实例等于 %d 的偏移" % args.id)
            return 1
        print("\n  %-6s %-10s %s" % ("唯一值数", "偏移", "该实例地址"))
        for uniq, off, a in found[:args.top]:
            tag = "  <- 全部唯一，几乎肯定是 id 字段" if uniq == len(addrs) else ""
            print("  %-6s +0x%-8X 0x%X%s" % ("%d/%d" % (uniq, len(addrs)), off, a, tag))
        uniq, off, a = found[0]
        print("\n下一步：")
        print("  python -m v3live explore 0x%X --value %s --tol 30000 --len 0x5000"
              % (a, args.next_value))
        return 0
    finally:
        p.close()
    return 0


# ----------------------------------------------------------------- explore
def cmd_explore(args):
    """只扫一个对象的内部偏移，找值落在区间的位置。

    比 hunt 更聚焦：先用一个唯一的数值（如信用额度）定位到具体国家，
    再在这个对象内部找第二个数值（如国库），两步交叉即可锁定字段。
    --auto-scale 会顺带试定点/千单位等换算，省去反复换数值。
    """
    p = _attach(args)
    try:
        addr = int(args.addr, 0)
        kinds = (["i32", "i64", "f32", "f64"] if args.kind == "auto" else [args.kind])
        scales = args.scales if args.auto_scale else [1.0]
        buf = p.read(addr, args.len)
        print("对象 0x%X，读 0x%X 字节，目标 %s ±%s，换算 %s"
              % (addr, args.len, args.value, args.tol, scales))
        bool_dummy = None
        rows = []
        if args.byte_mode:
            target = int(args.value)
            if not (0 <= target <= 255):
                print("--byte 模式下值必须在 0..255")
                return 1
            buf = p.read(addr, args.len)
            for i, ch in enumerate(buf):
                if ch == target:
                    rows.append((i, "u8", target, 1.0))
            if not rows:
                print("  没有命中")
                return 1
            print("  %-10s %-6s %s" % ("偏移", "类型", "值"))
            for off, kind, v, _s in rows[:args.top]:
                print("  +0x%-8X %-6s %s" % (off, kind, v))
            return 0
        for scale in scales:
            tgt = args.value * scale
            tol = max(args.tol * scale, 1e-6)
            lo, hi = tgt - tol, tgt + tol
            for kind in kinds:
                dt, sz = discover.DTYPES[kind]
                cut = len(buf) // sz * sz
                for i in range(0, cut, sz):
                    v = struct.unpack_from(STRUCT[kind], buf, i)[0]
                    if lo <= v <= hi:
                        rows.append((i, kind, v, scale))
        if not rows:
            print("  没有命中")
            return 1
        print("  %-10s %-6s %-8s %s" % ("偏移", "类型", "换算", "值"))
        for off, kind, v, scale in sorted(rows)[:args.top]:
            print("  +0x%-8X %-6s x%-8g %s" % (off, kind, scale, v))
        off, kind, _v, _s = sorted(rows)[0]
        print("\n验证：python -m v3live peek 0x%X --offset 0x%X --kind %s --interval 2 --rounds 10"
              % (addr, off, kind))
    finally:
        p.close()
    return 0


# ------------------------------------------------------------------- hunt
def cmd_hunt(args):
    """在某个类的所有实例里找「值落在区间」的偏移。

    比全内存盲扫精确得多：候选空间只有 实例数 × 对象长度/4，
    而且每个偏移的命中数直接告诉你它是全局标量（多实例同时命中）
    还是某个国家独有的字段（1 个实例命中）。
    """
    p = _attach(args)
    try:
        insts = _instances_of(p, args.cls, args.limit, verbose=True)
        if not insts:
            print("没有实例")
            return 1
        kinds = (["i32", "i64", "f32", "f64"] if args.kind == "auto" else [args.kind])
        scales = args.scales if args.auto_scale else [1.0]
        values = args.value if isinstance(args.value, list) else [args.value]
        print("%s：%d 个实例，读 0x%X 字节/实例，目标 %s ±%s，换算 %s"
              % (args.cls, len(insts), args.len, values, args.tol, scales))
        agg = {}
        for val in values:
            for scale in scales:
                lo = val * scale - max(args.tol * scale, 1e-6)
                hi = val * scale + max(args.tol * scale, 1e-6)
                res = discover.hunt_in_instances(p, insts, kinds, lo, hi, args.len)
                for (kind, off), info in res.items():
                    agg[(kind, off, scale, val)] = info
        rows = sorted(agg.items(), key=lambda kv: -kv[1]["count"])
        print("\n%-6s %-10s %-8s %-8s %-14s %s"
              % ("类型", "偏移", "命中数", "换算", "目标值", "样例"))
        for (kind, off, scale, val), info in rows[:args.top]:
            print("  %-6s +0x%-8X %-8d x%-8g %-14s 实例 0x%X = %s"
                  % (kind, off, info["count"], scale, val, info["addr"], info["value"]))
        if not rows:
            print("  （该类对象里没有落在这个区间的值）")
        else:
            kind, off, scale, val = rows[0][0]
            print("\n下一步：python -m v3live read %s --offset 0x%X --kind %s --limit %d"
                  % (args.cls, off, kind, len(insts)))
    finally:
        p.close()
    return 0


# ----------------------------------------------------------------- prices
OFF_MARKET_PRICES = 0x6F0   # CMarket：54 × i64 定点价格（已实测确认）
GOODS_FILE = r"E:\SteamLibrary\steamapps\common\Victoria 3\game\common\goods\00_goods.txt"
_goods_cache = None


def goods_table():
    """按定义顺序解析商品：[(name, cost, category)]，下标即内存里的商品索引。"""
    global _goods_cache
    if _goods_cache is not None:
        return _goods_cache
    import re
    out = []
    try:
        lines = open(GOODS_FILE, encoding="utf-8", errors="ignore").read().split("\n")
    except OSError:
        _goods_cache = out
        return out
    depth = 0
    cur = None
    costs = {}
    cats = {}
    for ln in lines:
        code = ln.split("#")[0].rstrip()
        if not code.strip():
            continue
        m = re.match(r"\s*([A-Za-z_][\w]*)\s*=\s*\{", code)
        if m and depth == 0:
            cur = m.group(1)
            out.append(cur)
            depth = 1
            continue
        if depth == 1 and cur:
            mc = re.match(r"\s*cost\s*=\s*([\d.]+)", code)
            if mc:
                costs[cur] = float(mc.group(1))
            mk = re.match(r"\s*category\s*=\s*(\w+)", code)
            if mk:
                cats[cur] = mk.group(1)
        depth += code.count("{") - code.count("}")
        if depth < 0:
            depth = 0
    _goods_cache = [(n, costs.get(n), cats.get(n, "")) for n in out]
    return _goods_cache


def cmd_prices(args):
    """打印市场的 54 种商品价格（带商品名与基础成本）。"""
    p = _attach(args)
    try:
        if args.addr:
            a = int(args.addr, 0)
        else:
            insts = _instances_of(p, "CMarket", args.index + 1)
            if len(insts) <= args.index:
                print("市场实例不足")
                return 1
            a = insts[args.index]
        ptr = p.read_u64(a + OFF_MARKET_PRICES)
        cnt = p.read_u32(a + OFF_MARKET_PRICES + 8)
        print("CMarket 0x%X  价格表 0x%X  %d 项\n" % (a, ptr, cnt))
        if not (0x10000 <= ptr < 0x7FFFFFFF0000) or cnt > 200:
            print("  价格指针不合法，偏移可能已变")
            return 1
        data = p.read(ptr, cnt * 8)
        vals = struct.unpack_from("<%dq" % cnt, data, 0)
        goods = goods_table()
        print("  %-4s %-22s %10s %8s %7s" % ("idx", "商品", "价格", "基础价", "倍数"))
        for i, v in enumerate(vals):
            name, cost, _cat = goods[i] if i < len(goods) else ("?", None, "")
            price = v / FIXED
            ratio = ("%.2f" % (price / cost)) if cost else "-"
            print("  [%2d] %-22s %10.2f %8s %7s"
                  % (i, name, price, ("%g" % cost) if cost else "-", ratio))
        return 0
    finally:
        p.close()


OFF_STATE_LOCAL_PRICES = 0xA08     # 54 × i64 定点，本地价
OFF_STATE_MKT_SUPPLY = 0xD08       # 54 × i64，所在市场的供给
OFF_STATE_MKT_DEMAND = 0xD58       # 54 × i64，所在市场的需求


def cmd_localprices(args):
    """打印某州的本地价格，并与所在市场的价格对比。"""
    global _DEDUPE_BY
    if _DEDUPE_BY is None:
        _DEDUPE_BY = OFF_STATE_ID
    p = _attach(args)
    try:
        addrs = _instances_of(p, "CState", 0, verbose=False)
        target = None
        for a in addrs:
            if p.read_u32(a + OFF_STATE_ID) == args.state:
                target = a
                break
        if target is None:
            print("找不到州 %d" % args.state)
            return 1
        local = _vec_i64(p, target, OFF_STATE_LOCAL_PRICES)
        goods = goods_table()
        # 该州所在市场的价格
        cid = p.read_u32(target + OFF_STATE_OWNER)
        market = None
        _DEDUPE_BY = None
        for m in _instances_of(p, "CMarket", 0, verbose=False):
            if p.read_u32(m + OFF_MKT_OWNER) == cid:
                market = m
                break
        mp = _vec_i64(p, market, OFF_MARKET_PRICES) if market else []
        print("州 %d @ 0x%X  本地价 %d 项  市场价 %d 项"
              % (args.state, target, len(local), len(mp)))
        print("  %-22s %10s %10s %8s" % ("商品", "本地价", "市场价", "偏差"))
        rows = []
        for i, g in enumerate(goods):
            lp = local[i] / FIXED if i < len(local) else 0.0
            mkt = mp[i] / FIXED if i < len(mp) else 0.0
            rows.append((g[0], lp, mkt, (lp - mkt)))
        if args.sort == "dev":
            rows.sort(key=lambda r: -abs(r[3]))
        for name, lp, mkt, dev in rows[:args.limit or len(rows)]:
            print("  %-22s %10.2f %10.2f %+8.2f" % (name, lp, mkt, dev))
        return 0
    finally:
        p.close()


# --------------------------------------------------------------- modifiers
OFF_STATICMOD_NAME = 0x18    # 指向名字对象
OFF_STATICMOD_ICON = 0x90    # 指向图标路径字符串
MOD_CLS = "CStaticModifier::NJominiModifier"


def _first_string(p, ptr, limit=0x80):
    if not ptr:
        return None
    raw = p.read(ptr, limit)
    if not raw:
        return None
    m = re.search(rb"[ -~]{4,}", raw)
    return m.group(0).decode("latin-1") if m else None


def cmd_modifiers(args):
    """列出所有静态修正（名字 + 图标 + 数据文件里的效果）。"""
    global _DEDUPE_BY
    _DEDUPE_BY = None
    p = _attach(args)
    try:
        addrs = _instances_of(p, MOD_CLS, 0, verbose=False)
        print("%s：%d 个定义" % (MOD_CLS, len(addrs)))
        defs = {}
        try:
            import json
            import os as _os
            path = _os.path.join(CACHE_DIR, "static_modifiers.json")
            if _os.path.exists(path):
                defs = json.load(open(path, encoding="utf-8"))
        except Exception:
            defs = {}
        rows = []
        for a in addrs:
            name = _first_string(p, p.read_u64(a + OFF_STATICMOD_NAME))
            icon = _first_string(p, p.read_u64(a + OFF_STATICMOD_ICON), 0xC0)
            if not name:
                continue
            rows.append((name, icon or "", a))
        rows.sort()
        print("能读出名字的 %d 个（有数据文件定义的 %d 个）"
              % (len(rows), sum(1 for r in rows if r[0] in defs)))
        print("  %-40s %-6s %s" % ("修正名", "效果数", "图标"))
        for name, icon, a in rows[:args.limit or len(rows)]:
            eff = defs.get(name, {})
            n = len([k for k in eff if k not in ("icon", "_values")])
            print("  %-40s %-6s %s" % (name, n, icon.split("/")[-1] if icon else ""))
        if args.csv:
            import csv
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["modifier", "effects_count", "icon", "addr", "effects"])
                for name, icon, a in rows:
                    eff = {k: v for k, v in defs.get(name, {}).items()
                           if k not in ("icon", "_values")}
                    w.writerow([name, len(eff), icon, "0x%X" % a,
                                "; ".join("%s=%s" % kv for kv in eff.items())])
            print("\n已写出 %s（%d 行）" % (args.csv, len(rows)))
        return 0
    finally:
        p.close()


# ------------------------------------------------------------- timed mods
OFF_COUNTRY_TIMED = 0x730     # vector<*>: 临时（带期限）修正列表
OFF_TMOD_DEF = 0x00
OFF_TMOD_VALUE = 0x08
OFF_TMOD_START = 0x10
OFF_TMOD_END = 0x18
OFF_TDEF_ICON = 0x10


def _cstr(p, addr, maxlen=256):
    """读 C 字符串；长度未知时逐步加长，避免越界导致整段读失败。"""
    if not addr:
        return None
    for size in (64, 128, maxlen):
        raw = p.read(addr, size)
        if not raw:
            continue
        z = raw.find(b"\x00")
        if z >= 0:
            raw = raw[:z]
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _decode_date(v):
    """临时修正日期：低 32 位 = 小时计数，u16 = 日，u16 = 年。"""
    low = v & 0xFFFFFFFF
    day = (v >> 32) & 0xFFFF
    year = (v >> 48) & 0xFFFF
    return "%d" % year


def _duration_days(start, end):
    a = start & 0xFFFFFFFF
    b = end & 0xFFFFFFFF
    return (b - a) / 24.0


def cmd_timedmods(args):
    """列出某国当前的临时修正（带期限），含数值与起止日期。"""
    global _DEDUPE_BY
    p = _attach(args)
    try:
        cid = args.id if args.id is not None else _player_country_id(p)
        _DEDUPE_BY = OFF_COUNTRY_ID
        addrs = _instances_of(p, "CCountry", 0, verbose=False)
        addr = None
        for a in addrs:
            if p.read_u32(a + OFF_COUNTRY_ID) == cid:
                addr = a
                break
        if addr is None:
            print("找不到国家 %s" % cid)
            return 1
        _print_timed_list(p, addr, OFF_COUNTRY_TIMED,
                          "国家 %s @ 0x%X" % (cid, addr))
        return 0
    finally:
        p.close()


LAWS_DIR = os.path.join(os.environ.get("V3_GAME", r"E:\SteamLibrary\steamapps\common\Victoria 3\game"),
                        "common", "laws")
_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SAVE = os.path.join(_PROJ, "tmp", "save", "save.txt")


TECH_DIR = os.path.join(os.environ.get(
    "V3_GAME", r"E:\SteamLibrary\steamapps\common\Victoria 3\game"),
    "common", "technology", "technologies")

PM_DIR = os.path.join(os.environ.get(
    "V3_GAME", r"E:\SteamLibrary\steamapps\common\Victoria 3\game"),
    "common", "production_methods")

STATIC_DIR = os.path.join(os.environ.get(
    "V3_GAME", r"E:\SteamLibrary\steamapps\common\Victoria 3\game"),
    "common", "static_modifiers")

DECREE_DIR = os.path.join(os.environ.get(
    "V3_GAME", r"E:\SteamLibrary\steamapps\common\Victoria 3\game"),
    "common", "decrees")


def _all_decrees(p):
    """所有地方法令：[(国家 id, 州 id, 法令名)]，基址用字段自校验。

    CDecree 有 2 张 vtable，多数实例挂在次要表上，所以扫全部 vtable。
    多 vtable 时「簇内最小地址」未必是对象起点（次要 vtable 可能就在 +8），
    因此对每个候选地址试几个可能的基址，用「能读出 decree_ 开头的类型名」
    自校验，而不是盲信固定偏移。
    """
    from collections import Counter
    out = []
    seen = set()
    for a in _instances_of(p, "CDecree", 0, verbose=False, all_vtables=True):
        for base in (a, a - 8, a - 0x10, a + 8, a + 0x10):
            if base in seen:
                continue
            buf = p.read(base, 0x28)
            if len(buf) < 0x28:
                continue
            tp = struct.unpack_from("<Q", buf, 0x10)[0]
            if not tp:
                continue
            nm = p.msvc_string(tp + 0x18)
            if not nm or not nm.startswith("decree_"):
                continue
            seen.add(base)
            out.append((struct.unpack_from("<I", buf, 0x08)[0],
                        struct.unpack_from("<I", buf, 0x0C)[0], nm))
            break
    return out


def _country_decrees(p, cid):
    """该国生效的地方法令：[(法令名, 州 id)]。"""
    return [(nm, st) for c, st, nm in _all_decrees(p) if c == cid]


def _load_static_mods():
    """解析 common/static_modifiers/*.txt：静态修正名 -> {修正键: 值}。

    其中 base_values 是全局基线，提供
      country_bureaucracy_add = 100 / country_authority_add = 100 /
      country_minting_add = 500 / country_weekly_innovation_add = 50 等。
    工具提示里「+500 来自基础值」「+100」这类就是这个。
    """
    import json
    cache = os.path.join(CACHE_DIR, "static_mods.json")
    if os.path.exists(cache):
        try:
            return json.load(open(cache, encoding="utf-8"))
        except Exception:
            pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gamedata
    raw = gamedata.parse_dir(STATIC_DIR)
    out = {}
    for name, d in raw.items():
        if not isinstance(d, dict):
            continue
        vals = {k: v for k, v in d.items()
                if not k.startswith("_") and not isinstance(v, dict)}
        if vals:
            out[name] = vals
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    return out


def _load_pms():
    """解析 common/production_methods/*.txt：PM 名 -> 各作用域的修正。

    结构：
        pm_xxx = {
            country_modifiers = { workforce_scaled = { country_construction_add = 2 }
                                  level_scaled = {...}  unscaled = {...} }
            state_modifiers   = { ... }
            building_modifiers= { ... }
        }
    workforce_scaled 按就业规模缩放，level_scaled 按建筑等级，unscaled 不缩放。
    本命令只取 country_modifiers / state_modifiers 里与我们关心的键。
    """
    import json
    cache = os.path.join(CACHE_DIR, "pms.json")
    if os.path.exists(cache):
        try:
            return json.load(open(cache, encoding="utf-8"))
        except Exception:
            pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gamedata
    raw = gamedata.parse_dir(PM_DIR)
    scopes = ("country_modifiers", "state_modifiers", "building_modifiers")
    scales = ("workforce_scaled", "level_scaled", "unscaled")
    out = {}
    for name, d in raw.items():
        if not isinstance(d, dict):
            continue
        rec = {}
        for sc in scopes:
            blk = d.get(sc)
            if not isinstance(blk, dict):
                continue
            for scale in scales:
                sub = blk.get(scale)
                if isinstance(sub, dict):
                    vals = {k: v for k, v in sub.items() if not k.startswith("_")}
                    if vals:
                        rec.setdefault(sc, {})[scale] = vals
        if rec:
            out[name] = rec
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    return out


def _load_techs():
    """解析 technology/technologies/*.txt：科技名 -> {era, category, modifiers}。"""
    import json
    cache = os.path.join(CACHE_DIR, "technologies.json")
    if os.path.exists(cache):
        try:
            return json.load(open(cache, encoding="utf-8"))
        except Exception:
            pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gamedata
    raw = gamedata.parse_dir(TECH_DIR)
    out = {}
    for name, d in raw.items():
        if not isinstance(d, dict):
            continue
        mod = d.get("modifier")
        out[name] = {
            "era": d.get("era", ""),
            "category": d.get("category", ""),
            "modifiers": {k: v for k, v in mod.items() if not k.startswith("_")}
            if isinstance(mod, dict) else {},
        }
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def _techs_from_save(cid, save_path):
    """从存档 technology 块读出该国已研发的科技名。"""
    data = open(save_path, "rb").read()
    i = data.find(b"\ntechnology={")
    if i < 0:
        return []
    s = data.rfind(b"\n", 0, i) + 1
    blk = data[s:s + 20_000_000].decode("utf-8", "replace")
    lines = blk.split("\n")

    def ind(l):
        return len(l) - len(l.lstrip("\t"))

    in_db = False
    found_country = False
    names = []
    for ln in lines[1:]:
        tt = ln.strip()
        if not tt:
            continue
        d = ind(ln)
        if d == 1 and tt == "database={":
            in_db = True
            continue
        if not in_db:
            continue
        if d == 2 and re.match(r"^\d+=\{$", tt):
            found_country = False
            continue
        if d == 2 and tt == "}":
            if found_country:
                break
            continue
        if d == 3 and tt.startswith("country="):
            found_country = (tt.partition("=")[2].strip() == str(cid))
            continue
        if found_country and d == 3 and tt == "acquired_technologies={":
            continue
        if found_country and d >= 4:
            names += re.findall(r'"([^"]+)"', tt)
    return names


def _load_laws():
    """解析 game/common/laws/*.txt：法律名 -> {group, modifiers}。"""
    import json
    cache = os.path.join(CACHE_DIR, "laws.json")
    if os.path.exists(cache):
        try:
            return json.load(open(cache, encoding="utf-8"))
        except Exception:
            pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gamedata
    raw = gamedata.parse_dir(LAWS_DIR)
    out = {}
    for name, d in raw.items():
        if not isinstance(d, dict):
            continue
        mod = d.get("modifier")
        out[name] = {
            "group": d.get("group", ""),
            "modifiers": {k: v for k, v in mod.items() if not k.startswith("_")}
            if isinstance(mod, dict) else {},
        }
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def _active_laws_from_save(cid, save_path):
    """从存档 laws 块读出该国当前生效的法律。"""
    data = open(save_path, "rb").read()
    i = data.find(b"\nlaws={")
    if i < 0:
        return []
    s = data.rfind(b"\n", 0, i) + 1
    blk = data[s:s + 20_000_000].decode("utf-8", "replace")
    lines = blk.split("\n")

    def ind(l):
        return len(l) - len(l.lstrip("\t"))

    cur = None
    act = []
    for ln in lines[1:]:
        tt = ln.strip()
        if not tt:
            continue
        d = ind(ln)
        if re.match(r"^\d+=\{$", tt) and d == 2:
            cur = {}
            continue
        if d == 2 and tt == "}":
            if cur is not None:
                if cur.get("country") == str(cid) and cur.get("active") == "yes":
                    act.append(cur.get("law", ""))
            cur = None
            continue
        if cur is not None and d == 3 and "=" in tt and not tt.endswith("={"):
            k, _, v = tt.partition("=")
            cur[k] = v.strip('"')
    return [a for a in act if a]


def cmd_lawmods(args):
    """按存档里生效的法律，累加出该国的永久修正（法律部分）。"""
    cid = args.country
    laws = _load_laws()
    save = args.save or DEFAULT_SAVE
    if not os.path.exists(save):
        print("找不到存档：%s" % save)
        return 1
    # 法律优先走内存（实时），失败再退回存档
    active = None
    src = ""
    if not getattr(args, "from_save", False):
        try:
            global _DEDUPE_BY
            _DEDUPE_BY = None
            pr = _attach(args)
            try:
                mm = _laws_from_memory(pr, cid)
            finally:
                pr.close()
            if mm:
                defs0 = _load_laws()
                best = {}
                for name, when, addr in mm:
                    g = defs0.get(name, {}).get("group", "?")
                    cur = best.get(g)
                    if cur is None or addr < cur[2]:
                        best[g] = (name, when, addr)
                active = sorted(v[0] for v in best.values())
                src = "内存"
        except Exception:
            active = None
    if active is None:
        active = _active_laws_from_save(cid, save)
        src = "存档 %s" % save
    techs = []
    tsrc = ""
    if not getattr(args, "no_tech", False):
        if not getattr(args, "from_save", False):
            try:
                pr2 = _attach(args)
                try:
                    techs = _techs_from_memory(pr2, cid)
                finally:
                    pr2.close()
                if techs:
                    tsrc = "内存"
            except Exception:
                techs = []
        if not techs:
            techs = _techs_from_save(cid, save)
            tsrc = "存档"
    print("法律来源：%s   科技来源：%s" % (src, tsrc or "未读取"))
    print("国家 %d 生效法律 %d 条，已研发科技 %d 项" % (cid, len(active), len(techs)))
    total = {}
    per = {}
    for law in active:
        info = laws.get(law)
        if not info:
            print("  %-44s (数据文件里没有)" % law)
            continue
        mods = info.get("modifiers", {})
        print("  %-44s %-34s %d 项效果"
              % (law, info.get("group", ""), len(mods)))
        for k, v in mods.items():
            try:
                fv = float(str(v).replace(",", ""))
            except ValueError:
                continue
            total[k] = total.get(k, 0.0) + fv
            per.setdefault(k, []).append((law, fv))

    techdefs = _load_techs() if techs else {}
    for tech in techs:
        info = techdefs.get(tech)
        if not info:
            continue
        for k, v in info.get("modifiers", {}).items():
            try:
                fv = float(str(v).replace(",", ""))
            except ValueError:
                continue
            total[k] = total.get(k, 0.0) + fv
            per.setdefault(k, []).append((tech, fv))
    if not total:
        print("\n没有可累加的数值型修正")
        return 0
    print("\n== 法律 + 科技贡献的永久修正（%d 项）==" % len(total))
    print("  %-52s %12s  %s" % ("修正类型", "合计", "来源"))
    for k, v in sorted(total.items(), key=lambda kv: -abs(kv[1])):
        src = ", ".join("%s(%g)" % (l.replace("law_", ""), x)
                        for l, x in per.get(k, [])[:3])
        print("  %-52s %12.4g  %s" % (k, v, src[:60]))
    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["modifier_type", "total", "sources"])
            for k, v in sorted(total.items(), key=lambda kv: -abs(kv[1])):
                w.writerow([k, v, "; ".join("%s=%g" % (l, x) for l, x in per.get(k, []))])
        print("\n已写出 %s" % args.csv)
    return 0


# -------------------------------------------------------------------- laws
OFF_CLAW_COUNTRY = 0x30
OFF_CLAW_TYPE = 0x28
OFF_CLAW_ACTIVE = 0x34
OFF_CLAW_DATE = 0x38
OFF_LAWTYPE_NAME = 0x18


def _laws_from_memory(p, cid):
    """从内存读某国生效的法律名列表。"""
    addrs = _instances_of(p, "CCountryLaw", 0, verbose=False)
    out = []
    seen = set()
    for a in addrs:
        buf = p.read(a, 0x50)
        if not buf:
            continue
        if struct.unpack_from("<i", buf, OFF_CLAW_COUNTRY)[0] != cid:
            continue
        if struct.unpack_from("<i", buf, OFF_CLAW_ACTIVE)[0] != 1:
            continue
        tp = struct.unpack_from("<Q", buf, OFF_CLAW_TYPE)[0]
        # +0x18 是 MSVC std::string（短名 SSO 内联，长名堆分配）
        name = p.msvc_string(tp + OFF_LAWTYPE_NAME) if tp else None
        when = struct.unpack_from("<q", buf, OFF_CLAW_DATE)[0]
        # 旧副本的 +0x40 不是有效堆指针（是常量残值），真对象才指向堆
        if name:
            out.append((name, when, a))
    out.sort()
    return out


def cmd_laws(args):
    """列出某国当前生效的法律（纯内存，实时）。"""
    global _DEDUPE_BY
    _DEDUPE_BY = None
    p = _attach(args)
    try:
        cid = args.country
        raw_rows = _laws_from_memory(p, cid)
        defs = _load_laws()
        # 同一法律组可能同时存在新旧两条（历史对象），保留生效日期最新的
        best = {}
        for name, when, addr in raw_rows:
            g = defs.get(name, {}).get("group", "?")
            cur = best.get(g)
            # 同组多条时取地址最小的那份（真对象在较低区块，旧副本在高位区块）
            if cur is None or addr < cur[2]:
                best[g] = (name, when, addr)
        rows = sorted((v[0], v[1]) for v in best.values())
        print("国家 %d 生效法律 %d 条（内存读取，原始 %d 条）"
              % (cid, len(rows), len(raw_rows)))
        print("  %-40s %-34s %s" % ("法律", "法律组", "效果数"))
        for name, when in rows:
            info = defs.get(name, {})
            print("  %-40s %-34s %d"
                  % (name, info.get("group", "?"),
                     len(info.get("modifiers", {}))))
        if args.csv:
            import csv
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["law", "group", "modifiers"])
                for name, when in rows:
                    info = defs.get(name, {})
                    w.writerow([name, info.get("group", ""),
                                "; ".join("%s=%s" % kv
                                          for kv in info.get("modifiers", {}).items())])
            print("\n已写出 %s" % args.csv)
        return 0
    finally:
        p.close()


# -------------------------------------------------------------------- tech
OFF_TECHSTATUS_COUNTRY = 0x110
OFF_TECHSTATUS_CAP = 0x60
OFF_TECHSTATUS_SIZE = 0x64
OFF_TECHSTATUS_LIST = 0x58
OFF_TECHTYPE_NAME = 0x18
TECHSTATUS_STRIDE = 0x128


def _techs_from_memory(p, cid):
    """从内存读某国已研发的科技名列表。

    CCountryTechnologyStatus（步长 0x128，每国一个真对象）：
      +0x110 u32 国家 id
      +0x60  u32 容量
      +0x64  u32 已研发数量
      +0x58  ptr std::vector<CTechnology const*> 的数据区
    元素是 8 字节指针，指向科技对象；科技名在对象 +0x18 的 MSVC std::string。
    另有 374 个全零对象（+0x64 == 0 且 +0x58 == 0），是真对象的清空备份，跳过。
    """
    addrs = _instances_of(p, "CCountryTechnologyStatus", 0, verbose=False)
    out = []
    for a in addrs:
        buf = p.read(a, TECHSTATUS_STRIDE)
        if len(buf) < TECHSTATUS_STRIDE:
            continue
        if struct.unpack_from("<I", buf, OFF_TECHSTATUS_COUNTRY)[0] != cid:
            continue
        n = struct.unpack_from("<I", buf, OFF_TECHSTATUS_SIZE)[0]
        lst = struct.unpack_from("<Q", buf, OFF_TECHSTATUS_LIST)[0]
        if n <= 0 or not lst or n > 400:
            continue
        raw = p.read(lst, n * 8)
        if len(raw) < n * 8:
            continue
        for i in range(n):
            ptr = struct.unpack_from("<Q", raw, i * 8)[0]
            if not ptr:
                continue
            name = p.msvc_string(ptr + OFF_TECHTYPE_NAME)
            if name:
                out.append(name)
    out.sort()
    return out


CAP_KEYS = {
    "行政力（bureaucracy）": [
        ("country_bureaucracy_add", "行政力 +"),
        ("country_bureaucracy_mult", "行政力 ×"),
        ("country_bureaucracy_cost_add", "行政力花费 +"),
        ("country_bureaucracy_investment_cost_factor_mult", "机构投入行政力花费 ×"),
        ("building_level_bureaucracy_cost_add", "建筑等级行政力花费 +"),
        ("state_bureaucracy_population_base_cost_factor_mult", "人口基础行政力花费 ×"),
    ],
    "建造力（construction）": [
        ("country_construction_add", "建造力 +"),
        ("state_construction_mult", "地区建造力 ×"),
        ("country_max_weekly_construction_progress_add", "每周最大建造进度 +"),
        ("country_construction_goods_cost_mult", "建造商品花费 ×"),
        ("country_private_construction_allocation_mult", "私人建造分配 ×"),
    ],
}


def _fnum(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


OFF_BLD_STATE = 0xDC
OFF_BLD_LEVELS = 0xE0
OFF_BLD_TYPE = 0xE8
OFF_BLD_STAFFING = 0x120
OFF_BLD_THROUGHPUT = 0x128
OFF_BLD_PM = 0x1E0
OFF_STATE_ID = 0x10
OFF_STATE_OWNER = 0xEC8


def _state_owners(p):
    """state_id -> 国家 id。"""
    out = {}
    for a in _instances_of(p, "CState", 0, verbose=False):
        buf = p.read(a, 0xF00)
        if len(buf) < 0xED0:
            continue
        sid = struct.unpack_from("<I", buf, OFF_STATE_ID)[0]
        out[sid] = struct.unpack_from("<I", buf, OFF_STATE_OWNER)[0]
    return out


def _building_pms(p, addr):
    """读建筑的生产方式名列表（vector 在 +0x1E0）。"""
    ptr = p.read_u64(addr + OFF_BLD_PM)
    n = p.read_u32(addr + OFF_BLD_PM + 8)
    if not ptr or not (1 <= n <= 32):
        return []
    raw = p.read(ptr, n * 8)
    out = []
    for i in range(n):
        e = struct.unpack_from("<Q", raw, i * 8)[0]
        if not e:
            continue
        nm = p.msvc_string(e + 0x18)
        if nm:
            out.append(nm)
    return out


def _country_buildings(p, cid):
    """该国所有建筑：(类型名, 州 id, 等级, 就业规模, 吞吐量, [PM 名])。"""
    import time as _t
    t0 = _t.time()
    owner = _state_owners(p)
    t1 = _t.time()
    insts = _instances_of(p, "CBuilding", 0, verbose=False)
    t2 = _t.time()
    out = []
    n_read = 0
    for a in insts:
        buf = p.read(a, 0x1F0)
        n_read += 1
        if len(buf) < 0x1E8:
            continue
        st = struct.unpack_from("<I", buf, OFF_BLD_STATE)[0]
        if owner.get(st) != cid:
            continue
        tp = struct.unpack_from("<Q", buf, OFF_BLD_TYPE)[0]
        tname = p.msvc_string(tp + 0x18) if tp else "?"
        out.append((tname or "?",
                    st,
                    struct.unpack_from("<i", buf, OFF_BLD_LEVELS)[0],
                    struct.unpack_from("<q", buf, OFF_BLD_STAFFING)[0] / 100000.0,
                    struct.unpack_from("<q", buf, OFF_BLD_THROUGHPUT)[0] / 100000.0,
                    _building_pms(p, a)))
    t3 = _t.time()
    print("    [建筑内部] 州归属 %.1fs / 取实例列表 %.1fs / 扫 %d 栋 %.1fs"
          % (t1 - t0, t2 - t1, n_read, t3 - t2), flush=True)
    return out


def cmd_capacity(args):
    """行政力/建造力相关修正：来源 = 法律 + 科技 + 建筑生产方式 + GDP 静态修正。"""
    global _DEDUPE_BY
    _DEDUPE_BY = None
    import time as _t
    T0 = _t.time()

    def _lap(msg):
        print("    [%6.1fs] %s" % (_t.time() - T0, msg), flush=True)

    p = _attach(args)
    try:
        cid = args.country if args.country is not None else _player_country_id(p)
        _lap("玩家国家 id = %s" % cid)
        lawdefs = _load_laws()
        techdefs = _load_techs()
        pmdefs = _load_pms()
        statdefs = _load_static_mods()
        _lap("游戏文件解析完成（法律/科技/生产方式/静态修正）")
        scale_mode = getattr(args, "scale", "throughput") or "throughput"

        raw = _laws_from_memory(p, cid)
        _lap("读完法律实例（原始 %d 条）" % len(raw))
        best = {}
        for name, when, addr in raw:
            g = lawdefs.get(name, {}).get("group", "?")
            cur = best.get(g)
            if cur is None or addr < cur[1]:
                best[g] = (name, addr)
        techs = _techs_from_memory(p, cid)
        blds = _country_buildings(p, cid)
        _lap("读完建筑（%d 栋）" % len(blds))
        decs = _country_decrees(p, cid)
        defs_dec = _load_decrees()
        _lap("读完法令（%d 条）" % len(decs))
        print("国家 %d  法律 %d 条  科技 %d 项  建筑 %d 栋  法令 %d 条（内存读取）"
              % (cid, len(best), len(techs), len(blds), len(decs)))
        print("  折算方式: workforce_scaled 使用 %s"
              % {"throughput": "等级 × 吞吐量", "levels": "建筑等级",
                 "staffing": "就业规模(staffing)"}.get(scale_mode, scale_mode))

        # GDP 静态修正：country_gdp_construction -> country_construction_add = 10
        # 系数 = 0.2 + 0.8 × (GDP / 2000000)，GDP ≥ 200 万就封顶 1.0
        # 默认不实时算（那要额外扫市场类 + 读 1156 个州的本地价，很慢），
        # 需要精确值时用 --gdp auto 或 --gdp <数值>。
        gdp_arg = getattr(args, "gdp", None)
        if isinstance(gdp_arg, str) and gdp_arg.lower() == "auto":
            print("  正在按建筑估算 GDP（较慢）…", flush=True)
            weekly, _rows, _mid, _np = _compute_gdp(
                p, cid, blds, use_staffing=getattr(args, "staffing", False))
            gdp = weekly * 52
            mult = min(1.0, 0.2 + 0.8 * (gdp / 2000000.0))
            gdp_note = "实时算出年化 GDP %.0f，系数 %.2f" % (gdp, mult)
        elif gdp_arg:
            gdp = float(gdp_arg)
            mult = min(1.0, 0.2 + 0.8 * (gdp / 2000000.0))
            gdp_note = "GDP %.0f，系数 %.2f" % (gdp, mult)
        else:
            mult = 1.0
            gdp_note = "假设 GDP ≥ 200 万（默认，系数封顶 1.0）；" \
                       "要精确值加 --gdp auto 或 --gdp <数值>"

        for cat, keys in CAP_KEYS.items():
            print("\n  == %s ==" % cat)
            shown = False
            for key, label in keys:
                rows = []
                for g, (name, _a) in sorted(best.items()):
                    v = lawdefs.get(name, {}).get("modifiers", {}).get(key)
                    if _fnum(v) is not None:
                        rows.append(("法律 " + name, _fnum(v), None))
                for t in techs:
                    v = techdefs.get(t, {}).get("modifiers", {}).get(key)
                    if _fnum(v) is not None:
                        rows.append(("科技 " + t, _fnum(v), None))
                for tname, st, lv, sf, thr, pms in blds:
                    for pm in pms:
                        rec = pmdefs.get(pm, {})
                        for scope in ("country_modifiers", "state_modifiers"):
                            for scale, vals in rec.get(scope, {}).items():
                                v = _fnum(vals.get(key))
                                if v is None:
                                    continue
                                if scale == "workforce_scaled":
                                    if scale_mode == "levels":
                                        f, note = float(lv), "×%d级" % lv
                                    elif scale_mode == "throughput":
                                        f = float(lv) * thr
                                        note = "×%d级×%.2f吞吐" % (lv, thr)
                                    else:
                                        f, note = sf, "×%.2f就业" % sf
                                elif scale == "level_scaled":
                                    f, note = float(lv), "×%d级" % lv
                                else:
                                    f, note = 1.0, "固定"
                                rows.append(("建筑 %s@%d 的 %s %s"
                                             % (tname, st, pm, note), v * f, st))
                # 地方法令（如道路养护给 state_construction_mult +0.10）
                for dname, dst in decs:
                    dmods = defs_dec.get(dname, {}).get("modifiers", {})
                    dv = _fnum(dmods.get(key))
                    if dv is not None:
                        rows.append(("法令 %s@%d" % (dname, dst), dv, dst))
                if key == "country_construction_add":
                    rows.append(("静态修正 country_gdp_construction（%s）" % gdp_note,
                                 10.0 * mult, None))
                # 全局基线 base_values（行政力 +100、铸币 +500 等）
                base = statdefs.get("base_values", {}) if statdefs else {}
                bv = _fnum(base.get(key)) if base else None
                if bv is not None:
                    rows.append(("静态修正 base_values（全局基线）", bv, None))
                if not rows:
                    continue
                shown = True
                # 州级修正（state_*）不能把各州数值相加，要按州分组显示
                if key.startswith("state_") and any(r[2] is not None for r in rows):
                    by_state = {}
                    for src, v, st in rows:
                        k = st if st is not None else -1
                        by_state.setdefault(k, []).append((src, v))
                    print("    %-16s %s（州级修正，按州分组；%d 个州）"
                          % (label, key, len([k for k in by_state if k >= 0])))
                    for st, lst in sorted(by_state.items(),
                                          key=lambda kv: -abs(sum(v for _s, v in kv[1]))):
                        tot = sum(v for _s, v in lst)
                        name = "州 %d" % st if st >= 0 else "国家层面"
                        print("        %-14s 合计 %+8.3f%%   （%d 项）"
                              % (name, tot * 100, len(lst)))
                        for src, v in sorted(lst, key=lambda x: -abs(x[1]))[:4]:
                            print("            %+8.3f%%  %s" % (v * 100, src))
                    continue
                tot = sum(v for _, v, _s in rows)
                print("    %-16s %-34s 合计 %+9.3f  （%d 个来源）"
                      % (label, key, tot, len(rows)))
                agg = {}
                for src, v, _st in rows:
                    agg[src] = agg.get(src, 0.0) + v
                for src, v in sorted(agg.items(), key=lambda kv: -abs(kv[1]))[:12]:
                    print("        %+9.3f  %s" % (v, src))
                if len(agg) > 12:
                    print("        …另有 %d 个来源" % (len(agg) - 12))
            if not shown:
                print("    （没有找到来源）")
        return 0
    finally:
        p.close()


OFF_BLD_SALARY = 0x130
OFF_BLD_PROFIT = 0x160


def _market_with_prices(p, mid):
    """按 market_id 找市场，且必须价格表有效。

    CMarket 也有「两代对象」：陈旧副本的 +0x6F0 是 0/0，真对象的才是
    {ptr, 54, 54}。所以不能取第一个匹配，要挑价格表有效的那个。
    """
    for a in _instances_of(p, "CMarket", 0, verbose=False):
        if p.read_u32(a + 0x10) != mid:
            continue
        ptr = p.read_u64(a + OFF_MARKET_PRICES)
        cnt = p.read_u32(a + OFF_MARKET_PRICES + 8)
        if 0x10000 <= ptr < 0x7FFFFFFF0000 and 0 < cnt <= 200:
            return a
    return None


def _state_local_prices(p):
    """state_id -> [54 个本地价格]（÷100000 后的实际价格）。"""
    out = {}
    for a in _instances_of(p, "CState", 0, verbose=False):
        buf = p.read(a, 0xF00)
        if len(buf) < 0xED0:
            continue
        sid = struct.unpack_from("<I", buf, OFF_STATE_ID)[0]
        ptr = struct.unpack_from("<Q", buf, OFF_STATE_LOCAL_PRICES)[0]
        cnt = struct.unpack_from("<I", buf, OFF_STATE_LOCAL_PRICES + 8)[0]
        if not (0x10000 <= ptr < 0x7FFFFFFF0000) or not (0 < cnt <= 200):
            continue
        raw = p.read(ptr, cnt * 8)
        if len(raw) < cnt * 8:
            continue
        out[sid] = [v / 100000.0
                    for v in struct.unpack_from("<%dq" % cnt, raw, 0)]
    return out


_last_type_gdp = {}


def _compute_gdp(p, cid, blds=None, use_staffing=False, use_local=True,
                 use_base=False, price_out=None, price_in=None,
                 skip_nonproduction=False):
    """按游戏文件公式算 GDP（每周）。

        GDP = Σ(产出商品数量 × 价格) - Σ(投入商品数量 × 价格)

    **数量来自生产方式**（goods_output_<good>_add / goods_input_<good>_add），
    不是市场供需——价格只用于估值。

    price_out / price_in 可取 "local" / "market" / "base"，分别指定产出侧与
    投入侧的价格基准（默认两侧同源）。三者含义：
        local  = 各州本地价（受市场接入度影响）
        market = 本国市场统一价
        base   = 商品基础价（common/goods 里的 cost）
    """
    pmdefs = _load_pms()
    mid = None
    for a in _instances_of(p, "CCountry", 0, verbose=False):
        if p.read_u32(a + 0x08) == cid:
            mid = p.read_u32(a + 0x0AE4)
            break
    prices = []
    if mid is not None and mid != 0xFFFFFFFF:
        mkt = _market_with_prices(p, mid)
        if mkt is not None:
            ptr = p.read_u64(mkt + OFF_MARKET_PRICES)
            cnt = p.read_u32(mkt + OFF_MARKET_PRICES + 8)
            if 0x10000 <= ptr < 0x7FFFFFFF0000 and 0 < cnt <= 200:
                prices = list(struct.unpack_from("<%dq" % cnt, p.read(ptr, cnt * 8), 0))
    tbl = goods_table()
    idx_of = {nm: i for i, (nm, _c, _k) in enumerate(tbl)}
    base_cost = {i: c for i, (_n, c, _k) in enumerate(tbl)}

    if price_out is None:
        price_out = "base" if use_base else ("market" if not use_local else "local")
    if price_in is None:
        price_in = price_out
    need_local = "local" in (price_out, price_in)
    local = _state_local_prices(p) if need_local else {}

    if blds is None:
        blds = _country_buildings(p, cid)

    # 产出与投入分开累加，两侧可以用不同价格基准
    qout, qin = {}, {}
    typ = {}          # 建筑类型 -> {"out": {(州,商品): 数量}, "in": {...}}，用于按类型统计贡献
    skipped = 0
    for tname, st, lv, sf, thr, pms in blds:
        base = sf if use_staffing else float(lv)
        # 先看这栋建筑到底产不产商品：政府行政楼/兵营等只消耗不产出
        if skip_nonproduction:
            produces = False
            for pm in pms:
                rec = pmdefs.get(pm, {})
                for scope in ("country_modifiers", "state_modifiers", "building_modifiers"):
                    for _scale, vals in rec.get(scope, {}).items():
                        for k, v in vals.items():
                            if k.startswith("goods_output_") and k.endswith("_add"):
                                fv = _fnum(v)
                                if fv and fv > 0:
                                    produces = True
                                    break
                        if produces:
                            break
                    if produces:
                        break
                if produces:
                    break
            if not produces:
                skipped += 1
                continue
        for pm in pms:
            rec = pmdefs.get(pm, {})
            for scope in ("country_modifiers", "state_modifiers", "building_modifiers"):
                for scale, vals in rec.get(scope, {}).items():
                    if scale == "workforce_scaled":
                        f = base * thr
                    elif scale == "level_scaled":
                        f = float(lv)
                    else:
                        f = 1.0
                    for k, v in vals.items():
                        if not k.endswith("_add"):
                            continue
                        if k.startswith("goods_output_"):
                            g, tgt = k[len("goods_output_"):-4], qout
                        elif k.startswith("goods_input_"):
                            g, tgt = k[len("goods_input_"):-4], qin
                        else:
                            continue
                        fv = _fnum(v)
                        if fv is None:
                            continue
                        key = (st, g)
                        tgt[key] = tgt.get(key, 0.0) + fv * f
                        side_key = "out" if k.startswith("goods_output_") else "in"
                        typ.setdefault(tname, {"out": {}, "in": {}})[side_key][key] = \
                            typ.setdefault(tname, {"out": {}, "in": {}})[side_key].get(key, 0.0) + fv * f

    def _price(kind, st, i):
        if kind == "base":
            c = base_cost.get(i) or 0.0
            return float(c) if c > 0 else 0.0
        if kind == "local":
            plist = local.get(st)
            if plist and i < len(plist) and plist[i] > 0:
                return plist[i]
        if i < len(prices) and prices[i] > 0:
            return prices[i] / 100000.0
        return 0.0

    total = 0.0
    agg = {}
    qsum = {}
    for side, table, kind, sign in (("out", qout, price_out, 1),
                                    ("in", qin, price_in, -1)):
        for (st, g), q in table.items():
            i = idx_of.get(g)
            if i is None:
                continue
            price = _price(kind, st, i)
            if price <= 0:
                continue
            val = sign * q * price
            total += val
            agg[g] = agg.get(g, 0.0) + sign * q * price
            qsum[g] = qsum.get(g, 0.0) + sign * q
    rows = [(v, g, qsum.get(g, 0.0), (v / qsum[g]) if qsum.get(g) else 0.0)
            for g, v in agg.items()]
    rows.sort(key=lambda r: -abs(r[0]))

    # 按建筑类型统计净贡献（用同一套价格规则）
    type_gdp = {}
    for tname, d in typ.items():
        v = 0.0
        for side, kind, sign in (("out", price_out, 1), ("in", price_in, -1)):
            for (st, g), q in d[side].items():
                i = idx_of.get(g)
                if i is None:
                    continue
                pr = _price(kind, st, i)
                if pr > 0:
                    v += sign * q * pr
        type_gdp[tname] = v
    _last_type_gdp.clear()
    _last_type_gdp.update(type_gdp)

    if skip_nonproduction:
        print("  （跳过 %d 栋不产出商品的建筑：政府行政楼/兵营/港口等）" % skipped,
              flush=True)
    return total, rows, mid, len(prices)


def cmd_gdp(args):
    """按建筑汇总 GDP 候选算法，用来和界面 GDP 校准。

    GDP 在存档里只是趋势块（没有标量值），内存里也不在 CCountry 上，
    所以只能按建筑算：
        GDP ≈ Σ(每栋建筑的「增加值」) = Σ(利润 + 工资)
    工资按 salary_rate × 等级 × 吞吐量 折算（与 capacity 的 workforce_scaled 一致）。
    """
    global _DEDUPE_BY
    _DEDUPE_BY = None
    p = _attach(args)
    try:
        # ---- 多国并排比对（优先处理，不受 --formula 影响）----
        if getattr(args, "compare", None):
            ids = [int(x) for x in str(args.compare).replace(" ", "").split(",") if x]
            p_out = getattr(args, "out_price", None)
            if p_out is None:
                p_out = ("base" if getattr(args, "base_prices", False)
                         else ("local" if getattr(args, "local_prices", False)
                               else "market"))
            p_in = getattr(args, "in_price", None) or p_out
            per, tot = {}, {}
            for c in ids:
                b = _country_buildings(p, c)
                g, rws, _m, _n = _compute_gdp(
                    p, c, b, use_staffing=getattr(args, "staffing", False),
                    use_local=("local" in (p_out, p_in)),
                    price_out=p_out, price_in=p_in,
                    skip_nonproduction=not getattr(args, "all_buildings", False))
                per[c] = {gname: val for val, gname, _q, _pr in rws}
                tot[c] = g
            names = set()
            for d in per.values():
                names |= set(d)
            order = sorted(names, key=lambda n: -max(abs(per[c].get(n, 0)) for c in ids))
            print("多国 GDP 构成比对（每周价值）  价格口径: %s" % p_out)
            print("  %-18s" % "商品" + "".join("%14s" % ("国家%d" % c) for c in ids))
            print("  %-18s" % "总计" + "".join("%14.0f" % tot[c] for c in ids))
            print("  %-18s" % "年化×52" + "".join("%14.0f" % (tot[c] * 52) for c in ids))
            print("  " + "-" * (18 + 14 * len(ids)))
            for n in order[:args.top]:
                print("  %-18s" % n + "".join("%14.0f" % per[c].get(n, 0) for c in ids))
            return 0

        cid = args.country if args.country is not None else _player_country_id(p)
        owner = _state_owners(p)
        sp = sq = 0.0
        n = 0
        nz = 0
        by_state = {}
        for a in _instances_of(p, "CBuilding", 0, verbose=False):
            buf = p.read(a, 0x1F0)
            if len(buf) < 0x1E8:
                continue
            st = struct.unpack_from("<I", buf, OFF_BLD_STATE)[0]
            if owner.get(st) != cid:
                continue
            lv = struct.unpack_from("<i", buf, OFF_BLD_LEVELS)[0]
            thr = struct.unpack_from("<q", buf, OFF_BLD_THROUGHPUT)[0] / 100000.0
            sal = struct.unpack_from("<q", buf, OFF_BLD_SALARY)[0] / 100000.0
            prof = struct.unpack_from("<q", buf, OFF_BLD_PROFIT)[0] / 100000.0
            w = sal * lv * thr
            sp += prof
            sq += w
            n += 1
            if abs(prof) > 0.004 or abs(w) > 0.004:
                nz += 1
            by_state[st] = by_state.get(st, 0.0) + prof + w
        va = sp + sq
        if not args.formula:
            print("国家 %d  建筑 %d 栋（其中 %d 栋有产出）" % (cid, n, nz))
            print("\n  == 粗略口径（每周）==")
            print("    Σ利润 %16.2f   Σ工资 %16.2f   Σ增加值 %16.2f"
                  % (sp, sq, va))
            print("    Σ增加值×52 %16.2f   ×12 %16.2f" % (va * 52, va * 12))
            print("\n  加 --formula 按游戏文件公式（商品数量 × 市场价格）计算")
            return 0

        # ===== 按游戏文件公式：Σ(产出×价格) - Σ(投入×价格) =====
        # 默认用实际就业规模（实测：美国 +106%→+0.6%、俄罗斯 +22.7%→-1.4%）
        use_st = not getattr(args, "levels", False)
        use_mkt = getattr(args, "market_prices", False)
        use_base = getattr(args, "base_prices", False)
        use_loc = getattr(args, "local_prices", False)
        p_out = getattr(args, "out_price", None)
        p_in = getattr(args, "in_price", None)
        if p_out is None:
            # 默认用市场统一价（实测三种口径里最接近游戏值）
            p_out = "base" if use_base else ("local" if use_loc else "market")
        if p_in is None:
            p_in = p_out

        # ---- 多国并排比对 ----
        blds = _country_buildings(p, cid)      # 只读一次，别重复扫 6932 栋
        nb = len(blds)
        gdp, rows, mid, nprices = _compute_gdp(
            p, cid, blds, use_staffing=use_st,
            use_local=("local" in (p_out, p_in)),
            price_out=p_out, price_in=p_in,
            skip_nonproduction=not getattr(args, "all_buildings", False))
        CN = {"local": "各州本地价", "market": "本国市场统一价", "base": "商品基础价"}
        print("国家 %d  建筑 %d 栋  市场 id=%s  价格项 %d"
              % (cid, nb, mid, nprices))
        print("  数量口径: %s"
              % ("实际就业规模 × 吞吐量" if use_st else "等级 × 吞吐量（满员）"))
        print("  产出侧价格: %s" % CN.get(p_out, p_out))
        print("  投入侧价格: %s" % CN.get(p_in, p_in))
        print("  数量来自生产方式（goods_output_*/goods_input_*），"
              "价格只用于估值，不取成交量")
        print("\n  == 按游戏文件公式 ==")
        print("    GDP = Σ(产出商品数量×价格) - Σ(投入商品数量×价格)")
        print("    每周:  %18.2f" % gdp)
        print("    ×52:   %18.2f" % (gdp * 52))
        print("\n  == 贡献最大的商品（前 12，每周）==")
        print("    %-18s %14s %12s %10s" % ("商品", "价值", "净数量", "价格"))
        for val, g, q, price in rows[:12]:
            print("    %-18s %14.2f %12.1f %10.2f" % (g, val, q, price))

        if getattr(args, "by_type", False):
            from collections import Counter
            cnt = Counter(t for t, _s, _l, _f, _th, _p in blds)
            tot = sum(cnt.values())
            print("\n  == 计入的建筑类型（共 %d 栋，按 GDP 贡献排序）==" % tot)
            print("    %-40s %5s %14s %8s" % ("类型", "栋数", "年化贡献", "占比"))
            ranked = sorted(cnt.items(),
                            key=lambda kv: -abs(_last_type_gdp.get(kv[0], 0.0) * 52))
            gsum = sum(_last_type_gdp.values()) or 1.0
            for t, n in ranked[:30]:
                v = _last_type_gdp.get(t, 0.0) * 52
                print("    %-40s %5d %14.0f %7.1f%%" % (t, n, v, v / (gsum * 52) * 100))
            if len(ranked) > 30:
                print("    …另有 %d 种类型" % (len(ranked) - 30))
        return 0
    finally:
        p.close()


PREWARM_CLASSES = (
    "CCountry", "CState", "CBuilding", "CPop", "CMarket", "CInterestGroup",
    "CPoliticalMovement", "CCountryLaw", "CDecree", "CInstitution", "CCompany",
    "CCountryTechnologyStatus", "CJominiPlayer", "CTechnology", "CDecreeType",
)


def cmd_prewarm(args):
    """一次扫描收集所有常用类的实例，写进缓存。

    每个类各扫一遍要各走一次全内存（冷缓存时 60~120 秒/类）。
    底层 scan_instances 支持一次传多个 vtable -> 类名，所以可以一遍扫完，
    之后所有命令都直接读缓存。
    """
    p = _attach(args)
    try:
        base = p.module_base("victoria3.exe")
        start = getattr(p, "start_time", lambda: 0)()
        vs = {}
        todo = []
        for cls in PREWARM_CLASSES:
            try:
                sel = _selected("^%s$" % re.escape(cls), base)
            except SystemExit:
                continue
            if not sel:
                continue
            primary = next(iter(sel))
            cpath = _cache_path(cls + "_p", p.pid, base, start)
            if not args.refresh and os.path.exists(cpath):
                print("  %-28s 已有缓存，跳过" % cls)
                continue
            vs[primary] = cls
            todo.append((cls, primary, cpath))
        if not todo:
            print("全部类都已有缓存，无需扫描。要强制重扫加 --refresh")
            return 0
        print("需要扫描的类 %d 个：%s" % (len(todo), ", ".join(c for c, _v, _p in todo)))
        print("正在一次性扫描内存（每个类单独扫要各一遍，这里只走一遍）…", flush=True)
        hits = discover.scan_instances(p, vs, progress=True)
        for cls, primary, cpath in todo:
            addrs = sorted(set(hits.get(cls, [])))
            n_obj, bases = discover.cluster_count(addrs, window=0x10)
            # 簇内最小地址未必是对象起点（多继承对象的次要 vtable 可能更靠前），
            # 过滤成「首 8 字节确实是本类 vtable」的那些，否则 _load_cache 的
            # 抽查会失败，导致下次命令又白扫一遍全内存。
            good = [b for b in bases if p.read_u64(b) == primary]
            if len(good) != len(bases):
                print("  %-28s 过滤 %d 个非 vtable 起点的基址"
                      % (cls, len(bases) - len(good)))
            _save_cache(cpath, good)
            print("  %-28s 命中 %6d 处 -> 对象 %5d 个（保留 %d）"
                  % (cls, len(addrs), n_obj, len(good)))
        print("\n完成。之后所有命令都会直接读缓存。")
        return 0
    finally:
        p.close()


def _print_techs(cid, techs, src, csv_path=None):
    """打印科技清单 + 时代统计 + 累加后的永久修正。"""
    defs = _load_techs()
    eras = {}
    mods_total = {}
    print("国家 %d 已研发科技 %d 项（%s读取）" % (cid, len(techs), src))
    print("  %-30s %-3s %-14s %s" % ("科技", "时代", "分类", "效果数"))
    for name in techs:
        info = defs.get(name, {})
        era = str(info.get("era", "")).replace("era_", "")
        m = info.get("modifiers", {})
        eras[era] = eras.get(era, 0) + 1
        for k, v in m.items():
            try:
                mods_total[k] = mods_total.get(k, 0.0) + float(str(v).replace(",", ""))
            except ValueError:
                pass
        print("  %-30s %-3s %-14s %d" % (name, era, info.get("category", ""), len(m)))
    if len(techs) > 1:
        print("\n按时代统计: %s"
              % ", ".join("时代%s=%d" % (k or "?", v)
                          for k, v in sorted(eras.items())))
    if mods_total:
        print("\n== 科技贡献的永久修正（%d 项）==" % len(mods_total))
        for k, v in sorted(mods_total.items(), key=lambda kv: -abs(kv[1])):
            print("  %-52s %12.4g" % (k, v))
    if csv_path:
        import csv
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["technology", "era", "category", "modifiers"])
            for name in techs:
                info = defs.get(name, {})
                w.writerow([name, info.get("era", ""), info.get("category", ""),
                            "; ".join("%s=%s" % kv
                                      for kv in info.get("modifiers", {}).items())])
        print("\n已写出 %s" % csv_path)
    return 0


def cmd_tech(args):
    """列出某国已研发的科技（默认纯内存，实时）。"""
    global _DEDUPE_BY
    _DEDUPE_BY = None
    if getattr(args, "from_save", False):
        cid = args.country
        if cid is None:
            print("--from-save 需要同时给 --country")
            return 1
        return _print_techs(cid, _techs_from_save(cid, args.save or DEFAULT_SAVE),
                            "存档 ", args.csv)
    p = _attach(args)
    try:
        cid = args.country if args.country is not None else _player_country_id(p)
        if cid is None:
            print("没能从内存里判断玩家国家，请用 --country 指定")
            return 1
        return _print_techs(cid, _techs_from_memory(p, cid), "内存 ", args.csv)
    finally:
        p.close()


# --------------------------------------------------------------------- tax
OFF_CC_ID = 0x08
OFF_TAX_LEVEL = 0x2498
OFF_TAXED_PTR = 0x2500
OFF_TAXED_N = 0x2508
OFF_TAXED_CAP = 0x250C
OFF_TARIFF_IMP_PTR = 0x24E0
OFF_TARIFF_IMP_N = 0x24E8
OFF_TARIFF_IMP_MASK = 0x24EC
OFF_TARIFF_EXP_PTR = 0x24B8
OFF_TARIFF_EXP_N = 0x24C0
OFF_TARIFF_EXP_MASK = 0x24C4
OFF_GOODS_NAME = 0x18
OFF_WEEKLY_POOL = 0x1D18     # 每周投资池变化（i64 定点）
OFF_BASE_WAGE = 0x2150       # 基础工资（i64 定点）
TARIFF_NODE_SIZE = 0x20
TARIFF_LEVELS = ["max_subventions", "high_subventions", "low_subventions",
                 "no_tariffs_or_subventions", "low_tariffs", "high_tariffs",
                 "max_tariffs"]

TAX_LEVELS = ["very_low", "low", "medium", "high", "very_high"]
TAX_KIND_CN = {
    "tax_consumption_add": "消费税",
    "tax_land_add": "土地税",
    "tax_per_capita_add": "人头税",
    "tax_income_add": "所得税",
    "tax_dividends_add": "红利税",
}
INCOME_CN = {0: "额外收入", 1: "所得税", 2: "人头税", 3: "消费税",
             4: "红利税", 5: "外交条约", 7: "投资池回收(建造)", 8: "铸币收入",
             9: "关税", 10: "政府分红", 11: "条约(商品)", 12: "条约(资金)",
             15: "补给网络"}
OFF_EXPENSES = 0x1D50
EXPENSE_CN = {0: "额外支出", 1: "政府奴隶", 2: "政府建筑商品", 3: "政府工资",
              4: "军用奴隶", 5: "军事维护商品", 6: "军队工资",
              7: "建造商品", 8: "补贴", 9: "补助金", 10: "利息",
              11: "外交条约", 13: "福利金", 14: "国有建筑亏损",
              15: "条约(资金)", 16: "条约(商品)", 17: "补给船建造",
              18: "船舰建造力", 19: "补给船", 20: "军事船舰",
              21: "船运线路"}


def _country_from_memory(p, cid):
    """按 +0x08 的国家 id 找 CCountry 对象。"""
    for a in _instances_of(p, "CCountry", 0, verbose=False):
        if p.read_u32(a + OFF_CC_ID) == cid:
            return a
    return None


def _tax_from_memory(p, cid):
    """读税收等级、消费税商品、关税条目数。

    CCountryBudget（内联于 CCountry +0x12D8）尾部是三个同构容器：
      +0x24B0 出口关税率表  {ptr, u32 n, u32 cap, ...}
      +0x24D8 进口关税率表
      +0x2500 消费税商品列表 {ptr, u32 n, u32 cap}，元素是 CGoods 指针
    """
    a = _country_from_memory(p, cid)
    if a is None:
        return None
    out = {
        "addr": a,
        "level": p.read_u32(a + OFF_TAX_LEVEL),
        "goods": [],
        "imp": p.read_u32(a + OFF_TARIFF_IMP_N),
        "exp": p.read_u32(a + OFF_TARIFF_EXP_N),
    }
    ptr = p.read_u64(a + OFF_TAXED_PTR)
    n = p.read_u32(a + OFF_TAXED_N)
    if ptr and n and 0 < n <= 120:
        raw = p.read(ptr, n * 8)
        for i in range(n):
            g = struct.unpack_from("<Q", raw, i * 8)[0]
            if not g:
                continue
            out["goods"].append(p.msvc_string(g + OFF_GOODS_NAME) or "?0x%X" % g)
    return out


OFF_COUNTRY_MARKET = 0x0AE4
OFF_MARKET_ID = 0x10
OFF_MARKET_OWNER = 0x838
OFF_MARKET_SUPPLY = 0x220
OFF_MARKET_DEMAND = 0x1D0


def _read_vec54(p, addr):
    """读 CMarket 的 std::vector<int64>[54]（偏移处是 {ptr, size=54, cap=54}）。"""
    ptr = p.read_u64(addr)
    if not ptr or p.read_u32(addr + 8) != 54:
        return None
    raw = p.read(ptr, 54 * 8)
    if len(raw) < 54 * 8:
        return None
    return list(struct.unpack_from("<54q", raw, 0))


def _market_active_goods(p, country_addr):
    """该国市场里「供给或需求非零」的商品名集合。

    CCountry.market_id @ +0x0AE4（0xFFFFFFFF = 没有市场）
    CMarket: market_id@0x10, owner@0x838, supply@0x220, demand@0x1D0
    """
    mid = p.read_u32(country_addr + OFF_COUNTRY_MARKET)
    if mid is None or mid == 0xFFFFFFFF:
        return None
    for a in _instances_of(p, "CMarket", 0, verbose=False):
        if p.read_u32(a + OFF_MARKET_ID) != mid:
            continue
        sup = _read_vec54(p, a + OFF_MARKET_SUPPLY)
        dem = _read_vec54(p, a + OFF_MARKET_DEMAND)
        if sup is None or dem is None:
            continue
        names = [g[0] for g in goods_table()]
        return {names[i] for i in range(min(54, len(names)))
                if sup[i] or dem[i]}
    return None


def _tariff_levels_from_memory(p, addr):
    """读进出口关税表 -> {"import": [(商品名, 等级)], "export": [...]}。

    表是哈希表：ptr + (mask+1) 个 0x20 槽，实际占用 n 个，空槽里是垃圾数据。
    节点：+0x08 = CGoods 指针，+0x10 u8 = 等级，+0x12 u8 = 默认/上一档，
          +0x14 u64 = 最后变更日期（全 FF = 从未改过）。
    等级枚举是倒序的：0=max_subventions … 3=无关税 … 4=low_tariffs … 6=max_tariffs。
    """
    out = {}
    for tag, po, no, mo in (
            ("import", OFF_TARIFF_IMP_PTR, OFF_TARIFF_IMP_N, OFF_TARIFF_IMP_MASK),
            ("export", OFF_TARIFF_EXP_PTR, OFF_TARIFF_EXP_N, OFF_TARIFF_EXP_MASK)):
        rows = []
        ptr = p.read_u64(addr + po)
        n = p.read_u32(addr + no)
        mask = p.read_u32(addr + mo)
        if ptr and n and 0 < n <= 200:
            slots = mask + 1 if 0 < mask < 4096 else n
            raw = p.read(ptr, slots * TARIFF_NODE_SIZE)
            for i in range(slots):
                o = i * TARIFF_NODE_SIZE
                if o + TARIFF_NODE_SIZE > len(raw):
                    break
                g = struct.unpack_from("<Q", raw, o + 0x08)[0]
                if not g:
                    continue
                name = p.msvc_string(g + OFF_GOODS_NAME)
                if not name:
                    continue
                rows.append((name, raw[o + 0x10]))
        out[tag] = rows
    return out


def _load_tax_rates():
    """税法 -> {level: {税种: 税率}}，来自 laws 文件里的 tax_modifier_<level> 块。"""
    import json
    cache = os.path.join(CACHE_DIR, "tax_rates.json")
    if os.path.exists(cache):
        try:
            return json.load(open(cache, encoding="utf-8"))
        except Exception:
            pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gamedata
    raw = gamedata.parse_dir(LAWS_DIR)
    out = {}
    for name, d in raw.items():
        if not isinstance(d, dict):
            continue
        levels = {}
        for key, val in d.items():
            if not key.startswith("tax_modifier_") or not isinstance(val, dict):
                continue
            lv = key[len("tax_modifier_"):]
            rates = {}
            for k, v in val.items():
                if not k.startswith("tax_"):
                    continue
                try:
                    rates[k] = float(str(v).replace(",", ""))
                except ValueError:
                    pass
            if rates:
                levels[lv] = rates
        if levels:
            out[name] = {"progressiveness": d.get("progressiveness"),
                         "levels": levels}
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def _tariffs_from_save(cid, save_path):
    """从存档 country_manager 里读该国的消费税商品和进出口关税等级。"""
    if not os.path.exists(save_path):
        return None
    data = open(save_path, "rb").read()
    i = data.find(b"\ncountry_manager={")
    if i < 0:
        return None
    s = data.rfind(b"\n", 0, i) + 1
    j = data.find(b"\n}\n", i + 20)
    lines = data[s:j + 2].decode("utf-8", "replace").split("\n")

    def ind(l):
        return len(l) - len(l.lstrip("\t"))

    res = {"taxed_goods": [], "import": {}, "export": {}}
    inside = False
    sec = sub = collect = None
    for ln in lines:
        t = ln.strip()
        if not t:
            continue
        d = ind(ln)
        if collect is not None:
            if d == 4:
                res["taxed_goods"].extend(
                    __import__("re").findall(r'"([^"]+)"', t))
                continue
            if t == "}":
                collect = None
                continue
        if d == 2 and t.endswith("={"):
            inside = t[:-2] == str(cid)
            sec = sub = None
            if not inside:
                continue
            continue
        if not inside:
            continue
        if d == 2 and t == "}":
            break
        if d == 3:
            sec = sub = None
            if t.startswith("taxed_goods={"):
                import re as _re
                res["taxed_goods"].extend(_re.findall(r'"([^"]+)"', t))
                if not t.endswith("}"):
                    collect = "taxed_goods"
            elif t == "import_tariffs={":
                sec = "import"
            elif t == "export_tariffs={":
                sec = "export"
            continue
        if sec and d == 4:
            if t.endswith("={"):
                k = t[:-2]
                sub = int(k) if k.isdigit() else None
            elif t == "}":
                sub = None
            continue
        if sec and d == 5 and sub is not None and t.startswith("level="):
            res[sec][sub] = t.split("=", 1)[1]
    return res


def cmd_tax(args):
    """列出某国的税收法律、税率、消费税商品和关税概况。"""
    global _DEDUPE_BY
    _DEDUPE_BY = None
    p = _attach(args)
    try:
        cid = args.country if args.country is not None else _player_country_id(p)
        if cid is None:
            print("没能判断玩家国家，请用 --country 指定")
            return 1
        info = _tax_from_memory(p, cid)
        if info is None:
            print("内存里找不到国家 %d" % cid)
            return 1

        # 税收法律：从内存读生效法律，取 lawgroup_taxation 那条
        law_name = None
        try:
            defs_law = _load_laws()
            best = {}
            for name, when, addr in _laws_from_memory(p, cid):
                g = defs_law.get(name, {}).get("group", "?")
                if g != "lawgroup_taxation":
                    continue
                cur = best.get(g)
                if cur is None or addr < cur[2]:
                    best[g] = (name, when, addr)
            if best:
                law_name = list(best.values())[0][0]
        except Exception:
            law_name = None

        lv = info["level"]
        lvname = TAX_LEVELS[lv] if 0 <= lv < len(TAX_LEVELS) else "?%d" % lv
        print("国家 %d   税收概况（内存读取）" % cid)
        print("  税法          %s" % (law_name or "（未读到）"))
        print("  税收等级      %s  （第 %d 档 / 共 5 档）"
              % (lvname, lv + 1 if 0 <= lv < len(TAX_LEVELS) else 0))

        rates = _load_tax_rates()
        lawinfo = rates.get(law_name or "", {})
        cur_rates = lawinfo.get("levels", {}).get(lvname, {})
        if cur_rates:
            print("\n  == 当前实际税率 ==")
            for k, v in sorted(cur_rates.items(), key=lambda kv: -kv[1]):
                print("    %-10s %6.2f%%" % (TAX_KIND_CN.get(k, k), v * 100))
        if args.levels and lawinfo:
            print("\n  == %s 五档税率表 ==" % (law_name or "?"))
            allk = []
            for lv2 in TAX_LEVELS:
                for k in lawinfo["levels"].get(lv2, {}):
                    if k not in allk:
                        allk.append(k)
            print("    %-12s %s" % ("税种", "  ".join("%-9s" % x for x in TAX_LEVELS)))
            for k in allk:
                row = []
                for lv2 in TAX_LEVELS:
                    v = lawinfo["levels"].get(lv2, {}).get(k)
                    row.append("%-9s" % ("%.2f%%" % (v * 100) if v is not None else "-"))
                print("    %-12s %s" % (TAX_KIND_CN.get(k, k), "  ".join(row)))

        print("\n  == 消费税 ==")
        if info["goods"]:
            print("    开征商品 %d 件：%s"
                  % (len(info["goods"]), ", ".join(sorted(info["goods"]))))
            cr = cur_rates.get("tax_consumption_add")
            if cr is not None:
                print("    统一税率   %.2f%%" % (cr * 100))
        else:
            print("    未开征消费税")

        print("\n  == 关税 ==【等级准确，条目范围待定】")
        print("    进口  内存声明 %d 种商品" % info["imp"])
        print("    出口  内存声明 %d 种商品" % info["exp"])
        print("    注：等级字段与存档一致率 99.86%%；但「哪些商品算一条」只能靠市场活跃度"
              "近似判断，\n        约 14%% 的陈旧条目识别不出来（疑与贸易路线有关），故此节标为待定。")
        if args.tariffs:
            save = args.save or DEFAULT_SAVE
            sv = _tariffs_from_save(cid, save)
            if sv is None:
                print("    存档不可用，逐商品等级需要存档：%s" % save)
            else:
                for k, cn in (("import", "进口"), ("export", "出口")):
                    rows = sorted(sv[k].items(), key=lambda kv: int(kv[0]))
                    print("    %s关税 %d 条（存档）：" % (cn, len(rows)))
                    for g, v in rows:
                        print("       商品 #%-3s %s" % (g, v))
        if args.mem_tariffs:
            lv_rows = _tariff_levels_from_memory(p, info["addr"])
            act = _market_active_goods(p, info["addr"])
            for tag, cn in (("import", "进口"), ("export", "出口")):
                raw_rows = lv_rows[tag]
                seen = {}
                for name, lv in raw_rows:
                    seen.setdefault(name, lv)
                if act is not None:
                    live = {k: v for k, v in seen.items() if k in act}
                    stale = sorted(set(seen) - set(live))
                else:
                    live, stale = seen, []
                rows = sorted(live.items())
                print("    %s  表里 %d 种，市场活跃 %d 种，过滤后 %d 种"
                      % (cn, len(seen), len(live), len(rows)))
                if stale:
                    print("        市场不活跃、判为陈旧: %s" % ", ".join(stale))
                if act is None:
                    print("        （该国没有市场（+0x0AE4 = -1），未做活跃度过滤）")
                for name, lv in rows:
                    lname = (TARIFF_LEVELS[lv] if lv < len(TARIFF_LEVELS)
                             else "?%d" % lv)
                    print("        %-22s %s" % (name, lname))

        inc = [p.read_i64(info["addr"] + OFF_INCOME + 8 * i) for i in range(N_INCOME)]
        print("\n  == 收入（内存数组，已结算）==")
        for i, v in enumerate(inc):
            if not v:
                continue
            label = INCOME_CN.get(i, "未命名[%d]" % i)
            print("    %-12s %.2f" % (label, v / 100000.0))
        print("    注：这是「上周已结算」的值；界面显示的是「下周预测」，"
              "刚读档未走一周时两者会不同\n"
              "        （典型：铸币收入这时只有基础值 500）")
        pool = p.read_i64(info["addr"] + OFF_WEEKLY_POOL)
        wage = p.read_i64(info["addr"] + OFF_BASE_WAGE)
        exp = [p.read_i64(info["addr"] + OFF_EXPENSES + 8 * i)
               for i in range(N_EXPENSES)]
        if any(exp):
            print("\n  == 支出（内存数组，已结算）==")
            tot = 0
            for i, v in enumerate(exp):
                if not v:
                    continue
                tot += v
                print("    %-14s %.2f"
                      % (EXPENSE_CN.get(i, "未命名[%d]" % i), v / 100000.0))
            print("    %-14s %.2f" % ("合计", tot / 100000.0))
        print("\n  == 投资与工资 ==")
        print("    每周投资池变化  %.2f" % (pool / 100000.0))
        print("    基础工资        %.2f" % (wage / 100000.0))
        return 0
    finally:
        p.close()


# ----------------------------------------------------------------- politics
OFF_IG_IN_GOV = 0x0A0
OFF_IG_COUNTRY = 0x0AC
OFF_IG_LEADER = 0x0B0
OFF_IG_PARTY = 0x0B4
OFF_IG_APPROVAL = 0x0CC
OFF_IG_POLSTR = 0x0E0
OFF_IG_RADPOL = 0x0E8
OFF_IG_LOYPOL = 0x0F0
OFF_IG_CLOUT = 0x0F8
OFF_IG_NAME = 0x270
IG_STRIDE = 0x338
FIXED = 100000.0

def _approval_cn(v):
    """好感度 -> 状态。阈值来自存档全量统计：
    <=-10 angry / -9~-5 unhappy / -4~4 neutral / 5~9 happy / >=10 loyal。
    """
    if v <= -10:
        return "愤怒"
    if v < -4:
        return "不满"
    if v < 5:
        return "中立"
    if v < 10:
        return "满意"
    return "忠诚"


def _igs_from_memory(p, cid):
    """读某国的利益集团。CInterestGroup 步长 0x338，两代并存但同国同名唯一。"""
    import re
    pat = re.compile(r"^ig_[a-z0-9_]+$")
    out = []
    for a in _instances_of(p, "CInterestGroup", 0, verbose=False):
        buf = p.read(a, 0x2A0)
        if len(buf) < 0x2A0:
            continue
        if struct.unpack_from("<I", buf, OFF_IG_COUNTRY)[0] != cid:
            continue
        name = p.msvc_string(a + OFF_IG_NAME)
        if not name or not pat.match(name):
            continue
        out.append({
            "name": name,
            "addr": a,
            "in_gov": struct.unpack_from("<I", buf, OFF_IG_IN_GOV)[0] != 0,
            "leader": struct.unpack_from("<i", buf, OFF_IG_LEADER)[0],
            "party": struct.unpack_from("<i", buf, OFF_IG_PARTY)[0],
            "approval": struct.unpack_from("<i", buf, OFF_IG_APPROVAL)[0],
            "polstr": struct.unpack_from("<q", buf, OFF_IG_POLSTR)[0],
            "radpol": struct.unpack_from("<q", buf, OFF_IG_RADPOL)[0],
            "loypol": struct.unpack_from("<q", buf, OFF_IG_LOYPOL)[0],
            "clout": struct.unpack_from("<i", buf, OFF_IG_CLOUT)[0] / FIXED,
        })
    out.sort(key=lambda x: -x["clout"])
    return out


OFF_MV_COUNTRY = 0x130
OFF_MV_RADICALISM = 0x0D0
OFF_MV_IDEOLOGY = 0x110
OFF_MV_TYPE = 0x168
OFF_MV_POPS = 0x15C
MV_STRIDE = 0x3C8
OFF_TYPENAME = 0x18


def _moves_from_memory(p, cid):
    """读某国的政治运动。

    CPoliticalMovement 步长 0x3C8：
      +0x0D0 i32 激进度（定点 ÷100000）
      +0x110 ptr 意识形态对象（名字在 +0x18）
      +0x130 u32 国家 id
      +0x15C i32 pops 数量（0 = 陈旧副本，跳过）
      +0x168 ptr 运动类型对象（名字在 +0x18）

    内存里每个运动会存两代，旧副本的 +0x15C 恒为 0；用它筛掉后
    总数 1097，与存档 1101 吻合 99.6%。
    """
    out = []
    for a in _instances_of(p, "CPoliticalMovement", 0, verbose=False):
        buf = p.read(a, 0x180)
        if len(buf) < 0x180:
            continue
        if struct.unpack_from("<I", buf, OFF_MV_COUNTRY)[0] != cid:
            continue
        pops = struct.unpack_from("<i", buf, OFF_MV_POPS)[0]
        if pops <= 0:
            continue
        tp = struct.unpack_from("<Q", buf, OFF_MV_TYPE)[0]
        ip = struct.unpack_from("<Q", buf, OFF_MV_IDEOLOGY)[0]
        tname = p.msvc_string(tp + OFF_TYPENAME) if tp else None
        iname = p.msvc_string(ip + OFF_TYPENAME) if ip else None
        if not tname or not tname.startswith("movement_"):
            continue
        out.append({
            "type": tname, "ideology": iname or "?",
            "radicalism": struct.unpack_from("<i", buf, OFF_MV_RADICALISM)[0] / FIXED,
            "pops": pops, "addr": a,
        })
    out.sort(key=lambda x: -x["radicalism"])
    return out


def cmd_politics(args):
    """列出某国利益集团（内存，实时）。"""
    global _DEDUPE_BY
    _DEDUPE_BY = None
    p = _attach(args)
    try:
        cid = args.country if args.country is not None else _player_country_id(p)
        if cid is None:
            print("没能判断玩家国家，请用 --country 指定")
            return 1
        igs = _igs_from_memory(p, cid)
        if not igs:
            print("国家 %d 没读到利益集团" % cid)
            return 1
        total = sum(x["clout"] for x in igs)
        print("国家 %d  利益集团 %d 个（内存读取）  势力合计 %.2f%%"
              % (cid, len(igs), total * 100))
        print("  %-24s %7s %10s %8s %-6s %-4s %s"
              % ("集团", "势力", "政治力量", "好感", "状态", "执政", "政党"))
        for x in igs:
            st = _approval_cn(x["approval"])
            print("  %-24s %6.2f%% %10d %8d %-6s %-4s %s"
                  % (x["name"], x["clout"] * 100, x["polstr"], x["approval"],
                     st, "是" if x["in_gov"] else "-",
                     x["party"] if x["party"] >= 0 else "-"))
        if args.detail:
            print("\n  == 明细 ==")
            for x in igs:
                print("  %-24s 激进力量 %-10d 忠诚力量 %-8d 领袖 %d"
                      % (x["name"], x["radpol"], x["loypol"], x["leader"]))
        if args.movements:
            mvs = _moves_from_memory(p, cid)
            print("\n  == 政治运动 %d 个（内存，已剔除陈旧副本）==" % len(mvs))
            if not mvs:
                print("    无")
            else:
                print("  %-34s %-40s %8s %8s"
                      % ("类型", "意识形态", "激进度", "支持人数"))
                for x in mvs:
                    print("  %-34s %-40s %7.2f%% %8d"
                          % (x["type"], x["ideology"], x["radicalism"] * 100,
                             x["pops"]))
        if args.csv:
            import csv
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["interest_group", "clout", "political_strength",
                            "approval", "in_government", "party", "leader",
                            "radicals_strength", "loyalists_strength"])
                for x in igs:
                    w.writerow([x["name"], "%.5f" % x["clout"], x["polstr"],
                                x["approval"], int(x["in_gov"]), x["party"],
                                x["leader"], x["radpol"], x["loypol"]])
            print("\n已写出 %s" % args.csv)
        return 0
    finally:
        p.close()


# -------------------------------------------------------- institutions / companies
OFF_INST_TYPE = 0x018
OFF_INST_COUNTRY = 0x020
OFF_INST_INVEST = 0x024
INST_STRIDE = 0x1C0

OFF_COMP_COUNTRY = 0x060
OFF_COMP_BUILDING = 0x064
OFF_COMP_CEO = 0x068
OFF_COMP_TYPE = 0x0A0      # 公司类型指针（company_hbc 等）
OFF_COMP_REGION = 0x0A8    # 州区域指针 -> CStateRegionTemplate，名字在 +0x18
OFF_COMP_NAME_OBJ = 0x0B0  # 公司显示名对象（CDynamicCompanyName）
COMP_STRIDE = 0x238


def _obj_name(p, ptr):
    """取指针指向对象里的标识符名（先在 +0x18 试，再扫几个偏移）。"""
    import re
    if not ptr or not (0x10000000000 <= ptr <= 0x7FFFFFFFFFFF):
        return None
    pat = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,48}$")
    for off in (0x18, 0x10, 0x20, 0x08, 0x00):
        try:
            s = p.msvc_string(ptr + off)
        except Exception:
            s = None
        if s and pat.match(s):
            return s
    for off in range(0, 0x60, 8):
        raw = p.read(ptr + off, 48)
        if not raw:
            continue
        z = raw.find(b"\x00")
        if 2 < z < 48:
            t = raw[:z].decode("latin-1", "replace")
            if pat.match(t):
                return t
    return None


def _institutions_from_memory(p, cid=None):
    """读机构。CInstitution 步长 0x1C0：类型指针@0x18、国家@0x20、投入等级@0x24。"""
    out = []
    for a in _instances_of(p, "CInstitution", 0, verbose=False):
        buf = p.read(a, 0x40)
        if len(buf) < 0x40:
            continue
        c = struct.unpack_from("<I", buf, OFF_INST_COUNTRY)[0]
        if cid is not None and c != cid:
            continue
        tp = struct.unpack_from("<Q", buf, OFF_INST_TYPE)[0]
        out.append({
            "country": c,
            "type": _obj_name(p, tp) or "?",
            "investment": struct.unpack_from("<I", buf, OFF_INST_INVEST)[0],
            "addr": a,
        })
    out.sort(key=lambda x: (x["country"], -x["investment"]))
    return out


def cmd_institutions(args):
    """列出机构投入等级（内存，实时）。"""
    global _DEDUPE_BY
    _DEDUPE_BY = None
    p = _attach(args)
    try:
        cid = None if args.all else (
            args.country if args.country is not None else _player_country_id(p))
        rows = _institutions_from_memory(p, cid)
        if cid is not None:
            print("国家 %d  机构 %d 个（内存读取）" % (cid, len(rows)))
        else:
            print("全部国家  机构 %d 个" % len(rows))
        if not rows:
            print("  无（该国所有机构投资为 0）")
            return 0
        print("  %-34s %-6s %s" % ("机构", "投入", "国家"))
        for x in rows:
            print("  %-34s %-6d %s" % (x["type"], x["investment"],
                                       "" if cid is not None else x["country"]))
        return 0
    finally:
        p.close()


def _companies_from_memory(p, cid=None):
    """读公司。CCompany 步长 0x238。"""
    out = []
    for a in _instances_of(p, "CCompany", 0, verbose=False):
        buf = p.read(a, 0xB0)
        if len(buf) < 0xB0:
            continue
        c = struct.unpack_from("<I", buf, OFF_COMP_COUNTRY)[0]
        if cid is not None and c != cid:
            continue
        tp = struct.unpack_from("<Q", buf, OFF_COMP_TYPE)[0]
        rp = struct.unpack_from("<Q", buf, OFF_COMP_REGION)[0]
        out.append({
            "country": c,
            "building": struct.unpack_from("<I", buf, OFF_COMP_BUILDING)[0],
            "ceo": struct.unpack_from("<I", buf, OFF_COMP_CEO)[0],
            "type": _obj_name(p, tp) or "?",
            "region": _obj_name(p, rp) or "?",
            "addr": a,
        })
    out.sort(key=lambda x: (x["country"], x["type"]))
    return out


def cmd_companies(args):
    """列出公司（内存，实时）。"""
    global _DEDUPE_BY
    _DEDUPE_BY = None
    p = _attach(args)
    try:
        cid = None if args.all else (
            args.country if args.country is not None else _player_country_id(p))
        rows = _companies_from_memory(p, cid)
        if cid is not None:
            print("国家 %d  公司 %d 家（内存读取）" % (cid, len(rows)))
        else:
            print("全部国家  公司 %d 家" % len(rows))
        if not rows:
            print("  无")
            return 0
        print("  %-36s %-24s %-8s %-6s %s"
              % ("公司", "州区域", "母建筑", "CEO", "国家"))
        unk = any(x["region"] == "?" for x in rows)
        for x in rows:
            print("  %-36s %-24s %-8d %-6d %s"
                  % (x["type"], x["region"] if x["region"] != "?" else "-",
                     x["building"], x["ceo"],
                     "" if cid is not None else x["country"]))
        if unk:
            print("\n  注：州区域字段未定位（+0x020 的指针指向的不是带名字的对象），"
                  "以 - 表示。")
        return 0
    finally:
        p.close()


# -------------------------------------------------------------------- pops
OFF_POP_ID = 0x08
OFF_POP_PROF = 0x10
OFF_POP_RELIGION = 0x18
OFF_POP_WORKFORCE = 0x30
OFF_POP_STATE = 0x40
OFF_POP_CULTURE = 0x48
OFF_POP_WEALTH = 0x50
_SAVE_POPS = None


def _load_save_pops():
    """解析存档 pops 块：id -> 字段字典（带缓存）。"""
    global _SAVE_POPS
    if _SAVE_POPS is not None:
        return _SAVE_POPS
    path = DEFAULT_SAVE
    if not os.path.exists(path):
        _SAVE_POPS = {}
        return _SAVE_POPS
    data = open(path, "rb").read()
    i = data.find(b"\npops={")
    if i < 0:
        _SAVE_POPS = {}
        return _SAVE_POPS
    s = data.rfind(b"\n", 0, i) + 1
    blk = data[s:s + 40_000_000].decode("utf-8", "replace")
    lines = blk.split("\n")

    def ind(l):
        return len(l) - len(l.lstrip("\t"))

    end = next((k for k, l in enumerate(lines[1:], 1)
                if l.strip() == "}" and ind(l) == 0), len(lines))
    cur = None
    out = {}
    for n in range(1, end):
        tt = lines[n].strip()
        if not tt:
            continue
        d = ind(lines[n])
        if d == 2 and re.match(r"^\d+=\{$", tt):
            cur = int(tt[:-2])
            out[cur] = {}
            continue
        if d == 2 and tt == "}":
            cur = None
            continue
        if cur is not None and d == 3 and "=" in tt and not tt.endswith("={"):
            k, _, v = tt.partition("=")
            out[cur][k] = v.strip('"')
    _SAVE_POPS = out
    return out


def cmd_pops(args):
    """按州或国家统计人口：职业 × 文化，附带劳动力。"""
    global _DEDUPE_BY
    p = _attach(args)
    try:
        pops = _load_save_pops()
        _DEDUPE_BY = None
        addrs = _instances_of(p, "CPop", 0, verbose=False)
        rows = []
        prof_name, rel_name = {}, {}
        for a in addrs:
            buf = p.read(a, 0x58)
            if not buf:
                continue
            pid = struct.unpack_from("<I", buf, OFF_POP_ID)[0] & 0xFFFFFF
            st = struct.unpack_from("<i", buf, OFF_POP_STATE)[0]
            cul = struct.unpack_from("<i", buf, OFF_POP_CULTURE)[0]
            wl = struct.unpack_from("<i", buf, OFF_POP_WEALTH)[0]
            wf = struct.unpack_from("<q", buf, OFF_POP_WORKFORCE)[0]
            prof = struct.unpack_from("<Q", buf, OFF_POP_PROF)[0]
            rel = struct.unpack_from("<Q", buf, OFF_POP_RELIGION)[0]
            rec = pops.get(pid, {})
            if rec:
                if prof not in prof_name and rec.get("type"):
                    prof_name[prof] = rec["type"]
                if rel not in rel_name and rec.get("religion"):
                    rel_name[rel] = rec["religion"]
            rows.append((st, cul, prof, rel, wf, wl, pid))

        # 州 -> 国家 映射（用于 --country 过滤）
        owner = {}
        if args.country is not None:
            _DEDUPE_BY = OFF_STATE_ID
            for a in _instances_of(p, "CState", 0, verbose=False):
                sid = p.read_u32(a + OFF_STATE_ID)
                ow = p.read_u32(a + OFF_STATE_OWNER)
                if sid is not None and ow == args.country:
                    owner[sid] = ow

        sel = rows
        if args.state is not None:
            sel = [r for r in sel if r[0] == args.state]
        if args.country is not None:
            sel = [r for r in sel if r[0] in owner]
        if args.profession:
            sel = [r for r in sel if prof_name.get(r[2]) == args.profession]

        agg = {}
        for st, cul, prof, rel, wf, wl, pid in sel:
            key = (prof_name.get(prof, "0x%X" % prof), rel_name.get(rel, "?"))
            agg[key] = agg.get(key, 0) + wf
        total = sum(agg.values())
        head = []
        if args.state is not None:
            head.append("州 %d" % args.state)
        if args.country is not None:
            head.append("国家 %d（%d 个州）" % (args.country, len(owner)))
        print("人口统计%s：%d 条 pop，就业人数合计 %s"
              % (("（%s）" % "，".join(head)) if head else "",
                 len(sel), _qty_raw(total)))
        print("  （口径：+0x30 是「就业人数」，不是总劳动力；总量请看 states 的 pop 列）")
        print("  %-18s %-14s %14s %8s" % ("职业", "宗教", "就业人数", "占比"))
        for (pn, rn), wf in sorted(agg.items(), key=lambda kv: -kv[1])[:args.limit or len(agg)]:
            print("  %-18s %-14s %14s %7.1f%%"
                  % (pn, rn, _qty_raw(wf), 100.0 * wf / total if total else 0))
        if args.csv:
            import csv
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["profession", "religion", "workforce"])
                for (pn, rn), wf in sorted(agg.items(), key=lambda kv: -kv[1]):
                    w.writerow([pn, rn, wf])
            print("\n已写出 %s" % args.csv)
        return 0
    finally:
        p.close()


# ---------------------------------------------------------------- decrees
OFF_DECREE_COUNTRY = 0x08
OFF_DECREE_STATE = 0x0C
OFF_DECREE_TYPE = 0x10
OFF_DTYPE_NAME = 0x18
DECREE_FILE = os.path.join(os.environ.get(
    "V3_GAME", r"E:\SteamLibrary\steamapps\common\Victoria 3\game"),
    "common", "decrees", "00_decree.txt")


def _load_decrees():
    import json
    cache = os.path.join(CACHE_DIR, "decrees.json")
    if os.path.exists(cache):
        try:
            return json.load(open(cache, encoding="utf-8"))
        except Exception:
            pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gamedata
    raw = gamedata.parse_file(DECREE_FILE)
    out = {}
    for name, d in raw.items():
        if not isinstance(d, dict):
            continue
        mod = d.get("modifier")
        out[name] = {
            "cost": d.get("cost", ""),
            "modifiers": {k: v for k, v in mod.items() if not k.startswith("_")}
            if isinstance(mod, dict) else {},
        }
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def cmd_decrees(args):
    """列出地方法令：按国家或州过滤，附带效果。"""
    global _DEDUPE_BY
    _DEDUPE_BY = None
    p = _attach(args)
    try:
        defs = _load_decrees()
        rows = []
        for cid, sid, nm in _all_decrees(p):
            if args.country is not None and cid != args.country:
                continue
            if args.state is not None and sid != args.state:
                continue
            rows.append((cid, sid, nm, 0))
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        scope = []
        if args.country is not None:
            scope.append("国家 %d" % args.country)
        if args.state is not None:
            scope.append("州 %d" % args.state)
        print("法令 %d 条%s" % (len(rows),
                              ("（%s）" % "，".join(scope)) if scope else ""))
        shown = rows[:args.limit or len(rows)]
        detail = args.detail if args.detail is not None else (len(shown) <= 20)
        if detail:
            for cid, sid, name, a in shown:
                d = defs.get(name, {})
                eff = d.get("modifiers", {})
                print("\n  国家 %s  州 %s  %s  (花费 %s)"
                      % (cid, sid, name, d.get("cost", "-")))
                if not eff:
                    print("       (无效果)")
                for k, v in eff.items():
                    print("      %-56s %s" % (k, v))
        else:
            print("  %-6s %-6s %-40s %-6s %s"
                  % ("国家", "州", "法令", "成本", "效果数"))
            for cid, sid, name, a in shown:
                d = defs.get(name, {})
                eff = d.get("modifiers", {})
                txt = "; ".join("%s=%s" % (k, v) for k, v in eff.items())
                print("  %-6s %-6s %-40s %-6s %-3d %s"
                      % (cid, sid, name, d.get("cost", "-"), len(eff), txt[:70]))
        if args.csv:
            import csv
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["country", "state", "decree", "cost", "modifiers"])
                for cid, sid, name, a in rows:
                    d = defs.get(name, {})
                    w.writerow([cid, sid, name, d.get("cost", ""),
                                "; ".join("%s=%s" % kv
                                          for kv in d.get("modifiers", {}).items())])
            print("\n已写出 %s（%d 行）" % (args.csv, len(rows)))
        return 0
    finally:
        p.close()


OFF_STATE_TIMED = 0x1808      # vector<*>: 州的临时（带期限）修正


def _print_timed_list(p, base, off, title):
    ptr = p.read_u64(base + off)
    n = p.read_u32(base + off + 8)
    print("%s  临时修正 %d 个" % (title, n or 0))
    if not ptr or not n:
        return
    raw = p.read(ptr, 8 * n)
    if not raw:
        return
    print("  %-44s %10s  %-8s %-8s %s" % ("修正名", "数值", "起始年", "结束年", "时长"))
    for i in range(n):
        inst = struct.unpack_from("<Q", raw, 8 * i)[0]
        if not inst:
            continue
        d = p.read_u64(inst + OFF_TMOD_DEF)
        name = icon = None
        if d:
            name_ptr = p.read_u64(d + OFF_STATICMOD_NAME)
            name = _first_string(p, name_ptr) if name_ptr else None
            icon_ptr = p.read_u64(d + OFF_STATICMOD_ICON)
            icon = _cstr(p, icon_ptr) if icon_ptr else None
        val = (p.read_i64(inst + OFF_TMOD_VALUE) or 0) / FIXED
        st = p.read_i64(inst + OFF_TMOD_START) or 0
        en = p.read_i64(inst + OFF_TMOD_END) or 0
        label = name or (icon.rsplit("/", 1)[-1] if icon else "?")
        print("  %-44s %10.2f  %-8s %-8s %.0f 天"
              % (label[:44], val, _decode_date(st), _decode_date(en),
                 _duration_days(st, en)))


def cmd_statemods(args):
    """列出某州的临时修正（带期限）。"""
    global _DEDUPE_BY
    if _DEDUPE_BY is None:
        _DEDUPE_BY = OFF_STATE_ID
    p = _attach(args)
    try:
        addrs = _instances_of(p, "CState", 0, verbose=False)
        target = None
        for a in addrs:
            if p.read_u32(a + OFF_STATE_ID) == args.state:
                target = a
                break
        if target is None:
            print("找不到州 %d" % args.state)
            return 1
        _print_timed_list(p, target, OFF_STATE_TIMED,
                          "州 %d @ 0x%X" % (args.state, target))
        return 0
    finally:
        p.close()


# ----------------------------------------------------------------- building
OFF_BLD_ID = 0x08
OFF_BLD_STATE = 0xDC
OFF_BLD_LEVELS = 0xE0
OFF_BLD_CASH = 0x128
OFF_BLD_STAFF = 0x120        # i64 定点 ÷100000，满员时 == levels
OFF_BLD_HIRING = 0x110       # i64 定点 ÷100000，通常 0.1
OFF_BLD_SALARY = 0x130
OFF_BLD_PROFIT = 0x158
OFF_BLD_TYPE = 0xE8          # 指向 CBuildingType
OFF_BTYPE_NAME = 0x18        # CBuildingType 上的 std::string 名字


def cmd_buildings(args):
    """按建筑列出：id / 所在州 / 等级 / 类型 / 现金 / 薪资 / 利润。"""
    global _DEDUPE_BY
    if _DEDUPE_BY is None:
        _DEDUPE_BY = OFF_BLD_ID        # 建筑自带唯一 id，默认按它去重
    p = _attach(args)
    try:
        addrs = _instances_of(p, "CBuilding", 0, verbose=False)
        rows = []
        tcache = {}
        for a in addrs:
            bid = p.read_u32(a + OFF_BLD_ID)
            if bid is None:
                continue
            tp = p.read_u64(a + OFF_BLD_TYPE)
            name = tcache.get(tp)
            if name is None and tp:
                name = p.msvc_string(tp + OFF_BTYPE_NAME) or "?"
                tcache[tp] = name
            rows.append((
                bid,
                p.read_u32(a + OFF_BLD_STATE),
                p.read_u32(a + OFF_BLD_LEVELS),
                name or "?",
                (p.read_i64(a + OFF_BLD_STAFF) or 0) / FIXED,
                p.read_u32(a + OFF_BLD_CASH),
                (p.read_i64(a + OFF_BLD_SALARY) or 0) / FIXED,
                (p.read_i64(a + OFF_BLD_PROFIT) or 0) / FIXED,
                a,
            ))
        if args.state is not None:
            rows = [r for r in rows if r[1] == args.state]
        rows.sort()
        print("CBuilding %d 个%s" % (len(rows),
                                    "（state=%d）" % args.state if args.state is not None else ""))
        print("  %-6s %-6s %6s %8s %-34s %9s %8s %11s"
              % ("id", "state", "levels", "staff", "type", "cash", "salary", "profit"))
        for bid, st, lv, ty, staff, cash, sal, prof, a in rows[:args.limit or len(rows)]:
            print("  %-6s %-6s %6s %8s %-34s %9s %8.2f %11.2f"
                  % (bid, st, lv, staff, ty, cash, sal, prof))
            if getattr(args, "pms", False):
                pms = _building_pms(p, a)
                print("        PM: %s" % (", ".join(pms) if pms else "（无）"))
        if args.csv:
            import csv
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["building_id", "state", "levels", "type", "cash",
                            "salary", "profit", "addr"])
                for bid, st, lv, ty, cash, sal, prof, a in rows:
                    w.writerow([bid, st, lv, ty, cash, sal, prof, "0x%X" % a])
            print("\n已写出 %s（%d 行）" % (args.csv, len(rows)))
        return 0
    finally:
        p.close()


# ------------------------------------------------------------------ findref
def cmd_findref(args):
    """在 A 类对象里找「指向 B 类对象」的指针字段。

    思路：B 类对象地址集合已知（靠 vtable 扫描），逐偏移统计有多少实例的
    8 字节值落在这个集合里。命中率高的偏移就是 B 类指针。
    """
    p = _attach(args)
    try:
        targets = set(_instances_of(p, args.target, 0, verbose=False))
        print("%s 实例 %d 个（指向目标集合）" % (args.target, len(targets)))
        if not targets:
            print("目标类没有实例")
            return 1
        srcs = _instances_of(p, args.cls, 0, verbose=False)
        print("%s 实例 %d 个" % (args.cls, len(srcs)))
        hits = {}
        n = len(srcs)
        for a in srcs:
            buf = p.read(a, args.len)
            for off in range(0, len(buf) - 8 + 1, 8):
                v = struct.unpack_from("<Q", buf, off)[0]
                if v in targets:
                    hits[off] = hits.get(off, 0) + 1
        rows = sorted(hits.items(), key=lambda kv: -kv[1])
        print("\n  %-10s %-12s %s" % ("偏移", "命中", "占实例比例"))
        for off, cnt in rows[:args.top]:
            print("  +0x%-8X %-12s %.1f%%"
                  % (off, "%d/%d" % (cnt, n), 100.0 * cnt / max(n, 1)))
        if not rows:
            print("  没有找到指向 %s 的字段" % args.target)
        return 0
    finally:
        p.close()


# ----------------------------------------------------------------- findenum
def cmd_findenum(args):
    """找「小整数枚举」字段：取值种类多、但最大值很小。

    建筑类型只有 122 种，所以「不同取值≈122 且 max 很小」的字段就是类型索引。
    """
    p = _attach(args)
    try:
        addrs = _instances_of(p, args.cls, 0, verbose=False)
        n = len(addrs)
        print("%s 实例 %d 个，扫描 0x%X 字节" % (args.cls, n, args.len))
        bufs = [p.read(a, args.len) for a in addrs]
        rows = []
        for off in range(0, args.len - 4 + 1, args.step):
            vals = []
            for b in bufs:
                v = struct.unpack_from("<I", b, off)[0]
                if args.skip_zero and v == 0:
                    continue
                vals.append(v)
            if not vals:
                continue
            uniq = len(set(vals))
            mx = max(vals)
            if mx > args.max or uniq < args.min_distinct or uniq > args.max_distinct:
                continue
            rows.append((uniq, off, mx, len(vals)))
        rows.sort(reverse=True)
        print("\n  %-10s %-10s %-8s %s" % ("偏移", "不同取值", "最大值", "样本数"))
        for uniq, off, mx, cnt in rows[:args.top]:
            print("  +0x%-8X %-10d %-8d %d" % (off, uniq, mx, cnt))
        if not rows:
            print("  没有符合范围的字段")
        return 0
    finally:
        p.close()


# -------------------------------------------------------------------- map
def cmd_map(args):
    """按偏移统计一个类的结构：每一格存的是指针/整数/浮点。

    比逐个 dump 快得多，用来快速摸清一个大对象的形状。
    """
    p = _attach(args)
    try:
        insts = _instances_of(p, args.cls, args.limit)
        if not insts:
            print("没有实例")
            return 1
        base_img = p.module_base("victoria3.exe") or 0x140000000
        lo_img, hi_img = base_img, base_img + 0x8000000
        bufs = [p.read(a, args.len) for a in insts]
        n = len(insts)
        print("%s：%d 个实例，扫描 0x%X 字节\n" % (args.cls, n, args.len))
        print("  %-8s %-8s %-26s %s" % ("偏移", "非零", "类型", "样例"))
        for off in range(0, args.len - (8 if args.step == 8 else 4), args.step):
            k_ptr_img = k_ptr_heap = k_small = k_big = k_float = 0
            sample = None
            for b in bufs:
                if args.step == 8:
                    v = struct.unpack_from("<Q", b, off)[0]
                else:
                    v = struct.unpack_from("<I", b, off)[0]
                if v == 0:
                    continue
                if sample is None:
                    sample = v
                if args.step == 8 and lo_img <= v < hi_img:
                    k_ptr_img += 1
                elif args.step == 8 and 0x10000 <= v < 0x7FFFFFFF0000:
                    k_ptr_heap += 1
                elif v < 0x100000:
                    k_small += 1
                else:
                    k_big += 1
                f = struct.unpack_from("<f", b, off)[0] if args.step == 4 else None
                if f is not None and 0.0 < abs(f) < 1e6:
                    k_float += 1
            nz = k_ptr_img + k_ptr_heap + k_small + k_big
            if nz == 0:
                continue
            kinds = []
            if k_ptr_img:
                kinds.append("指针(镜像):%d" % k_ptr_img)
            if k_ptr_heap:
                kinds.append("指针(堆):%d" % k_ptr_heap)
            if k_float and args.step == 4:
                kinds.append("浮点:%d" % k_float)
            if k_small:
                kinds.append("小整数:%d" % k_small)
            if k_big:
                kinds.append("大整数:%d" % k_big)
            if not kinds:
                continue
            if args.step == 4 and nz:
                fv = struct.unpack_from("<f", bufs[0], off)[0]
                sv = "%d / %.4f" % (sample, fv)
            else:
                sv = "%d" % sample
            if args.float_only:
                # 只保留「绝大多数实例读成 0.001~1000 的浮点」的偏移，
                # 并显示取值范围与不同取值个数，用来区分常量和真实数据
                vals = []
                for b in bufs:
                    f = struct.unpack_from("<f", b, off)[0]
                    # 允许 0，也允许正常的普通浮点数；排除反规格化数（整数被当 float 读时的样子）
                    if f == 0.0 or (1e-3 <= abs(f) <= 1e4):
                        vals.append(f)
                if args.step != 4 or len(vals) < args.float_ratio * n:
                    continue
                if len(set(vals)) < args.min_distinct:
                    continue
                uniq = len(set(vals))
                sv = "%g ~ %g   不同取值 %d" % (min(vals), max(vals), uniq)
                print("  +0x%-6X %-8s %-26s %s"
                      % (off, "%d/%d" % (nz, n), "浮点", sv))
                continue
            print("  +0x%-6X %-8s %-26s %s"
                  % (off, "%d/%d" % (nz, n), " ".join(kinds)[:26], sv))
        return 0
    finally:
        p.close()


# ------------------------------------------------------------------- vecs
def cmd_vecs(args):
    """找出对象里所有「N 元素 vector」并打印内容摘要。

    MSVC std::vector 的形态是 {ptr, size, capacity} 三个 qword。
    这里按 size==cap==N 去找，适合商品表这种固定长度容器。
    """
    p = _attach(args)
    try:
        insts = _instances_of(p, args.cls, args.limit or 1)
        if not insts:
            print("没有实例")
            return 1
        a = insts[0]
        buf = p.read(a, args.len)
        print("%s 实例 0x%X，在 0x%X 字节里找 %d 元素 vector\n"
              % (args.cls, a, args.len, args.count))
        found = 0
        for off in range(0, len(buf) - 0x18, 8):
            ptr = struct.unpack_from("<Q", buf, off)[0]
            n = struct.unpack_from("<I", buf, off + 8)[0]
            cap = struct.unpack_from("<I", buf, off + 0xC)[0]
            if n != args.count or cap != args.count:
                continue
            if not (0x10000 <= ptr < 0x7FFFFFFF0000):
                continue
            data = p.read(ptr, args.count * 8)
            if len(data) < args.count * 8:
                continue
            qs = list(struct.unpack_from("<%dq" % args.count, data, 0))
            i64 = [struct.unpack("<q", struct.pack("<Q", x))[0] for x in qs]
            f32pair = [struct.unpack("<f", struct.pack("<I", x & 0xFFFFFFFF))[0] for x in qs]
            found += 1
            print("  +0x%-6X -> 0x%X  (%d 项)" % (off, ptr, n))
            print("     i64  前6: %s" % i64[:6])
            print("     /1e5 前6: %s" % ["%.2f" % (v / 100000.0) for v in i64[:6]])
            print("     f32低半 前6: %s" % ["%.3f" % v for v in f32pair[:6]])
            inprice = sum(1 for v in i64 if 100000 <= v <= 30000000)
            print("     落在价格区间(1~300)×1e5 的项数: %d / %d" % (inprice, args.count))
            print()
        if not found:
            print("  没找到 %d 元素的 vector" % args.count)
        return 0
    finally:
        p.close()


# ----------------------------------------------------------------- arrays
def cmd_arrays(args):
    """找「连续 N 个数值都落在某个区间」的偏移——用来发现价格表这类数组。

    不依赖具体数值，所以即使数据变了也能定位结构。
    """
    p = _attach(args)
    try:
        insts = _instances_of(p, args.cls, args.limit)
        if not insts:
            print("没有实例")
            return 1
        print("%s：%d 个实例，每实例读 0x%X 字节，寻找连续 %d 个值落在区间内"
              % (args.cls, len(insts), args.len, args.count))
        kinds = [("f32", 4, args.fmin, args.fmax), ("f64", 8, args.fmin, args.fmax),
                 ("i32", 4, args.imin, args.imax), ("i64", 8, args.imin, args.imax)]
        agg = {}
        for kind, sz, lo, hi in kinds:
            fmt = {"f32": "<f", "f64": "<d", "i32": "<i", "i64": "<q"}[kind]
            for a in insts:
                buf = p.read(a, args.len)
                n = len(buf) // sz
                run = 0
                run_start = 0
                for i in range(n):
                    v = struct.unpack_from(fmt, buf, i * sz)[0]
                    if lo <= v <= hi:
                        if run == 0:
                            run_start = i
                        run += 1
                        if run == args.count:
                            key = (kind, run_start * sz)
                            agg[key] = agg.get(key, 0) + 1
                    else:
                        run = 0
        rows = sorted(agg.items(), key=lambda kv: -kv[1])
        if not rows:
            print("  没有找到这样的连续数组")
            return 1
        print("\n%-6s %-10s %s" % ("类型", "起始偏移", "有多少实例在该偏移有这种数组"))
        for (kind, off), n in rows[:args.top]:
            print("  %-6s +0x%-8X %d / %d" % (kind, off, n, len(insts)))
        kind, off = rows[0][0]
        print("\n查看第一个实例的该段：python run.py dump 0x%X --offset 0x%X --len 0x100"
              % (insts[0], off))
        return 0
    finally:
        p.close()


# -------------------------------------------------------------------- read
def cmd_read(args):
    """按 类 + 偏移 读实时值。配合 find/hunt 用。"""
    p = _attach(args)
    try:
        addrs = _instances_of(p, args.cls, args.limit, verbose=False)
        if not addrs:
            print("没有实例")
            return 1
        print("%s：%d 个实例，读 +0x%X (%s)" % (args.cls, len(addrs), args.offset, args.kind))
        shown = 0
        values = []
        for a in addrs:
            if args.kind.startswith("f"):
                raw = p.read(a + args.offset, 8 if args.kind == "f64" else 4)
                v = struct.unpack("<d" if args.kind == "f64" else "<f", raw)[0]
            else:
                fmt = {"i8": "<b", "u8": "<B", "i16": "<h", "u16": "<H",
                       "i32": "<i", "u32": "<I", "i64": "<q", "u64": "<Q"}[args.kind]
                sz = struct.calcsize(fmt)
                raw = p.read(a + args.offset, sz)
                v = struct.unpack(fmt, raw)[0]
            if args.near is not None and abs(v - args.near) > args.tol:
                continue
            values.append(v)
            if not args.summary:
                print("  0x%-14X %s" % (a, v))
            shown += 1
        if args.summary and values:
            from collections import Counter
            c = Counter(values)
            print("\n  不同取值 %d 种，最小 %s，最大 %s" % (len(c), min(values), max(values)))
            print("  最常见的取值：")
            for v, n in c.most_common(15):
                print("    %-14s %d 次" % (v, n))
        if args.near is not None:
            print("  匹配 %d / %d 个实例" % (shown, len(addrs)))
    finally:
        p.close()
    return 0


# ------------------------------------------------------------------ watch
def _vals(raw, off):
    """把一个字段解成 (i32, u32, f32, i64)。"""
    b = raw[off:off + 8]
    if len(b) < 8:
        return None
    return (struct.unpack("<i", b[:4])[0], struct.unpack("<I", b[:4])[0],
            struct.unpack("<f", b[:4])[0], struct.unpack("<q", b)[0])


def cmd_watch(args):
    """差分：让游戏跑一段时间，看哪些字段真的在变。

    这是定位业务字段最可靠的办法——不需要猜数值，也不需要模板。
    """
    import statistics

    p = _attach(args)
    try:
        addrs = _instances_of(p, args.cls, args.limit)
        if not addrs:
            print("没有实例")
            return 1
        print("%s：跟踪 %d 个实例，%d 轮，间隔 %.1fs，每实例读 0x%X 字节"
              % (args.cls, len(addrs), args.rounds, args.interval, args.len))
        prev = {a: p.read(a, args.len) for a in addrs}
        for rnd in range(1, args.rounds + 1):
            time.sleep(args.interval)
            cur = {a: p.read(a, args.len) for a in addrs}
            old = prev
            groups = {}
            for a in addrs:
                for off in discover.diff_offsets(old[a], cur[a]):
                    groups.setdefault(off, []).append(a)
            print("\n== 第 %d 轮：%d 个偏移有变化 ==" % (rnd, len(groups)))
            print("  %-10s %-14s %-30s %s" % ("偏移", "变化实例", "当前值(i32 / f32)", "中位增量(i64)"))
            rows = sorted(groups.items(), key=lambda kv: -len(kv[1]))
            for off, insts in rows[:args.top]:
                a = insts[0]
                ov = _vals(cur[a], off)
                deltas = []
                for i in insts:
                    v0, v1 = _vals(old[i], off), _vals(cur[i], off)
                    if v0 and v1:
                        deltas.append(v1[3] - v0[3])
                med = int(statistics.median(deltas)) if deltas else 0
                print("  +0x%-7X %-14s i32=%-14d f32=%-14.4f %d"
                      % (off, "%d/%d" % (len(insts), len(addrs)),
                         ov[0] if ov else 0, ov[2] if ov else 0.0, med))
            prev = cur
    finally:
        p.close()
    return 0


# ----------------------------------------------------------------- static
def cmd_static(args):
    """静态分析：字符串 -> 交叉引用 -> 函数 -> 字段偏移（不需要开游戏）。"""
    from .static import Static

    S = Static(EXE)
    S.functions()
    if args.disp is not None:
        disp = int(args.disp, 0)
        hits = S.find_disp(disp)
        print("偏移 0x%X：%d 条指令" % (disp, len(hits)))
        funcs = []
        for addr, mnem, op in hits[:12]:
            print("  0x%X  %s %s" % (addr, mnem, op))
            f = S.func_of(addr)
            if f and f not in funcs:
                funcs.append(f)
        for b, e in funcs[:6]:
            print("\n  函数 0x%X-0x%X (%d 字节)" % (b, e, e - b))
            strs = S.function_strings(b, e, limit=8)
            print("    引用字符串:", strs)
            from collections import Counter
            c = Counter(d for d, _, _ in S.field_accesses(b, e))
            deep = [("+0x%X" % d, n) for d, n in c.most_common(12) if d >= 0x400]
            print("    深层偏移:", deep)
        return 0

    if args.func_addr is not None:
        b = int(args.func_addr, 0)
        f = S.func_of(b)
        print("函数:", f)
        if not f:
            return 1
        from collections import Counter
        acc = S.field_accesses(*f)
        print("引用字符串:", S.function_strings(*f, limit=10))
        c = Counter(d for d, _, _ in acc)
        for d, n in c.most_common(20):
            samples = [f"{m} {o}" for dd, m, o in acc if dd == d][:2]
            print("  +0x%-7X %2d  %s" % (d, n, samples))
        return 0

    if not args.grep:
        print("需要 --grep <字符串> 或 --disp <偏移> 或 --fn <地址>")
        return 2
    r = S.trace(args.grep)
    print("字符串:", [("0x%X" % a, s) for a, s in r["strings"]])
    print("代码引用:", ["0x%X" % x for x in r["xrefs"]])
    print("所在函数:", [("0x%X-0x%X" % f) for f in r["funcs"]])
    print("字段偏移（按出现次数）:")
    for d, n in r["offsets"][:20]:
        print("  +0x%-7X %2d  %s" % (d, n, r["samples"].get(d, [])[:2]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="v3live", description="V3 实时内存读取（只读）")
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--refresh", action="store_true", help="忽略实例缓存，强制重新扫描")
    ap.add_argument("--dedupe-keep", choices=["auto", "first", "last"], default="auto",
                    help="同名实例保留哪一个：auto=按字段合理性自动挑，first/last=按地址")
    ap.add_argument("--dedupe-by", type=lambda x: int(x, 0), default=None,
                    help="按实例内某偏移的 u32 去重（如 CState 用 0x10 按 id 去重）")
    ap.add_argument("--all-vtables", action="store_true",
                    help="用全部 vtable 定位实例（默认只用主 vtable，对象计数更准）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("classes")
    s.add_argument("--match", default=None)
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_classes)

    s = sub.add_parser("scan")
    s.add_argument("--match", default=None)
    s.add_argument("--min", type=int, default=1)
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("dump")
    s.add_argument("cls", help="类名，或直接给 0x 开头的地址")
    s.add_argument("--limit", type=int, default=3)
    s.add_argument("--len", type=lambda x: int(x, 0), default=0x400)
    s.add_argument("--offset", type=lambda x: int(x, 0), default=0)
    s.set_defaults(fn=cmd_dump)

    s = sub.add_parser("laws")
    s.add_argument("--country", type=int, required=True)
    s.add_argument("--csv", default=None)
    s.set_defaults(fn=cmd_laws)

    s = sub.add_parser("tech")
    s.add_argument("--country", type=int, default=None, help="默认取玩家国家")
    s.add_argument("--csv", default=None)
    s.add_argument("--save", default=None, help="配合 --from-save，默认用 DEFAULT_SAVE")
    s.add_argument("--from-save", action="store_true", help="改走存档（离线模式）")
    s.set_defaults(fn=cmd_tech)

    s = sub.add_parser("tax")
    s.add_argument("--country", type=int, default=None, help="默认取玩家国家")
    s.add_argument("--levels", action="store_true", help="显示该税法的五档税率表")
    s.add_argument("--tariffs", action="store_true", help="逐商品列出关税等级（存档）")
    s.add_argument("--mem-tariffs", action="store_true",
                   help="逐商品列出关税等级（内存，含陈旧槽，仅供参考）")
    s.add_argument("--save", default=None, help="配合 --tariffs，默认用 DEFAULT_SAVE")
    s.set_defaults(fn=cmd_tax)

    s = sub.add_parser("politics")
    s.add_argument("--country", type=int, default=None, help="默认取玩家国家")
    s.add_argument("--detail", action="store_true", help="加打激进/忠诚力量与领袖")
    s.add_argument("--movements", action="store_true", help="加打政治运动")
    s.add_argument("--csv", default=None)
    s.set_defaults(fn=cmd_politics)

    s = sub.add_parser("gdp")
    s.add_argument("--country", type=int, default=None, help="默认取玩家国家")
    s.add_argument("--by-state", action="store_true", help="同时按州列出")
    s.add_argument("--formula", action="store_true",
                   help="按游戏文件公式算：Σ(产出×价格) - Σ(投入×价格)")
    s.add_argument("--staffing", action="store_true", default=True,
                   help="数量按实际就业规模折算（默认就是这个，实测最准）")
    s.add_argument("--levels", action="store_true",
                   help="改用建筑等级折算（满员假设，会高估未招满的建筑）")
    s.add_argument("--market-prices", action="store_true",
                   help="用全国市场统一价估值（默认就是它）")
    s.add_argument("--local-prices", action="store_true",
                   help="用各州本地价估值")
    s.add_argument("--base-prices", action="store_true",
                   help="用商品基础价（common/goods 的 cost）估值")
    s.add_argument("--out-price", choices=("local", "market", "base"),
                   default=None, help="单独指定产出侧价格基准")
    s.add_argument("--in-price", choices=("local", "market", "base"),
                   default=None, help="单独指定投入侧价格基准")
    s.add_argument("--by-type", action="store_true",
                   help="列出计入的建筑类型与数量，用于核对是否漏算")
    s.add_argument("--compare", default=None,
                   help="多国并排比对 GDP 构成，例如 --compare 157,1,5,3,10")
    s.add_argument("--top", type=int, default=15, help="对比表显示前 N 个商品")
    s.add_argument("--all-buildings", action="store_true",
                   help="把不产出商品的建筑也算进去（政府行政楼/兵营等，会扣减它们的投入）")
    s.set_defaults(fn=cmd_gdp)

    s = sub.add_parser("capacity")
    s.add_argument("--country", type=int, default=None, help="默认取玩家国家")
    s.add_argument("--scale", choices=("throughput", "levels", "staffing"),
                   default="throughput",
                   help="workforce_scaled 的折算基准：throughput=等级×吞吐量(默认) levels=等级 staffing=就业规模")
    s.add_argument("--gdp", default=None,
                   help="GDP 数值（影响建造力修正系数）；也可以写 auto 实时估算")
    s.set_defaults(fn=cmd_capacity)

    s = sub.add_parser("prewarm")
    s.add_argument("--refresh", action="store_true", help="强制重扫（忽略已有缓存）")
    s.set_defaults(fn=cmd_prewarm)

    s = sub.add_parser("institutions")
    s.add_argument("--country", type=int, default=None, help="默认取玩家国家")
    s.add_argument("--all", action="store_true", help="列出全部国家")
    s.set_defaults(fn=cmd_institutions)

    s = sub.add_parser("companies")
    s.add_argument("--country", type=int, default=None, help="默认取玩家国家")
    s.add_argument("--all", action="store_true", help="列出全部国家")
    s.set_defaults(fn=cmd_companies)

    s = sub.add_parser("pops")
    s.add_argument("--state", type=int, default=None)
    s.add_argument("--country", type=int, default=None)
    s.add_argument("--profession", default=None)
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--csv", default=None)
    s.set_defaults(fn=cmd_pops)

    s = sub.add_parser("decrees")
    s.add_argument("--country", type=int, default=None)
    s.add_argument("--state", type=int, default=None)
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--csv", default=None)
    s.add_argument("--detail", action="store_true", default=None,
                   help="逐条展开完整效果（默认结果<=20 条时自动开启）")
    s.set_defaults(fn=cmd_decrees)

    s = sub.add_parser("statemods")
    s.add_argument("--state", type=int, required=True)
    s.set_defaults(fn=cmd_statemods)

    s = sub.add_parser("lawmods")
    s.add_argument("--country", type=int, required=True)
    s.add_argument("--save", default=None, help="默认用 DEFAULT_SAVE")
    s.add_argument("--csv", default=None)
    s.add_argument("--no-tech", action="store_true", help="只算法律，不含科技")
    s.add_argument("--from-save", action="store_true", help="法律也走存档（离线模式）")
    s.set_defaults(fn=cmd_lawmods)

    s = sub.add_parser("timedmods")
    s.add_argument("--id", type=int, default=None, help="默认取玩家国家")
    s.set_defaults(fn=cmd_timedmods)

    s = sub.add_parser("modifiers")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--csv", default=None)
    s.set_defaults(fn=cmd_modifiers)

    s = sub.add_parser("localprices")
    s.add_argument("--state", type=int, required=True)
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--sort", choices=["none", "dev"], default="dev")
    s.set_defaults(fn=cmd_localprices)

    s = sub.add_parser("buildings")
    s.add_argument("--state", type=int, default=None, help="只看某州的建筑")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--csv", default=None)
    s.add_argument("--pms", action="store_true",
                   help="同时列出每栋建筑当前启用的生产方式")
    s.set_defaults(fn=cmd_buildings)

    s = sub.add_parser("states")
    s.add_argument("--owner", type=int, default=None, help="只看某国的州")
    s.add_argument("--id", type=int, default=None, help="只看某个州 id")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--csv", default=None, help="导出 CSV 路径")
    s.add_argument("--ids-only", action="store_true", help="只输出 id 列表，便于和存档对比")
    s.add_argument("--max-id", type=int, default=0, help="过滤掉大于此值的 id（CState 用 1155）")
    s.set_defaults(fn=cmd_states)

    s = sub.add_parser("quals")
    s.add_argument("--state", type=int, required=True, help="州 id")
    s.add_argument("--csv", default=None)
    s.set_defaults(fn=cmd_quals)

    s = sub.add_parser("report")
    s.add_argument("--country", type=int, default=None, help="默认取玩家国家")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("verify")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("market")
    s.add_argument("--id", type=int, default=None, help="市场 id，默认取玩家所在市场")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--sort", choices=["none", "gap"], default="gap")
    s.add_argument("--csv", default=None)
    s.set_defaults(fn=cmd_market)

    s = sub.add_parser("budget")
    s.add_argument("--id", type=int, default=None, help="国家 id，如大清 157")
    s.add_argument("--addr", default=None, help="直接给国家对象地址")
    s.set_defaults(fn=cmd_budget)

    s = sub.add_parser("prices")
    s.add_argument("--index", type=int, default=0, help="第几个市场实例")
    s.add_argument("--addr", default=None, help="直接给市场对象地址")
    s.set_defaults(fn=cmd_prices)

    s = sub.add_parser("findref")
    s.add_argument("cls")
    s.add_argument("--target", required=True, help="被指向的类，如 CBuildingType")
    s.add_argument("--len", type=lambda x: int(x, 0), default=0x290)
    s.add_argument("--top", type=int, default=15)
    s.set_defaults(fn=cmd_findref)

    s = sub.add_parser("findenum")
    s.add_argument("cls")
    s.add_argument("--len", type=lambda x: int(x, 0), default=0x290)
    s.add_argument("--step", type=int, default=4)
    s.add_argument("--max", type=int, default=300, help="允许的最大取值")
    s.add_argument("--min-distinct", type=int, default=40)
    s.add_argument("--max-distinct", type=int, default=400)
    s.add_argument("--skip-zero", action="store_true", default=True)
    s.add_argument("--top", type=int, default=20)
    s.set_defaults(fn=cmd_findenum)

    s = sub.add_parser("map")
    s.add_argument("cls")
    s.add_argument("--len", type=lambda x: int(x, 0), default=0x400)
    s.add_argument("--step", type=int, default=8, choices=[4, 8])
    s.add_argument("--float-only", action="store_true", help="只列出像浮点数的偏移（需 --step 4）")
    s.add_argument("--float-ratio", type=float, default=0.9)
    s.add_argument("--min-distinct", type=int, default=1, help="至少有多少种不同取值（过滤常量偏移）")
    s.add_argument("--limit", type=int, default=0)
    s.set_defaults(fn=cmd_map)

    s = sub.add_parser("find")
    s.add_argument("--kind", default="auto",
                   choices=["auto"] + list(discover.DTYPES),
                   help="auto = 依次试 i32/i64/f32/f64")
    s.add_argument("--value", type=float, required=True)
    s.add_argument("--classes", default=None, help="限定类名正则，默认全部类")
    s.add_argument("--tol", type=float, default=0,
                   help="容差：界面上四舍五入的值用这个，例如 --tol 5000")
    s.add_argument("--max-hits", type=int, default=20000)
    s.add_argument("--top", type=int, default=20)
    s.set_defaults(fn=cmd_find)

    s = sub.add_parser("peek")
    s.add_argument("addr", help="实例地址，形如 0x2545352CBF0")
    s.add_argument("--offset", type=lambda x: int(x, 0), required=True)
    s.add_argument("--kind", default="i32",
                   choices=["i8", "u8", "i16", "u16", "i32", "u32", "i64", "u64", "f32", "f64"])
    s.add_argument("--interval", type=float, default=3.0)
    s.add_argument("--rounds", type=int, default=5)
    s.set_defaults(fn=cmd_peek)

    s = sub.add_parser("findid")
    s.add_argument("cls")
    s.add_argument("--id", type=int, required=True, help="目标对象 id，如大清的 157")
    s.add_argument("--raw", action="store_true", help="用全部原始命中（对象可能被漏掉时用）")
    s.add_argument("--len", type=lambda x: int(x, 0), default=0x400)
    s.add_argument("--top", type=int, default=12)
    s.add_argument("--next-value", default="5770000", help="仅用于打印下一步命令")
    s.set_defaults(fn=cmd_findid)

    s = sub.add_parser("explore")
    s.add_argument("addr")
    s.add_argument("--value", type=float, required=True)
    s.add_argument("--tol", type=float, default=5000)
    s.add_argument("--kind", default="auto", choices=["auto"] + list(discover.DTYPES))
    s.add_argument("--len", type=lambda x: int(x, 0), default=0x4000)
    s.add_argument("--auto-scale", action="store_true",
                   help="同时试 x0.001/x0.01/x0.1/x10/x100/x1000/x10000（定点数）")
    s.add_argument("--scales", type=lambda s: [float(x) for x in s.split(",")],
                   default=[1.0, 100.0, 10000.0, 0.001])
    s.add_argument("--top", type=int, default=30)
    s.add_argument("--byte", dest="byte_mode", action="store_true",
                   help="按单字节搜索（等级这类 u8 字段）")
    s.set_defaults(fn=cmd_explore)

    s = sub.add_parser("hunt")
    s.add_argument("cls")
    s.add_argument("--value", type=float, action="append", required=True, help="可重复，一次扫内存匹配多个值")
    s.add_argument("--tol", type=float, default=5000)
    s.add_argument("--kind", default="auto", choices=["auto"] + list(discover.DTYPES))
    s.add_argument("--len", type=lambda x: int(x, 0), default=0x800)
    s.add_argument("--limit", type=int, default=0, help="0 = 全部实例")
    s.add_argument("--auto-scale", action="store_true")
    s.add_argument("--scales", type=lambda s: [float(x) for x in s.split(",")],
                   default=[1.0, 100.0, 10000.0, 0.001])
    s.add_argument("--top", type=int, default=25)
    s.set_defaults(fn=cmd_hunt)

    s = sub.add_parser("vecs")
    s.add_argument("cls")
    s.add_argument("--count", type=int, default=54, help="元素个数，默认 54（商品数）")
    s.add_argument("--len", type=lambda x: int(x, 0), default=0xD00)
    s.add_argument("--limit", type=int, default=1)
    s.set_defaults(fn=cmd_vecs)

    s = sub.add_parser("arrays")
    s.add_argument("cls")
    s.add_argument("--count", type=int, default=30, help="连续多少个值在区间内")
    s.add_argument("--len", type=lambda x: int(x, 0), default=0x4000)
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--fmin", type=float, default=1.0, help="浮点区间下界")
    s.add_argument("--fmax", type=float, default=300.0)
    s.add_argument("--imin", type=int, default=100000, help="整数(定点)区间下界")
    s.add_argument("--imax", type=int, default=30000000)
    s.add_argument("--top", type=int, default=20)
    s.set_defaults(fn=cmd_arrays)

    s = sub.add_parser("read")
    s.add_argument("cls")
    s.add_argument("--offset", type=lambda x: int(x, 0), required=True)
    s.add_argument("--kind", default="i32",
                   choices=["i8", "u8", "i16", "u16", "i32", "u32", "i64", "u64", "f32", "f64"])
    s.add_argument("--limit", type=int, default=0, help="0 = 全部实例")
    s.add_argument("--near", type=float, default=None, help="只显示接近这个值的实例")
    s.add_argument("--summary", action="store_true", help="只打印取值分布，不逐条输出")
    s.add_argument("--tol", type=float, default=10000)
    s.set_defaults(fn=cmd_read)

    s = sub.add_parser("watch")
    s.add_argument("cls")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--len", type=lambda x: int(x, 0), default=0x400)
    s.add_argument("--interval", type=float, default=2.0)
    s.add_argument("--rounds", type=int, default=3)
    s.add_argument("--top", type=int, default=12)
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("static")
    s.add_argument("--grep", default=None, help="按字符串正向追：字符串 -> 函数 -> 偏移")
    s.add_argument("--disp", default=None, help="反查：谁在访问这个偏移，如 0x1E08")
    s.add_argument("--func", dest="func_addr", default=None, help="反汇编某个函数并列出字段访问")
    s.set_defaults(fn=cmd_static)

    args = ap.parse_args(argv)
    _set_refresh(getattr(args, "refresh", False))
    _set_all_vtables(getattr(args, "all_vtables", False))
    _set_dedupe(getattr(args, "dedupe_by", None), getattr(args, "dedupe_keep", "first"))
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\n已中断（Ctrl+C）")
        return 130
    except (RuntimeError, OSError) as e:
        print("错误: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
