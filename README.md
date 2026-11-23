# 波次物料领料引擎（Wave MRP）

面向同一生产波次的多设备订单：从冻结订单、固定配方、各仓点库存构建领料单。
只做净需求汇总与半成品整组投料，**不做产能排程、不做替代配方优化、不实时扣库存**。

## 运行方式

```bash
python3 -I /workspace/run_tests.py   # 102 项验收（领料 19 + 追溯 37 + 成本结账 23 + 审包 23）
python3 -I /workspace/demo.py        # 可读的领料解释与库存更正对照
python3 -I /workspace/trace_demo.py  # 来料抽检：隔离/召回清单与可回查依据
python3 -I /workspace/cost_demo.py   # 存货成本结账：期初/期内/期末与逐批核对
python3 -I /workspace/amendment_demo.py  # 更正包审包：补报、逐包接纳结果与发布截止号
```

仅使用 Python 标准库；全部计数为精确整数（无浮点、无逐实体单位拆行）。

## 核心规则与实现位置

| 需求条款 | 实现 |
| --- | --- |
| 需求按 (物料, 仓点) 汇总，仓点相互独立 | `NodeKey = 物料+仓点`；按仓点产出独立 `WarehouseDocument` |
| 可用库存抵用、隔离库存不得抵用 | `_solve_component` 中 `netted=min(available, gross)`，`isolated` 原样留在 `isolated_remainder` |
| 半成品只能整组投料，多出来按批次记期末 | 先汇总毛需求并抵库存，再 `ceil(净缺/每组产出)`；`lot_remainder` 为批次尾数，不摊平回成品 |
| 不能按每个成品独立取整后相加 | 父项贡献先聚合到同一节点，取整只发生在聚合后的净缺上（验收 `test_shared_subassembly_net_before_round` 对照错误做法 3 组 vs 正确 1 组） |
| 真实组数 × 固定耗用形成子料需求 | 子料 `gross += lots * qty_per_lot`，贡献以父项批件 `BATCH|<修订>|<节点>` 引用 |
| 原料缺料只交付数量与形成项，不擅自向上游投料 | `RawShortage` + `ShortageSource`（订单或父项批件），不产生任何额外投料 |
| 每项可解释：直接需求/抵库存/组数或缺料/余数 | `PickingLine` 含 gross、contributions、netted、available/isolated remainder、lots、lot_remainder、issue_qty |
| 可达环 / 未知子项 / 缺配方 → 该仓点整单不可用，附引用链 | 迭代 DFS 记 `cycle/unknown_child/missing_recipe`，污染沿反向边传播；`WarehouseDocument.available=False` |
| 其他仓点不受影响 | 污染只标记其所在仓点；验收 `test_other_warehouse_unaffected` |
| 与波次无关的配方（含无关环）仅诊断 | `_unreachable_notes`，不否决任何领料 |
| 修订一次提交、不混用前后批 | `Revision` + `_effective_wave` 生成不可变快照；基线结果不被后续修订改变 |
| 更正不重算无关连通组件，校验过的配方可复用 | 弱连通组件签名缓存；`cache_hits/cache_misses` 可观测 |
| 共享件不反复展开，不生成指数路径 | 去重节点图 + 拓扑序单次求解；深 DAG 验收：3^14 条路径仅 43 个节点、每节点一行 |

## 求解流程（`engine.py`）

1. `_expand_graph`：从订单根迭代 DFS 展开需求图（显式栈，避免深 DAG 递归），
   同时记录环引用链、未知子项、缺配方；
2. `_propagate_taint`：缺陷节点及其全部需求祖先标污染，含污染节点的仓点整单不可用；
3. `_components`：无向弱连通组件，作为修订隔离与缓存边界；
4. `_solve_component`：干净组件做 Kahn 拓扑（父先子后），
   毛需求汇总 → 可用库存抵用 → 半成品向上整组 → 真实组数下推子料；
   原料净缺直接进缺料清单；
