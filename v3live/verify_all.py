"""逐项验证所有逆向成果。

分四层：
  A. 游戏文件层  —— defines / 静态修正 / 法律 / 生产方式 / 法令 / 商品表
  B. 存档层      —— 收支数组槽位、好感阈值、机构/公司字段、建筑数
  C. 内存快照层  —— 用存档值撞 CCountry / CInterestGroup / CCompany 等快照
  D. 文档与代码  —— 字段表完整性、记录无乱码、命令注册、自检

用法:
  python v3live\\verify_all.py            # 全部
  python v3live\\verify_all.py A          # 只跑某一层（A/B/C/D）

退出码：全部通过为 0，否则为 1。
"""
import json
import os
import re
import struct
import subprocess
import sys

# 工程根 = tools/v3live 往上两级
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GAME = os.environ.get("V3_GAME", r"E:\SteamLibrary\steamapps\common\Victoria 3\game")
SAVE = os.path.join(ROOT, "data", "saves", "sb.txt")
TMP = os.path.join(ROOT, "data")
FIELDS = os.path.join(ROOT, "docs", "字段表.json")
RECORD = os.path.join(ROOT, "docs", "逆向记录.md")

RESULTS = []
SKIPS = []

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except Exception:
        pass


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, "异常: %s: %s" % (type(e).__name__, e)
    if ok is None:
        SKIPS.append((name, detail))
        print("  [SKIP] %-58s %s" % (name, detail))
        return None
    RESULTS.append((name, ok, detail))
    print("  [%s] %-58s %s" % ("PASS" if ok else "FAIL", name, detail))
    return ok


def _read(p):
    return open(p, encoding="utf-8", errors="replace").read()


# ---------------------------------------------------------------- A 游戏文件层
def a_defines():
    t = _read(os.path.join(GAME, "common", "defines", "00_defines.txt"))
    want = {
        "GOLD_RESERVE_LIMIT_FACTOR": "0.2",
        "COUNTRY_GDP_CONSTRUCTION_MODIFIER_DIVISOR": "2000000",
        "COUNTRY_GDP_CONSTRUCTION_MODIFIER_BASE_MULTIPLIER": "0.2",
        "COUNTRY_GDP_CONSTRUCTION_MODIFIER_SCALED_MULTIPLIER": "0.8",
        "COUNTRY_GDP_CONSTRUCTION_MODIFIER_MAX_MULTIPLIER": "1.0",
        "EARNINGS_ABSOLUTE_HIGH_THRESHOLD": "30",
        "EARNINGS_ABSOLUTE_LOW_THRESHOLD": "3",
        "EARNINGS_RATIO_HIGH_THRESHOLD": "1.5",
        "EARNINGS_RATIO_LOW_THRESHOLD": "0.66",
        "BUILDING_WAGES_AVERAGE_PRODUCTIVITY_THRESHOLD": "0.75",
    }
    bad = []
    for k, v in want.items():
        m = re.search(r"(?m)^\s*%s\s*=\s*([\d.]+)" % k, t)
        if not m or m.group(1) != v:
            bad.append("%s=%s(期望%s)" % (k, m.group(1) if m else "缺失", v))
    return (not bad, "10 个常量全部吻合" if not bad else "; ".join(bad))


def a_base_values():
    t = _read(os.path.join(GAME, "common", "static_modifiers", "00_code_static_modifiers.txt"))
    want = {"country_bureaucracy_add": "100", "country_minting_add": "500",
            "country_authority_add": "100", "country_weekly_innovation_add": "50",
            "state_infrastructure_add": "3"}
    bad = []
    for k, v in want.items():
        m = re.search(r"(?m)^\s*%s\s*=\s*([\d.]+)" % k, t)
        if not m or m.group(1) != v:
            bad.append("%s=%s(期望%s)" % (k, m.group(1) if m else "缺失", v))
    return (not bad, "base_values 5 项吻合" if not bad else "; ".join(bad))


