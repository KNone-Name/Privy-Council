# Victoria 3 内存数据读取 —— 交接文档

> 面向：**计算层会话**（要用这些数据做经济/数值计算，不关心逆向过程）
> 版本：2026-11-12　|　验证状态：`python tools\v3live\verify_all.py` → **16/16 通过，8 项跳过**（跳过项需存档/快照，未随包附）

---

## 0. 一句话说明

游戏运行时，可以用 `tools\v3live` 这套工具从 `victoria3.exe` 进程内存里
**实时读出**国家的收支、建筑、人口、政治、市场等数据；**公式与系数**来自游戏文件。
行政力与建造力的计算结果与游戏界面**精确相等**，GDP 误差在 **±1.2%（总量）** 以内。

---

## 1. 使用前提

| 项 | 要求 |
|---|---|
| 游戏 | 必须**正在运行**（我们只有读权限，不写内存） |
| 启动方式 | 普通启动即可，不需要调试器 |
| Python | 已装 numpy（扫描用），无其他第三方依赖 |
| 进程名 | `victoria3.exe` |

**每次游戏重启后，先跑一次预热**（否则第一个命令要等 1~3 分钟扫内存）：

```
python tools\v3live\run.py prewarm
```

缓存键 = `PID + 模块基址 + 进程启动时间`，游戏重启即失效，这是为了正确性。

---

## 2. 两种调用方式

### 2.1 命令行（人看）

```
python tools\v3live\run.py <命令> [参数]
```

| 命令 | 输出 |
|---|---|
| `report` | 国家总览 |
| `tax` | 收支明细 + 税率 + 关税 + 投资池 |
| `capacity` | 行政力/建造力修正的完整溯源 |
| `gdp --formula` | GDP（含商品贡献表） |
| `states` / `pops` / `buildings` | 州 / 人口 / 建筑 |
| `market` / `prices` / `localprices` | 市场 / 价格 / 本地价 |
| `politics` | 利益集团（`--movements` 加政治运动） |
| `institutions` / `companies` | 机构 / 公司 |
| `laws` / `tech` / `lawmods` / `decrees` | 法律 / 科技 / 法令 |
| `statemods` / `timedmods` / `modifiers` | 州修正 / 限时修正 / 静态修正 |

### 2.2 Python 接口（**计算层建议用这个**）

```python
import os, sys
sys.path.insert(0, os.path.abspath("tools"))   # 在包根目录下运行
from v3live import cli, proc

p = proc.attach("victoria3.exe")
try:
    cid = cli._player_country_id(p)            # 玩家国家 id
    blds = cli._country_buildings(p, cid)      # 本国建筑
    # ... 计算 ...
finally:
    p.close()                                  # 一定要关
```

**必须 `p.close()`**，否则进程句柄泄漏。

---

## 3. 核心数据结构

### 3.1 `_country_buildings(p, cid)` 返回值

```python
[(类型名, 州id, 等级, 就业规模, 吞吐量, [生产方式名, ...]), ...]
#  'building_construction_sector', 914, 3, 3.0, 1.00, ['pm_wooden_buildings']
```

- **等级**：整数，建筑规模
- **就业规模**：浮点，满员时 ≈ 等级；未招满时更小
- **吞吐量**：浮点，通常 0.6~1.67，1.0 为基准
- **生产方式**：当前启用的 PM 名列表

### 3.2 内存对象的固定偏移

完整表见 [V3-字段表.json](docs\字段表.json)（16 类 / 154 字段）。
计算层最常用：