5. `_component_signature`：组件身份 + 边/组产出/订单/库存签名，
   修订后签名不变的组件直接复用历史结果。

## 输出模型（`result.py`）

- `PickingList`：按仓点的单据集合 + 无关配方诊断 + 展开/求解节点统计；
- `WarehouseDocument`：仓点整单（可用/不可用、缺陷链、领料行、缺料、到达节点）；
- `PickingLine`：单节点的完整备料解释（每个物料+仓点仅一行聚合数据）；
- `RawShortage`：缺料数量与本波次全部形成需求的领料项；
- `Defect`：缺陷类型、仓点、从需求方向给出的节点引用链。

## 来料抽检追溯（`trace_*` 模块）

质量员圈出某收货批次内的若干半开区间 `[start,end)`（可重叠），引擎按固定的
**业务截止号**与**报送截止号**重放过账流水，回答：哪些货还在库需隔离、哪些已
发给客户需召回。实际流向只以过账记录为准，不读领料计划或配方。

### 接口（`wave_mrp/trace_model.py`、`trace_result.py`）

```python
from wave_mrp import (
    EventReport, Interval, ReceiptPayload, TransferPayload, TransferSegment,
    ProductionPayload, Segment, ShipmentPayload, TraceQuery,
    LotTraceabilityEngine, build_traceability_list,
)

reports = [EventReport(report_seq, event_id, business_seq, payload), ...]
query = TraceQuery(
    receipt_batch="B0",                       # 必须是截止账内可见的收货批
    marked_intervals=(Interval(2, 5), Interval(4, 6)),  # 重叠圈选，自动合并
    business_cutoff=6,                        # 含端点
    report_cutoff=350,                        # 含端点
)
result: TraceabilityResult = build_traceability_list(reports, query)
```

- `payload` 四选一：`ReceiptPayload` / `TransferPayload` / `ProductionPayload`
  / `ShipmentPayload`；同一 `event_id` 再报一条 `payload=None` 即撤销。
- 输出 `TraceabilityResult`：
  - `refused=False`：`isolation`（按仓点/物料/当前批次）、`recall`
    （按收货方/发运事件/发货批次），每行带排序不重叠的 `intervals` 与
    `quantity`，同一位置只计一次、无零数量行；
  - 依据三件套：`receipt_evidence`（命中的原收货区间）、
    `transfer_evidence`（每次命中调拨的源批区间↔目标区间）、
    `production_evidence`（受影响产出批 + 实际命中的投入片段）；
  - `adopted_reports`：账内每个事件实际采用的报送序号（撤销为 `None`），
    让依据能区分报送版本；
  - `refused=True`：清单与依据为空，`contradictions` 给出全部拒绝来源
    （原因枚举 `RefusalReason` + 事件 ID + 说明），不出具部分清单。

### 规则与实现位置（`trace_engine.py`）

| 需求条款 | 实现 |
| --- | --- |
| 双截止含端点、最后可见报送为准、撤销不发生 | `_select_reports`：截止号外记录不入选也不参与校验，同事件取最大报送号 |
| 固定截止号重查不被后来更正改变 | 每次查询独立筛选，旧版本/截止号外记录完全不进入实物账 |
| 批内位置从 0 起、不随出库重编号 | 全流程只操作半开 `Interval`，不维护逐件编号 |
| 调拨按单序拼新批、仅命中片段继续受影响 | `_propagate` 调拨分支：偏移量 = 此前各片段长度和，交集映射到目标坐标 |
| 生产任一投入位置命中则整批产出受影响 | 生产分支：命中即把产出 `[0, 实报产量)` 整段置为受影响，产量取实报值 |
| 已被生产消耗的源片段不再计为在库 | 隔离 = `subtract_lists(受影响区间, 已耗区间)`，空结果不出行 |
| 只能从更早且有余额的批次取货、同位置不可二次耗用、批号不复用 | `_replay`：缺批/超界/重复耗用/重复建批/混料混仓全部记矛盾 |
| 来源不可见或区间超界即拒绝 | 主入口先验来源收货批与圈选区间，再重放 |
| 批量可超万亿、不逐件分配内存、不展开全路径 | 区间列表（排序不重叠）+ 一跳依据；区间数只随过账次数增长 |

