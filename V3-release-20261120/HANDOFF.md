# Victoria 3 数据层 / 计算层 交接文档

> 接收方：中心决策层
> 交付方：`D:\Game\ai\V3`（v3calc）
> 日期：2026-11-12（2026-11-18 增补决策计算层）
> 状态：读取 + 计算 + 决策计算层可联调；`python -m unittest discover -s tests -v` → **52/52 通过**，
> `python -m v3calc selftest` → **34/34 通过**
> 游戏版本：Victoria 3 **1.15.1**（存档/内存字段以该版本为准）

---

## 0. 一句话

这一层只做两件事：**把游戏里的事实读成 `snap.json`**，以及**在事实上算 GDP / 当前允许的生产方式 / 换 PM 的 GDP 增量**。
它**不写游戏、不执行操作**；决策层拿 JSON 或 Python API 做判断，执行由你自己的行动层负责。

### 给决策层的 8 条硬规则

1. 绝对 GDP 只用 **live + market + staffing + producers**；`local` 和 `all` 已实测否定。
2. GDP、PM 表、升级方案必须来自**同一份 `snap.json`**，不要跨快照拼数据。
3. `save` 模式只看结构/相对排序，**不看绝对 GDP**（存档没有本国市场价）。
4. PM 只执行 `available`；`conditional` 需要人工确认，`locked` 不可执行。
5. `upgrades` 只按商品产出−投入排序；执行前必须看行政力/就业/死亡率/生活水平。
6. GDP/市场/预算按周 tick 变化；暂停时连续读到相同值是正常的。
7. 不读主菜单/加载中的半成品；`read`/`watch` 会拒绝，不要用 `snap.partial_*`。
8. 本层只读；建造、换法、换 PM 由独立行动层执行，执行后重新 `read`。

---

## 1. 职责边界

| 本层负责 | 本层不负责 |
|---|---|
| 只读进程内存（`PROCESS_VM_READ`） | 写内存、点 UI、改存档 |
| 主菜单/读档/加载中判定 | 自动玩游戏、执行建造/换法 |
| 国家、预算、州、建筑、市场、法律、科技、法令、机构、公司、IG、政治运动 | 贸易路线明细、所有权拆分、subjects 拆分 |
| GDP（可配置价格/缩放/建筑范围） | 精确到界面个位数的绝对 GDP（见 §6） |
| 当前科技/法律下 PM 是否允许 | 权力集团原则/公司类别/地理区域的完整判定（标 conditional） |
| 换 PM 的全国 GDP 增量 | 行政力/就业/死亡率/生活水平的完整多目标优化 |

---

## 2. 30 秒上手

游戏运行且**已加载存档**：

```bat
cd /d D:\Game\ai\V3

python v3.py doctor
python v3.py prewarm                 :: 游戏重启后跑一次
python v3.py read --json snap.json
python v3.py gdp --snapshot snap.json --target 85100000
python v3.py pm --snapshot snap.json --building building_tea_plantation --show-locked
python v3.py upgrades --snapshot snap.json --best --top 20
```

持续刷新：

```bat
python v3.py watch --interval 1 --json snap.json
```

游戏没开时，只能用**文本存档**做离线分析：

```bat
python v3.py read --save "...\save.txt" --json snap.json
python v3.py gdp --snapshot snap.json --price base        :: 只能看相对，不要看绝对值
```

---

## 3. 三种数据来源

| 模式 | 命令 | 数据新鲜度 | 用途 |
|---|---|---|---|
| live | `read` / `watch` 不带 `--save/--snapshot` | 每次命令/每轮重新读字段 | **决策主路径** |
| snapshot | `--snapshot snap.json` | 固定，文件是某时刻快照 | 回放、对比、离线计算 |
| save | `--save save.txt` | 固定，文本存档 | 无游戏时的结构/PM 分析；绝对 GDP 不可用 |

`watch` 写 `snap.json` 时是**原子覆盖**（先写 `.tmp` 再 `os.replace`），不会出现半截 JSON。

---

## 4. 决策层推荐流程

