"""存货成本结账验收测试（手工可复核的小账 + 余分/拆单/矛盾/规模场景）。

运行： python3 -I /workspace/run_tests.py
"""

from __future__ import annotations

import time
import unittest

from wave_mrp import (
    ClosingQuery,
    CostTable,
    EventReport,
    Interval,
    InventoryCostClosingEngine,
    ProductionCost,
    ProductionPayload,
    ReceiptCost,
    ReceiptPayload,
    RefusalReason,
    Segment,
    ShipmentPayload,
    TransferPayload,
    TransferSegment,
    close_inventory_costs,
)


def receipt(report_seq: int, event: str, biz: int, batch: str,
            material: str = "M", wh: str = "W1", qty: int = 10) -> EventReport:
    return EventReport(report_seq, event, biz,
                       ReceiptPayload(batch, material, wh, qty))


def transfer(report_seq: int, event: str, biz: int, target: str, wh: str,
             *sources: tuple[str, int, int]) -> EventReport:
    return EventReport(report_seq, event, biz, TransferPayload(
        target, wh,
        tuple(TransferSegment(b, Interval(s, e)) for b, s, e in sources)))


def production(report_seq: int, event: str, biz: int, out_batch: str,
               out_qty: int, *inputs: tuple[str, int, int],
               material: str = "M", wh: str = "W1") -> EventReport:
    return EventReport(report_seq, event, biz, ProductionPayload(
        out_batch, material, wh, out_qty,
        tuple(Segment(b, Interval(s, e)) for b, s, e in inputs)))


def shipment(report_seq: int, event: str, biz: int, consignee: str,
             *sources: tuple[str, int, int]) -> EventReport:
    return EventReport(report_seq, event, biz, ShipmentPayload(
        consignee, tuple(Segment(b, Interval(s, e)) for b, s, e in sources)))


def rcost(event: str, report_seq: int, amount: int) -> ReceiptCost:
    return ReceiptCost(event, report_seq, amount)


def pcost(event: str, report_seq: int, amount: int) -> ProductionCost:
    return ProductionCost(event, report_seq, amount)


def closing_of(stmt) -> dict[tuple[str, str, str], tuple[int, int]]:
    return {(r.warehouse, r.material, r.batch): (r.quantity, r.amount)
            for r in stmt.closing}


def opening_of(stmt) -> dict[tuple[str, str, str], tuple[int, int]]:
    return {(r.warehouse, r.material, r.batch): (r.quantity, r.amount)
            for r in stmt.opening}


class RemainderAllocationTest(unittest.TestCase):
    """整批 Q 件 C 分：位置 [a,b) 值 floor(C*b/Q)-floor(C*a/Q)。"""

    def test_whole_lot_remainder_positions(self) -> None:
        # 收货 10 件共 3 分：前 7 件各 0 分，第 8、9 件各 1 分，第 10 件 1 分？
        # floor(3*7/10)-floor(3*6/10)=2-1=1：第 7 件 1 分；逐位置累计。
        reports = [receipt(1, "R1", 1, "B0", qty=10)]
        stmt = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 3),)),
            ClosingQuery(0, 1, 9))
        self.assertFalse(stmt.refused)
        self.assertEqual(closing_of(stmt)[("W1", "M", "B0")], (10, 3))

    def test_partial_shipment_uses_position_value(self) -> None:
        # 10 件 3 分；发 [0,1) = floor(3/10)-floor(0)=0，发 [9,10)=3-2=1
        base = [receipt(1, "R1", 1, "B0", qty=10),
                shipment(2, "S1", 2, "C", ("B0", 0, 1), ("B0", 9, 10))]
        stmt = close_inventory_costs(
            base, CostTable(receipt_costs=(rcost("R1", 1, 3),)),
            ClosingQuery(0, 2, 9))
        moves = {(m.batch, m.quantity): m.amount for m in stmt.movements
                 if m.kind == "shipment"}
        self.assertEqual(moves, {("B0", -2): -1})   # 两段合计：0 + 1
        self.assertEqual(closing_of(stmt)[("W1", "M", "B0")], (8, 2))


