# v3calc — Victoria 3 实时数据总程序 + 计算层

> 工作目录：`D:\Game\ai\V3`
> 读取层：`v3live`（从 `Victoria3-RE\tools\v3live` 原样引入，逆向层已验证）
> 计算层：`v3calc`（本目录新增）
> 决策层：`v3ai`（LLM + 工具 + 审查 + 执行；见 [v3ai/README.md](v3ai/README.md)）
> 决策计算层：`v3calc.advisor`（价格反馈 / 候选包 / 简报 / 台账；见 [v3calc/ADVISOR.md](v3calc/ADVISOR.md)）
> **给中心决策层的交接文档：[HANDOFF.md](HANDOFF.md)**（自包含，可直接转发）
> **项目总览：[项目总览.md](项目总览.md)**（整体架构 / 五大模块详解 / 命令速查 / 版本适配状态）
> 版本：2026-11-18　|　测试：`python -m unittest discover -s tests -v` → **52/52 通过**；
> `python -m v3calc selftest` → **34/34 通过**（决策计算层）

---

## 0. 它是什么

一句话：**先一次读全，再离线算**。

```
victoria3.exe 内存 ──v3live──┐
                            ├──> Snapshot（JSON，事实） ──> v3calc 计算层
文本存档 save.txt ──savefile─┘                              ├─ GDP
游戏脚本 common/*.txt ──gamedata──> GameData（规则）         ├─ 允许的生产方式
                                                          ├─ PM 升级方案（GDP 增量）
                                                          └─ 口径校准
```

- **总程序**：`python v3.py read` 一次 attach，把国家/预算/州/建筑/市场/法律/科技/
  法令/机构/公司/利益集团/政治运动/临时修正全部读成一个 `Snapshot`，可落盘 JSON。
- **计算层**：不碰内存、不碰存档，只吃 `Snapshot + GameData`，纯函数式计算，
  可回放、可测试、可做"如果换成 X 会怎样"的方案对比。

---

## 1. 快速开始

### 1.1 游戏正在运行（实时，推荐）

```bat
cd /d D:\Game\ai\V3
python v3.py doctor                 :: 环境自检
python v3.py read --json snap.json  :: 读全量快照并落盘
python v3.py gdp --by-good          :: 实时快照 + GDP
python v3.py pm --building building_food_industry --show-locked
python v3.py upgrades --top 20
```

live 首次读取会自动 prewarm：一次扫完全部常用类（本机约 2~3 分钟），
之后 `read` 只走 `v3live/_cache`（秒级）。游戏重启后缓存失效，会自动重建。
不想自动 prewarm 加 `--no-prewarm`；也可手动 `python v3.py prewarm --refresh`。

### 1.2 游戏没开（存档模式，离线）

需要**文本存档**（`SAV...` 开头的 `save.txt`）：

```bat
python v3.py read --save "D:\...\save.txt" --json snap.json
python v3.py gdp  --snapshot snap.json --price base --scale staffing --by-good
python v3.py pm   --snapshot snap.json --building building_food_industry --show-locked
python v3.py upgrades --snapshot snap.json --best
```

本仓库开发时用的就是 `D:\Game\ai\Victoria3-RE\data\saves\save.txt`
（大清 1836.1.1，版本 1.15.0）。随仓库还带了一份**已落盘的示例快照**
（`examples\snapshot_chi_1836.json`，159 KB），没存档也能直接算：

```bat
python v3.py gdp --snapshot examples\snapshot_chi_1836.json --price base --scale staffing --by-good
python v3.py pm  --snapshot examples\snapshot_chi_1836.json --building building_food_industry --show-locked
python v3.py upgrades --snapshot examples\snapshot_chi_1836.json --best
```

> 二进制 `.v3`（zip 里的 `gamestate`）目前**不支持**：它是 token 压缩格式，
> 需要 token 表解码；文本存档足够验证计算层。

### 1.3 实时循环（watch）

```bat
python v3.py watch --interval 10 --json snap.json --history history.csv --target 85100000
```

每 10 秒重读一次，打印一行：

