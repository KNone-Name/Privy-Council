# v3ai —— Victoria 3 AI 决策层

> 位置：`D:\Game\ai\V3\v3ai`
> 依赖：`v3calc`（事实 + 计算）、`v3live`（内存读取）、游戏文件（规则）、`v3bridge`（执行，待适配 1.15.1）
> 状态：离线链路（snap.json + dry-run + mock LLM）已跑通；真实 LLM / 真实执行端待接入

---

## 一键启动

双击工作区根目录的 `启动项目.bat`，或在管理员 PowerShell 运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\v3ai\host\start_project.ps1
```

它会：确认游戏已进入存档 → 启动宿主 web → 等待健康检查 → 启动 DLL/watch → 打开 `http://127.0.0.1:8765`。

---

## 0. 一句话

**LLM 看概况 → 自己调工具查数据 → 提议动作 → 脚本审查 → 执行端 → 结果回灌下一轮。**

v3ai 不直接写游戏内存。它把"事实（快照）"和"规则（游戏文件）"拼成世界模型，
让 LLM 在**策略 + 硬约束**下自主查证和决策；审查层挡在 LLM 与执行端之间，
只放行当前科技/法律/资源/队列条件下确实能执行的动作。

三端边界：

| 端 | 目录 | 负责 |
|---|---|---|
| 数据端 | `Victoria3-RE`（`tools/v3live`、`docs/字段表.json`、`data/`） | 从内存/存档读出事实与静态地理 |
| 计算端 | `V3/v3calc` | GDP、PM 可用性、换 PM 增量、快照模型 |
| 执行端 | `v3bridge 0.2` | `requests.txt` 建造、`pmm.txt` 换 PM、队列/PM 回读 |

---

## 1. 运行架构

```
                    ┌──────────────────────────────┐
                    │  LLM（云端/本地，OpenAI 兼容） │
                    └──────────────┬───────────────┘
                           function calling / MCP
                                   │
   ┌───────────────────────────────▼───────────────────────────────┐
   │ v3ai.agent.AgentLoop                                          │
   │  ① Runtime.refresh()  → 快照 + 队列 + 策略                     │
   │  ② Runtime.brief()    → compact 概况 + 上轮反馈 + 策略          │
   │  ③ LLM 工具循环（get_overview / query_* / calc_* / propose_*） │
   │  ④ 回合末 Runtime.execute_pending()                            │
   │  ⑤ journal.jsonl 记录；下一轮反馈                              │
   └───────────────────────────────┬───────────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
┌───────▼────────┐        ┌────────▼────────┐        ┌────────▼────────┐
│ 世界模型        │        │ 审查层           │        │ 执行层           │
│ world.py        │        │ review.py        │        │ executor.py      │
│ 事实+规则+地理   │        │ 科技/法律/资源/  │        │ dry_run /        │
│ 队列+待执行      │        │ 上限/队列/PM/配额│        │ bridge / mock    │
└───────┬────────┘        └────────┬────────┘        └────────┬────────┘
        │                          │                          │
┌───────▼────────┐        ┌────────▼────────┐        ┌────────▼────────┐
│ v3calc/v3live  │        │ v3calc.pm       │        │ v3bridge 文件泵  │
│ Snapshot+GameData│      │ PMChecker       │        │ requests/pmm/queue│
└────────────────┘        └─────────────────┘        └─────────────────┘
```

**一轮的生命周期**：

1. `Runtime.refresh()` 重新读快照（live 或文件）和建造队列；
2. 生成 `brief`：国家/预算/GDP/市场/州/建筑/政治/队列/策略/上轮执行结果，
   外加 **`brief.advise`**——计算层算好的简报（文本 + 信号 + 贪心方案前三项）；
3. LLM 首轮就看到概况；需要细节时自己调工具（不是把所有 JSON 塞给它）；
4. LLM 用 `propose_build` / `propose_pm` 提交，审查层**当场**返回通过/拒绝与原因；
5. 回合结束，执行已通过动作；执行结果写入 `journal.jsonl`；
6. 下一轮 `brief` 带 `execution_feedback`，LLM 根据真实结果调整。