def a_gdp_construction():
    t = _read(os.path.join(GAME, "common", "static_modifiers", "00_code_static_modifiers.txt"))
    i = t.find("country_gdp_construction")
    if i < 0:
        return False, "找不到 country_gdp_construction"
    seg = t[i:i + 300]
    ok = "country_construction_add = 10" in seg
    return ok, "country_construction_add = 10" if ok else "内容不符: %r" % seg[:80]


def a_tax_table():
    """核对四税种 × 五档，键名是 tax_consumption_add 这类。"""
    t = _read(os.path.join(GAME, "common", "laws", "01_taxation.txt"))
    i = t.find("law_per_capita_based_taxation")
    if i < 0:
        return False, "找不到该法律"
    seg = t[i:i + 8000]
    want = {
        "tax_consumption_add": [0.15, 0.20, 0.25, 0.30, 0.35],
        "tax_per_capita_add": [0.40, 0.55, 0.70, 0.85, 1.00],
        "tax_land_add": [0.20, 0.275, 0.35, 0.425, 0.50],
        "tax_income_add": [0.05, 0.075, 0.10, 0.125, 0.15],
    }
    bad = []
    for key, exp in want.items():
        got = [float(x) for x in re.findall(r"(?m)^\s*%s\s*=\s*([\d.]+)" % key, seg)[:5]]
        if len(got) != 5 or any(abs(a - b) > 1e-6 for a, b in zip(got, exp)):
            bad.append("%s=%s(期望%s)" % (key, got, exp))
    return (not bad, "四税种 × 五档全部吻合" if not bad else "; ".join(bad))


def a_pm_construction():
    t = _read(os.path.join(GAME, "common", "production_methods", "13_construction.txt"))
    ok1 = re.search(r"pm_wooden_buildings[\s\S]{0,600}?country_construction_add\s*=\s*2", t)
    ok2 = re.search(r"pm_wooden_buildings[\s\S]{0,900}?state_construction_mult\s*=\s*0\.002", t)
    ok3 = re.search(r"pm_iron_frame_buildings[\s\S]{0,900}?country_construction_add\s*=\s*5", t)
    return (bool(ok1 and ok2 and ok3),
            "木质 +2/级、地区 +0.2%/级；铁框架 +5" if ok1 and ok2 and ok3 else "PM 数值不符")


def a_pm_bureaucracy():
    t = _read(os.path.join(GAME, "common", "production_methods", "07_government.txt"))
    ok = bool(re.search(r"pm_horizontal_drawer_cabinets[\s\S]{0,500}?country_bureaucracy_add\s*=\s*50", t))
    return ok, "pm_horizontal_drawer_cabinets = +50/级" if ok else "数值不符"


def a_decree():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from v3live import cli
    d = cli._load_decrees()
    m = d.get("decree_road_maintenance", {}).get("modifiers", {})
    v = m.get("state_construction_mult")
    return (v is not None and abs(float(v) - 0.10) < 1e-9,
            "decree_road_maintenance state_construction_mult = %s" % v)


def a_goods():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from v3live import cli
    tbl = cli.goods_table()
    if len(tbl) != 54:
        return False, "商品数 %d（期望 54）" % len(tbl)
    j = json.load(open(os.path.join(ROOT, "data", "goods.json"), encoding="utf-8"))
    bad = [i for i in range(54) if j[str(i)]["name"] != tbl[i][0]]
    return (not bad, "54 种商品顺序与 goods.json 一致" if not bad
            else "顺序不一致: %s" % bad[:5])


def a_concept_texts():
    t = _read(os.path.join(GAME, "localization", "simp_chinese",
                           "concepts_l_simp_chinese.yml"))
    ok_gdp = "concept_gdp_desc" in t and "减去生产过程中投入" in t
    ok_prod = "concept_productivity_desc" in t and "乘以52" in t.replace(" ", "")
    ok_cap = "concept_gdp_per_capita_desc" in t and "除以" in t
    ok = ok_gdp and ok_cap
    return ok, "GDP 公式/人均公式来自 concept 描述%s" % ("（生产率公式也在）" if ok_prod else "")