```
[02:30:15] GDP 85,263,929 Δ+0 +0.19% | 人均 0.233 | 国库 4,242,494 | 周结余 +12,774 | 建筑 302/19498 | 43 州 | 0.42s
```

- `--interval`：刷新间隔秒（默认 10；最小实际受单次读取耗时限制）
- `--json`：每轮**原子覆盖**最新快照（先写 `.tmp` 再 `os.replace`）
- `--history`：追加 CSV，列：`captured_at,country,tag,population,treasury,weekly_balance,gdp_annual,gdp_per_capita,buildings,levels,states,market_prices`
- `--rounds N`：只跑 N 轮（0=无限；没开游戏时按尝试次数计）
- `--wait-load`：主菜单时每隔 N 秒自动重试（0=等待按 Enter）
- `--no-gdp`：只看快照；`--target`：打印相对目标偏差
- 进程重启会自动重连并重建缓存；Ctrl+C 退出

**主菜单适配（直接读州注册表，不靠窗口/文件）**：
从 `victoria3.exe` 的 resolver prologue 找回 `g_regState` 全局，读：

```
registry +0x20  entries 指针
registry +0x2C  容量（1024/2048；不是州数量）
entry[i] +8     对象指针
```

只有找到一个首 qword 是 **CState vtable**（RTTI 解析，不硬编码）的真实对象，
才算“在世界里”。主菜单时 global/容量为空；读档中途则数组已分配、条目还全空，
会判为 `loading`。**主菜单和加载中都不会扫描**，所以不再白扫几十秒。

`watch` 在主菜单会直接等待：

```
[主菜单] 请在游戏里加载存档，然后按 Enter 继续（Ctrl+C 退出）…
```

加载后按 Enter（或 `--wait-load 30` 自动轮询注册表）→ prewarm 一次 → 开始循环。
`read`/`gdp` 在主菜单下会直接报“主菜单”并返回 2；单独跑 `prewarm` 也不会扫描，
直接清理缓存并返回 2。

**扫描过程中加载存档**：读取是只读的，不会坏档；若扫描正好跨过加载，注册表
会显示 `loading`，或扫描结果被 `CCountry/CState/CJominiPlayer/CBuilding`
完整性检查拦下。半成品缓存会被清掉并自动重扫。最稳妥仍是：
**先等存档完全加载，再 prewarm/read**。

> 这是**轮询**，不是事件推送：每次重读字段，缓存只缓存对象地址；游戏暂停时数值不变。
> 单次读取复用同一个 `Proc` 句柄和 `GameData`，所以耗时主要是内存读 + JSON 写。

### 1.4 扫描速度

首次/游戏重启后的全量扫描现在是**并行版**（`v3calc/fastscan.py`）：

- 16 MB 一块，多线程同时 `ReadProcessMemory` + numpy 比较；
- 线程本地复用 16 MB 缓冲，避免每块重新分配；
- 语义与 `v3live.discover.scan_instances` 完全一致，单测里与串行版逐地址比对通过；
- 默认线程数 `min(4, CPU)`，可用环境变量调大：

```bat
set V3_SCAN_WORKERS=8
python v3.py prewarm --refresh
```

缓存建立之后，`read`/`watch` 的单次读取约 0.6 秒（本机 `diagnostics.timings` 可查），
不再是扫描瓶颈。**全量扫描仍要遍历游戏私有内存**，无法做到“秒开”；能省的是重复扫描。
如果只想尽快进入可用状态：先 `python v3.py prewarm`，再 `read`/`watch`。

> 开局 `CDecree=0` 时，空缓存会被 `_load_cache` 当成 miss，导致每轮重扫 16 秒。
> 现在空缓存有 900 秒 TTL，TTL 内直接按“没有法令”处理；如果新颁布法令后
> `watch` 没显示，跑一次 `python v3.py prewarm --refresh` 即可。
> 修好后实时循环可以开 `--interval 1` 或 `2`。

---

## 2. 命令一览