### `advise`：算好的账

`v3calc.advisor` 先把价格反馈、建造点、工期、风险算完，再把一页纸交给 LLM：

* `brief.advise.text`：市场按产业链 + 候选包（年增值已扣价格反馈）+ 信号，≤2400 字符；
* `brief.advise.signals`：`severity>=3` 先处理，空列表 = 市况平稳；
* `brief.advise.plan`：逐步贪心参考方案（不是命令）；
* `brief.advise.capacity`：建造力的**成本**（建造点/现金/周维护）与可控性判定——
  建造部门不参与增值排序，也不该为它编估值；
* `build_candidates` / `estimate_build` 由计算层实现，旧键名保留，
  新增 `annual_value` / `per_point` / `risk` / `price_changes`；
  建造部门以 `value_basis=capacity_cost` 附在候选表末尾（可提议、不打分）；
* 审查层的可建性判定与计算层**共用** `CandidateEngine.check`（科技/资源/耕地/上限/队列）。

计算层不可用时（如游戏数据目录缺失）自动降级回旧逻辑，只记一条 note，不阻塞决策。

### 回合冻结（重要）

`agent.freeze_cycle` 默认开启。一轮的边界是：

```text
begin_cycle()
  ├─ 读一次快照 + 队列 + 执行端状态，之后整个回合冻结
  ├─ LLM 思考/调工具：只能读这份冻结数据
  │    refresh_world / execute_pending 会被拒绝（cycle_frozen）
  ├─ LLM finish_cycle
  ├─ 运行时执行已通过动作，并读取 DLL 反馈（queue.csv / pmm.result.csv）
  └─ end_cycle()
next begin_cycle()
  └─ 这时才重新读快照/队列，进入下一轮
```

这样 LLM 不会在思考中途看到"自己刚执行完"的新数据，避免同回合内前后依据不一致；
执行反馈仍然会在下一轮 `brief` 里完整出现。MCP 外部调用不受此限制（由外部编排决定）。

### 两种决策模式

| 模式 | 配置 | 行为 |
|---|---|---|
| `tools`（默认） | `agent.mode=tools` | LLM 自主调 19/31 个工具，信息最全，但本地模型 prefill 慢 |
| `oneshot` | `agent.mode=oneshot` | 运行时先算好 `build_candidates` / `pm_candidates` 短名单，LLM 一次输出 JSON 动作；审查/执行/冻结反馈完全一致 |

本地模型建议 `oneshot`；云端或需要深度查证时用 `tools`。

### 决策计算层开关

```json
"advisor": {"enabled": true, "char_budget": 2400, "top": 8, "plan_items": 3}
```

* `enabled=false`：不生成 `brief.advise`，`build_candidates`/`estimate_build` 退回旧估算；
* 计算层本身出错（游戏数据缺失、快照异常）时也会**自动降级**，只写一条 note，不阻塞决策；
* 想单独验证计算层：`python -m v3calc selftest -v`。

---

## 2. 快速开始

```bat
cd /d D:\Game\ai\V3

:: 1) 自检（不连游戏也能检查数据/规则/执行端配置）
python -m v3ai doctor --snapshot snap.json

:: 2) 看 compact 概况
python -m v3ai overview --snapshot snap.json

:: 3) 离线跑一轮（mock LLM，不联网；dry-run 不写游戏）
python -m v3ai run --snapshot snap.json --mode dry_run --once --mock

:: 4) 用脚本回放“提议→审查→执行”
python -m v3ai run --snapshot snap.json --mode dry_run --once ^
  --mock-script v3ai\examples\mock_script.json

:: 5) 只审查一个动作文件
python -m v3ai review --file v3ai\examples\actions.json --snapshot snap.json

:: 6) 审查并演练执行
python -m v3ai review --file v3ai\examples\actions.json --snapshot snap.json --apply --yes

:: 7) 接真实 LLM（先启动本地端点或填 DeepSeek key）
python -m v3ai config use local
python -m v3ai config ping local
python -m v3ai run --live --mode dry_run --once

:: 8) 以 MCP server 暴露工具（给外部 MCP 客户端）
python -m v3ai mcp
```