# ---------------------------------------------------------------- B 存档层
def _need_save():
    if not os.path.exists(SAVE):
        return None, "跳过：未包含存档（76MB，随工作副本单独保存）"
    return True, ""


def _country_blocks():
    data = open(SAVE, "rb").read()
    i = data.find(b"\ncountry_manager={")
    pos = data.find(b"database={", i) + len(b"database={")
    pat = re.compile(rb"\n(\d+)=\{")
    while True:
        m = pat.search(data, pos)
        if not m:
            break
        end = data.find(b"\n}\n", m.end() - 1)
        if end < 0:
            break
        txt = data[m.end():end + 1].decode("utf-8", "replace")
        if "definition=" not in txt[:400]:
            break
        yield int(m.group(1)), txt
        pos = end


def b_income_slots():
    _ok, _msg = _need_save()
    if _ok is None:
        return None, _msg
    """收入数组 1/2/9/10 槽与存档具名分类求和一致。"""
    cats = {"income_taxes": 1, "poll_taxes": 2,
            "government_share_dividends": 10, "tariffs": 9}
    hits = {k: 0 for k in cats}
    tot = 0
    for cid, txt in _country_blocks():
        m = re.search(r"weekly_income=\{([^}]*)\}", txt)
        if not m:
            continue
        vals = [float(x) for x in m.group(1).split()]
        if len(vals) != 16:
            continue
        tot += 1
        for cat, slot in cats.items():
            i = txt.find(cat + "={")
            if i < 0:
                continue
            seg = txt[i:i + 4000]
            nums = re.findall(r"=\s*([\d.]+)", seg.split("\n")[0:80].__str__())
            # 直接抓该块内的数值
            blk = seg.split("}")[0]
            s = sum(float(x) for x in re.findall(r"=\s*([\d.]+)", blk))
            if s and abs(vals[slot] - s) < 0.02:
                hits[cat] += 1
    ok = all(hits[k] >= 20 for k in cats)
    return ok, "国家 %d 个；各槽命中 %s" % (tot, hits)


def b_approval_thresholds():
    _ok, _msg = _need_save()
    if _ok is None:
        return None, _msg
    """好感度阈值：不满区间 -9~-5、愤怒 <=-10。"""
    data = open(SAVE, "rb").read()
    i = data.find(b"\ninterest_groups={")
    pos = data.find(b"database={", i) + len(b"database={")
    pat = re.compile(rb"\n(\d+)=\{")
    pairs = []
    while True:
        m = pat.search(data, pos)
        if not m:
            break
        end = data.find(b"\n}\n", m.end() - 1)
        if end < 0:
            break
        txt = data[m.end():end + 1].decode("utf-8", "replace")
        if "approval=" not in txt:
            break
        pos = end
        a = re.search(r"(?m)^\tapproval=(-?[\d.]+)", txt)
        s = re.search(r"(?m)^\tapproval_state=(\w+)", txt)
        if a and s:
            pairs.append((float(a.group(1)), s.group(1)))
    if len(pairs) < 100:
        return False, "样本不足 %d" % len(pairs)
    bad = 0
    for v, st in pairs:
        exp = ("angry" if v <= -10 else "unhappy" if v <= -5 else
               "happy" if v >= 5 else "loyal" if v >= 10 else "neutral")
        if st == "neutral":
            continue
        if st != exp:
            bad += 1
    return bad == 0, "%d 条好感度，映射不符 %d 条" % (len(pairs), bad)


def b_institutions():
    j = json.load(open(os.path.join(TMP, "save_inst.json"), encoding="utf-8"))
    inst = j["institutions"]
    if len(inst) != 217:
        return False, "机构 %d 条（期望 217）" % len(inst)
    from collections import Counter
    c = Counter(v.get("investment") for v in inst.values())
    ok = c.get(1) == 175 and c.get(2) == 36 and c.get(3) == 5 and c.get(4) == 1
    return ok, "217 条，投资分布 %s" % dict(sorted(c.items()))