| 命令 | 作用 |
|---|---|
| `read` | 读快照：实时（默认）或 `--save`；`--json` 落盘；`--pops` 聚合人口 |
| `watch` | 实时循环：每 N 秒重读，打印 GDP/国库/结余，可覆盖 JSON、追加 CSV 历史 |
| `gdp` | 计算 GDP；`--by-good` / `--by-building` / `--calibrate` |
| `pm` | 当前科技/法律下每个建筑、每个 PM 组允许哪些 PM；`--show-locked` 给原因 |
| `upgrades` | 枚举"换 PM"方案，按年 GDP 增量排序；`--best` 每组只留最优 |
| `prewarm` | 一次扫完常用类，写 `v3live/_cache`（游戏重启后跑一次） |
| `advise` | 决策计算层：`brief`（一页纸）/`market`/`candidates`/`plan`/`signals`/`quick`/`ledger` |
| `selftest` | 跑决策计算层自带测试（34 项） |
| `info` | 游戏数据统计（商品/PM/建筑/科技/法律） |
| `doctor` | 检查游戏目录、进程、v3live、存档 |

通用参数：`--game-dir`、`--country`（默认玩家）、`--save`、`--snapshot`、`--json`。

`pm` 的规划参数：

```bat
python v3.py pm --building building_food_industry ^
  --tech baking_powder,canneries --show-locked
```

`--tech` / `--law` 只影响本次判定（"如果我研发了这些科技，会解锁什么"），
不会写游戏。

`gdp` 关键参数：

| 参数 | 取值 | 说明 |
|---|---|---|
| `--price` | `auto` / `market` / `base` / `local` | 价格基准；live 默认 auto→market，save 默认 auto→base |
| `--scale` | `staffing`（默认）/ `throughput` / `levels` / `staffing_only` | `workforce_scaled` 的乘数；默认“实际就业规模 × 吞吐” |
| `--buildings` | `producers`（默认）/ `all` / 类型列表 | 默认只算有商品产出的建筑 |
| `--exclude-goods` | `services` 等 | 排除商品，用于口径实验 |
| `--calibrate` | — | 在价格×缩放×建筑范围上扫一遍，和界面 GDP 比 |

---

## 3. GDP 计算口径

```
GDP(周) = Σ_建筑 Σ_PM Σ_商品 (产出数量 − 投入数量) × 价格
GDP(年) = GDP(周) × 52
人均 GDP = 年 GDP ÷ 总人口
```

数量来自 PM 的 `goods_output_<good>_add` / `goods_input_<good>_add`，按缩放级别乘系数：

| scale | 乘数 |
|---|---|
| `workforce_scaled` | 就业规模 × 吞吐量（**默认，校准最优：实际就业**） |
| `workforce_scaled` + `--scale throughput` | 等级 × 吞吐量（v3live 旧口径） |
| `level_scaled` | 等级 |
| `throughput_scaled` | 等级 × 吞吐量 |
| `unscaled` | 1 |

价格：
- `market`：本国市场统一价（**默认，逆向层校准最接近界面**）
- `base`：商品基础价（`common/goods` 的 `cost`）
- `local`：各州本地价（是"生产地价格"；实测比界面低 ~17%，**不是**游戏 GDP 口径）
- `auto`：live → market，save → base（存档不存本国市场价）

### 3.1 精度（必读）

**live 模式（本国市场价，本机实测 2026-11-12，大清，界面约 85.1M）**：

| 口径 | 年 GDP | 相对界面 |
|---|---|---|
| **market + staffing + producers（默认，02:38 快照）** | **85,263,929** | **+0.19%** |
| market + staffing + producers（02:16 快照，人口少 1.4 万） | 84,795,715 | −0.36% |
| market + staffing + all | 80,151,138 | −5.82% |
| market + throughput + all | 82,451,880 | −3.11% |
| market + throughput + producers | 87,096,457 | +2.35% |
| local + throughput + all | 70,893,037 | −16.69% |

默认口径 85.26M / 人均 0.233；与另一会话独立算出的 **85,259,000（+0.19%）**
只差 0.006%，跨会话确认。`local`（各州本地价）低 ~17%，因为包含市场接入度折扣；
`all` 含政府/军营会单方面扣投入，系统性偏低 3~6%。单国偏差方向不一致，
适合趋势/相对比较，做绝对值注意误差。

**save 模式（本仓库实测，大清 1836.1.1）**：