class TransferRemainderTest(unittest.TestCase):
    def test_transfer_specific_example_keeps_position_amounts(self) -> None:
        # 4 件共 2 分；把 [1,2) 接在 [2,3) 之前调拨：
        # 原位置 1 值 floor(2*2/4)-floor(2*1/4)=1；原位置 2 值
        # floor(2*3/4)-floor(2*2/4)=0。目标按单序排列，第 1 件仍值 1 分、
        # 第 2 件值 0 分，不按调入后的总额重新摊平。
        reports = [
            receipt(1, "R1", 1, "B0", qty=4),
            transfer(2, "T1", 2, "B1", "W2",
                     ("B0", 1, 2), ("B0", 2, 3)),
        ]
        stmt = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 2),)),
            ClosingQuery(0, 2, 9))
        self.assertFalse(stmt.refused)
        # B1 目标位置 [0,1) 值 1 分，[1,2) 值 0 分；发走第一件应减 1 分
        ship_first = reports + [shipment(3, "S1", 3, "C", ("B1", 0, 1))]
        stmt2 = close_inventory_costs(
            ship_first, CostTable(receipt_costs=(rcost("R1", 1, 2),)),
            ClosingQuery(0, 3, 9))
        self.assertEqual(
            [m.amount for m in stmt2.movements if m.kind == "shipment"], [-1])
        # 发走第二件减 0 分
        ship_second = reports + [shipment(3, "S1", 3, "C", ("B1", 1, 2))]
        stmt3 = close_inventory_costs(
            ship_second, CostTable(receipt_costs=(rcost("R1", 1, 2),)),
            ClosingQuery(0, 3, 9))
        self.assertEqual(
            [m.amount for m in stmt3.movements if m.kind == "shipment"], [0])
        # 目标批总金额等于转出的 2 件在源批的金额之和（不重新摊平）
        self.assertEqual(
            sum(m.amount for m in stmt.movements if m.event_id == "T1"), 0)
        self.assertEqual(
            [m.amount for m in stmt.movements
             if m.event_id == "T1" and m.warehouse == "W2"], [1])

    def test_second_transfer_does_not_repool_remainder(self) -> None:
        # B0: 4 件 2 分（位置值 0,1,0,1）。先发 [3,4)（值 1 分），再转
        # [1,3)（值 1+0=1 分）。剩余 3 件总金额 1 分，若按剩余重新摊平会
        # 算成 floor(1*2/3)-floor(1*1/3)=0，错；必须仍按原位置价值转 1 分。
        reports = [
            receipt(1, "R1", 1, "B0", qty=4),
            shipment(2, "S1", 2, "C", ("B0", 3, 4)),
            transfer(3, "T1", 3, "B1", "W2", ("B0", 1, 3)),
        ]
        stmt = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 2),)),
            ClosingQuery(0, 3, 9))
        transfer_rows = [m for m in stmt.movements if m.event_id == "T1"]
        self.assertEqual(
            [(m.warehouse, m.quantity, m.amount) for m in transfer_rows],
            [("W1", -2, -1), ("W2", 2, 1)])
        self.assertEqual(closing_of(stmt)[("W2", "M", "B1")], (2, 1))

    def test_transfer_amounts_sum_to_zero_regardless_of_splits(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", qty=7),
            receipt(2, "R2", 2, "B9", qty=3, wh="W9"),
            transfer(3, "T1", 3, "B1", "W2",
                     ("B0", 1, 3), ("B0", 5, 7), ("B0", 0, 1)),
            shipment(4, "S1", 4, "C", ("B1", 0, 2)),
        ]
        stmt = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 5),
                                              rcost("R2", 2, 1))),
            ClosingQuery(0, 4, 9))
        for group in ("T1",):
            self.assertEqual(
                sum(m.amount for m in stmt.movements
                    if m.event_id == group), 0)