### Web 控制台（御前会议）

```bat
:: 读 snap.json / snap_live.json；没有 watch 时显示快照时间与 stale 标记
python -m v3ai web --snapshot snap_live.json --port 8765

:: 或直接在游戏同会话用 live 内存（host）
python -m v3ai web --live --port 8765

:: 浏览器打开 http://127.0.0.1:8765
```

页面包含：

- **中间是御前 AI 对话框**，左右两栏是国势信息（GDP/国库/建造/市场/朝局/典章），**不展示州级明细**；
- 御前对话（DSH 风格：消息气泡、思考过程折叠、模型状态、快捷问句）；
- **连续模式默认关闭**：点“连续：开”后，一轮结束等 `agent.loop.interval_s`（0=立刻）自动开下一轮；
  可设自动执行、快照过期暂停；顶部状态条显示倒计时/暂停原因；
- **启动扫描锁定**：点击“启动服务”且确实重新注入 DLL 后，等待 `types ready`；
  期间显示区域扫描进度，决策/连续/待办按钮全部锁定，完成后提示“服务已就绪”；
  不再固定等待 130 秒；
- “仅建议 / 执行一轮”按钮；计划动作会显示审查通过/拒绝，待办可单独执行；
  中心对话的“AI 思考链 / 决策轨迹”会展示结论、依据、工具调用与审批结果；
- 右上角 ⚙ **设置面板**：provider / 模型 / Base URL / API Key、one-shot/tools、toolset、
  每轮动作上限、单动作等级、上下文总字符、单工具结果字符、历史轮数、工具循环上限、
  回合冻结、连续模式/间隔/自动执行/过期暂停、页面刷新间隔；可测试连接；
  所有上限字段 **0 = 不限制**；设置写入 `v3ai/config.json`，模型切换立即生效；
- **方略拆成两个面板**：
  - **区域方略**：区域模式、当前州数、重点州列表；编辑时自动滚到区域段；
  - **建造方略**：当前策略/版本、历史策略切换、目标权重、约束；
  两个“编辑”打开同一个表单，分别滚到区域/建造段；
  区域列表随国家/存档变化自动刷新（`/api/strategy` 会 refresh 快照）；
  可点“AI 起草”填入表单，或“AI 应用”直接以 `source=ai` 写入；
- **产业链/物价**：one-shot 会附带 `market_signals` 和 `priority_chains`；
  AI 先按价格/基准比（>1.25 短缺、<0.8 过剩）补上游输入，输出过剩不扩产；
- **建设区域策略**：全国共同发展 / 核心省份 / 沿海 / 农业 / 资源 / 京畿 / 区域均衡 /
  **自定义省份**（勾选自己的 43 个州，显示中文名与人口），可限制每轮最多涉及州数；
  one-shot 的建造候选会按该策略过滤，自定义模式下审查层也会拒绝范围外的州。
- 数据每 3 秒轮询；`snap.json` 由游戏会话的 `v3.py watch` 持续更新，页面显示快照年龄，过期会标 `STALE`；
- 建造队列只统计 **government（政府/玩家）**，private 自动建造不混入玩家队列。

实时链路推荐：

```bat
:: 游戏会话（一个窗口常驻）
python v3.py watch --interval 5 --json snap.json --no-gdp

:: DSH 侧
python -m v3ai web --snapshot snap.json --port 8765
```

连续模式配置（也可在页面 ⚙ 里改）：

```json
"loop": {"enabled": false, "interval_s": 20, "execute": true, "pause_if_stale": true}
```

`interval_s=0` 表示上一轮结束立刻开下一轮；快照过期时默认暂停，避免拿旧数据连续下单。