中间批可以出现在依据里，但只要受影响位置已被后续业务消耗，就不会出现在
隔离或召回行（验收 `test_intermediate_batch_not_counted_as_pending_goods`）。

## 存货成本结账（`cost_*` 模块）

在同一份实物账（收货/调拨/生产/发运过账）上贴金额，结出**期初 / 期内增减 /
期末**三张可核对的成本表。数量口径、版本规则和矛盾判定与追溯模块完全一致：
以现有实物账的批次有效版本、批内位置和耗用规则为准，不读领料计划，也不需要
任何抽检范围。不做税费、跨币种或总账入账。

### 接口（`wave_mrp/cost_model.py`、`cost_result.py`）

```python
from wave_mrp import (
    ReceiptCost, ProductionCost, CostTable, ClosingQuery,
    CostClosingEngine, build_closing_statement, close_inventory_costs,
)

reports = [EventReport(report_seq, event_id, business_seq, payload), ...]
cost_table = CostTable(
    receipt_costs=(ReceiptCost("R1", 100, 2), ...),      # (事件 ID, 报送序号) -> 整批入账金额（分）
    production_costs=(ProductionCost("P1", 120, 6), ...) # (事件 ID, 报送序号) -> 加工金额（分）
)
query = ClosingQuery(
    opening_business_cutoff=2,   # 含端点；之前的业务进期初
    closing_business_cutoff=6,   # 含端点；之前的业务进期末
    report_cutoff=500,           # 整次结账统一的报送截止号（含端点）
)
statement: ClosingStatement = build_closing_statement(reports, cost_table, query)
# 同义入口：close_inventory_costs(...)；或复用引擎：
# CostClosingEngine(reports).build_closing_statement(cost_table, query)
```

- `refused=False` 时读取结账单：
  - `opening` / `closing`：`StockBalance` 行，按仓点/物料/批次给出
    `quantity` 与 `amount`（分）；**有数量但金额为零的行也会列出**；
  - `movements`：期内按业务顺序的 `MovementLine`，每笔按受影响仓点/物料/
    批次给出带符号数量与金额，并注明 `event_id`、**采用的 `report_seq`**；
    发运减记的负数金额就是这笔发出成本；一次调拨的多行共享
    `transfer_group`，组内金额之和为零；一次生产的净增金额等于加工金额；
  - `adopted_reports`：本次结账采用的事件→报送版本清单。
- `refused=True` 时不出部分结账单：`opening`/`movements`/`closing` 全为空，
  `contradictions` 给出全部拒绝来源（`RefusalReason` + `event_id` +
  `report_seq` + 说明）。
- 核对关系：对每个 (仓点, 物料, 批次)，`期初 + 期内 = 期末`（数量与金额
  各自成立），可直接用 `statement.balance(仓点, 物料, 批次)` 逐行检查。

### 规则与实现位置（`cost_engine.py`）

