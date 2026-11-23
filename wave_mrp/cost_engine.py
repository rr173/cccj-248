"""存货成本结账引擎：在同一份实物过账流水上重放数量与金额。

数量口径完全复用追溯模块（``trace_engine``）的报送筛选与实物账校验；
本模块只负责把成本金额贴到**批内位置**上，并按业务顺序产出可核对的
期初、期内增减、期末三张表。

三个关键约定
------------
1. 金额只认**当前采用的报送版本**：成本记录用 ``(event_id, report_seq)``
   指向具体版本；未被本次结账采用的记录（旧版、截止号外、撤销事件）完全
   不参与校验，也绝不从旧版借价。
2. 批次金额按位置连续分配：整批 ``Q`` 件、总额 ``C`` 分时，最初编号在
   ``[a,b)`` 的货值为 ``floor(C*b/Q) - floor(C*a/Q)``。调拨时目标批按
   实物单上的片段顺序拼接，每个位置**保留原位置金额**；发运/生产按账上
   实际指定的位置减记，因此拆单与先发后发都不会重新摊平余分。
3. 全部计算只用整数分与半开区间；批量可超过万亿，不逐件展开。

成本基（cost basis）是一棵不可变的小 DAG：
  - ``_LotBasis``：一次收货或一次生产产出的整批金额；
  - ``_SliceBasis``：某批位置的连续子区间（部分调拨时产生）；
  - ``_ConcatBasis``：按调拨单顺序拼接若干子区间。
整批调拨直接共享源批的成本基，不遍历此前的拼接段；只有本次调拨单上
实际列出的源片段才参与计价。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from .cost_model import (
    ClosingQuery,
    CostTable,
    ProductionCost,
    ReceiptCost,
)
from .cost_result import ClosingStatement, MovementLine, StockBalance
from .trace_engine import LotTraceabilityEngine
from .trace_model import (
    EventReport,
    ProductionPayload,
    ReceiptPayload,
    ShipmentPayload,
    TransferPayload,
)
from .trace_result import LedgerContradiction, RefusalReason


# --------------------------------------------------------------------- #
# 批内位置成本基：不可变小 DAG，所有金额均为非负整数分
# --------------------------------------------------------------------- #
class _CostBasis:
    """一段批内位置的成本查询接口（半开区间，整数分）。

    ``total`` 是该段自身的总额：整批调拨、整批发运等“取满”的计价直接
    取它，不遍历此前拼接过的片段。
    """

    size: int
    total: int

    def value(self, start: int, end: int) -> int:  # pragma: no cover - 抽象
        raise NotImplementedError


class _LotBasis(_CostBasis):
    """收货批/生产产出批的整批金额：按 ``floor(C*b/Q)-floor(C*a/Q)`` 分位置。"""

    __slots__ = ("size", "_amount", "total")

    def __init__(self, size: int, amount: int) -> None:
        self.size = size
        self._amount = amount
        self.total = amount

    def value(self, start: int, end: int) -> int:
        a, b = self._amount, self.size
        return (a * end) // b - (a * start) // b


class _SliceBasis(_CostBasis):
    """某批连续位置 ``[offset, offset+size)`` 的成本（部分调拨时产生）。

    只切本次调拨单上列出的那一段，总额在构造时一次算出；源批可以是
    拼接批，但这里不会再次遍历它此前的所有片段。
    """

    __slots__ = ("parent", "offset", "size", "total")

    def __init__(self, parent: _CostBasis, offset: int, size: int,
                 total: int) -> None:
        self.parent = parent
        self.offset = offset
        self.size = size
        self.total = total

    def value(self, start: int, end: int) -> int:
        if start == 0 and end == self.size:
            return self.total
        return self.parent.value(self.offset + start, self.offset + end)


@dataclass(frozen=True)
class _BasisPart:
    basis: _CostBasis
    length: int


class _ConcatBasis(_CostBasis):
    """按调拨单顺序拼接的若干片段；目标位置 = 各片段长度累加。

    构造时记下各片段起点与金额前缀和（只遍历本次调拨单列出的片段）；
    之后无论整批还是任意子区间计价，都只需二分定位与至多两个片段的部分
    金额，不再遍历历史拼接段。
    """

    __slots__ = ("parts", "_starts", "_prefix", "size", "total")

    def __init__(self, parts: tuple[_BasisPart, ...], total: int) -> None:
        self.parts = parts
        starts: list[int] = []
        prefix: list[int] = []
        offset = 0
        running = 0
        for part in parts:
            starts.append(offset)
            prefix.append(running)
            offset += part.length
            running += part.basis.total
        self._starts = tuple(starts)
        self._prefix = tuple(prefix)
        self.size = offset
        self.total = total

    def value(self, start: int, end: int) -> int:
        if start >= end:
            return 0
        if start == 0 and end == self.size:
            return self.total
        starts = self._starts
        i = max(0, bisect_right(starts, start) - 1)
        j = max(0, bisect_left(starts, end) - 1)
        if i == j:
            part = self.parts[i]
            base = starts[i]
            return part.basis.value(start - base, end - base)
        out = 0
        first = self.parts[i]
        first_end = starts[i] + first.length
        out += first.basis.value(start - starts[i], first_end - starts[i])
        out += self._prefix[j] - self._prefix[i + 1]   # 中间整段之和
        last = self.parts[j]
        out += last.basis.value(0, end - starts[j])
        return out


# --------------------------------------------------------------------- #
# 成本重放状态
# --------------------------------------------------------------------- #
@dataclass
class _CostBatch:
    """一个批次在成本账上的当前余额（数量与金额同步减记）。"""

    batch: str
    material: str
    warehouse: str
    size: int
    basis: _CostBasis
    remaining_qty: int
    remaining_amount: int


def _valid_amount(value: object) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= 0)


class InventoryCostClosingEngine:
    """固定报送流水的存货成本结账引擎；同一流水可按不同截止号反复结账。"""

    def __init__(self, reports: list[EventReport] | tuple[EventReport, ...]) -> None:
        self._reports = tuple(reports)
        self._selector = LotTraceabilityEngine(self._reports)

    # ------------------------------------------------------------------ #
    # 参数与成本记录校验
    # ------------------------------------------------------------------ #
    @staticmethod
    def _param_errors(query: ClosingQuery) -> list[LedgerContradiction]:
        errors: list[LedgerContradiction] = []
        for name, value in (
            ("期初业务截止号", query.opening_business_cutoff),
            ("期末业务截止号", query.closing_business_cutoff),
            ("报送截止号", query.report_cutoff),
        ):
            if (not isinstance(value, int) or isinstance(value, bool)
                    or value < 0):
                errors.append(LedgerContradiction(
                    RefusalReason.INVALID_CUTOFF, None,
                    f"{name}必须是非负整数: {value!r}"))
        if (not errors
                and query.closing_business_cutoff
                < query.opening_business_cutoff):
            errors.append(LedgerContradiction(
                RefusalReason.INVALID_CLOSING_RANGE, None,
                f"期末业务截止号 {query.closing_business_cutoff} "
                f"早于期初业务截止号 {query.opening_business_cutoff}"))
        return errors

    @staticmethod
    def _validate_costs(
        events: list[tuple[int, str, int, object]],
        cost_table: CostTable,
    ) -> list[LedgerContradiction]:
        """只校验本次结账实际采用的收货/生产事件版本。

        未被采用的记录（旧版本、截止号外、撤销事件、另一类事件）不参与
        任何校验；缺记录不以零补齐。
        """
        receipt_index: dict[tuple[str, int], list[ReceiptCost]] = {}
        production_index: dict[tuple[str, int], list[ProductionCost]] = {}
        for cost in cost_table.receipt_costs:
            receipt_index.setdefault(
                (cost.event_id, cost.report_seq), []).append(cost)
        for cost in cost_table.production_costs:
            production_index.setdefault(
                (cost.event_id, cost.report_seq), []).append(cost)

        errors: list[LedgerContradiction] = []
        for _seq, eid, rseq, payload in events:
            if payload is None:
                continue
            if isinstance(payload, ReceiptPayload):
                records: list[ReceiptCost] | list[ProductionCost] = \
                    receipt_index.get((eid, rseq), [])
                label = "收货"
            elif isinstance(payload, ProductionPayload):
                records = production_index.get((eid, rseq), [])
                label = "生产"
            else:
                continue

            if not records:
                errors.append(LedgerContradiction(
                    RefusalReason.MISSING_COST, eid,
                    f"{label}事件缺少成本记录（事件={eid}, 报送={rseq}）；"
                    "漏价不得按零元处理", report_seq=rseq))
                continue
            if len(records) > 1:
                errors.append(LedgerContradiction(
                    RefusalReason.DUPLICATE_COST, eid,
                    f"{label}事件版本（事件={eid}, 报送={rseq}）出现 "
                    f"{len(records)} 条成本记录，入账金额必须唯一",
                    report_seq=rseq))
                continue
            record = records[0]
            if not _valid_amount(record.amount):
                errors.append(LedgerContradiction(
                    RefusalReason.INVALID_COST, eid,
                    f"{label}事件版本（事件={eid}, 报送={rseq}）金额非法: "
                    f"{record.amount!r}，必须是非负整数分",
                    report_seq=rseq))
        return errors

    # ------------------------------------------------------------------ #
    # 成本重放：逐业务生成期内增减，并在截止点取余额快照
    # ------------------------------------------------------------------ #
    def _cost_replay(
        self,
        events: list[tuple[int, str, int, object]],
        cost_table: CostTable,
        opening_cutoff: int,
    ) -> tuple[list[StockBalance], list[MovementLine], list[StockBalance]]:
        """从空库起按业务顺序重放；期初截止号之后的事件才进期内增减。

        期初快照取在首笔“期内”业务之前；期初截止号之后的报送版本若改写
        了期初之前的事件，则整账按采用版本重算，期初同样使用采用版本。
        """
        receipt_cost = {
            (c.event_id, c.report_seq): c.amount
            for c in cost_table.receipt_costs
        }
        production_cost = {
            (c.event_id, c.report_seq): c.amount
            for c in cost_table.production_costs
        }

        batches: dict[str, _CostBatch] = {}
        opening: list[StockBalance] = []
        movements: list[MovementLine] = []
        snapshot_taken = False

        def take_snapshot() -> list[StockBalance]:
            rows = [
                StockBalance(b.warehouse, b.material, b.batch,
                             b.remaining_qty, b.remaining_amount)
                for b in batches.values()
                if b.remaining_qty > 0 or b.remaining_amount != 0
            ]
            rows.sort(key=lambda r: (r.warehouse, r.material, r.batch))
            return rows

        def movement(
            seq: int, eid: str, rseq: int | None, kind: str,
            warehouse: str, material: str,
            batch_id: str, qty: int, amount: int,
            transfer_group: str | None = None,
        ) -> MovementLine:
            return MovementLine(
                business_seq=seq, event_id=eid, report_seq=rseq, kind=kind,
                warehouse=warehouse, material=material, batch=batch_id,
                quantity=qty, amount=amount, transfer_group=transfer_group)

        def consume(
            batch_id: str, start: int, end: int,
            aggregate: dict[str, list[int]],
        ) -> int:
            batch = batches[batch_id]
            if start == 0 and end == batch.size:
                amount = batch.basis.total
            else:
                amount = batch.basis.value(start, end)
            length = end - start
            batch.remaining_qty -= length
            batch.remaining_amount -= amount
            slot = aggregate.setdefault(batch_id, [0, 0])
            slot[0] -= length
            slot[1] -= amount
            return amount

        for seq, eid, rseq, payload in events:
            if payload is None:
                continue
            if not snapshot_taken and seq > opening_cutoff:
                opening = take_snapshot()
                snapshot_taken = True

            period = seq > opening_cutoff

            if isinstance(payload, ReceiptPayload):
                amount = receipt_cost[(eid, rseq)]
                basis = _LotBasis(payload.quantity, amount)
                batches[payload.batch] = _CostBatch(
                    batch=payload.batch, material=payload.material,
                    warehouse=payload.warehouse, size=payload.quantity,
                    basis=basis, remaining_qty=payload.quantity,
                    remaining_amount=amount)
                if period:
                    movements.append(movement(
                        seq, eid, rseq, "receipt",
                        payload.warehouse, payload.material, payload.batch,
                        payload.quantity, amount))

            elif isinstance(payload, TransferPayload):
                aggregate: dict[str, list[int]] = {}
                parts: list[_BasisPart] = []
                total_amount = 0
                material = warehouse = ""
                for src in payload.sources:
                    source = batches[src.source_batch]
                    iv = src.source_interval
                    taken = consume(
                        src.source_batch, iv.start, iv.end, aggregate)
                    total_amount += taken
                    if iv.start == 0 and iv.end == source.size:
                        part_basis: _CostBasis = source.basis
                    else:
                        part_basis = _SliceBasis(
                            source.basis, iv.start, iv.length, taken)
                    parts.append(_BasisPart(part_basis, iv.length))
                    material, warehouse = source.material, source.warehouse
                if len(parts) == 1 and parts[0].length == parts[0].basis.size:
                    target_basis: _CostBasis = parts[0].basis
                else:
                    target_basis = _ConcatBasis(tuple(parts), total_amount)
                target_size = sum(p.length for p in parts)
                batches[payload.target_batch] = _CostBatch(
                    batch=payload.target_batch, material=material,
                    warehouse=payload.target_warehouse, size=target_size,
                    basis=target_basis, remaining_qty=target_size,
                    remaining_amount=total_amount)
                if period:
                    for batch_id in aggregate:
                        source = batches[batch_id]
                        qty, amount = aggregate[batch_id]
                        movements.append(movement(
                            seq, eid, rseq, "transfer",
                            source.warehouse, source.material, batch_id,
                            qty, amount, transfer_group=eid))
                    movements.append(movement(
                        seq, eid, rseq, "transfer",
                        payload.target_warehouse, material, payload.target_batch,
                        target_size, total_amount, transfer_group=eid))

            elif isinstance(payload, ProductionPayload):
                aggregate = {}
                for seg in payload.inputs:
                    consume(seg.batch, seg.interval.start,
                            seg.interval.end, aggregate)
                # 产出批成本 = 实际耗用片段成本之和 + 加工金额
                input_amount = -sum(slot[1] for slot in aggregate.values())
                processing = production_cost[(eid, rseq)]
                output_amount = input_amount + processing
                basis = _LotBasis(payload.reported_output_qty, output_amount)
                batches[payload.output_batch] = _CostBatch(
                    batch=payload.output_batch, material=payload.output_material,
                    warehouse=payload.registration_warehouse,
                    size=payload.reported_output_qty,
                    basis=basis, remaining_qty=payload.reported_output_qty,
                    remaining_amount=output_amount)
                if period:
                    for batch_id in aggregate:
                        source = batches[batch_id]
                        qty, amount = aggregate[batch_id]
                        movements.append(movement(
                            seq, eid, rseq, "production",
                            source.warehouse, source.material, batch_id,
                            qty, amount))
                    movements.append(movement(
                        seq, eid, rseq, "production",
                        payload.registration_warehouse,
                        payload.output_material, payload.output_batch,
                        payload.reported_output_qty, output_amount))

            elif isinstance(payload, ShipmentPayload):
                aggregate = {}
                for seg in payload.sources:
                    consume(seg.batch, seg.interval.start,
                            seg.interval.end, aggregate)
                if period:
                    for batch_id in aggregate:
                        source = batches[batch_id]
                        qty, amount = aggregate[batch_id]
                        movements.append(movement(
                            seq, eid, rseq, "shipment",
                            source.warehouse, source.material, batch_id,
                            qty, amount))

        if not snapshot_taken:
            opening = take_snapshot()
        closing = take_snapshot()
        return opening, movements, closing

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def build_closing_statement(
        self, cost_table: CostTable, query: ClosingQuery
    ) -> ClosingStatement:
        """结出期初/期内/期末存货成本；任何矛盾都整单拒绝。"""
        param_errors = self._param_errors(query)
        if param_errors:
            return self._refused(query, param_errors)

        events, select_errors, audit = self._selector.select_reports(
            query.closing_business_cutoff, query.report_cutoff)

        cost_errors = self._validate_costs(events, cost_table)

        # 实物账校验与追溯接口完全一致（同版本规则、同矛盾类别）
        _batches, replay_errors = self._selector.replay_physical_ledger(events)

        adopted_by_event = {eid: rseq for eid, rseq in audit}
        enriched_replay: list[LedgerContradiction] = []
        for c in replay_errors:
            rseq = c.report_seq
            if rseq is None and c.event_id is not None:
                rseq = adopted_by_event.get(c.event_id)
            enriched_replay.append(LedgerContradiction(
                c.reason, c.event_id, c.detail, rseq))

        errors = list(param_errors)
        for group in (select_errors, cost_errors, enriched_replay):
            for c in group:
                if c not in errors:
                    errors.append(c)
        if errors:
            return self._refused(query, errors, audit)

        opening, movements, closing = self._cost_replay(
            events, cost_table, query.opening_business_cutoff)
        return ClosingStatement(
            refused=False,
            opening_business_cutoff=query.opening_business_cutoff,
            closing_business_cutoff=query.closing_business_cutoff,
            report_cutoff=query.report_cutoff,
            opening=tuple(opening),
            movements=tuple(movements),
            closing=tuple(closing),
            adopted_reports=tuple(audit))

    @staticmethod
    def _refused(
        query: ClosingQuery,
        errors: list[LedgerContradiction],
        audit: list[tuple[str, int | None]] | None = None,
    ) -> ClosingStatement:
        return ClosingStatement(
            refused=True,
            opening_business_cutoff=query.opening_business_cutoff,
            closing_business_cutoff=query.closing_business_cutoff,
            report_cutoff=query.report_cutoff,
            adopted_reports=tuple(audit or ()),
            contradictions=tuple(errors))


def close_inventory_costs(
    reports: list[EventReport] | tuple[EventReport, ...],
    cost_table: CostTable,
    query: ClosingQuery,
) -> ClosingStatement:
    """模块级便捷接口：给定报送流水、成本表与结账口径，返回结账单。"""
    return InventoryCostClosingEngine(reports).build_closing_statement(
        cost_table, query)


# 与追溯模块 ``build_traceability_list`` 对称的命名，便于并列调用。
build_closing_statement = close_inventory_costs
CostClosingEngine = InventoryCostClosingEngine