### 一键启动 / 停止服务

页面右上角有 **启动服务 / 停止服务** 按钮：

- 启动：注入 `_dist/v3bridge-0.3-1.15.1/bin/v3inject.dll`（已注入则跳过）→ 启动 `v3.py watch`
  （`--live` 模式下跳过）→ 不自动开启连续模式，由玩家点“连续：开”显式开启；
- 停止：关闭连续模式 → 停止由本服务启动的 watch → 写 `unload.flag` 卸载 DLL；
- **网页控制台本身不关闭**，停止后还能从同一页面重新启动；
- 注入需要 web 与游戏在同一 Windows 会话/权限级别。

**DSH 沙箱里的 web 看不到游戏进程**。有两个办法让页面直接注入/卸载：

1. 在游戏会话启动 web：
   ```bat
   powershell -ExecutionPolicy Bypass -File .\v3ai\host\run_live.ps1 -Web -Port 8766
   ```
2. 或者让 DSH 里的 web 继续跑，只在游戏会话启动一次**主机代理**：
   ```bat
   powershell -ExecutionPolicy Bypass -File .\v3ai\host\start_host_agent.ps1
   ```
   代理通过 `v3ai/state/host_agent_cmd.json` / `host_agent_result.json`
   接收网页指令，代为 inject / unload / watch；页面显示“主机代理在线”。
   代理与游戏必须在同一会话，且最好同权限（游戏用管理员时，代理也用管理员）。

**扫描进度**：注入后页面服务条会显示 `类型表扫描 45s / 预计 ~125s` 的进度条。
DLL 只在扫描结束写 `types ready`，所以中途是按 125 秒估算；类型表就绪后变为 100%。

路径在 `config.service` 里改：

```json
"service": {
  "injector": "D:\\Game\\ai\\V3\\_archive\\injector.exe",
  "dll": "D:\\Game\\ai\\V3\\_archive\\v3inject_v109.dll",
  "watch_script": "v3.py", "watch_interval": 5
}
```

策略/设置 API：

```text
GET  /api/settings        读取 provider / 模型 / 决策参数
POST /api/settings        保存设置（含 API Key，写入 config.json）
POST /api/settings/test   测试当前 provider 连接
GET  /api/strategy        当前方略 + 历史版本
POST /api/strategy        玩家保存方略（JSON）
POST /api/strategy/activate 切换到某条历史方略 {index}
POST /api/strategy/draft  AI 起草方略（返回草稿，不保存）
POST /api/strategy/reset  重置默认方略
GET  /api/service         服务状态（DLL/watch/连续/游戏可见性）
POST /api/service/start   一键启动 DLL + watch + 连续
POST /api/service/stop    一键停止（卸载 DLL / watch / 连续）
GET  /api/loop            连续模式状态
POST /api/loop            开关连续模式 / 间隔 / 自动执行
```


> 本地模型（`kvmem-polystrata-qwen35`，262k context）首次 prefill 可能较慢；
> 冷缓存下完整工具循环可能 >5 分钟。v3ai 默认只给 LLM **compact 概况**（约 6.5KB）
> 并可用 `agent.toolset=core`（19 个工具）；需要更快节奏时建议用云端 provider，
> 或者切 **one-shot 快速路径**：
>
> ```bat
> python -m v3ai config set agent.mode oneshot
> python -m v3ai config set agent.max_tool_rounds 2
> ```
>
> one-shot 不让模型自由调工具，而是由运行时先算好 `build_candidates` /
> `pm_candidates` 短名单，模型一次输出 JSON 动作；审查/执行/冻结反馈完全一致。
>
> 如果 v3ai 跑在看不到 `victoria3.exe` 的会话/权限级别（例如 DSH 沙箱），
> 请用游戏同会话的终端运行 [v3ai/host/run_live.ps1](host/run_live.ps1)：
>
> ```bat
> powershell -ExecutionPolicy Bypass -File .\v3ai\host\run_live.ps1
> ```
>
> 它会 prewarm、写出 `snap_live.json`，然后做 live 自检。之后在 DSH 侧用：
>
> ```bat
> python -m v3ai run --snapshot snap_live.json --mode dry_run --once
> ```
>
> 或者加 `-Run` 直接在游戏会话里跑决策循环；  
> 要使用网页的一键启动/停止（DLL 注入/卸载），请在游戏会话启动：
>
> ```bat
> powershell -ExecutionPolicy Bypass -File .\v3ai\host\run_live.ps1 -Web
> ```
>
> 这会以 `--live` 启动 web，页面右上角的“启动服务/停止服务”可以真正注入和卸载 DLL。

