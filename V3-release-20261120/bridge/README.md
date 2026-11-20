# v3bridge 0.3 — Victoria 3 外部控制层

DLL 注入式，不改游戏文件、不装 mod。目标版本 **Victoria 3 1.15.1**（internal 4.5.2 / 3270）。
**已做抗更新处理**：游戏更新后即使指纹不匹配，也会用签名自行重新定位，不需要重新人工逆向。

## 目录

```
本目录     v3inject.dll（主 DLL）、injector.exe（注入器）
src\      v3bridge.cpp、injector.cpp（完整源码 + 构建命令）
docs\     交接文档.md（总览）、逆向-建造注册.md（建造链路 + 抗更新机制）
re\       下次游戏更新时用的差分分析脚本
```

## 快速开始

```powershell
# 1. 启动游戏，进入存档
# 2. 注入（必须用绝对路径）
.\injector.exe victoria3 "$PWD\v3inject.dll"

# 3. 等就绪：日志出现 capabilities: ... on，同时会响两声
Get-Content v3inject.log | Select-String "capabilities"
```

## 命令

写一个 txt → DLL 处理后改名成 `.done.txt` → 读结果文件。

| 文件 | 内容 | 结果 |
|---|---|---|
| `requests.txt` | `<州id> <建筑键> <等级>` | 直接建造，走引擎完整命令链 |
| `query.txt` | `building <建筑键> [all]` | `states.csv`（州 id / 所有者 / 是否已有该建筑） |
| `pms.txt` | `<州id> <建筑键>` | `pms.csv`（激活的生产方式 + 各组可选列表） |
| `pmm.txt` | `<州id> <建筑键> <pm键>` | 切换生产方式 |
| `queue.txt` | 任意内容 | `queue.csv`（建造队列） |
| `pmfix.txt` | `<目标州> <建筑键> <参照州> [dry]` | 从同类型建筑复制完整 PM 集合 |
| `bdump.txt` | `<州id> <建筑键>` | `bdump.csv`（建筑对象前 0x200 字节） |
| `peek.txt` | `<十六进制地址> <数量>` | `peek.csv`（读任意内存） |
| `probe.on` | 空文件 | 开启硬件断点探针，写 `buildprobe.log` |
| `watch.txt` | `<州id> <建筑键> [秒]` | 在该建筑的 PM 数组上设写断点 |

**注意**：煤矿等资源类建筑只能建在有对应资源的州，否则引擎会走它自己坏掉的断言路径
（DLL 会接住，但那类建筑会被记为"本会话不可用"）。

## 抗更新

游戏更新后直接注入即可。日志会显示：

```
build UNKNOWN (expected 100221048 / 0xF8A08D788306BC2F) - resolving by signature
sig: ResolveState   recovered from wrapper call[0]
sig: ResolveCountry recovered from wrapper call[1]
ctx: global 0x5C0DCA8 -> 0x... (vt ok)
capabilities: build=on pm=on player=on queue=on
```

能自动恢复：函数地址、注册表全局、请求对象虚表、ctx。
不能自动恢复：数据结构偏移（`CBuilding+0xF0`、PM 向量、队列布局）——这些是语义约定，
签名找不回来，但 1.15.0 → 1.15.1 实测一个都没变。

**自测**：放一个空文件 `force_unknown` 到 DLL 目录，会强制走签名路径，
用来验证抗更新逻辑本身是否完好。

## 安全设计

- **写锁**：只允许写玩家自己的国家（只读可加 `all` 放开，写永远不行）
- **失败即拒**：玩家 id 未解析时挂起写命令（30 秒上限）
- **启动自检**：类型表 / 玩家 / 建造 / PM / 队列逐项验证，失败的能力直接禁用
- **世界切换静默期**：读档、选国家期间不碰任何引擎对象
- **非法建造记忆**：触发过引擎断言的建筑类型，本会话内拒绝重试

## 构建

```powershell
D:\msys64\ucrt64\bin\g++.exe -std=c++17 -O2 -shared -static `
  -o v3inject.dll v3bridge.cpp -Wl,--kill-at -luser32 -lgdi32
```

`-Wl,--kill-at` 必须作为独立数组元素传入。

## 指纹（1.15.1）

```
size = 100221048
hash = 0xF8A08D788306BC2F
tds  = 0x6AF3094F
```

指纹不匹配不再导致罢工，只影响"是否信任记录的 RVA"。