| 口径 | 年 GDP | 相对界面 84,674,628.40 |
|---|---|---|
| **base + staffing + producers（save 默认）** | **113,462,652** | **+34.00%** |
| base + staffing + all | 108,791,024 | +28.48% |
| base + throughput + all | 111,920,807 | +32.18% |
| market + staffing + all | 113,154,495 | +33.63% |

原因不是公式错，而是**存档不存本国市场价**：存档只有世界市场价格趋势，
不是大清市场 49 的价格；基础价反而更接近 1836 年的初始市场。用同一公式算
TUR/SWE（市场接入度高、市场价≈基础价）时，排除 services 后与界面几乎相等
（TUR 0.987×、SWE 1.001×），说明"产出−投入×价格"的主干是对的。

> 结论：**live 模式 + market 价**做绝对值（−3% 量级）；**save 模式看相对变化/方案排序**。
> `gdp --calibrate` 会把所有口径列出来，方便按当前存档日期挑最接近的一组。

### 3.2 建筑范围

- `producers`（默认）：只算有商品产出的建筑（不扣纯消费建筑的投入）
- `all`：所有建筑，扣掉政府/军营/贸易中心等的投入（v3live 旧口径，系统性偏低 3~6%）
- 指定类型：`--buildings building_tea_plantation,building_silk_plantation`

政府行政、贸易中心这类建筑没有商品产出但会消耗商品；把它们计入只会单方面扣投入，
所以默认排除，并显式保留 `all` 供对比。

---

## 4. 生产方式（PM）可用性

### 4.1 判定规则

| 字段 | 含义 | 实现 |
|---|---|---|
| `unlocking_technologies` | 任一科技已研发 → 解锁 | OR；缺全部才算 locked |
| `unlocking_laws` | 任一法律生效 → 解锁 | OR |
| `unlocking_production_methods` | 任一 PM 已启用 → 解锁 | OR（同组 PM 互斥，列表通常是同组档位） |
| `disallowing_laws` | 任一法律生效 → 禁止 | NOR |
| `unlocking_religions` / `disallowing_religions` | 国教判定 | 可判定 |
| `required_input_goods` | 市场要有该商品 | 有供需数据时判定，否则 conditional |
| `unlocking_principles` | 权力集团原则 | **conditional**（内存/存档未读） |
| `unlocking_company_categories` | 公司类别 | **conditional**（公司类型→类别未加载） |
| `unlocking_identity` | 权力集团身份 | **conditional** |
| `unlocking_geographic_regions` | 州所在地理区域 | 目前 **conditional**（州→区域映射未接） |

状态四种：

- `已启用`：当前建筑正在用
- `可用`：现在就能换
- `条件未知`：缺少判定数据（权力集团/公司/区域），**不是**不可用
- `不可用`：被科技/法律/前置 PM/宗教/输入商品明确卡住，并给出原因

### 4.2 一致性验证

用大清 302 栋建筑里**实际启用的 81 种 PM** 回测：

```
active pm types 81   locked 0   conditional 0
```

即规则不会把游戏里正在用的 PM 误判为不可用。科技门/法律门/前置 PM 门都有单测。

### 4.3 升级方案

`upgrades` 对每个建筑/PM 组枚举当前允许的候选 PM，替换后重算全国 GDP，
按年增量排序。例（live 大清，market + staffing + producers）：

| 建筑 | 当前 | 候选 | 年增量 |
|---|---|---|---|
| building_tea_plantation | default_labour | worker_exploitation_tea | +8,208,884 |
| building_logging_camp | pm_hardwood / pm_no_hardwood | pm_increased_hardwood | +1,239,630 |
| building_banana_plantation | default_labour | worker_exploitation_banana | +499,327 |
| building_urban_center | pm_no_street_lighting | pm_gas_streetlights | +102,476 |

**注意**：增量只算商品产出−投入，不含行政力、就业、死亡率、生活水平等非商品效果。
`worker_exploitation_*` 这类 PM 会显著提高 GDP，但会推高死亡率；政府行政换
`pm_simple_organization` 会因少消耗纸张而让 GDP 上升（在 `--buildings all` 下可见），
但行政力会下降。换 PM 时按**每个建筑自己的 PM 组合**复核前置，不满足的建筑会跳过
（例如没有锯木厂的伐木场不能直接换 `pm_increased_hardwood`）。
**换 PM 前用 `pm --building` 看完整修正。**