> `--snapshot` 用旧快照是为了离线/回放；`--live` 才实时读游戏。
> 游戏重启后先跑一次 `python v3.py prewarm`（读取层缓存），否则首次 live 读取会重扫内存。

---

## 3. Provider（云端/本地自由切换）

配置在 `v3ai/config.json`，结构类似 DSH 的 provider 档案：

```json
{
  "active_provider": "local",
  "providers": {
    "local":   {"base_url": "http://127.0.0.1:18200/v1",
                "model": "kvmem-polystrata-qwen35", "api_key": ""},
    "deepseek":{"base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat", "api_key": ""},
    "ollama":  {"base_url": "http://127.0.0.1:11434/v1",
                "model": "qwen3:32b", "api_key": "ollama"}
  }
}
```

```bat
python -m v3ai config providers
python -m v3ai config use deepseek
python -m v3ai config set providers.deepseek.api_key sk-...
python -m v3ai config set providers.deepseek.model deepseek-chat
python -m v3ai config ping deepseek
```

临时覆盖（不落盘）：`V3AI_PROVIDER` / `V3AI_BASE_URL` / `V3AI_API_KEY` / `V3AI_MODEL` /
`V3AI_TEMPERATURE` / `V3AI_MAX_TOKENS` / `V3AI_TIMEOUT_S`。

要求端点支持 OpenAI 兼容的 `POST /v1/chat/completions` 与 `tools` 字段（function calling）。
不支持原生 tool_calls 的模型会走保守的正文 JSON 兜底（`{"tool": "...", "arguments": {...}}`），
但推荐用原生工具调用。

---

## 4. 策略：玩家可定，AI 也可定

策略只描述"优化什么 + 硬约束"，不产生动作；动作仍由 LLM 在策略下提议、审查层放行。

```json
{
  "name": "大清工业化",
  "rationale": "先扩建造力与出口品，压低激进派",
  "objectives": [
    {"metric": "gdp_growth", "weight": 0.4, "direction": "max"},
    {"metric": "weekly_balance", "weight": 0.2, "direction": "max"},
    {"metric": "standard_of_living", "weight": 0.2, "direction": "max"},
    {"metric": "radicals", "weight": 0.1, "direction": "min"},
    {"metric": "construction_capacity", "weight": 0.1, "direction": "max"}
  ],
  "constraints": {
    "max_actions_per_cycle": 6,
    "max_levels_per_action": 3,
    "min_treasury": 200000,
    "allow_conditional_pm": false,
    "block_pm_patterns": ["worker_exploitation_"],
    "warn_pm_patterns": ["pm_simple_organization"],
    "priority_buildings": ["building_construction_sector", "building_tea_plantation"],
    "forbidden_buildings": []
  },
  "notes": "不要为了 GDP 切工人剥削 PM"
}
```

```bat
python -m v3ai strategy show
python -m v3ai strategy set my_strategy.json
python -m v3ai strategy reset
```

- **玩家**：`strategy set` 写入，立即生效。
- **AI**：在决策中调用 `strategy_propose`；`agent.allow_ai_strategy_switch` 控制是否允许。
- `objectives` 是给 LLM 的目标权重（提示层）；`constraints` 才是审查层执行的硬约束。
- AI 策略会被强制 `allow_conditional_pm=false`，动作上限也会被审查层再夹一次；
  策略切换不能关闭审查层硬护栏。