```text
1. 确认游戏已加载存档（不是主菜单）
2. prewarm（每次游戏重启后一次）
3. read --json snap.json
4. 校验 captured_at / states / buildings / population
5. 用同一个 snap.json 做：
   - GDP：GDPCalculator(gd, snap).compute()（默认口径）
   - 允许 PM：PMChecker(gd, snap)
   - 升级方案：rank_pm_upgrades(gd, snap)
6. 决策（结合非 GDP 效果）
7. 行动层执行
8. 行动后重新 read / 等下一轮 watch
```

**关键约束**：

- 绝对 GDP、PM 收益、方案排序必须来自**同一份快照**；不要拿 10:00 的 GDP 对比 10:05 的 PM 表。
- GDP 只有**跨周 tick** 才变；建筑/PM 换完后下一轮快照立即可见。暂停时数值不变是正常的。
- 只看 GDP 会选到“少消耗投入所以 GDP 上升”的 PM（例如政府行政换 `pm_simple_organization`），
  或死亡率换 GDP 的 `worker_exploitation_*`；必须用 `pm --building` 看完整修正后再决策。

### 4.1 新的推荐入口：`v3calc.advisor`（2026-11-18）

`gdp.py` 回答"现在值多少"，`scenario.py` 回答"换这个 PM 值多少"；
**`advisor` 回答"再加一级会怎样"**——价格是内生的，所以候选必须先把价格反馈算进去。

```
Advisor(gd, snap) ──► text()      一页纸简报（≤2400 字符，可直接喂 LLM）
                  ──► candidates() 候选包：年增值/建造点/工期/风险/价格影响
                  ──► plan()      逐步贪心（选一项→重算价格→再选）
                  ──► signals()   触发信号（空 = 本轮不必干预）
                  ──► ledger      预测记账 → 对账 → bias 校准
```

价格公式（用 54 商品逐项验证，误差 0；`gold` 除外）：

```
d > s : mult = 1 + 0.75 × (d/s − 1)
s > d : mult = 1 − 0.75 × (s/d − 1)
价格 = 基础价 × clamp(mult, 0.25, 1.75)
```

命令行等价入口：

```bat
python -m v3calc advise brief   --snapshot snap.json
python -m v3calc advise plan    --snapshot snap.json --weeks 12
python -m v3calc advise signals --snapshot snap.json
python -m v3calc selftest -v
```

已接进 `v3ai`：`Runtime.brief()["advise"]`（文本+信号+方案）、
`build_candidates`/`estimate_build` 由计算层算、审查层与计算层共用可建性判定。
设计细节与近似说明见 `v3calc/ADVISOR.md`。

---

## 5. 数据契约

### 5.1 Snapshot（`read --json` / `snap.json`）

