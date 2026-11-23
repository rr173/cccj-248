"""存货成本结账的输出模型（可核对的结账单）。"""

from __future__ import annotations

from dataclasses import dataclass

from .trace_result import LedgerContradiction


@dataclass(frozen=True)
class StockBalance:
    """某仓点/物料/批次在某时点的在库数量与金额。

    数量与金额分开列示；数量为正而金额为零的行必须保留。同一批次内部
    不同位置可能有不同单价（收货批次含余分），因此批次余额以金额合计
    表示，不另给“平均单价”。
    """

    warehouse: str
    material: str
    batch: str
    quantity: int
    amount: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.warehouse, self.material, self.batch)

    @property
    def is_empty(self) -> bool:
        return self.quantity == 0 and self.amount == 0


@dataclass(frozen=True)
class MovementLine:
    """期内一笔库存增减，按受影响的仓点/物料/批次一行。

    ``quantity``/``amount`` 均带符号：发运与生产投料为负，收货与生产产出
    为正，调拨在源仓点为负、目标仓点为正。一次调拨的全部增量金额之和为零。
    """

    business_seq: int
    event_id: str
    report_seq: int | None          # 撤销事件在正常账内不会出现，保留 None 语义
    kind: str                       # receipt / transfer / production / shipment
    warehouse: str
    material: str
    batch: str
    quantity: int
    amount: int
    # 调拨两行共享同一调拨组号；其他业务为空。组内金额之和恒为零。
    transfer_group: str | None = None


@dataclass(frozen=True)
class ClosingStatement:
    """一份可核对的存货成本结账单。

    ``refused=True`` 时不出具任何部分结账单：期初/期内/期末均为空，
    ``contradictions`` 给出全部拒绝来源（含事件 ID 与报送序号）。
    未采用的成本记录不参与任何校验，也不会出现在结果中。

    核对关系（全部为整数分，无四舍五入误差）：
      - 每个 (仓点, 物料, 批次)：期初数量 + 期内数量 = 期末数量，
        期初金额 + 期内金额 = 期末金额；
      - 每笔调拨的期内增减金额之和为零；
      - 每笔生产的净增金额等于该笔加工金额（产出批成本 = 耗用片段成本
        之和 + 加工金额）。
    """

    refused: bool
    opening_business_cutoff: int
    closing_business_cutoff: int
    report_cutoff: int
    opening: tuple[StockBalance, ...] = ()
    movements: tuple[MovementLine, ...] = ()
    closing: tuple[StockBalance, ...] = ()
    adopted_reports: tuple[tuple[str, int | None], ...] = ()
    contradictions: tuple[LedgerContradiction, ...] = ()

    @property
    def total_opening_amount(self) -> int:
        return sum(row.amount for row in self.opening)

    @property
    def total_closing_amount(self) -> int:
        return sum(row.amount for row in self.closing)

    @property
    def total_movement_amount(self) -> int:
        return sum(row.amount for row in self.movements)

    def balance(self, warehouse: str, material: str, batch: str) -> StockBalance | None:
        """按仓点/物料/批次查期末行；不存在（数量与金额皆零）时返回 None。"""
        return next(
            (row for row in self.closing
             if (row.warehouse, row.material, row.batch)
             == (warehouse, material, batch)),
            None,
        )