---

## 5. 决策计算层（advise）

> 设计与近似说明见 [v3calc/ADVISOR.md](v3calc/ADVISOR.md)；本章只讲"它能回答什么"。

`gdp.py` 回答"现在值多少"；`advisor` 回答"**再加一级**会怎样"——把每个候选
算成 5 个数字，LLM 只做取舍：

| 数字 | 含义 |
|---|---|
| `annual_value` | 年增值 =（产出 − 投入）× **推演后价格** × 52 |
| `per_point` | 年增值 ÷ 建造点（跨建筑可比） |
| `weeks` | 建造周数（建造力折算，队列深度） |
| `risk` | 0~1：价格触底/触顶、基建饱和、激进派、吃地见顶 |
| `price_changes` | 这一单会把哪些商品推高/压低多少 |

价格公式是用 54 个商品反推并逐项验证的（误差 0，`gold` 除外）：

```
d > s :  mult = 1 + 0.75 × (d/s − 1)
s > d :  mult = 1 − 0.75 × (s/d − 1)
价格 = 基础价 × clamp(mult, 0.25, 1.75)
```

```bat
python -m v3calc advise brief --snapshot snap.json      :: 一页纸（≤2400 字符）
python -m v3calc advise candidates --top 10             :: 候选包
python -m v3calc advise plan --weeks 12                 :: 逐步贪心（选一项→重算价格→再选）
python -m v3calc advise signals                         :: 空列表 = 这轮不用叫 LLM
python -m v3calc advise quick                           :: 规则快通道（财政护栏/PM/补队列）
python -m v3calc advise ledger --record                 :: 记账；隔几周 --reconcile 对账
python -m v3calc selftest -v                            :: 34 项测试
```

已接进 `v3ai`：

* `Runtime.brief()` 新增 `advise`（简报文本 + 信号 + 方案前三项）；
* `build_candidates` / `estimate_build` 改由计算层算（旧键名保留，新增
  `annual_value` / `per_point` / `risk` / `price_changes`）；
* 审查层与计算层共用同一份可建性判定（科技/资源/耕地/上限/队列）；
* 新增工具 `advise_brief` / `advise_plan` / `advise_signals`。

---

## 6. 快照格式（`snapshot.json`）

顶层字段：

| 字段 | 内容 |
|---|---|
| `game` | 来源（live/save）、时间、版本、进程/模块基址、存档路径/日期 |
| `country` | id、tag、市场 id、宗教、国库、信贷、贷款、基础工资、人口、GDP（存档趋势值） |
| `country.budget` | 收入 16 槽 / 支出 22 槽 / 合计 / 税收等级 / 每周投资池变化 |
| `states[]` | id、归属、首府、耕地、基础设施、人口分层、激进派/失业/劳工、本地价 54、市场供需 54 |
| `buildings[]` | 类型、州、等级、就业规模、吞吐、当前 PM、利润、工资率、招聘率、active |
| `market` | 市场 id、54 商品价格/基础价/供给/需求 |
| `laws[]` / `techs[]` | 生效法律（含组）/ 已研发科技 |
| `decrees[]` / `institutions[]` / `companies[]` | 法令 / 机构投入 / 公司 |
| `interest_groups[]` / `movements[]` | 势力/好感/执政/政党；运动类型/意识形态/激进度/支持人数 |
| `timed_modifiers[]` | 国家/州临时修正：名称、数值、起止 |
| `pops[]` | 可选（`--pops`）：州×职业的 workforce/dependents/pops 数 |
| `diagnostics` | 每段耗时、状态（ok/empty/error）、警告 |

所有金额/价格是定点除法后的"英镑"；人口是原始人数；读不到的字段是 `null`，
不会填 0 假装读到。`Snapshot.load()/save()` 可无损往返。

---

## 7. 目录结构