| 需求条款 | 实现 |
| --- | --- |
| 只认现有实物账的有效版本，旧版不借价 | 复用 `LotTraceabilityEngine.select_reports`：先按业务/报送双截止筛选，再取每事件最后可见版本 |
| 报送截止号整次结账统一使用 | `ClosingQuery.report_cutoff` 同时作用于期初与期末；两个业务截止号可相同，期末早于期初即 `invalid_closing_range` |
| 每笔生效收货唯一整批入账金额、每笔生效生产唯一加工金额 | `_validate_costs`：按 `(event_id, report_seq)` 精确匹配采用版本 |
| 零元必须显式登记、缺价不补零、不借旧价 | 缺记录 → `missing_cost`；同版本多条 → `duplicate_cost`；非非负整数分 → `invalid_cost` |
| 未采用的成本记录完全不参与校验 | 只遍历本次生效事件；旧版本/截止号外/撤销事件/另一类记录一律忽略 |
| 收货按入账金额建成本，生产按耗用片段成本 + 加工金额建产出批成本 | 收货/生产分支分别建 `_LotBasis`，生产 `input_amount = -Σ耗用金额` 再加加工金额 |
| 拆单不移动余分：`[a,b)` 值 `floor(C*b/Q)-floor(C*a/Q)` | `_LotBasis.value`；调拨/发运/生产都按账上实际位置计价 |
| 调拨保留各位置原金额、目标按单据顺序对应 | 目标批成本基为 `_ConcatBasis`/`_SliceBasis`；不从目标总金额重新摊平 |
| 整批转运不遍历此前所有拼接段 | 整批取 `basis.total`（O(1)）；`_ConcatBasis` 预存起点与前缀和，任意子区间 O(log 段数) |
| 批量超万亿不逐件展开 | 全流程仅用半开区间端点与整数分 |
| 实物账有矛盾则整单拒绝 | 复用 `replay_physical_ledger`，与追溯接口同一套矛盾与版本规则 |

`cost_demo.py` 给出期初/期内/期末的完整手算对照，包括把 `B0[1,2)` 接在
`B0[2,3)` 之前调拨后目标批“第 1 件仍值 1 分、第 2 件值 0 分”的例子。

## 更正包审包（`amendment_*` 模块）

更正一张原始单据常常要同时改另一张（两张发运单互换领用货段时，单改
任何一张都会重复占货）。审包接口把按收单顺序到达的**更正包**裁定成一份
可发布的补报：每包有唯一单号与一组按单内顺序排列的更正，包内更正
**同时生效**。更正只替换既有事件的报送内容（或撤销），不新建事件身份、
不改业务序号；版本校验以制单所见报送序号与当前现版比对。

### 接口（`wave_mrp/amendment_model.py`、`amendment_result.py`）

```python
from wave_mrp import (
    Correction, CorrectionPackage, CorrectionAcceptanceEngine,
    adjudicate_correction_packages,
)

# 每条更正：既有事件 ID、制单时看到的报送序号、完整替换载荷（None=撤销）
packages = [
    CorrectionPackage("PKG-2", (
        Correction("S1", observed_report_seq=110,
                  replacement=ShipmentPayload("CUST-A", (Segment("B0", Interval(3, 6)),))),
        Correction("S2", observed_report_seq=120,
                  replacement=ShipmentPayload("CUST-B", (Segment("B0", Interval(0, 3)),))),
    )),
]
result = adjudicate_correction_packages(history_reports, packages)
# 复用引擎可连续受理多批，后续包以前面已接纳包生效后的现版为准：
# engine = CorrectionAcceptanceEngine(history_reports)
# result = engine.adjudicate(packages)
```

- 历史账本身有矛盾时 `history_refused=True`：不出包级结果与补报，
  `history_contradictions` 指出原因（复用追溯模块同一套矛盾口径）。
- 否则逐包返回 `package_results`（与输入同序）：
  - 接纳：`accepted=True`，`new_reports` 是本包按单内顺序取得的连续新报送；
  - 拒绝：`accepted=False`，`contradictions` 给出所涉事件与具体原因
    （结构原因或定位到事件/批次的实物矛盾，不要求列完所有矛盾）。
- `new_reports` 是全部已接纳包的新增 `EventReport`；`publish_cutoff`
  是发布截止号——新报送号从历史最大报送号之后连续分配，即使替换内容
  相同也产生新报送；拒绝包不占号，故截止号不会落在只生效一部分的包上。
  无新增时截止号仍为历史最大报送号，空历史以 0 为起点。
- 原始列表不改动；把 `new_reports` 追加到历史后，原有追溯/成本接口按
  `publish_cutoff` 读到新账，用原截止号仍读到旧账。