```
CCountry  步长 0x2638
  +0x0008  id (u32)
  +0x0AE4  市场 id (u32, 0xFFFFFFFF = 无市场)
  +0x12D8  CCountryBudget 内联（长 0x1360）
  +0x12F8  国库 (i64 定点)
  +0x1D18  每周投资池变化 (i64 定点)
  +0x2150  基础工资 (i64 定点)
  +0x2198  信用额度 (i64 定点)
  +0x21A0  贷款本金 (i64 定点)
  +0x2498  税收等级 (u8, 0~4)
  +0x1D50  支出数组 expenses[22] (i64 定点)
  +0x1E00  收入数组 income[16] (i64 定点，内存实为 18 格)

CState    步长 0x1EF8
  +0x0010  state_id (u32)
  +0x0A08  本地价 local_prices[54] (向量，i64 定点)
  +0x0EC8  所属国家 id (u32)

CBuilding 步长 0x290
  +0x0DC   州 id (u32)
  +0x0E0   等级 (i32)
  +0x0E8   类型指针（名字在 +0x18）
  +0x110   招聘率 (i64 定点)
  +0x120   就业规模 (i64 定点)
  +0x128   吞吐量 (i64 定点)
  +0x130   工资率 (i64 定点)
  +0x158   扣除储备后利润 (i64 定点)
  +0x160   扣除投资后利润 (i64 定点)
  +0x1E0   生产方式指针向量 {ptr, size, cap}

CMarket   按 market_id 找，必须校验价格表有效
  +0x0010  market_id (u32)
  +0x06F0  价格表 ptr / +0x06F8 数量（应为 54）

CInterestGroup 步长 0x338
  +0x0AC   国家 id
  +0x0F8   势力 clout (i32 定点)
  +0x270   集团名 (MSVC std::string)
```

### 3.3 单位约定（**最容易出错的地方**）

| 类型 | 存储 | 换算 |
|---|---|---|
| 金额、价格、工资 | i64 | **÷ 100000** |
| 吞吐量、就业规模、招聘率 | i64 | ÷ 100000 |
| 势力 clout | i32 | ÷ 100000，显示时再 ×100 成百分比 |
| 人口（`CPop`） | i64 | **原始人数，不除** |
| 等级、id | i32/u32 | 原值 |

> 反例提醒：`CBuilding+0x128` 曾是「金库 10 万英镑」，实际是**吞吐量 1.0**。
> 遇到「数值看起来像金额」时，先想一下会不会是 1.0 / 1.16 这类倍率。

---

## 4. 已验证的公式与系数

### 4.1 GDP

```
GDP = Σ(产出商品数量 × 价格) − Σ(投入商品数量 × 价格)
人均 GDP = GDP ÷ 总人口
```

- **数量**来自每个生产方式的 `goods_output_<good>_add` / `goods_input_<good>_add`
- 缩放：`workforce_scaled` → **等级 × 吞吐量**；`level_scaled` → 等级；`unscaled` → 1
- 价格：默认用**本国市场统一价**（`CMarket+0x6F0`），也可选各州本地价或商品基础价
- 年化 **×52**

**现成函数**：

```python
gdp_week, rows, market_id, n_prices = cli._compute_gdp(p, cid, blds)
gdp_year = gdp_week * 52
# rows = [(价值, 商品名, 净数量, 均价), ...] 已按绝对值降序
```

### 4.2 行政力 / 建造力

```
修正值 = Σ 生产方式贡献 + Σ 法令贡献 + Σ 法律/科技贡献 + 静态修正
生产方式贡献 = PM值 × 等级 × 吞吐量      （workforce_scaled）
```

**全局基线**（`static_modifiers/00_code_static_modifiers.txt` 的 `base_values`）：

```
行政力 +100    铸币基数 +500   权威 +100   影响力 +100
创新力 +50     创新上限 +50    税收能力 +100  基础设施 +3
```

**建造力的 GDP 修正**：

```
+10 × (0.2 + 0.8 × min(1, GDP ÷ 2000000))
```

**现成函数**：`python tools\v3live\run.py capacity` 给出逐条溯源。

### 4.3 税率表（`law_per_capita_based_taxation`，5 档）

| 税种 | 档1 | 档2 | 档3 | 档4 | 档5 |
|---|---|---|---|---|---|
| 消费税 | 15% | 20% | 25% | 30% | 35% |
| 人头税 | 40% | 55% | 70% | 85% | 100% |
| 土地税 | 20% | 27.5% | 35% | 42.5% | 50% |
| 所得税 | 5% | 7.5% | 10% | 12.5% | 15% |