- 每次切换写 `v3ai/state/strategy.json` 的 history（保留 50 条）。

---

## 5. 工具一览（in-process + MCP 共用）

| 工具 | 用途 |
|---|---|
| `get_overview` | 决策第一步：compact 概况（预算/GDP/市场/州/建筑/政治/队列/策略） |
| `advise_brief` | **算好的简报**：市场按链、候选包（含价格反馈+风险）、信号、方案 |
| `advise_plan` | 逐步贪心方案（选一项→重算价格→再选），带备选 |
| `advise_signals` | 触发信号；空列表 = 本轮不必干预 |
| `advise_capacity` | 建造力成本：建造点/现金/周维护 + 是否可控；各档建造方式按每点容量成本排序（不估值） |
| `refresh_world` | 重读快照与队列（仅回合外；回合内返回 `cycle_frozen`） |
| `query_states` | 州列表：人口/耕地/基建/激进派/资源；可排序、分页 |
| `state_detail` | 单州：建筑、耕地占用、队列、待执行、资源/特质 |
| `query_buildings` | 建筑实例：等级/就业/吞吐/利润/PM |
| `building_detail` | 单类建筑：知识库 + 实例 + PM 组 |
| `query_market` | 54 商品价格/基础价/供需；最贵/最便宜 |
| `query_budget` | 收支/结余/国库/信贷/税级/主要收支项 |
| `query_politics` | 法律/利益集团/运动/机构/法令/公司 |
| `query_techs` | 已研发 + 当前可研发（含前置） |
| `query_laws` | 当前法律 + 同组可替换候选 |
| `calc_gdp` | GDP（默认 market+staffing+producers），可给 goods/buildings/states 明细 |
| `pm_report` | 某建筑每个 PM 组：active/available/conditional/locked + 原因 + 非GDP效果 |
| `pm_upgrades` | 按全国年 GDP 增量排序的换 PM 候选 |
| `build_candidates` | 州×建筑可建候选，按估算 GDP 排序；含 `weeks_1`（建造周数） |
| `estimate_build` | 某州建 N 级的年 GDP 增量、就业、建造点数与周数估算 |
| `strategy_get` / `strategy_propose` | 读取/切换策略 |
| `propose_build` / `propose_pm` | 提交动作，**当场审查** |
| `pending_actions` / `cancel_action` | 查看/取消待执行 |
| `execute_pending` | 手动执行（仅回合外；回合内返回 `cycle_frozen`） |
| `queue_status` / `execution_feedback` | 执行端状态 / 上轮结果 |
| `history_recent` | 最近几轮摘要 |
| `finish_cycle` | 结束本回合、写 summary/rationale |

MCP server 用同一份注册表：`tools/list` 返回 31 个工具，`tools/call` 直接调用。
小模型/小上下文可切核心集（19 个）：

```bat
python -m v3ai config set agent.toolset core
```

---

## 6. 审查层规则（执行前的硬闸门）

审查层是**纯脚本**，不调用 LLM。任何 `propose_*` 都会立刻过一遍：

### 建造

- 州必须在当前快照（玩家州）；建筑键必须存在；
- `buildable` / 非自给建筑 / 非 `expandable=no` 的重复扩建；
- 科技、法律前置（`unlocking_technologies` / `unlocking_laws`）；
- 地理限制（`state_region`）、沿海、已整合、州特质；
- 唯一建筑不能重复；`max_level` 上限；
- 资源类建筑：州必须有该资源；有 `resource_caps` 的按 cap 扣已建+队列+待执行；
  农田/牧场/种植园按 `arable_land` 扣已用耕地；
- 已有建造队列的同州同建筑先拒绝；同周期重复指令拒绝；
- 估算建造成本（`required_construction` × 等级）与国库/赤字护栏；
- 策略禁建/优先建筑；
- 每周期动作数、建造数、等级上限。

### 换 PM