class ProductionCostTest(unittest.TestCase):
    def test_output_cost_is_consumed_cost_plus_processing(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", qty=4),
            production(2, "P1", 2, "B2", 3,
                       ("B0", 0, 2), ("B0", 3, 4)),
        ]
        stmt = close_inventory_costs(
            reports,
            CostTable(receipt_costs=(rcost("R1", 1, 100),),
                      production_costs=(pcost("P1", 2, 7),)),
            ClosingQuery(0, 2, 9))
        self.assertFalse(stmt.refused)
        prod = [m for m in stmt.movements if m.event_id == "P1"]
        self.assertEqual(sum(m.amount for m in prod), 7)      # 净增 = 加工金额
        # 投 [0,2)+[3,4)：25*2 + 25 = 75；产出 3 件共 82 分
        self.assertEqual(closing_of(stmt)[("W1", "M", "B2")], (3, 82))

    def test_output_quantity_unrelated_to_input_quantity(self) -> None:
        # 产出 10 件而只投入 1 件；成本仍为投入成本 + 加工金额
        reports = [
            receipt(1, "R1", 1, "B0", qty=1),
            production(2, "P1", 2, "B2", 10, ("B0", 0, 1)),
        ]
        stmt = close_inventory_costs(
            reports,
            CostTable(receipt_costs=(rcost("R1", 1, 4),),
                      production_costs=(pcost("P1", 2, 1),)),
            ClosingQuery(0, 2, 9))
        self.assertEqual(closing_of(stmt)[("W1", "M", "B2")], (10, 5))
        self.assertEqual(sum(m.amount for m in stmt.movements
                             if m.event_id == "P1"), 1)

    def test_zero_cost_receipt_and_production_must_be_registered(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", qty=2),
            production(2, "P1", 2, "B2", 2, ("B0", 0, 2)),
        ]
        stmt = close_inventory_costs(
            reports,
            CostTable(receipt_costs=(rcost("R1", 1, 0),),
                      production_costs=(pcost("P1", 2, 0),)),
            ClosingQuery(0, 2, 9))
        self.assertFalse(stmt.refused)
        # 有数量、金额为零的行必须列出
        self.assertEqual(closing_of(stmt), {
            ("W1", "M", "B2"): (2, 0),
        })

    def test_missing_production_cost_refused_not_treated_as_zero(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", qty=2),
            production(2, "P1", 2, "B2", 2, ("B0", 0, 2)),
        ]
        stmt = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 0),)),
            ClosingQuery(0, 2, 9))
        self.assertTrue(stmt.refused)
        self.assertEqual((stmt.opening, stmt.closing), ((), ()))
        self.assertEqual(stmt.movements, ())
        self.assertTrue(any(c.reason == RefusalReason.MISSING_COST
                            and c.event_id == "P1"
                            and c.report_seq == 2
                            for c in stmt.contradictions))


class PeriodSplitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reports = [
            receipt(100, "R1", 1, "B0", qty=10),
            shipment(110, "S1", 2, "C", ("B0", 0, 4)),
            receipt(120, "R2", 3, "B1", qty=5),
            shipment(130, "S2", 4, "C", ("B1", 0, 1)),
        ]
        self.costs = CostTable(receipt_costs=(rcost("R1", 100, 10),
                                             rcost("R2", 120, 5)))
        self.stmt = close_inventory_costs(
            self.reports, self.costs, ClosingQuery(2, 4, 999))

    def test_opening_and_closing_balances(self) -> None:
        self.assertFalse(self.stmt.refused)
        self.assertEqual(opening_of(self.stmt),
                         {("W1", "M", "B0"): (6, 6)})
        self.assertEqual(closing_of(self.stmt), {
            ("W1", "M", "B0"): (6, 6),
            ("W1", "M", "B1"): (4, 4),
        })

    def test_movements_only_within_period_in_business_order(self) -> None:
        self.assertEqual(
            [(m.business_seq, m.event_id, m.quantity, m.amount)
             for m in self.stmt.movements],
            [(3, "R2", 5, 5), (4, "S2", -1, -1)])

    def test_opening_plus_movements_equals_closing(self) -> None:
        for row in self.stmt.closing:
            key = (row.warehouse, row.material, row.batch)
            open_qty, open_amt = opening_of(self.stmt).get(key, (0, 0))
            moves = [m for m in self.stmt.movements
                     if (m.warehouse, m.material, m.batch) == key]
            self.assertEqual(open_qty + sum(m.quantity for m in moves),
                             row.quantity)
            self.assertEqual(open_amt + sum(m.amount for m in moves),
                             row.amount)

    def test_same_cutoffs_means_no_period_movements(self) -> None:
        stmt = close_inventory_costs(
            self.reports, self.costs, ClosingQuery(4, 4, 999))
        self.assertFalse(stmt.refused)
        self.assertEqual(stmt.movements, ())
        self.assertEqual(opening_of(stmt), closing_of(stmt))