### 4.4 其他

```
铸币收入 = (基数 500 + GDP部分 + 州级贡献 + 限时修正) × (1 + 铸币修正)
生产率   = (每周收入 − 材料费) ÷ 就业人数 × 52
金库上限 = 年化 GDP × 0.2
```

---

## 5. 精度声明（计算前务必读）

| 数据 | 精度 | 说明 |
|---|---|---|
| 行政力 | **精确** | 大清 11,156.5 ↔ 界面 11,156.5 |
| 建造力 | **精确** | 大清 22 ↔ 界面 22 |
| 州建造力效率 | **精确** | 圣彼得堡 10.4% ↔ 界面 10.4% |
| 收支明细 | **精确** | 每条都与界面逐项对得上 |
| 建筑数 | **精确** | 大清存档 302 = 内存 302 |
| 机构/公司 | **精确** | 217 条 / 8 家，字段全等 |
| GDP | **近似** | 大清 −2.5%、日本 −6.0%、俄罗斯 +18.3%，**总量 +1.2%** |
| 关税条目 | **近似** | 等级 99.86% 准确；「哪些商品算一条」约 86% 解释率 |

GDP 偏差方向不一致（−6% ~ +18%），说明不是缺统一系数，而是「用哪个市场的价格」
或「哪些建筑计入」的细节。**做趋势或相对比较可靠，做绝对值需注意误差。**

---

## 6. 六个必知陷阱

**1. 收支数组是「上周已结算」，界面是「下周预测」**
刚读档没走时间时，铸币收入只有基数 500，走一周才填完整值。
→ 计算时若不希望时序影响，用数组；若要「下周会怎样」，需要自己按公式预测。

**2. 同一个类常有「两代对象」**
旧副本仍在内存，字段是 0 或旧值。判别方式各不相同：
`CCountryLaw` 取同组地址最小者、`CPoliticalMovement` 看 `+0x15C > 0`、
`CMarket` 看价格表指针是否有效。

**3. 多继承类要扫全部 vtable**
`CDecree` 有 2 张表，只扫主表全世界只找到 1 条法令（实际 65 条）。
但多 vtable 时「簇内最小地址」未必是对象起点，要**用字段自校验**确定基址。

**4. 定点除法不要漏**
金额一律 ÷100000。人口是原始值不除。

**5. 首次运行慢**
每个类要扫一遍全内存（60~120 秒）。先 `prewarm` 一次。

**6. 游戏重启缓存失效**
发现命令突然变慢就是这个原因，重跑 `prewarm`。

---

## 7. 计算层常用代码片段

### 7.1 取某个国家的全部经济数据

```python
import os, sys; sys.path.insert(0, os.path.abspath("tools"))
from v3live import cli, proc

p = proc.attach("victoria3.exe")
try:
    cid = cli._player_country_id(p)
    blds = cli._country_buildings(p, cid)
    gdp_w, _rows, _mid, _np = cli._compute_gdp(p, cid, blds)
    laws = cli._laws_from_memory(p, cid)
    techs = cli._techs_from_memory(p, cid)
    decs = cli._country_decrees(p, cid)
    print("国家 %d：%d 栋建筑，GDP %.0f/年，%d 法律 %d 科技 %d 法令"
          % (cid, len(blds), gdp_w * 52, len(laws), len(techs), len(decs)))
finally:
    p.close()
```

### 7.2 读收支数组

```python
inc = [p.read_i64(addr + 0x1E00 + 8 * i) / 100000.0 for i in range(16)]
exp = [p.read_i64(addr + 0x1D50 + 8 * i) / 100000.0 for i in range(22)]
```

槽位含义见 `V3-字段表.json` 的 `income_index` / `expense_index`
（收入 13 个有效槽，支出 21 个已定，支出槽 12 未知）。

### 7.3 按生产方式汇总某修正（以行政力为例）

