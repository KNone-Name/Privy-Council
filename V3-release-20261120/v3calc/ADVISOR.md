# v3calc 决策计算层（advisor）

> 版本：2026-11-18　|　测试：`python -m v3calc selftest -v` → **34/34 通过**
> 边界：**只算账，不调 LLM、不写游戏**。事实来自 `snap.json`，规则来自游戏脚本。

---

## 0. 一句话

把"感觉该建什么"变成**每个候选带 5 个数字的排序表**：价格反馈、建造点、工期、
现金、风险全部算进分数；简报压到 2500 字符以内。LLM 只负责取舍，不再负责搜索和算术。

```
snap.json ──► market.py    价格模型（含价格反馈推演）
          ──► construction 造价 / 建造力 / 州静态地理
          ──► candidates   候选包 + 五元打分 + 逐步贪心
          ──► signals      触发信号（正常时为空 = 这轮不用叫 LLM）
          ──► brief        一页纸（≤2500 字符）
          ──► ledger       预测记账 / 对账 / 偏差校准
                   │
                   ▼
             v3ai 决策（LLM 或规则）──► review.py ──► executor
```

---

## 1. 模块

| 文件 | 负责 |
|---|---|
| `market.py` | 价格公式、`PriceModel`（可平移推演）、产业链聚合、缺口定价 |
| `construction.py` | 建造点造价、每周建造力、州静态地理（`state_meta.json`） |
| `candidates.py` | 可建性检查、商品向量、候选打分、逐步贪心方案、PM 候选、`Intent` |
| `signals.py` | 价格/财政/结构异常 → `Signal`；`should_escalate()` |
| `brief.py` | 一页纸（dict + 文本渲染，带字符预算） |
| `ledger.py` | JSONL 台账：记账 → 对账 → `bias` / `suggested_scale` |
| `advisor.py` | 门面：一次构造，导出上面全部；`quick()` 是纯规则快通道 |
| `selftest.py` | 34 项自带测试 |

---

## 2. 用法

### 2.1 命令行

```bat
:: 一页纸简报（默认动作）
python -m v3calc advise brief --snapshot snap.json

:: 市场诊断（10 条链，谁短缺谁过剩）
python -m v3calc advise market --snapshot snap.json

:: 候选包（按"每建造点年增值"排序）
python -m v3calc advise candidates --snapshot snap.json --top 10

:: 逐步贪心方案（在给定预算内选一组）
python -m v3calc advise plan --snapshot snap.json --weeks 12 --top 6
python -m v3calc advise capacity --snapshot snap.json   :: 建造力：只算成本

:: 触发信号（空 = 这轮不需要 LLM）
python -m v3calc advise signals --snapshot snap.json

:: 规则快通道：财政护栏 + PM 升级 + 队列补位
python -m v3calc advise quick --snapshot snap.json

:: 台账：先记账，隔几周再对账
python -m v3calc advise ledger --snapshot snap.json --record
python -m v3calc advise ledger --snapshot f2.json --reconcile
```

常用开关：`--prev`（上一份快照，算变化）、`--intent`（意图 JSON）、
`--state-meta`、`--char-budget`、`--no-pm`、`--json out.json`。
`python -m v3calc advise brief --snapshot ... --record` 可把这一轮方案直接记进台账。

`python -m v3calc advise brief` 走的是 `v3.py` 同一套读取路径：不传 `--snapshot`
就实时读游戏（和 `v3.py read` 一样需要 prewarm）。

### 2.2 Python API

```python
from v3calc.gamedata import GameData
from v3calc.model import Snapshot
from v3calc.advisor import Advisor

gd = GameData(r"...\Victoria 3\game")
snap = Snapshot.load("snap.json")
a = Advisor(gd, snap)

print(a.text())            # 一页纸（给 LLM）
a.brief()                  # 结构化（可直接 JSON 给工具）
a.candidates(10)           # 候选包
a.plan(weeks=12)           # 逐步贪心（weeks = 队列深度）
a.signals()                # 信号
a.quick()                  # 规则结论
a.record(); a.reconcile()  # 台账
```

意图（只描述优化什么，不产生动作）：

```json
{
  "name": "先上游后出口",
  "priority_chains": ["木材", "重工"],
  "chain_weight": 1.6,
  "ban_buildings": ["building_barrack"],
  "ban_pm_patterns": ["worker_exploitation"],
  "budget_weeks": 12
}
```

```bat
python -m v3calc advise plan --snapshot snap.json --intent intent.json
```

---

## 3. 核心：价格模型（这次最重要的发现）

### 3.1 公式

用 `snap_live.json`（1.15.1，54 商品）的 `market.supply` / `market.demand` 反推，
得到与游戏**逐项一致**的分段函数（53 个商品误差为 0）：