def b_companies():
    j = json.load(open(os.path.join(TMP, "save_inst.json"), encoding="utf-8"))
    comp = j["companies"]
    if len(comp) != 8:
        return False, "公司 %d 家（期望 8）" % len(comp)
    ceos = sorted(v.get("ceo") for v in comp.values())
    return True, "8 家，CEO 列表 %s" % ceos


def b_qing_buildings():
    _ok, _msg = _need_save()
    if _ok is None:
        return None, _msg
    data = open(SAVE, "rb").read()
    # 州 -> 国家
    owner = {}
    i = data.find(b"\nstates={")
    pos = data.find(b"database={", i) + len(b"database={")
    pat = re.compile(rb"\n(\d+)=\{")
    while True:
        m = pat.search(data, pos)
        if not m:
            break
        end = data.find(b"\n}\n", m.end() - 1)
        if end < 0:
            break
        txt = data[m.end():end + 1].decode("utf-8", "replace")
        if "capital=" not in txt[:300]:
            break
        pos = end
        c = re.search(r"(?m)^\tcountry=(\d+)", txt)
        if c:
            owner[int(m.group(1))] = int(c.group(1))
    n = 0
    i = data.find(b"\nbuilding_manager={")
    pos = data.find(b"database={", i) + len(b"database={")
    while True:
        m = pat.search(data, pos)
        if not m:
            break
        end = data.find(b"\n}\n", m.end() - 1)
        if end < 0:
            break
        txt = data[m.end():end + 1].decode("utf-8", "replace")
        if "building=" not in txt[:200]:
            break
        pos = end
        s = re.search(r"(?m)^\tstate=(\d+)", txt)
        if s and owner.get(int(s.group(1))) == 157:
            n += 1
    return n == 302, "大清建筑 %d 栋（期望 302）" % n


# ---------------------------------------------------------------- C 快照层
def _snap_list(path, magic):
    """返回 [(id, bytes)]；同一 id 可能有多条，所以不能用 dict。"""
    raw = open(path, "rb").read()
    assert raw[:4] == magic, raw[:4]
    cnt, ln = struct.unpack_from("<II", raw, 4)
    out = []
    off = 12
    for _ in range(cnt):
        out.append(raw[off + 8:off + 8 + ln])
        off += 8 + ln
    return out, ln


def _snap(path, magic, idoff=0x08):
    """按 id 建字典（仅当 id 唯一时用，如 CCountry / CCompany）。"""
    objs, ln = _snap_list(path, magic)
    d = {}
    for b in objs:
        d[struct.unpack_from("<I", b, idoff)[0]] = b
    return d, ln


def _save_scalar(pattern, group=1):
    """从国家块里取某个标量字段，返回 {cid: 值}。"""
    out = {}
    for cid, txt in _country_blocks():
        m = re.search(r"(?m)^\t+%s=([\d.\-]+)" % pattern, txt)
        if m:
            try:
                out[cid] = float(m.group(1))
            except ValueError:
                pass
    return out


def _corr(name, snap_path, magic, pattern, offset, scale=1e5, fmt="<q", tol=1e-6):
    objs, _ln = _snap(snap_path, magic)
    vals = _save_scalar(pattern)
    pairs = [(c, v) for c, v in vals.items() if c in objs]
    if len(pairs) < 20:
        return False, "可比国家不足 %d" % len(pairs)
    ok = 0
    for cid, v in pairs:
        got = struct.unpack_from(fmt, objs[cid], offset)[0] / scale
        if abs(got - v) <= max(tol, abs(v) * tol):
            ok += 1
    return ok == len(pairs), "%d/%d 命中（+0x%X）" % (ok, len(pairs), offset)


def c_ccountry():
    snap = os.path.join(TMP, "ccountry_snap.bin")
    if not os.path.exists(snap):
        return None, "跳过：未包含快照 ccountry_snap.bin"
    ok1, d1 = _corr("money", snap, b"CCS1", "money", 0x12F8)
    ok2, d2 = _corr("credit", snap, b"CCS1", "credit", 0x2198)
    ok3, d3 = _corr("base_wage", snap, b"CCS1", "base_wage", 0x2150)
    ok4, d4 = _corr("pool", snap, b"CCS1",
                    "weekly_investment_pool_change_from_investment", 0x1D18)
    ok = ok1 and ok2 and ok3 and ok4
    return ok, "money %s / credit %s / wage %s / pool %s" % (d1, d2, d3, d4)


