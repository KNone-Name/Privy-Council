# V3 项目优化计划 (2026-11-21)

> 项目：**枢密院（Privy Council）**

目标：在不破坏 52 项离线回归测试 + 34 项 v3calc selftest 的前提下，提升计算层（v3calc）决策模拟吞吐与 Mod（v3panel）稳定性。

工作区根：D:\Game\ai\V3
发布版：Project/V3-release-20261120/（v3bridge 0.3 / build 158 / Victoria 3 1.15.1）
Mod 版：Project/v3panel-release/

---

## A. 计算层 (v3calc)

架构：v3live（内存快照）→ Snapshot + GameData → 纯函数计算（GDP/PM/市场/建筑/决策 advisor）
入口：v3.py read → v3calc → v3ai。全部标准库，无第三方依赖。
测试：python -m unittest discover -s tests -v（52 项）；python -m v3calc selftest（34 项）。

### A1. candidates.py + market.py —— 决策模拟热路径 (P0)

现状：
- CandidateBuilder.plan() 贪心循环每选一个包就调用 work.shifted(deltas)：深拷贝整个 PriceModel（全部订单 + 重算 positions），N 步计划 = N 次全量拷贝。
- _mkt_positions() 每次都被全量重算；候选间共享同一基础市场状态，positions 被重复计算。
- to_dict() 在搜索过程中产生大量临时嵌套 dict。

建议方案（由执行 agent 选定并实现）：
1. PriceModel.apply_delta(deltas)：增量更新订单 + 按需懒算 positions（缓存 key = 订单哈希），避免全量拷贝。
2. CandidateBuilder 持有一个可回滚的 market 实例（栈式撤销），或按"选择序列"回放而非逐次深拷贝。
3. positions 计算加 memoization（key = (supply_orders_id, demand_orders_id)）。
4. 对频繁构造的小对象加 __slots__。

验收：决策模拟吞吐提升；52 测试 + 34 selftest 全通过；输出基准对比。

### A2. gamedata.py —— 内存与解析 (P1)

现状：Obj/GameData 是通用 dict 容器，无 __slots__，字符串键未归一。
建议：Obj 加 __slots__；归一化高频字符串键（name/cost/buildtime/output 等）；对 Good/BuildingDef/PM 预计算并缓存常用派生字段。

---

## B. Mod 层 (v3panel-release)

### B1. panel_server.py —— 外部 LLM 服务热路径 (P0)

现状（233 行，独立 TCP 服务 127.0.0.1:8766，JSON-lines，调用 OpenAI 兼容 /v1/chat/completions）：
- load_ctx() 每次 reply() 都读 snap.json + 解析 → 加 TTL（mtime 探测，5s）缓存。
- log() 每行打开文件句柄 → 改缓冲写入。
- Backend.reply() 每次重建 messages list + payload → 预编译 system 消息模板。
- urllib 同步阻塞 → 可保持（简单优先），但加 connect/read timeout 与重试。

### B2. information_panel_bar.gui (830 行) —— UI 清理 (P1)

现状：官方基线 790 行；差异 = 我们自有的 ~40 行增量 + BOM/CRLF 转换。
检查项：末尾 sidebar_button_size / sidebar_button_size_small 模板是否与基线重复 → 去重；确认 BOM 处理方式一致。

### B3. 版本与描述符 (P1)

- v3ai_panel.mod：version 0.1.0 → 0.2.0；supported_version="1.15.*" 保持不变。
- mod/v3ai_panel/descriptor.mod：检查 Steam Workshop 元数据一致性。

### B4. 快照上下文时效 (B1 联动)

- load_ctx() 的 snapshot_age_s 已做过期警告（>600s）；缓存加 TTL 后确认 snap.json 生成频率与 game_version 字段保留。

### B5. BOM / 编码审计 (P2)

- 各文件头均有 UTF-8 BOM。Victoria 3 的 YAMLLoader/脚本解析器通常接受 BOM，但为稳妥：确认本地化 .yml 建议用无 BOM UTF-8，其余 .gui/.txt 保留与基线一致的处理方式。

---

## 任务分工

| 子代理 | 范围 | 文件 |
|--------|------|------|
| calc-core | A1 + A2（热路径 + 内存） | candidates.py, market.py, gamedata.py |
| calc-gdp | A3（增量 GDP/建筑） | gdp.py, construction.py |
| mod-side | B1-B5（Mod 优化与更新适配） | panel_server.py, information_panel_bar.gui, *.mod, 编码审计 |

## 约束
- 全部改动不得破坏 52 项回归测试 + 34 项 selftest（提交前跑一遍）。
- 保持标准库依赖（requirements.txt：无第三方包）。
- 增量计算必须与基线口径一致（数值回归通过）。