- 州里必须有该建筑；
- PM 必须属于该建筑的某个 PM 组；
- `PMChecker` 判定：`active` / `available` 放行；`locked` 拒绝并给缺科技/法律/前置 PM；
  `conditional` 默认拒绝（公司/原则/地理等数据缺失），策略允许才放行并警告；
- 建筑本身缺科技也拒绝；
- 同州同建筑同组不能同回合提交两个互斥 PM；
- 策略禁用/风险 PM 模式（默认警告 `worker_exploitation_*`、`pm_simple_organization`）；
- 非 GDP 效果（行政力/建造力/死亡率/生活水平/激进派/就业等）作为 warning 返回。

审查结果会原样回到 LLM；被拒动作带 `reasons`，LLM 应换目标而不是重复提交。

---

## 7. 执行层

### 三种模式

```bat
python -m v3ai run --mode dry_run   :: 只审查/记录，不写游戏（默认）
python -m v3ai run --mode bridge    :: 真实写 v3bridge 控制文件
python -m v3ai run --mode mock      :: 测试/演示
```

### bridge 文件协议

| 动作 | 写 | 等 | 读回 |
|---|---|---|---|
| 建造 | `requests.txt` `<state> <building> <levels>` | `requests.done.txt` | `queue.csv` 增量 + 日志 `queued=A->B` |
| 换 PM | `pmm.txt` `<state> <building> <pm>` | `pmm.done.txt` | `pmm.result.csv` 的 status |
| 队列 | `queue.txt` | `queue.done.txt` | `queue.csv` |

所有控制文件写到 **DLL 同目录**（`config.executor.bridge.dir`）。
写操作只能作用于玩家自己的州；越权会 `LOCK: denied`。

### ✅ 当前执行端：v3bridge 0.2（已适配 1.15.1）

- DLL：`_dist\v3bridge-0.3-1.15.1\bin\v3inject.dll`（754,723 字节），控制文件在 `_archive\`；
- 目标 exe：100,221,048 字节 / FNV `0xF8A08D788306BC2F` / TDS `0x6AF3094F`；
- 实测：`build fingerprint OK`、五项 `selfcheck ok`、`world=1`、`player=157`；
- **所有数据偏移都没变**；变的是指纹、函数 RVA 和类型表定位方式（全堆扫描 `CBuildingType`）；
- 类型表首次就绪约 **125 秒**（单线程全堆扫描），期间控制文件原地等待。
- v3ai 已用真机验证：`pm_market_squares → pm_market_stalls → pm_market_squares`
  往返 `status=ok`；`building_banana_plantation @州925 +1` 入队 `0->1`，队列回读正常。
- one-shot 真机验证：本地模型 24.33s / 7,110 tokens，基于 15:41 快照选择
  `州910 香蕉 +5`；审查通过后 bridge 入队 `0->5`，`requests done: ok=1`。

配置默认已指向 `_archive`：

```json
"executor": {"mode": "dry_run", "bridge": {
  "dir": "D:\\Game\\ai\\V3\\_archive", "expected_exe_size": 100221048}}