顶层：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema` | int | 当前为 `1`；对接时校验 |
| `game` | object | `source`(live/save)、`captured_at`(ISO)、`game_version`、`pid`、`module_base`、`save_path`、`save_date`、`notes` |
| `country` | object/null | 玩家国家 |
| `states[]` | array | 本国州 |
| `buildings[]` | array | 本国建筑 |
| `market` | object/null | 本国市场 |
| `laws[]` | array | 生效法律 |
| `techs[]` | array[str] | 已研发科技 |
| `decrees[]` | array | 国家/州法令 |
| `institutions[]` | array | 机构投入 |
| `companies[]` | array | 公司 |
| `interest_groups[]` | array | 利益集团 |
| `movements[]` | array | 政治运动 |
| `timed_modifiers[]` | array | 临时修正 |
| `tax` | object/null | 税收等级/税率/关税 |
| `pops` | array/null | `--pops` 仅 save 模式；州×职业 |
| `diagnostics` | object | `timings`、`sections`(ok/empty/error)、`warnings` |

`country`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | 国家 id（大清 = 157） |
| `tag` | str/null | live 模式通常为 null；save 模式有 |
| `market_id` | int/null | 所属市场；`0xFFFFFFFF` 读成 null |
| `religion` | str/null | live 通常 null |
| `treasury` | float | 国库（英镑） |
| `credit_limit` | float | 信贷额度 |
| `loan_principal` | float | 贷款本金（live） |
| `base_wage` | float | 基础工资 |
| `population` | int | 总人口（州人口合计） |
| `gdp` | float/null | **只有 save 模式**有界面趋势值；live 为 null |
| `budget` | object | 见下 |

`country.budget`：

| 字段 | 说明 |
|---|---|
| `income[16]` / `expenses[22]` | 已结算的固定槽位（英镑） |
| `income_total` / `expense_total` / `balance` | 合计/周结余 |
| `tax_level` | 0~4（very_low~very_high） |
| `weekly_investment_pool` | 每周投资池变化 |

> 槽位含义是固定的（收入 1 所得税、2 人头税、3 消费税、4 红利税、8 铸币、9 关税…；
> 支出 3 政府工资、6 军队工资、7 建造商品、10 利息…）。决策层通常只用合计/结余；
> 需要逐项时以 `v3live` 字段表为准。收支数组是**上周已结算**，不是下周预测。

`states[]`：

| 字段 | 说明 |
|---|---|
| `id` / `owner` / `capital` / `arable_land` | 州 id / 归属 / 首府 / 耕地 |
| `population` / `population_lower/middle/upper` | 人口（后三者 live 有，save 可能 null） |
| `radicals` / `unemployed` / `laborers` | 激进派 / 失业 / 劳工 |
| `infrastructure` / `infrastructure_used` | 基础设施总量/已用 |
| `local_prices[54]` | 各州本地价（按商品 index；index 顺序 = `GameData.goods`，live 有，save 为 null） |
| `market_supply[54]` / `market_demand[54]` | 州视角市场供需（live） |

> **不要用 `local_prices` 算 GDP**：它包含市场接入度折扣，实测比界面低 ~17%（见 §6）。

`buildings[]`：

| 字段 | 说明 |
|---|---|
| `type` | 建筑类型名（`building_*`） |
| `state_id` / `levels` | 所在州 / 等级 |
| `staffing` | 实际就业规模；满员 ≈ levels |
| `throughput` | 吞吐倍率（通常 0.6~1.67；读不到时按 1.0） |
| `production_methods[]` | 当前启用的 PM 名 |
| `active` | save 模式有；默认 true |
| `profit` / `salary_rate` / `hiring_rate` | 周利润 / 工资率 / 招聘率 |
| `id` | 建筑 id（live/save） |

`market`：

| 字段 | 说明 |
|---|---|
| `id` | 市场 id |
| `prices` | `{商品名: 价格}`；名字来自 54 商品表 |
| `base_prices` | 基础价 |
| `supply` / `demand` | `{商品名: 数量}`（live；save 可能为空） |

`laws[]`：`name` / `group` / `date`（date 原始值，不保证可读）。
`techs[]`：科技名列表。
`tax`：`level`、`taxation_law`、`rates`、`taxed_goods`、`import_tariffs`、`export_tariffs`。
`diagnostics.sections`：某一段读失败会标 `error`，但整体仍返回；决策前检查 `warnings` 和 `sections`。

### 5.2 GDPResult（`gdp --json`）

```json
{
  "weekly": 1644692.97,
  "annual": 85524035.0,
  "output_weekly": 95496841.0,
  "input_weekly": 9972807.0,
  "per_capita": 0.233,
  "population": 366665109,
  "price_basis": "market",
  "scale": "staffing",
  "building_filter": "producers",
  "goods": [
    {"good": "grain", "output_qty": 26880.0, "input_qty": 0.0,
     "output_value": 537601.0, "input_value": 0.0,
     "net_qty": 26880.0, "net_value": 537601.0}
  ],
  "building_types": [
    {"type": "building_tea_plantation", "levels": 199, "staffing": 199.0,
     "throughput": 1.0, "output": 222980.0, "input": 0.0,
     "net": 222980.0, "count": 22}
  ],
  "states": [{"state_id": 914, "output": 12345.0, "input": 678.0, "net": 11667.0}],
  "warnings": []
}
```

- `goods[]` 已按 `|net_value|` 降序；`building_types[]` 同理。
- `annual = weekly × 52`；`per_capita = annual / population`。
- 默认 `price_basis=market`、`scale=staffing`、`building_filter=producers`（见 §6）。

### 5.3 PM 报告（`pm --json`）

`building_report`：

```json
{
  "building": "building_tea_plantation",
  "count": 22,
  "levels": 199,
  "building_unlocked": true,
  "missing_building_techs": [],
  "groups": [
    {
      "group": "pmg_tea_exploitation",
      "current": ["default_labour"],
      "hidden_when_unavailable": false,
      "pms": [
        {
          "pm": "worker_exploitation_tea",
          "group": "pmg_tea_exploitation",
          "status": "available",
          "label": "可用",
          "reasons": [],
          "missing_techs": [], "missing_laws": [], "blocked_laws": [],
          "missing_pms": [], "conditional": [],
          "hidden": false, "is_default": false
        }
      ]
    }
  ],
  "warnings": []
}
```

`status` 只有四种：

| status | 含义 | 决策层怎么用 |
|---|---|---|
| `active` | 当前正在用 | 无需切换 |
| `available` | 现在就能换 | 可进候选 |
| `conditional` | 缺数据（公司/原则/地理），**不是不可用** | 需要人工确认，不能直接当可用 |
| `locked` | 被科技/法律/前置 PM/宗教/输入商品明确卡住 | 不可选；`reasons` 给原因 |

**判定规则**：科技 OR、法律 OR、前置 PM OR、`disallowing_laws` NOR、宗教；公司类别/权力集团原则/身份/地理区域因为数据未读，标 `conditional`。

### 5.4 换 PM 增量（`upgrades --json`）

```json
{
  "building": "building_tea_plantation",
  "group": "pmg_tea_exploitation",
  "current": ["default_labour"],
  "candidate": "worker_exploitation_tea",
  "status": "available",
  "gdp_before": 85524035.0,
  "gdp_after": 93746345.0,
  "delta": 8222310.0,
  "delta_pct": 9.61,
  "state_id": null
}
```

- `delta` 是**全国年 GDP** 增量；`state_id` 非空时只改该州。
- 只对**每栋建筑自己的 PM 组合**复核前置；不满足的建筑跳过（例如没锯木厂的伐木场不能直接换 `pm_increased_hardwood`）。
- **只算商品产出−投入**，不含行政力/就业/死亡率/生活水平。

---

## 6. 口径与精度（决策前必读）

### 6.1 已确认的 GDP 口径

```
价格 = 本国市场统一价 (market)
缩放 = 实际就业规模 × 吞吐量 (staffing)
建筑 = 只算有商品产出的建筑 (producers)
```

命令默认值就是这一组；`auto` 在 live 下解析为 market，在 save 下解析为 base。

### 6.2 实测

大清 1836 开局，界面 GDP ≈ 85.1M：

| 时间/来源 | 年 GDP | 偏差 |
|---|---|---|
| live 14:02 快照 | **85,524,035** | **+0.50%** |
| live 13:56 快照 | 84,754,975 | −0.41% |
| live 02:38 快照 | 85,263,929 | +0.19% |
| 中心决策层独立会话 | 85,259,000 | +0.19% |

同一口径在不同快照/人口/价格时点会差 ±0.5%，属于正常；**不要追求单次完全相等**。
参照值用界面 GDP；`gdp --target 85100000` 可打印偏差。

### 6.3 两条已否定的路

| 口径 | 实测偏差 | 为什么不能用 |
|---|---|---|
| `--price local` | **−16.7%** | 各州本地价含市场接入度折扣；生产州价格被本地供给压低，不是游戏 GDP 用的“市场价” |
| `--buildings all` | **−3~6%** | 政府行政/兵营消耗纸张木材却不产出商品，计入只会单方面扣投入 |

`save` 模式默认 `base + staffing + producers` = 113.46M（+34%），因为文本存档**没有本国市场价**；
只适合看结构/相对变化，**绝对值和界面不可比**。

### 6.4 tick 语义

- GDP、市场价、预算按**周**结算；暂停或没跨周时连续读到的值相同是正常的。
- 建筑等级/PM 改动在下一轮快照立刻可见。
- 收支数组是“上周已结算”；界面有时显示“下周预测”，两者不要混用。

---

## 7. PM 可用性：决策层注意

- `active`：当前建筑正在用；不要把它当“最优”。
- `available`：现在就能换；可以进候选。
- `conditional`：缺权力集团/公司/地理数据；**不能**当 available 自动执行。
- `locked`：有明确原因（缺科技/法律/前置 PM/宗教/输入商品）；`reasons` 可直接展示。

已验证：大清 302 栋建筑里实际启用的 **81 种 PM 全部判定通过（locked 0 / conditional 0）**。
科技门/法律门/前置 PM 门有单测。

`upgrades` 只是 GDP 排序；决策层至少要再加：

1. 非商品效果：行政力、就业、死亡率、生活水平、激进派；
2. 政策约束：不要为了 GDP 自动切到 `worker_exploitation_*` 或拆掉政府行政的纸张投入；
3. 只对 `available` 的候选执行，`conditional` 需人工确认。

---

## 8. 主菜单 / 加载 / 缓存 / 扫描

### 8.1 主菜单判定（精确）

直接读州注册表 `g_regState`：

```
registry +0x20  entries 指针
registry +0x2C  容量（1024/2048；不是州数量）
entry[i] +8     对象指针
```

只有找到首 qword 是 **CState vtable**（RTTI 解析，不硬编码）的真实对象才算“在世界里”。
主菜单/加载中**不会扫描**；`watch --wait-load N` 每 N 秒只做一次注册表探测。

输出：

```
[主菜单] 请在游戏里加载存档，然后按 Enter 继续（Ctrl+C 退出）…
[主菜单/加载中] 未检测到完整存档；10 秒后重试（会重新扫描）…
```

### 8.2 扫描/加载中的半成品

如果扫描跨过读档，或注册表显示 `loading`，本层会：

- 清掉当前进程的半成品缓存；
- `read` 自动重扫一次；
- `watch` 等待后重扫；
- 拒绝“0 个州”的快照。

**不要使用 `snap.partial_*.json`**；那是事故快照。正常 `snap.json` 只由成功读取写出。

### 8.3 缓存

- `v3live/_cache` 只缓存**对象地址**，不缓存字段值；同一局游戏内重复读很快。
- key = `PID + 模块基址 + 进程启动时间`；游戏重启后自动失效。
- 首次/重启后 `prewarm`；之后单轮读取约 **0.55~0.70 秒**（本机）。
- 开局 `CDecree=0` 的空缓存有 900 秒 TTL；新颁布法令后跑一次 `prewarm --refresh`。
- `--no-prewarm` 只在确认缓存新鲜时使用。

---

## 9. 已知限制（决策层不要依赖）

1. **只读 + 只算**：没有执行层，不能建造/换法/换 PM。
2. live 模式 `country.tag`、`religion` 通常为 null；save 模式有。
3. `conditional` PM：公司类别、权力集团原则/身份、州→地理区域映射未接。
4. 贸易路线、关税收入明细、所有权/subject GDP 拆分未做。
5. 二进制 `.v3` 存档不支持；只支持文本 `save.txt`。
6. live 模式不聚合 CPop；`--pops` 仅 save。
7. GDP 绝对值 ±0.5% 量级；单国偏差方向可能不同。
8. 游戏更新后偏移/签名可能失效：先跑 `doctor` + 测试；失败就用 save 模式或等读取层更新。
9. 游戏停在主菜单时不要执行 `read`/`gdp`；`watch` 会等待。

---

## 10. 验证记录

| 项 | 结果 |
|---|---|
| 单元测试 | **52/52 通过**（含决策层 24 项，默认对每份快照各跑一遍）；`python -m v3calc selftest` 34/34 |
| 全局对象（完整存档） | 735 CCountry / 1156 CState / 6496 CBuilding / 1 CJominiPlayer |
| 玩家（大清） | 43 州 / 302 栋建筑 / 19,498 级 |
| 人口 | 366,405,617（02:38）→ 366,665,109（14:02） |
| GDP（默认口径） | 84.75M ~ 85.52M，对 85.1M 在 ±0.5% 内 |
| 活跃 PM 判定 | 81 种，locked 0 / conditional 0 |
| 缓存读取 | 0.55~0.70 秒/轮 |
| 注册表判定 | 主菜单/加载中/存档 三层测试通过；resolver/vtable 在当前 exe 上可发现 |

---

## 11. 对接检查清单

- [ ] 游戏运行且**已加载存档**（不是主菜单）
- [ ] `python v3.py doctor` 全部 PASS（或仅 WARN 进程未启动）
- [ ] 游戏重启后跑过 `python v3.py prewarm`
- [ ] `python v3.py read --json snap.json`
- [ ] 检查 `captured_at` 足够新；`states`/`buildings` 不为 0
- [ ] 用同一份 `snap.json` 算 GDP/PM/升级
- [ ] 绝对 GDP 用 live + 默认口径；save 只看相对
- [ ] 只执行 `available` 的 PM；`conditional` 人工确认
- [ ] 换 PM 前看 `pm --building` 的非 GDP 效果
- [ ] 不使用 `snap.partial_*.json`；不手工改 `_cache`
- [ ] 游戏更新/重启后重新 prewarm
- [ ] 决策与执行分离；本层不写游戏

---

## 12. 命令速查

```bat
:: 环境 / 进程
python v3.py doctor