```python
pmdefs = cli._load_pms()          # PM 名 -> 各作用域修正（已带缓存）
total = 0.0
for tname, st, lv, sf, thr, pms in blds:
    for pm in pms:
        rec = pmdefs.get(pm, {})
        for scope in ("country_modifiers", "state_modifiers", "building_modifiers"):
            for scale, vals in rec.get(scope, {}).items():
                v = cli._fnum(vals.get("country_bureaucracy_add"))
                if v is None:
                    continue
                f = lv * thr if scale == "workforce_scaled" else (
                    lv if scale == "level_scaled" else 1.0)
                total += v * f
```

### 7.4 模拟税率变化的影响

税率只影响 `income` 数组的 1/2/3 槽（所得/人头/消费），且是**线性**的：
记录里已验证 `8410@20% → 14730.89@35% → 6312.08@15%`，比例吻合。
所以：

```
新税额 = 旧税额 × (新税率 ÷ 旧税率)
```

当前税率用 `run.py tax` 读（`CCountry+0x2498` 是档位，实际税率查法律表）。

---

## 8. 文件索引

| 路径 | 用途 |
|---|---|
| `tools\v3live\cli.py` | 主实现（17 个命令 + 所有 helper） |
| `tools\v3live\proc.py` | 进程内存读写 |
| `tools\v3live\discover.py` | 内存扫描 / 对象定位 |
| `tools\v3live\rtti.py` | 从 exe 解析类名与虚表 |
| `tools\v3live\gamedata.py` | Paradox 脚本解析 |
| `tools\v3live\verify_all.py` | 逐条验证（包内 16 项，缺数据自动 SKIP） |
| `docs\字段表.json` | 16 类 154 字段 + 收支索引 |
| `docs\逆向记录.md` | 逆向过程与所有踩坑 |
| `data\goods.json` | 54 种商品 index → 名称/基础价 |

---

## 9. 已知未解（不影响计算）

| 项 | 状态 |
|---|---|
| 支出槽 12 | 735 国从未非零 |
| 收入槽 6/13/14 | 从未非零 |
| 收入槽 16/17 | 内存有值（第 16 格大清 1772.80），含义未定 |
| `average_productivity` | 公式已知，但内存不存，只能算 |
| GDP 精确口径 | 总量误差 1.2%，单国 −6%~+18% |
| 贸易路线 | 存档无独立块 |

---

## 10. 交接检查清单

交给计算层前，建议先跑：

```
python tools\v3live\run.py prewarm         # 建缓存（游戏重启后必做）
python tools\v3live\verify_all.py          # 24 项全部 PASS 才继续
python tools\v3live\run.py capacity        # 确认能读到玩家国家数据
python tools\v3live\run.py gdp --formula   # 确认 GDP 能算
```

四条都正常，就可以开始做计算了。

---

## 11. 本包包含 / 未包含

**已包含（可直接运行）**

| 路径 | 内容 |
|---|---|
| `tools\v3live\*.py` | 运行所需的 11 个模块 |
| `tools\ida\*.py` | IDA 脚本 8 个（静态分析用，运行不需要） |
| `docs\逆向记录.md` | 逆向过程与踩坑 |
| `docs\字段表.json` | 16 类 154 字段 |
| `data\goods.json` | 54 种商品表 |
| `data\save_inst.json` | 机构 217 / 公司 8 的存档对照 |

**未包含（体积大，随工作副本单独保存）**

| 文件 | 大小 | 用途 |
|---|---|---|
| `data\saves\sb.txt` | 76 MB | 存档，`verify_all` 的 B 层用 |
| `data\ccountry_snap.bin` | 6.9 MB | 内存快照，C 层用 |
| `data\ig_snap.bin` | 3.9 MB | 同上 |
| `data\mv_snap.bin` | 1.7 MB | 同上 |
| `data\inst_snap.bin` / `comp_snap.bin` | 140 KB | 同上 |

缺这些文件时 `verify_all.py` 会把对应项标成 **SKIP** 而不是 FAIL，
所以本包默认能跑 **16 项（A 文件层 + D 文档层）全部通过**。

**运行前必做**：游戏要开着；每次游戏重启后先跑一次

```
python tools\v3live\run.py prewarm
```