def c_interest_group():
    snap = os.path.join(TMP, "ig_snap.bin")
    if not os.path.exists(snap):
        return None, "跳过：未包含快照 ig_snap.bin"
    objs, ln = _snap_list(snap, b"IGS1")
    # clout @0x0F8 是 i32 定点 ÷100000（命令行再 ×100 显示成百分比）。
    # 同一国家的势力合计应为 1.0；快照里每国常有两代对象，所以也会出现 2.0。
    sums = {}
    for b in objs:
        cid = struct.unpack_from("<I", b, 0x0AC)[0]
        v = struct.unpack_from("<i", b, 0x0F8)[0] / 100000.0
        if 0 < v < 1.1:
            sums[cid] = sums.get(cid, 0.0) + v
    full = [c for c, s in sums.items()
            if min(abs(s - k) for k in (1.0, 2.0, 3.0)) < 0.01]
    ok = len(full) >= 200
    return ok, "%d 个国家的势力合计 ≈1.0/2.0（样本 %d）" % (len(full), len(sums))


def c_movement_pops():
    snap = os.path.join(TMP, "mv_snap.bin")
    if not os.path.exists(snap):
        return None, "跳过：未包含快照 mv_snap.bin"
    objs, ln = _snap_list(snap, b"MVS1")
    qing = sorted(struct.unpack_from("<i", b, 0x15C)[0] for b in objs
                  if struct.unpack_from("<I", b, 0x130)[0] == 157
                  and struct.unpack_from("<i", b, 0x15C)[0] > 0)
    ok = qing == [691, 720, 965]
    return ok, "大清政治运动 pops = %s（期望 [691,720,965]）" % qing


def c_institution():
    snap = os.path.join(TMP, "inst_snap.bin")
    if not os.path.exists(snap):
        return None, "跳过：未包含快照 inst_snap.bin"
    objs, ln = _snap_list(snap, b"INS1")
    from collections import Counter
    c = Counter(struct.unpack_from("<I", b, 0x024)[0] for b in objs)
    ok = c.get(1) == 175 and c.get(2) == 36 and c.get(3) == 5 and c.get(4) == 1
    return ok, "%d 个机构，投资等级分布 %s" % (len(objs), dict(sorted(c.items())))


def c_company():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    snap = os.path.join(TMP, "comp_snap.bin")
    ref = os.path.join(TMP, "save_inst.json")
    if not (os.path.exists(snap) and os.path.exists(ref)):
        return None, "跳过：未包含快照 comp_snap.bin / save_inst.json"
    j = json.load(open(ref, encoding="utf-8"))["companies"]
    want = sorted((v.get("country"), v.get("building"), v.get("ceo")) for v in j.values())
    objs, ln = _snap_list(snap, b"CMP1")
    got = sorted((struct.unpack_from("<I", b, 0x060)[0],
                  struct.unpack_from("<I", b, 0x064)[0],
                  struct.unpack_from("<I", b, 0x068)[0]) for b in objs)
    return got == want, "8 家 (国家,建筑,CEO) 逐条相等" if got == want else \
        "快照 %s vs 存档 %s" % (got, want)


# ---------------------------------------------------------------- D 文档与代码
def d_field_table():
    d = json.load(open(FIELDS, encoding="utf-8"))
    classes = [k for k in d if k.startswith("C") and isinstance(d[k], dict)]
    fields = 0
    noaddr = []
    for c in classes:
        for k, v in d[c].items():
            if k.startswith("_"):
                continue
            fields += 1
            if not isinstance(v, dict) or ("offset" not in v and c not in
                                           ("CState", "CCountry")):
                noaddr.append("%s.%s" % (c, k))
    ok = len(classes) == 16 and fields >= 150
    return ok, "%d 个类 / %d 个字段 / 无偏移字段 %d 个" % (len(classes), fields, len(noaddr))