:: 游戏重启后一次
python v3.py prewarm
python v3.py prewarm --refresh

:: 一次性快照
python v3.py read --json snap.json
python v3.py read --save "...\save.txt" --pops --json snap_save.json

:: GDP
python v3.py gdp --snapshot snap.json --target 85100000
python v3.py gdp --snapshot snap.json --by-good --by-building
python v3.py gdp --snapshot snap.json --calibrate --target 85100000
python v3.py gdp --snapshot snap.json --json gdp.json

:: 允许的 PM
python v3.py pm --snapshot snap.json --building building_tea_plantation --show-locked
python v3.py pm --snapshot snap.json --all-buildings --json pm_all.json
python v3.py pm --snapshot snap.json --building building_food_industry --tech baking_powder

:: 换 PM 方案
python v3.py upgrades --snapshot snap.json --best --top 20
python v3.py upgrades --snapshot snap.json --building building_tea_plantation --top 10
python v3.py upgrades --snapshot snap.json --state 914
python v3.py upgrades --snapshot snap.json --best --top 20 --json upgrades.json

:: 实时
python v3.py watch --interval 1 --json snap.json
python v3.py watch --wait-load 10 --json snap.json
```

---

## 13. Python API 示例

```python
import sys
sys.path.insert(0, r"D:\Game\ai\V3")