```
d > s :  mult = 1 + 0.75 × (d/s − 1)
s > d :  mult = 1 − 0.75 × (s/d − 1)
s = d = 0 : mult = 1
价格 = 基础价 × clamp(mult, 0.25, 1.75)
```

两个细节：

* 分母是**较小的一方**——市场越薄，边际产量对价格的冲击越大；
* `gold` 不参与定价（固定 1.0×）；`d = 0, s > 0` 直接是地板价
  （实测 `explosives` 50 → 12.5）。

验证代码就是 `selftest.py::TestPriceAgainstSnapshot`：把公式代入快照的供需，
逐个商品和快照里的市价比，误差必须 < 1e-3。

### 3.2 它买到了什么

有了它，"加一级会怎样"可以**算**而不是猜：

* 纺织厂一级：衣服 45/周、布料 −40/周 → 衣服价格被自己压下去，布料价格被自己抬上去；
* 价格触底（0.25×）时继续扩产**必然亏**，这个判断不再依赖 LLM 的常识；
* 价格触顶（1.75×）意味着原料配给，候选会自动带 `risk`。

存档模式没有买卖单，模型退化为"按现价记账、不计价格反馈"，`has_orders=false` 会写进简报。

---

## 4. 候选的"五个数字"与打分

| 字段 | 含义 |
|---|---|
| `annual_value` | 年增值 = (产出 − 投入) × **推演后价格** × 52 |
| `points` / `weeks` | 建造点 / 要建几周（建造力折算） |
| `per_point` | 年增值 ÷ 建造点（跨建筑可比） |
| `risk` + `risk_notes` | 0~1：价格触底/触顶、基建饱和、激进派、吃地见顶 |
| `price_changes` | 这一单会把它自己的商品价格推高/压低多少 |
| `cash_cost` / `roi` | 现金成本（由预算槽 7 反推）与年回报率 |

```
score = 年增值 / 建造点 × 时间折现 × 产业链权重 × (1 − 风险)
时间折现 = 1 / (1 + 建造周数 / 52)
```

**建造部门不做估值。** 它没有商品产出，"每建造点年增值"对它没有意义，
硬编一个"影子价"只会让它长期霸榜。这里改成**纯成本口径**：

| 数字 | 含义 |
|---|---|
| `capacity_add` | 新建/升级一级带来多少建造力/周 |
| `points` / `weeks` | 建造点与工期 |
| `cash_cost` | 一次性现金 = 建造点 × 现金/点 |
| `weekly_upkeep` | 建成后每周消耗的投入商品（按市价） |
| `cost_per_capacity` | 周维护 ÷ 新增容量（换建造方式时比这个，不比分） |
| `affordable{ budget, cash, upkeep }` | 三笔成本是否都在可控范围 |

它**不进 `build_options` 的排名**，单独走 `capacity_options()` / `advise capacity`；
在 `build_candidates` 末尾以 `value_basis=capacity_cost` 附上，保证能被提议但不抢排序。
`upkeep` 的判定是"周维护 ≤ 周结余"——结余为负时标"在借钱养产能"。

**逐步贪心**（`plan`）不是先排序再截断：选一项 → 用推演后的价格重算剩余候选 → 再选。
所以第 2、3 座同类工厂的分数会自己掉下来（茶场 8.60万 → 8.33万），这才是"包"的正确算法。

**预算 = 队列深度**（`intent.budget_weeks`，默认 12 周），不是"本周施工量"：
它回答"愿意往队列里排多少建造点"。太小会把贵建筑（种植园 200 点）直接筛掉，
太大则会把国库排空——12 周是个折中，可用 `--weeks` 或意图覆盖。

---

## 5. 三个专门的防错

| 陷阱 | 现象 | 处理 |
|---|---|---|
| 部分均衡假收益 | "少产钢 → 钢价暴涨 → 账面更赚" | 减产时价格最多按 `1.25×` 原价计入（`PRICE_GAIN_CAP`），并提供 `test_delta_value_blocks_contraction_trick` |
| 建造部门霸榜 | 若给它编一个"影子价"，它会长期排第一 | **干脆不估值**：只算成本、只看可控性，不参与增值排序 |
| 现金幻觉 | 建造点不要钱，但预算槽 7 是真花钱 | `cash_per_point = 建造商品支出 / 建造力`，`plan` 受 `cash_budget` 约束（国库 + 半额剩余信贷） |

---

## 6. 简报长什么样

```
== 国势 ==
1836.1.1 save 大清(CHI)  人口 3.66亿  年GDP 8527万
国库 424万  周结余 +1.28万  建造力 22/周  本轮预算 264 点（12 周）

== 市场（按产业链）==
链        商品 短缺 过剩 最紧（比价）        缺口/周
纺织          5    2    0 clothes 1.31        1.2万
...
== 候选包（年增值已扣价格反馈；每点=年增值/建造点）==
#  州    建筑                级  点   周  年增值   每点  风险 链
1  910   textile_mill        1  600  27.3 4.89万   81.6 0.10 纺织
      · fabric 短缺
== 生产方式（无建造成本，优先做）==
...
== 信号 ==
[S3] transportation 价格触顶 — 补上游（运输能源 链）或进口
```

