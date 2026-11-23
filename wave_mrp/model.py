"""波次领料 MRP 的不可变数据模型。

所有数量均为非负整数；物料的订货/投料单位已统一，不做单位转换。
仓点是物料节点的一部分：同一物料在不同仓点是相互独立的需求节点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MaterialKind(Enum):
    """物料类型：原料可直接领用；半成品必须按配方整组投料。"""

    RAW = "raw"
    SUBASSEMBLY = "subassembly"


@dataclass(frozen=True)
class NodeKey:
    """需求图上的节点 = 物料 + 仓点。"""

    material: str
    warehouse: str

    def __str__(self) -> str:  # 便于引用链阅读
        return f"{self.material}@{self.warehouse}"

    def _key(self) -> tuple[str, str]:
        return (self.warehouse, self.material)

    def __lt__(self, other: "NodeKey") -> bool:
        return self._key() < other._key()

    def __le__(self, other: "NodeKey") -> bool:
        return self._key() <= other._key()

    def __gt__(self, other: "NodeKey") -> bool:
        return self._key() > other._key()

    def __ge__(self, other: "NodeKey") -> bool:
        return self._key() >= other._key()


@dataclass(frozen=True)
class RecipeInput:
    """配方子料行：每投一组固定消耗 qty_per_lot 单位，取自指定仓点。"""

    material: str
    warehouse: str
    qty_per_lot: int


@dataclass(frozen=True)
class Recipe:
    """半成品配方：每投一组产出 output_per_lot 单位父件。

    配方项唯一：同一 (父物料, 仓点) 只允许一条配方，同一子料节点只允许一行。
    """

    output_material: str
    output_warehouse: str
    output_per_lot: int
    inputs: tuple[RecipeInput, ...]

    @property
    def output_node(self) -> NodeKey:
        return NodeKey(self.output_material, self.output_warehouse)


@dataclass(frozen=True)
class OrderLine:
    """冻结订单行：同一生产波次内对某仓点某物料的成品需求。"""

    order_id: str
    material: str
    warehouse: str
    qty: int

    @property
    def node(self) -> NodeKey:
        return NodeKey(self.material, self.warehouse)


@dataclass(frozen=True)
class Inventory:
    """某节点的库存：available 可抵用；isolated 已隔离，一律不得抵用。"""

    available: int = 0
    isolated: int = 0


class WaveValidationError(ValueError):
    """波次基础资料本身的结构性错误（区别于展开期发现的环/未知子项）。"""


@dataclass
class Wave:
    """一个冻结的生产波次：订单 + 物料主数据 + 配方 + 各仓点库存。"""

    name: str
    materials: dict[str, MaterialKind]
    recipes: dict[NodeKey, Recipe]
    orders: list[OrderLine]
    inventory: dict[NodeKey, Inventory]

    def validate(self) -> None:
        """校验静态资料；与可达性相关的环/未知子项不在此处判定。"""
        if not self.materials:
            raise WaveValidationError("物料主数据为空")
        order_ids: set[str] = set()
        for o in self.orders:
            if o.qty < 0:
                raise WaveValidationError(f"订单 {o.order_id} 数量为负")
            if o.material not in self.materials:
                raise WaveValidationError(f"订单 {o.order_id} 引用未知物料 {o.material}")
            if o.order_id in order_ids:
                raise WaveValidationError(f"订单号重复: {o.order_id}")
            order_ids.add(o.order_id)
        for node, inv in self.inventory.items():
            if inv.available < 0 or inv.isolated < 0:
                raise WaveValidationError(f"{node} 库存为负")
            if node.material not in self.materials:
                raise WaveValidationError(f"{node} 库存挂在未知物料上")
        for node, recipe in self.recipes.items():
            if node != recipe.output_node:
                raise WaveValidationError("配方索引键与产出节点不一致")
            mat = self.materials.get(recipe.output_material)
            if mat is None:
                raise WaveValidationError(f"配方产出未知物料 {recipe.output_material}")
            if mat is not MaterialKind.SUBASSEMBLY:
                raise WaveValidationError(f"原料 {recipe.output_material} 不允许有配方")
            if recipe.output_per_lot <= 0:
                raise WaveValidationError(f"{node} 每组产出必须为正整数")
            seen_inputs: set[NodeKey] = set()
            for inp in recipe.inputs:
                if inp.qty_per_lot <= 0:
                    raise WaveValidationError(f"{node} 子料耗用必须为正整数")
                child = NodeKey(inp.material, inp.warehouse)
                if child in seen_inputs:
                    raise WaveValidationError(f"{node} 配方行重复: {child}")
                seen_inputs.add(child)
                # 注意：子料是否存在于主数据属于“未知子项”，
                # 只在该配方被需求可达时判该仓点单据不可用，这里不预先否决。

    def stock_of(self, node: NodeKey) -> Inventory:
        return self.inventory.get(node, Inventory(0, 0))
