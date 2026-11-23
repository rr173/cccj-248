# 波次物料领料引擎（Wave MRP）

面向同一生产波次的多设备订单：从冻结订单、固定配方、各仓点库存构建领料单。
只做净需求汇总与半成品整组投料，**不做产能排程、不做替代配方优化、不实时扣库存**。

## 运行方式

```bash
python3 -I /workspace/run_tests.py   # 54 项验收（领料 19 项 + 来料追溯 35 项）
python3 -I /workspace/demo.py        # 可读的领料解释与库存更正对照
python3 -I /workspace/trace_demo.py  # 来料抽检：隔离/召回清单与可回查依据
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

## 目录

```
wave_mrp/
  model.py    不可变数据模型（物料/配方/订单/库存/波次）
  result.py   领料行、单据、缺陷、修订等输出类型
  engine.py   展开、污染传播、拓扑净需求、整组取整、组件缓存
  trace_model.py    追溯输入：区间、四类过账载荷、报送、查询
  trace_result.py   追溯输出：隔离/召回行、三类依据、拒绝结论
  trace_engine.py   报送筛选、实物账重放校验、受影响传播与清单组装
tests/
  acceptance_test.py        19 项领料验收（5 个场景）
  trace_acceptance_test.py  35 项追溯验收（端到端/传播/版本/拒绝/规模）
run_tests.py      隔离模式测试入口（两套验收一起跑）
demo.py           领料手算解释演示
trace_demo.py     追溯手算演示（含版本替换、撤销、截止号外业务）
```