```

启用真实执行（会改游戏）：

```bat
python -m v3ai config set executor.mode bridge
python -m v3ai bridge queue            :: 只读验证：世界/玩家/capabilities/队列
python -m v3ai review --file v3ai\examples\actions.json --apply --yes
```

建议先 `bridge queue` 确认 `world=1`、`capabilities: build=on pm=on player=on queue=on`。
`dry_run` 仍是默认值，便于先看 LLM 决策质量再放行。

> 执行端 0.2 的建造判据仍是**队列条目增量**，不是 `rc` 返回值；PM 判据是
> `pmm.result.csv` 的 `status`。v3ai 已按这两个判据做校验。

---

## 8. 数据口径与已知限制

- **GDP 绝对值**：live + market + staffing + producers，误差约 ±0.5%；save 模式只看相对。
- **PM `conditional`**：公司类别/权力集团原则/身份等数据未读；审查默认拒绝自动执行。
- **州静态地理**：`v3ai/data/state_meta.json` 由 `v3_states.json` + `state_regions.json` 生成；
  归属（owner）不缓存，永远以实时快照为准。换版本/换存档后可用
  `python -m v3ai build_state_meta` 重建。
- **建造估算**：`estimate_build` 假设满员、吞吐 1.0、默认 PM；`construction.weeks`
  用 `required_construction` 脚本值估算，**实际受州建造效率影响**（真机队列点数是脚本值
  按州效率折算后的结果），只用于量级判断，不要当精确工期。
- **建造力**：`construction.capacity_per_week` 由建筑 PM 的 `country_construction_add`
  + GDP 基线 10 估算；真机大清为 22/周，与 RE `capacity` 命令一致。
- **换 PM 增量**：`pm_upgrades` 只算商品产出−投入，不含行政力/就业/死亡率/生活水平；
  审查层会把非 GDP 效果作为 warning 返回。
- **队列**：bridge 未连接时 `queue` 为空/`null`；dry-run 不读队列，上限校验按已建+待执行。
- **进程可见性**：如果 v3ai 与游戏不在同一 Windows 会话/权限级别，`v3calc` 可能看不到
  `victoria3.exe`（`find_pids` 为空），此时 bridge 控制文件仍可用，但完整 live 快照要在
  游戏同一会话里运行 `v3calc` / `v3ai`，或改用 save 快照。
- **游戏暂停**：数值不变是正常的；执行后下一轮快照才可见。
- **读不到就是 null**：不补 0，不猜。

---

## 9. 文件索引

| 路径 | 作用 |
|---|---|
| `v3ai/config.py` | provider 档案、执行模式、审查护栏、路径 |
| `v3ai/llm.py` | OpenAI 兼容客户端 + tool calls + mock |
| `v3ai/strategy.py` | 策略模型/校验/历史 |
| `v3ai/world.py` | 州地理、建筑知识库、可建性、PM 校验、概况/候选/估算 |
| `v3ai/advisor_bridge.py` | 计算层适配器：把 `v3calc.advisor` 接进 brief/工具/审查/台账 |
| `v3ai/review.py` | 审查层（纯脚本） |
| `v3ai/executor.py` | Executor 抽象 + dry-run + bridge 文件协议 + mock |
| `v3ai/runtime.py` | 快照/队列/策略/审查/执行的编排 |
| `v3ai/tools.py` | 31 个工具（in-process + MCP 共用） |
| `v3ai/agent.py` | LLM 决策循环 + 系统提示 + journal |
| `v3ai/mcp_server.py` | stdio MCP server（initialize/tools/list/tools/call） |
| `v3ai/cli.py` | `python -m v3ai ...` |
| `v3ai/build_state_meta.py` | 重建州静态地理缓存 |
| `v3ai/web_server.py` | Web 控制台（标准库 HTTP + API） |
| `v3ai/web/` | 页面（index.html / app.css / app.js / favicon.svg） |
| `v3ai/data/state_meta.json` | 1157 州静态地理缓存 |
| `v3ai/state/` | `strategy.json` / `journal.jsonl` / 运行期文件 |
| `tests/test_v3ai.py` | 24 项离线测试（全库 86 项：v3calc 34 + 读取/计算 28 + 决策层 24）；默认对每份可用快照各跑一遍 |

---

## 10. 下一步

1. **用真实 LLM 跑 `--live --mode dry_run`**，看工具调用质量、token 成本与决策稳定性。
2. **小批量真实执行**：先 `--mode bridge` + 单条 PM/建造，确认队列/结果回读，再放大。
3. 接入更多策略指标（识字率、创新力、生活水平分档、建造力瓶颈）。
4. 扩展动作面：法律/税/法令/机构（v3bridge 0.2 不支持，需要 UI 自动化或扩展 DLL）。
5. 把 `state_meta` 的生成并入数据端流水线（存档变化时自动刷新）。
6. 加 Web 看板（历史回合、工具调用、审查拒绝、执行结果可视化）。
