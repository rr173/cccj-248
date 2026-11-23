"""领料结果的不可变输出模型。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import NodeKey, OrderLine


@dataclass(frozen=True)
class Contribution:
    """一项直接需求的来源。

    source_kind:
      - "order"  来源是冻结订单（父项批件身份即订单号）
      - "parent" 来源是父项半成品的整组投料（父项批件身份见 batch_ref）
    batch_ref 对父项来源指向其领料批次（父项批件），订单来源指向订单号。
    qty 是该来源对本节点贡献的直接需求量（已按真实组数 × 耗用计算）。
    """

    source_kind: str
    batch_ref: str
    qty: int


@dataclass(frozen=True)
class PickingLine:
    """单个 (物料, 仓点) 节点的领料解释行，所有计数均为聚合整数。"""

    node: NodeKey
    kind: str                      # raw / subassembly
    gross_demand: int              # 订单 + 全部父项的直接需求汇总
    contributions: tuple[Contribution, ...]
    available_stock: int           # 该仓点可用库存
    isolated_stock: int            # 该仓点隔离库存（永不抵用）
    netted_qty: int                # 实际抵用掉的可用库存
    available_remainder: int       # 抵用后剩余的可用库存
    isolated_remainder: int        # 原样保留的隔离库存
    net_shortage: int              # 抵库存后的净短缺
    lots_to_issue: int             # 半成品要投的真实组数；原料为 0
    output_per_lot: int | None     # 半成品每组产出
    lot_output_qty: int | None     # 真实组数的总产出（≥ 净短缺）
    lot_remainder: int             # 整组多产出、留作批次尾数的数量
    issue_qty: int                 # 原料实际领用数量（= 净短缺）；半成品为 0

    @property
    def is_actionable(self) -> bool:
        return self.lots_to_issue > 0 or self.issue_qty > 0


@dataclass(frozen=True)
class ShortageSource:
    """原料缺料的一条形成路径：本波次在哪个领料项上形成了多少需求。"""

    source_node: NodeKey
    batch_ref: str                 # 订单号或父项批件
    required_qty: int              # 该来源对该缺料原料的直接需求量


@dataclass(frozen=True)
class RawShortage:
    """原料净缺料交付：数量 + 本波次全部形成需求的领料项。"""

    node: NodeKey
    shortage_qty: int
    sources: tuple[ShortageSource, ...]


@dataclass(frozen=True)
class Defect:
    """环或未知子项诊断，带从需求根出发的引用链。"""

    kind: str                      # cycle / unknown_child
    warehouse: str                 # 被判不可用的仓点
    chain: tuple[NodeKey, ...]
    detail: str


@dataclass(frozen=True)
class UnreachableRecipeNote:
    """与本波次无关的配方资料诊断（仅提示，不否决任何正常领料）。"""

    recipe_node: NodeKey
    note: str


@dataclass(frozen=True)
class WarehouseDocument:
    """一个仓点的领料整单；unavailable 时 picking_lines 为空。"""

    warehouse: str
    available: bool
    defects: tuple[Defect, ...] = ()
    picking_lines: tuple[PickingLine, ...] = ()
    raw_shortages: tuple[RawShortage, ...] = ()
    reached_nodes: tuple[NodeKey, ...] = ()   # 该仓点被需求到达的节点

    def line(self, material: str, warehouse: str) -> PickingLine:
        target = NodeKey(material, warehouse)
        return next(line for line in self.picking_lines if line.node == target)


@dataclass(frozen=True)
class PickingList:
    """整张波次领料单（按仓点拆分的独立单据集合）。"""

    wave_name: str
    revision_label: str
    documents: dict[str, WarehouseDocument]
    unreachable_notes: tuple[UnreachableRecipeNote, ...] = ()
    # 引擎统计：供验收“共享件不反复展开”使用
    nodes_expanded: int = 0       # 结构展开访问的去重节点数
    nodes_solved: int = 0         # 净需求求解的去重节点数

    def all_lines(self) -> list[PickingLine]:
        return [line for doc in self.documents.values() for line in doc.picking_lines]

    def line(self, material: str, warehouse: str) -> PickingLine:
        return next(
            line
            for doc in self.documents.values()
            for line in doc.picking_lines
            if line.node == NodeKey(material, warehouse)
        )


@dataclass(frozen=True)
class OrderOverride:
    """修订：整单替换某订单行的数量（不允许混用修订前后的批次）。"""

    order_id: str
    new_qty: int


@dataclass(frozen=True)
class InventoryOverride:
    """修订：整单替换某节点的可用/隔离库存。"""

    node: NodeKey
    new_available: int
    new_isolated: int = 0


@dataclass(frozen=True)
class Revision:
    """一次修订的完整变更包，必须一次提交。"""

    label: str
    order_overrides: tuple[OrderOverride, ...] = ()
    inventory_overrides: tuple[InventoryOverride, ...] = ()