def d_income_expense_index():
    d = json.load(open(FIELDS, encoding="utf-8"))
    inc = [k for k in d["income_index"] if not k.startswith("_")]
    exp = [k for k in d["expense_index"] if not k.startswith("_")]
    ok = len(inc) == 15 and len(exp) == 21
    return ok, "income_index %d 项 / expense_index %d 项" % (len(inc), len(exp))


def d_record():
    t = _read(RECORD)
    bad = t.count(chr(0xFFFD))
    secs = [l for l in t.split("\n") if l.startswith("## ")]
    ok = bad == 0 and len(secs) >= 10
    return ok, "%d 字符 / 乱码 %d / 章节 %d" % (len(t), bad, len(secs))


def _run_py(args):
    """跑子进程并强制 UTF-8 输出，避免中文在 GBK 环境下变成乱码。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def d_commands():
    r = _run_py([os.path.join(ROOT, "tools", "v3live", "run.py"), "--help"])
    out = (r.stdout or "") + (r.stderr or "")
    want = ["report", "tax", "capacity", "gdp", "states", "pops", "buildings",
            "market", "politics", "institutions", "companies", "laws", "tech",
            "decrees", "prewarm", "prices", "localprices"]
    missing = [c for c in want if c not in out]
    return not missing, "%d 个命令齐全" % len(want) if not missing else "缺 %s" % missing


def d_selftest():
    _tools = os.path.join(ROOT, "tools").replace("\\", "\\\\")
    r = _run_py(["-c",
                 "import sys;sys.path.insert(0,r'%s');"
                 "from v3live import selftest;selftest.main()" % _tools])
    out = (r.stdout or "") + (r.stderr or "")
    ok = "全部通过" in out
    if ok:
        return True, "selftest 全部通过"
    return False, "退出码 %d；输出尾部: %r" % (r.returncode, out[-160:])


LAYERS = {
    "A": [("defines 常量", a_defines), ("base_values 全局基线", a_base_values),
          ("country_gdp_construction", a_gdp_construction),
          ("税率表（消费税五档）", a_tax_table),
          ("建造部门生产方式", a_pm_construction),
          ("行政楼生产方式", a_pm_bureaucracy),
          ("道路养护法令", a_decree), ("商品表 54 种顺序", a_goods),
          ("concept 公式描述", a_concept_texts)],
    "B": [("收入数组槽位 vs 具名分类", b_income_slots),
          ("好感度阈值映射", b_approval_thresholds),
          ("机构 217 条与投资分布", b_institutions),
          ("公司 8 家", b_companies),
          ("大清建筑 302 栋", b_qing_buildings)],
    "C": [("CCountry 四个字段撞快照", c_ccountry),
          ("利益集团势力合计 100%", c_interest_group),
          ("政治运动 pops 965/720/691", c_movement_pops),
          ("机构投资分布撞快照", c_institution),
          ("公司 8 家撞快照", c_company)],
    "D": [("字段表结构", d_field_table), ("收支索引项数", d_income_expense_index),
          ("记录完整无乱码", d_record), ("命令全部注册", d_commands),
          ("selftest", d_selftest)],
}
TITLES = {"A": "A. 游戏文件层", "B": "B. 存档层", "C": "C. 内存快照层", "D": "D. 文档与代码"}


def main():
    which = sys.argv[1].upper() if len(sys.argv) > 1 else "ABCD"
    print("=" * 76)
    print("Victoria 3 逆向成果逐项验证")
    print("=" * 76)
    for layer in "ABCD":
        if layer not in which:
            continue
        print("\n%s" % TITLES[layer])
        for name, fn in LAYERS[layer]:
            check(name, fn)
    ok = sum(1 for _n, o, _d in RESULTS if o)
    extra = ("，%d 项跳过" % len(SKIPS)) if SKIPS else ""
    print("\n%s\n结果：%d/%d 通过%s" % ("-" * 76, ok, len(RESULTS), extra))
    if ok != len(RESULTS):
        print("未通过项：")
        for n, o, d in RESULTS:
            if not o:
                print("  - %s: %s" % (n, d))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