字符预算按优先级裁剪：先砍"变化对比"，再砍"生产方式"，最后才动候选表。
`claim` 说 2500 字符是经验值：LLM 在 2k 左右时数字翻错的概率明显低于 6k+。

---

## 7. 台账（准确率的长期来源）

```json
{"id":"e16536a75620","game_date":"...","kind":"build",
 "items":[{"state_id":0,"building":"building_construction_sector","levels":1,...}],
 "predicted_annual_value":283109.0,
 "baseline":{"gdp_annual":36525555.0,"prices":{"clothes":1.20,...}},
 "status":"open"}
```

隔几周对账：

```
实际年 GDP 增量 / 预测年增值 = bias
bias < 1  → 我们系统性高估（把 annual_value 乘回去校准）
```

`median_bias` 是给决策层的校准系数，也是"这类估算到底能不能信"的唯一证据。
**注意**：GDP 变动包含其它原因，bias 是**组合级**噪声信号，不是单栋建筑的实测。

---

## 8. 与 v3ai 的对接（已实现）

`v3ai/advisor_bridge.py` 是唯一的适配层：把 `Runtime` 的快照/地理/策略/队列
翻译成 `Advisor` 的输入，并把结果转回 v3ai 既有的键名。**计算层不可用时自动降级**
（只记一条 note，旧逻辑继续跑），所以接错也不会拖垮决策。

| 接入点 | 现在的行为 |
|---|---|
| `Runtime.brief()["advise"]` | `{text, signals, plan}`：一页纸 + 信号 + 方案前三项 |
| `build_candidates` | 由计算层生成；旧键名（`remaining`/`gdp_per_level`/`weeks_1`）保留，新增 `annual_value`/`per_point`/`risk`/`price_changes`/`roi` |
| `estimate_build` | 改用 `annual_value`（含价格反馈）；`gdp_delta` 仍是旧键名，`assumptions.price_feedback` 标明口径 |
| 审查层 | `DecisionContext.check_build` 委托 `CandidateEngine.check`，再补知识库条件（区域/沿海/州特质/法律/不可扩建/跨州唯一）——"能不能建"只有一个判断源 |
| 新工具 | `advise_brief` / `advise_plan` / `advise_signals` / `advise_capacity`（core 含 `advise_brief`） |
| 台账 | 执行成功的建造由 `Runtime._record_advise` 记账到 `state_dir/advise_ledger.jsonl` |
| 配置 | `advisor: {enabled, char_budget, top, plan_items}` |

仍属"下一步"（尚未做）：

1. **快层模式**：`Advisor.quick()` 已经很适合当 `agent.mode=fast`——规则全自动，
   只有 `signals.severity>=3` 才升级给 LLM；现在要人工调 `advise quick`。
2. **自动对账**：`Runtime` 目前只记账，不自动 `reconcile()`；可加"快照日期跨过 N 周"触发。
3. **PM 候选统一**：`ctx.pm_candidates` 仍走 `rank_pm_upgrades`（GDP 增量，不含价格反馈），
   计算层的 `pm_options` 已算好，只差按州拆分后替换。
4. **现金口径**：存档快照读不到预算槽 7，`cash_per_point` 为 `None`；live 快照才有效。

---

## 9. 已知限制（都写进了返回值，不藏在注释里）

* **工期**不含州建造效率 / 法令 / 临时修正，`weeks` 只用于量级判断；
* **部分均衡**：只算这一单对价格的影响，不重算全国其它建筑的反应；
* **非商品效果**（资质、行政力、死亡率、生活水平）只进 `warnings`，不进分数；
* **州静态地理**依赖 `state_meta.json`（默认 `v3ai/data/`）；存档换过后要重建
  （`python -m v3ai build_state_meta`），否则资源/耕地上限校验降级并给出 `warnings`；
* **现金/点**由预算槽 7 反推，会随税制与物价漂移；读不到预算时为 `None`；
* **PM** 不计换产线的时间损失与劳动力再配置；
* **建造力**不计州效率与临时修正（与 `v3ai` 同口径，约 5% 误差）；
* 存档快照没有买卖单 → **没有价格反馈**，只按现价记账。

---

## 10. 测试

```bat
python -m v3calc selftest -v
:: 34 项：价格公式与快照复现、产业链覆盖、商品向量、可建性、价格反馈方向、
::        贪心预算与去重、部分均衡陷阱、PM 候选、建造力、信号、简报字符预算、
::        台账记账对账、意图过滤
```

没有游戏目录 / 没有 `snap_live.json` 时相关用例会自动 skip，不会假绿。