class VersionAndCutoffTest(unittest.TestCase):
    def test_cost_must_point_to_adopted_version(self) -> None:
        # S1 有两版；结账采用报送 21，成本表只有旧版 20 -> 缺价拒绝
        reports = [
            receipt(1, "R1", 1, "B0", qty=4),
            shipment(20, "S1", 2, "C", ("B0", 0, 1)),
            shipment(21, "S1", 2, "C", ("B0", 0, 2)),
        ]
        stmt = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 4),)),
            ClosingQuery(0, 2, 99))
        self.assertFalse(stmt.refused)      # 发运不需要成本记录
        self.assertEqual(
            [(m.event_id, m.report_seq, m.quantity)
             for m in stmt.movements],
            [("R1", 1, 4), ("S1", 21, -2)])
        # 收货换版后沿用旧版价格：新版本缺记录必须拒绝，不能借旧价
        reports2 = [
            EventReport(1, "R1", 1, ReceiptPayload("B0", "M", "W1", 4)),
            EventReport(2, "R1", 1, ReceiptPayload("B0", "M", "W1", 5)),
        ]
        stmt2 = close_inventory_costs(
            reports2, CostTable(receipt_costs=(rcost("R1", 1, 8),)),
            ClosingQuery(0, 2, 99))
        self.assertTrue(stmt2.refused)
        self.assertTrue(any(c.reason == RefusalReason.MISSING_COST
                            and c.event_id == "R1" and c.report_seq == 2
                            for c in stmt2.contradictions))

    def test_unadopted_cost_records_skip_validation(self) -> None:
        # 旧版本有非法金额、截止号外有重复记录：均未被采用，不参与校验
        reports = [
            receipt(1, "R1", 1, "B0", qty=4),
            receipt(2, "R1", 1, "B0", qty=4),      # 替换版（采用 2）
            receipt(9, "R9", 9, "B9"),
        ]
        costs = CostTable(
            receipt_costs=(rcost("R1", 1, -5),     # 旧版，非法但未采用
                           rcost("R1", 2, 5),
                           rcost("R9", 9, 1),
                           rcost("R9", 9, 2)))     # 截止号外，重复也未采用
        stmt = close_inventory_costs(
            reports, costs, ClosingQuery(0, 2, 5))
        self.assertFalse(stmt.refused)
        self.assertEqual(closing_of(stmt)[("W1", "M", "B0")], (4, 5))

    def test_duplicate_and_invalid_cost_records_refused(self) -> None:
        reports = [receipt(1, "R1", 1, "B0", qty=4)]
        dup = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 4),
                                             rcost("R1", 1, 5))),
            ClosingQuery(0, 1, 9))
        self.assertTrue(dup.refused)
        self.assertTrue(any(c.reason == RefusalReason.DUPLICATE_COST
                            and c.event_id == "R1"
                            for c in dup.contradictions))
        bad = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 1.5),)),
            ClosingQuery(0, 1, 9))
        self.assertTrue(bad.refused)
        self.assertTrue(any(c.reason == RefusalReason.INVALID_COST
                            for c in bad.contradictions))
        negative = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, -1),)),
            ClosingQuery(0, 1, 9))
        self.assertTrue(negative.refused)

    def test_closing_before_opening_refused(self) -> None:
        stmt = close_inventory_costs(
            [receipt(1, "R1", 1, "B0")],
            CostTable(receipt_costs=(rcost("R1", 1, 1),)),
            ClosingQuery(5, 4, 9))
        self.assertTrue(stmt.refused)
        self.assertEqual(stmt.contradictions[0].reason,
                         RefusalReason.INVALID_CLOSING_RANGE)


class PhysicalLedgerTest(unittest.TestCase):
    def test_physical_contradiction_refuses_whole_statement(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", qty=4),
            shipment(2, "S1", 2, "C", ("B0", 0, 3)),
            shipment(3, "S2", 3, "C", ("B0", 2, 4)),   # 重复耗用 [2,3)
        ]
        stmt = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 4),)),
            ClosingQuery(0, 3, 9))
        self.assertTrue(stmt.refused)
        self.assertEqual((stmt.opening, stmt.movements, stmt.closing),
                         ((), (), ()))
        self.assertTrue(any(c.reason == RefusalReason.DOUBLE_CONSUMPTION
                            and c.event_id == "S2"
                            and c.report_seq == 3
                            for c in stmt.contradictions))

    def test_missing_cost_blocks_partial_statement(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", qty=4),
            receipt(2, "R2", 2, "B1", qty=4),
        ]
        stmt = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 4),)),
            ClosingQuery(0, 2, 9))
        self.assertTrue(stmt.refused)
        self.assertEqual((stmt.opening, stmt.closing), ((), ()))
        self.assertEqual([c.event_id for c in stmt.contradictions], ["R2"])