```
D:\Game\ai\V3\
  v3.py                    入口（python v3.py ...）
  v3live\                  读取层（从 Victoria3-RE 原样引入；见 VENDOR.md）
    cli.py proc.py discover.py rtti.py pe.py gamedata.py static.py run.py
  v3calc\
    pdx.py                 Paradox 脚本解析器（保留重复键/裸列表/引号#）
    gamedata.py            商品/PM/PM 组/建筑/科技/法律/地理区域
    model.py               Snapshot 数据模型 + JSON 往返
    live.py                实时读取后端（一次 attach 读全）
    fastscan.py            并行 vtable 扫描（替换 v3live 单线程实现）
    worldstate.py          州注册表 g_regState 判定（主菜单/存档/加载中）
    savefile.py            文本存档后端（离线，含 GDP 界面值）
    gdp.py                 GDP 计算 + 口径校准
    pm.py                  PM 可用性判定
    scenario.py            PM 替换/GDP 增量/最优升级
    market.py              价格模型（含价格反馈推演）+ 产业链聚合
    construction.py        造价 / 每周建造力 / 州静态地理
    candidates.py          候选包 + 五元打分 + 逐步贪心 + 意图
    signals.py             触发信号（价格/财政/结构）
    brief.py               一页纸简报（带字符预算）
    ledger.py              预测台账（记账 → 对账 → 偏差）
    advisor.py             决策计算层门面（Advisor）
    selftest.py            决策计算层 34 项测试
    ADVISOR.md             决策计算层设计/口径/对接说明
    cli.py                 命令行
  v3ai\                    决策层（LLM + 工具 + 审查 + 执行）
    agent.py               LLM 决策循环（概况→工具→提议→审查→执行→反馈）
    advisor_bridge.py      计算层适配器（v3calc.advisor ↔ Runtime/工具/审查）
    world.py               世界模型（快照+规则+州地理+可建性+PM 校验）
    review.py              审查层（纯脚本硬闸门）
    executor.py            执行层（dry_run / bridge 文件协议 / mock）
    tools.py               30 个工具（in-process + MCP 共用）
    mcp_server.py          stdio MCP server
    web_server.py          Web 控制台（国家数据 + 御前对话）
    web/                   页面（index.html / app.css / app.js）
    strategy.py            策略模型/历史
    llm.py                 OpenAI 兼容客户端 + tool calls
    cli.py                 python -m v3ai ...
    README.md              决策层架构/运行/对接文档
  tests\test_core.py       28 项计算/读取测试
  tests\test_v3ai.py       24 项决策层测试（契约层对每份快照各跑一遍 + 大清金标）
  README.md
```

---

## 8. 已验证 / 未验证

**已验证（本机，2026-11-12）**

- 游戏数据：54 商品、450 PM、203 PM 组、122 建筑、179 科技、148 法律；
  商品顺序与 `v3live.cli.goods_table()` **完全一致**。
- 存档：大清 157 → 43 州、302 栋建筑、19,498 级、366,405,617 人、
  GDP 84,674,628.3968；税收法律 `law_land_based_taxation`；24 法律/24 科技。
- 人口（`--pops`）：365 条州×职业，workforce 合计 91,601,391
  （= 存档 `population_political_participants`），dependents 合计 274,804,226，
  两者相加正好 366,405,617。
- 信贷额度：存档 `credit=155,464,941.55432`，与逆向层内存读数
  `155464941.55` 一致。
- PM：81 种实际启用 PM 全部判定通过；科技门/法律门/前置 PM 门单测通过。
- **live 实测（2026-11-12，1.15.1）**：`read` 成功读到 43 州 / 302 栋建筑 /
  54 商品市场价 / 43 州本地价；GDP 默认口径 85,263,929（+0.19%，界面约 85.1M）。
  缓存建立后单次读取约 0.6 秒（diagnostics.timings 可查）。
  旧版逐类扫描约 1067 秒；现已改为首次自动 `prewarm`（一次扫完全部类，约 2~3 分钟），
  之后走缓存。
- GDP：总量自洽（周=产出−投入、年=周×52、商品求和一致）；方案替换后
  deepcopy 不改原快照，增量与重算一致。
- CLI：`info` / `read`（live + save）/ `gdp` / `pm` / `upgrades` / `doctor` 全部跑通。