from v3calc.model import Snapshot
from v3calc.gamedata import GameData, discover_game_dir
from v3calc.gdp import GDPCalculator
from v3calc.pm import PMChecker
from v3calc.scenario import rank_pm_upgrades, best_upgrade_per_group

gd = GameData(discover_game_dir())
snap = Snapshot.load(r"D:\Game\ai\V3\snap.json")

# GDP：默认 market + staffing + producers
res = GDPCalculator(gd, snap).compute()
print(res.annual, res.per_capita, res.price_basis, res.scale, res.building_filter)

# 允许的 PM
checker = PMChecker(gd, snap)
rep = checker.building_report("building_tea_plantation")
for g in rep.groups:
    for s in g.statuses:
        print(g.group, s.pm, s.status, s.reasons)

# 换 PM 的 GDP 增量
rows = rank_pm_upgrades(gd, snap, price="auto", scale="staffing",
                        buildings="producers")
for r in rows[:10]:
    print(r.building, r.group, r.current, "->", r.candidate,
          r.delta, r.delta_pct)
```

如果要“假设已研发某科技/已生效某法律”：

```python
checker = PMChecker(gd, snap, extra_techs={"baking_powder"},
                    extra_laws={"law_command_economy"})
```

---

## 14. 文件索引

| 路径 | 作用 |
|---|---|
| `HANDOFF.md` | 本文件 |
| `README.md` | 使用说明/口径/限制（更详细） |
| `v3.py` | CLI 入口 |
| `v3calc/model.py` | Snapshot 数据模型 + JSON 往返 |
| `v3calc/live.py` | 实时读取后端 |
| `v3calc/savefile.py` | 文本存档后端 |
| `v3calc/gamedata.py` | 商品/PM/PM 组/建筑/科技/法律 |
| `v3calc/gdp.py` | GDP 计算 + 校准 |
| `v3calc/pm.py` | PM 可用性判定 |
| `v3calc/scenario.py` | PM 替换 / GDP 增量 / 最优升级 |
| `v3calc/market.py` | 价格模型（价格反馈推演）+ 产业链聚合 |
| `v3calc/construction.py` | 造价 / 每周建造力 / 州静态地理 |
| `v3calc/candidates.py` | 候选包 + 五元打分 + 逐步贪心 + 意图 |
| `v3calc/signals.py` | 触发信号（价格/财政/结构） |
| `v3calc/brief.py` | 一页纸简报（字符预算） |
| `v3calc/ledger.py` | 预测台账（记账/对账/偏差） |
| `v3calc/advisor.py` | 决策计算层门面 |
| `v3calc/selftest.py` | 决策计算层 31 项测试 |
| `v3calc/ADVISOR.md` | 决策计算层设计/口径/对接 |
| `v3ai/advisor_bridge.py` | 计算层适配器（Runtime / 工具 / 审查共用） |
| `v3calc/worldstate.py` | 主菜单/读档/加载中判定（`g_regState`） |
| `v3calc/fastscan.py` | 并行 vtable 扫描 |
| `v3live/` | 从 `Victoria3-RE\tools\v3live` 原样引入的读取层（见 `VENDOR.md`） |
| `tests/test_core.py` | 28 项测试 |
| `tests/test_v3ai.py` | 24 项决策层测试（契约层对每份可用快照各跑一遍；大清 1836 另作金标） |
| `examples/snapshot_chi_1836.json` | 大清 1836 文本存档快照（离线示例） |
| `examples/gdp_result.json` | GDPResult 示例 |
| `examples/pm_tea.json` | PM 报告示例 |
| `examples/upgrades.json` | 升级方案示例 |
| `snap.json` | 运行时最新快照（每次 `read --json`/`watch --json` 覆盖） |
| `snap.partial_20261112_1343.json` | **事故快照，不要使用** |

---

## 附录 A：最小可用 JSON 判断

```python
import json
s = json.load(open("snap.json", encoding="utf-8"))

assert s["schema"] == 1
assert s["game"]["source"] == "live"
assert s["country"]["id"] > 0
assert len(s["states"]) > 0 and len(s["buildings"]) > 0
assert not any(v == "error" for v in s["diagnostics"]["sections"].values())
```

如果 `game.source == "save"`：只做结构/PM/相对 GDP，不看绝对 GDP。
如果 `country.tag is None`：正常（live 不读 tag），用 `country.id` 识别。
如果 `states`/`buildings` 为 0：说明读到了主菜单或半成品，**不要决策**，重跑 `read`/`watch`。

## 附录 B：口径选择决策树

```text
要绝对 GDP？
├─ 是 → 必须 live + 默认（market/staffing/producers）
│       └─ 用 --target 对照界面，偏差应在 ±0.5% 量级
└─ 否 → 只需相对比较/排序
        ├─ live：默认口径
        └─ save：base + staffing + producers（只看同快照内排序）
```

---

**交接结论**：读取、GDP、PM 可用性、PM 升级增量四块已可用；主菜单/加载中/缓存/半成品都有防护；
决策层只需消费 `snap.json` + 本层 Python API，执行动作由独立行动层负责。
