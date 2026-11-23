# 波次物料领料引擎（Wave MRP）

面向同一生产波次的多设备订单：从冻结订单、固定配方、各仓点库存构建领料单。
只做净需求汇总与半成品整组投料，**不做产能排程、不做替代配方优化、不实时扣库存**。

## 运行方式

```bash
python3 -I /workspace/run_tests.py   # 19 项验收（含手算小 DAG 与深层共享 DAG）
python3 -I /workspace/demo.py        # 可读的领料解释与库存更正对照
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

## 目录

```
wave_mrp/
  model.py    不可变数据模型（物料/配方/订单/库存/波次）
  result.py   领料行、单据、缺陷、修订等输出类型
  engine.py   展开、污染传播、拓扑净需求、整组取整、组件缓存
tests/
  acceptance_test.py  19 项验收（5 个场景）
run_tests.py  隔离模式测试入口
demo.py       手算解释演示
```