class ScaleAndReuseTest(unittest.TestCase):
    def test_trillion_batch_not_expanded(self) -> None:
        huge = 10 ** 12
        reports = [
            receipt(1, "R1", 1, "B0", qty=huge),
            transfer(2, "T1", 2, "B1", "W2",
                     ("B0", huge - 5, huge)),
            shipment(3, "S1", 3, "C", ("B1", 2, 4)),
        ]
        start = time.perf_counter()
        stmt = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 10 ** 12),)),
            ClosingQuery(0, 3, 9))
        elapsed = time.perf_counter() - start
        self.assertFalse(stmt.refused)
        self.assertLess(elapsed, 2.0)
        # 每件 1 分：B0 转出末 5 件 = 5 分；B1 发 [2,4) = 2 分，余 3 分
        self.assertEqual(closing_of(stmt), {
            ("W1", "M", "B0"): (huge - 5, huge - 5),
            ("W2", "M", "B1"): (3, 3)})

    def test_repeated_whole_lot_transfers_are_linear(self) -> None:
        # 同一拼接批连续整批转运多次，计价不遍历此前所有拼接段
        n = 3000
        huge = 10 ** 12
        reports: list[EventReport] = [
            receipt(1, "R1", 1, "B0", qty=huge)]
        prev = "B0"
        for i in range(n):
            reports.append(transfer(10 + i, f"T{i}", 2 + i, f"B{i+1}", f"W{i+2}",
                                    (prev, 0, huge)))
            prev = f"B{i+1}"
        start = time.perf_counter()
        stmt = close_inventory_costs(
            reports, CostTable(receipt_costs=(rcost("R1", 1, 7),)),
            ClosingQuery(0, n + 1, 10 ** 9))
        elapsed = time.perf_counter() - start
        self.assertFalse(stmt.refused)
        self.assertLess(elapsed, 5.0)
        self.assertEqual(closing_of(stmt)[(f"W{n+1}", "M", prev)],
                         (huge, 7))

    def test_partial_transfer_of_concat_batch_scales_with_document(self) -> None:
        # 由 n 小段拼成的批，只按本次调拨单列出的片段计价
        n = 4000
        reports: list[EventReport] = [
            receipt(1, "R1", 1, "B0", qty=n)]
        segments = tuple((f"B0", i, i + 1) for i in range(n))
        reports.append(transfer(2, "T1", 2, "B1", "W2", *segments))
        reports.append(transfer(3, "T2", 3, "B2", "W3", ("B1", 0, 2)))
        start = time.perf_counter()
        stmt = close_inventory_costs(
            reports,
            CostTable(receipt_costs=(rcost("R1", 1, 4 * n),)),
            ClosingQuery(0, 3, 9))
        elapsed = time.perf_counter() - start
        self.assertFalse(stmt.refused)
        self.assertLess(elapsed, 6.0)
        # B1 共 4n 分、n 件，每件 4 分；取前 2 件 = 8 分
        self.assertEqual(
            [m.amount for m in stmt.movements
             if m.event_id == "T2" and m.warehouse == "W3"], [8])

    def test_fixed_cutoff_requery_is_deterministic(self) -> None:
        reports = [receipt(1, "R1", 1, "B0", qty=4),
                   shipment(2, "S1", 2, "C", ("B0", 1, 2))]
        engine = InventoryCostClosingEngine(reports)
        costs = CostTable(receipt_costs=(rcost("R1", 1, 8),))
        query = ClosingQuery(0, 2, 9)
        first = engine.build_closing_statement(costs, query)
        second = engine.build_closing_statement(costs, query)
        self.assertEqual(first.closing, second.closing)
        self.assertEqual(first.movements, second.movements)


if __name__ == "__main__":
    unittest.main(verbosity=2)