### 规则与实现位置（`amendment_engine.py`）

| 需求条款 | 实现 |
| --- | --- |
| 历史全账取现版并核对实物，有矛盾即不发布 | 构造时复用 `select_reports` + `replay_physical_ledger` 做一次全量校验，缓存现版索引 |
| 空包/同包重复修改/事件不存在/制单版本过期，整包拒绝 | `_adjudicate_one` 结构校验：`EMPTY_CORRECTION_PACKAGE`/`DUPLICATE_CORRECTION_TARGET`/`UNKNOWN_CORRECTION_EVENT`/`STALE_CORRECTION_VERSION` |
| 版本与前面已接纳包生效后的现版比较 | 接纳即更新 `_current_seq`；撤销也是现版，恢复须引用撤销报送序号 |
| 包内更正同时生效，替换后整份账符合实物规则才接纳 | 被改事件组成**关联子账**整体重放，沿用 `replay_physical_ledger` 全套实物规则（重复占货、超界、缺批、混料等） |
| 旧单同样受约束（调拨缩短新批、旧发运引用被删位置则拒绝） | 子账闭包包含未改动的关联事件，重放自然报 `OUT_OF_RANGE_SEGMENT` 等矛盾 |
| 不重读历史、不复制整份账、不重验无关货批 | 仅维护事件↔批次二部图关系索引；每包只重放关联闭包子账（验收以 monkeypatch 计数：4000 条无关流水一条不入子账） |
| 关联覆盖撤下去的旧关系与加进来的新关系 | 被改事件用“旧现版载荷 + 替换载荷”双重播种，再沿二部图求闭包 |
| 撤销建批事件后仍被下游引用要能发现 | 旧关系播种保证下游引用事件进入闭包，重放报 `MISSING_UPSTREAM_BATCH` |
| 货段按批内区间理解，不按件展开 | 子账重放只操作半开 `Interval`；万亿批量验收仅用端点 |
| 拒绝包不改任何现版、占用或报送号 | 拒绝在更新索引前返回；接纳才发号并更新 `_current_seq`/关系索引 |

## 目录

```
wave_mrp/
  model.py    不可变数据模型（物料/配方/订单/库存/波次）
  result.py   领料行、单据、缺陷、修订等输出类型
  engine.py   展开、污染传播、拓扑净需求、整组取整、组件缓存
  trace_model.py    追溯输入：区间、四类过账载荷、报送、查询
  trace_result.py   追溯输出：隔离/召回行、三类依据、拒绝结论
  trace_engine.py   报送筛选、实物账重放校验、受影响传播与清单组装
  cost_model.py     成本结账输入：收货/生产成本记录、成本表、结账口径
  cost_result.py    成本结账输出：期初/期末余额、期内增减、拒绝结论
  cost_engine.py    成本记录校验、批内位置计价、期初期末快照与结账单
  amendment_model.py    审包输入：更正（事件 ID+制单所见序号+替换/撤销）、更正包
  amendment_result.py   审包输出：逐包接纳结果、补报、发布截止号
  amendment_engine.py   现版/关系增量索引、关联闭包子账重放、连续发号
tests/
  acceptance_test.py        19 项领料验收（5 个场景）
  trace_acceptance_test.py  37 项追溯验收（端到端/传播/版本/拒绝/规模）
  cost_acceptance_test.py   23 项成本结账验收（余分/拆单/生产/版本/拒绝/规模）
  amendment_acceptance_test.py  23 项审包验收（接纳/结构拒绝/实物规则/闭包增量/规模）
run_tests.py      隔离模式测试入口（四套验收一起跑）
demo.py           领料手算解释演示
trace_demo.py     追溯手算演示（含版本替换、撤销、截止号外业务）
cost_demo.py      成本结账手算演示（期初/期内/期末与逐批核对）
amendment_demo.py 审包手算演示（互换货段、版本过期、新旧截止号两账）
```