**未验证 / 限制**

- `watch` 的循环/重连/CSV/主菜单等待已实现；游戏关闭时验证了“等待游戏→退出”
  和模拟进程下的两轮写入。主菜单实机验证：开游戏停在主菜单跑
  `python v3.py watch --rounds 1 --interval 5`，应看到 `[主菜单] ... 按 Enter`，
  而不是一堆 `[读取失败]`。
- 二进制 `.v3` 存档不支持。
- live 不读国家 tag/宗教（内存未定位）；`pm` 里依赖宗教的条目会显示"条件未知"。
- 州→地理区域映射未接（`unlocking_geographic_regions` 一律 conditional）。
- 公司类别 / 权力集团原则/身份未读（conditional）。
- live 暂未聚合 CPop（`--pops` 仅 save 模式可用）；州人口分层已读。
- 贸易路线/关税收入明细未单独计算（关税等级已读）。
- GDP 的 save 模式没有本国市场价，绝对值偏高（见 §3.1）；live 模式默认
  market 价，误差约 −3%。

---

## 9. 测试

```bat
cd /d D:\Game\ai\V3
python -m unittest discover -s tests -v      :: 52 项：读取/计算 + 决策层
python -m v3calc selftest -v                 :: 34 项：决策计算层
```

决策层测试分两层，**默认对每份可用快照各跑一遍**：

* **契约层**：不假设国名/州 id/科技年代，只断言"任何玩家国都必须成立"的性质
  （国家 id 与快照一致、GDP>0、候选必须落在真实州/真实建筑、
  不可建必被拒、`advise` 必须存在且可解析、MCP 工具齐全）；
* **金标层**：只针对 1836 大清夹具的历史时点语义（州 914/907 的资源、
  1836 年没有缝纫机科技、`worker_exploitation_*` 只警告不 schema 拒），
  它验证"那个年代的规则"，不是"代码只支持大清"。

默认夹具：`examples/snapshot_chi_1836.json`（存档模式、无买卖单）＋
根目录 `snap_live.json`（live、有买卖单，走价格反馈）。用 `V3_SNAPSHOT` 可只跑一份，
例如 `set V3_SNAPSHOT=snap_live.json` 可单独验证"换一个玩家国照样成立"。

其余覆盖：脚本解析、游戏数据计数与商品顺序、存档基础值、快照 JSON 往返、
GDP 自洽/价格模式/排除商品、PM 科技门/法律门/前置 PM/地理条件、
方案增量与不可变性、空快照合成；工具、审查、回合冻结、MCP、one-shot、Web 载荷。

价格模型与候选打分另有 34 项自测（`v3calc/selftest.py`）：逐商品复现快照市价、
链条覆盖、价格反馈方向、部分均衡陷阱防护、贪心预算、简报字符预算、台账对账。

依赖：
- 游戏目录（自动发现，或 `V3_GAME`）
- 文本存档（默认 `D:\Game\ai\Victoria3-RE\data\saves\save.txt`，或 `V3_SAVE`）
  缺失时相关测试自动 skip。
- 决策层测试固定用 `examples/snapshot_chi_1836.json`，可用 `V3_SNAPSHOT` 换数据。

---

## 10. 下一步建议

1. 已实测 live：默认 `market + staffing + producers` 年 GDP 85.26M（+0.19%），
   与另一会话 85.259M（+0.19%）只差 0.006%；后续按不同日期复核即可。
2. 接入州→地理区域、公司类别、权力集团原则，消灭 `conditional`。
3. 把同一套 `ModifierSet` 聚合用于行政力/建造力/权威/影响力（当前只有 GDP 和 PM）。
4. 支持二进制 `.v3`（token 表已在 `Victoria3-RE\docs\存档token表.json`）。
5. 加"换法律/科技"的方案枚举（与换 PM 共用 scenario 层）。
6. `advise` 的口径落地：把 `ledger` 的 `median_bias` 接回候选打分（自动校准），
   并把"州建造效率/临时修正"读进 `construction.py`，让 `weeks` 从量级变成工期。
7. `pm_candidates` 换成计算层版本（含价格反馈与风险），并给 PM 加"换产线时间损失"。





