"""解析 V3 的 Paradox 脚本数据文件（modifier 定义等）。

语法：`key = value` / `key = { ... }` / 裸值列表 / `#` 注释。
"""
import os
import re

TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[A-Za-z0-9_.\-+]+|[{}=<>]')


def tokenize(text):
    text = re.sub(r"#[^\n]*", "", text)
    return TOKEN_RE.findall(text)


def parse_tokens(toks, pos=0):
    """返回 (dict, 新位置)。值为 dict / str / list[str]。"""
    out = {}
    while pos < len(toks):
        t = toks[pos]
        if t == "}":
            return out, pos + 1
        if pos + 1 < len(toks) and toks[pos + 1] == "=":
            key = t.strip('"')
            pos += 2
            if pos >= len(toks):
                break
            if toks[pos] == "{":
                val, pos = parse_tokens(toks, pos + 1)
                out[key] = val
            else:
                out[key] = toks[pos].strip('"')
                pos += 1
            continue
        # 裸值 / 其它 token：并入列表
        if t in "{=<>":
            pos += 1
            continue
        out.setdefault("_values", []).append(t.strip('"'))
        pos += 1
    return out, pos


def parse_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        toks = tokenize(f.read())
    return parse_tokens(toks)[0]


def parse_dir(path):
    out = {}
    for name in sorted(os.listdir(path)):
        if not name.endswith(".txt"):
            continue
        try:
            out.update(parse_file(os.path.join(path, name)))
        except Exception as e:              # 单个文件失败不影响整体
            print("  跳过 %s: %s" % (name, e))
    return out


GAME = r"E:\SteamLibrary\steamapps\common\Victoria 3\game\common"


def load_modifier_types(base=GAME):
    return parse_dir(os.path.join(base, "modifier_type_definitions"))


def load_static_modifiers(base=GAME):
    return parse_dir(os.path.join(base, "static_modifiers"))


def load_scripted_modifiers(base=GAME):
    return parse_dir(os.path.join(base, "scripted_modifiers"))


if __name__ == "__main__":
    import json
    import sys

    types = load_modifier_types()
    statics = load_static_modifiers()
    scripted = load_scripted_modifiers()
    print("modifier 类型 %d 个" % len(types))
    print("static_modifier %d 个" % len(statics))
    print("scripted_modifier %d 个" % len(scripted))
    out = os.path.join(os.path.dirname(__file__), "_cache")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "modifier_types.json"), "w", encoding="utf-8") as f:
        json.dump(types, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "static_modifiers.json"), "w", encoding="utf-8") as f:
        json.dump(statics, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "scripted_modifiers.json"), "w", encoding="utf-8") as f:
        json.dump(scripted, f, ensure_ascii=False, indent=1)
    print("已写出到", out)
    # 抽样验证
    for k in list(statics)[:3]:
        print(" ", k, "->", {kk: vv for kk, vv in statics[k].items() if kk != "icon"})
