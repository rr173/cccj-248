"""来料抽检追溯验收测试（手工可复核的小流水 + 规模/矛盾场景）。

运行： python3 -I /workspace/run_tests.py
"""

from __future__ import annotations

import time
import unittest

from wave_mrp import (
    EventReport,
    Interval,
    LotTraceabilityEngine,
    PayloadType,
    ProductionPayload,
    ReceiptPayload,
    RefusalReason,
    Segment,
    ShipmentPayload,
    TraceQuery,
    TransferPayload,
    TransferSegment,
    build_traceability_list,
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


class EndToEndHandCalcTest(unittest.TestCase):
    """与 trace_demo 同一流水，断言逐行手算结果。"""

    def setUp(self) -> None:
        self.reports = [
            receipt(100, "R1", 1, "B0", qty=10),
            transfer(110, "T1", 2, "B1", "W2",
                     ("B0", 1, 3), ("B0", 7, 9)),
            production(120, "P1", 3, "B2", 4, ("B1", 0, 2), wh="W2"),
            shipment(130, "S1", 4, "CUST-A", ("B2", 0, 3)),
            shipment(140, "S2", 5, "CUST-B", ("B1", 1, 4)),
            shipment(141, "S2", 5, "CUST-B", ("B1", 2, 4)),
            shipment(150, "X1", 6, "CUST-X", ("B0", 5, 6)),
            EventReport(151, "X1", 6, None),
            shipment(160, "F1", 9, "CUST-F", ("B0", 2, 3)),
        ]
        self.engine = LotTraceabilityEngine(self.reports)
        self.query = TraceQuery("B0", (Interval(2, 5), Interval(4, 6)), 6, 350)
        self.result = self.engine.build_traceability_list(self.query)

    def test_list_issued_with_handcalc_rows(self) -> None:
        self.assertFalse(self.result.refused)
        iso = {(r.warehouse, r.batch): r for r in self.result.isolation}
        self.assertEqual(set(iso), {("W1", "B0"), ("W2", "B2")})
        self.assertEqual(iso[("W1", "B0")].intervals, (Interval(3, 6),))
        self.assertEqual(iso[("W1", "B0")].quantity, 3)
        self.assertEqual(iso[("W2", "B2")].intervals, (Interval(3, 4),))
        self.assertEqual(iso[("W2", "B2")].quantity, 1)
        self.assertEqual(self.result.total_isolation_qty, 4)

        rec = {(r.consignee, r.shipment_event_id, r.batch): r
               for r in self.result.recall}
        self.assertEqual(set(rec), {("CUST-A", "S1", "B2")})
        self.assertEqual(rec[("CUST-A", "S1", "B2")].intervals,
                         (Interval(0, 3),))
        self.assertEqual(self.result.total_recall_qty, 3)
        # S2 发出的 B1[2,4) 未命中受影响位置，不产生召回
        self.assertTrue(all(r.shipment_event_id != "S2"
                            for r in self.result.recall))

    def test_overlapping_marks_merged_and_no_zero_rows(self) -> None:
        mark = self.result.receipt_evidence
        self.assertEqual(mark.marked_intervals, (Interval(2, 6),))
        self.assertEqual(mark.report_seq, 100)
        self.assertTrue(all(r.quantity > 0 for r in self.result.isolation))
        self.assertTrue(all(r.quantity > 0 for r in self.result.recall))

    def test_evidence_is_one_hop_and_versioned(self) -> None:
        tev = self.result.transfer_evidence
        self.assertEqual(len(tev), 1)
        self.assertEqual(tev[0].event_id, "T1")
        self.assertEqual(tev[0].report_seq, 110)
        # 仅命中片段 B0[2,3) 入依据，B0[7,9) 未命中不出现；映射到 B1[1,2)
        self.assertEqual(len(tev[0].mappings), 1)
        m = tev[0].mappings[0]
        self.assertEqual((m.source_batch, m.source_interval),
                         ("B0", Interval(2, 3)))
        self.assertEqual((m.target_batch, m.target_interval),
                         ("B1", Interval(1, 2)))
        pev = self.result.production_evidence
        self.assertEqual(len(pev), 1)
        self.assertEqual(pev[0].hit_inputs, (Segment("B1", Interval(0, 2)),))
        self.assertEqual(pev[0].output_interval, Interval(0, 4))
        adopted = dict(self.result.adopted_reports)
        self.assertEqual(adopted["S2"], 141)     # 最后可见版本
        self.assertIsNone(adopted["X1"])         # 撤销
        self.assertNotIn("F1", adopted)          # 业务截止号外

    def test_intermediate_batch_not_counted_as_pending_goods(self) -> None:
        # B1 是中间批：受影响位置 B1[1,2) 已被 P1 消耗，其余位置未命中，
        # 不得出现在隔离行或召回行
        self.assertNotIn("B1", {r.batch for r in self.result.isolation})
        self.assertNotIn("B1", {r.batch for r in self.result.recall})

    def test_fixed_cutoff_requery_is_deterministic(self) -> None:
        again = self.engine.build_traceability_list(self.query)
        self.assertEqual(again.isolation, self.result.isolation)
        self.assertEqual(again.recall, self.result.recall)
        self.assertEqual(again.adopted_reports, self.result.adopted_reports)

    def test_superseded_report_can_make_older_cutoff_refuse(self) -> None:
        # 报送截止号=140：S2 只有第一版 B1[1,4)，与 P1 已耗 B1[1,2) 冲突
        old = self.engine.build_traceability_list(
            TraceQuery("B0", (Interval(2, 5), Interval(4, 6)), 6, 140))
        self.assertTrue(old.refused)
        self.assertTrue(any(c.reason == RefusalReason.DOUBLE_CONSUMPTION
                            and c.event_id == "S2"
                            for c in old.contradictions))
        adopted = dict(old.adopted_reports)
        self.assertEqual(adopted["S2"], 140)
        self.assertNotIn("X1", adopted)       # 截止号外 => 事件不存在
        # 拒绝时不出具任何部分清单
        self.assertEqual((old.isolation, old.recall), ((), ()))

    def test_cancelled_event_does_not_happen(self) -> None:
        # 把撤销报送去掉：X1 的发运即生效，B0[5,6) 应进召回且不在隔离
        live = [r for r in self.reports if r.report_seq != 151]
        res = LotTraceabilityEngine(live).build_traceability_list(
            TraceQuery("B0", (Interval(2, 5), Interval(4, 6)), 6, 350))
        self.assertFalse(res.refused)
        x1 = [r for r in res.recall if r.shipment_event_id == "X1"]
        self.assertEqual(len(x1), 1)
        self.assertEqual((x1[0].consignee, x1[0].intervals),
                         ("CUST-X", (Interval(5, 6),)))
        b0 = next(r for r in res.isolation if r.batch == "B0")
        self.assertEqual(b0.intervals, (Interval(3, 5),))  # [5,6) 已发出


class PropagationRuleTest(unittest.TestCase):
    def test_transfer_only_hit_segments_continue_taint(self) -> None:
        # B0 共 6，圈 [0,2)；调拨只搬 B0[2,6)（未命中）=> B1 完全干净
        reports = [
            receipt(1, "R1", 1, "B0", qty=6),
            transfer(2, "T1", 2, "B1", "W2", ("B0", 2, 6)),
            shipment(3, "S1", 3, "C", ("B1", 0, 4)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 2),), 9, 9))
        self.assertFalse(res.refused)
        self.assertEqual(res.recall, ())
        self.assertEqual(res.transfer_evidence, ())
        self.assertEqual([r.batch for r in res.isolation], ["B0"])

    def test_multi_hop_transfer_position_mapping(self) -> None:
        # B0[0,10) 圈 [3,5)；T1: B0[2,6)->B1[0,4)，故 B0[3,5)=>B1[1,3)
        # T2: B1[1,3)->B2[0,2)，故 B1[1,3)=>B2 整批 [0,2)
        reports = [
            receipt(1, "R1", 1, "B0", qty=10),
            transfer(2, "T1", 2, "B1", "W2", ("B0", 2, 6)),
            transfer(3, "T2", 3, "B2", "W3", ("B1", 1, 3)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(3, 5),), 9, 9))
        self.assertFalse(res.refused)
        iso = {r.batch: r for r in res.isolation}
        # B0[3,5) 已被 T1 全部搬走，源批无隔离行；B2 整批 [0,2) 受影响
        self.assertNotIn("B0", iso)
        self.assertEqual(iso["B2"].intervals, (Interval(0, 2),))
        self.assertEqual(len(res.transfer_evidence), 2)

    def test_production_any_hit_taints_whole_output_batch(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", qty=10),
            # 投入 B0[8,10)（干净）+ B0[2,3)（命中），产出 100 件
            production(2, "P1", 2, "FG", 100,
                      ("B0", 8, 10), ("B0", 2, 3)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(2, 3),), 9, 9))
        self.assertFalse(res.refused)
        fg = next(r for r in res.isolation if r.batch == "FG")
        self.assertEqual((fg.intervals, fg.quantity),
                         ((Interval(0, 100),), 100))
        ev = res.production_evidence[0]
        # 只列真正命中的投入片段，干净投入 B0[8,10) 不入依据
        self.assertEqual(ev.hit_inputs, (Segment("B0", Interval(2, 3)),))
        # 源片段已被生产消耗，不再计为在库
        self.assertNotIn("B0", {r.batch for r in res.isolation})

    def test_consumed_source_not_in_isolation_but_shown_in_evidence(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", qty=4),
            shipment(2, "S1", 2, "C", ("B0", 0, 4)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 4),), 9, 9))
        self.assertEqual(res.isolation, ())
        self.assertEqual(res.recall[0].quantity, 4)

    def test_same_position_counted_once_with_overlapping_shipments_merged(self) -> None:
        # 同一发运事件内两个相邻片段命中，归并为一行一个区间
        reports = [
            receipt(1, "R1", 1, "B0", qty=10),
            shipment(2, "S1", 2, "C", ("B0", 2, 4), ("B0", 4, 6)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(2, 6),), 9, 9))
        self.assertEqual(len(res.recall), 1)
        self.assertEqual(res.recall[0].intervals, (Interval(2, 6),))
        self.assertEqual(res.recall[0].quantity, 4)


class CutoffAndVersionTest(unittest.TestCase):
    def test_business_cutoff_excludes_later_events(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", qty=5),
            shipment(2, "S1", 4, "C", ("B0", 0, 2)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 2),), 3, 9))
        self.assertFalse(res.refused)
        self.assertEqual(res.recall, ())                  # 发运未生效
        self.assertEqual(res.isolation[0].quantity, 2)    # 仍在库

    def test_report_cutoff_excludes_later_versions(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", qty=5),
            shipment(10, "S1", 2, "C1", ("B0", 0, 2)),
            shipment(20, "S1", 2, "C2", ("B0", 0, 2)),
        ]
        old = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 2),), 9, 15))
        new = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 2),), 9, 25))
        self.assertEqual(old.recall[0].consignee, "C1")
        self.assertEqual(new.recall[0].consignee, "C2")
        # 旧清单对象不被新报送改变
        self.assertEqual(old.recall[0].consignee, "C1")

    def test_replacement_report_changes_consumed_positions(self) -> None:
        # 第一版发 [0,2)，替换版发 [3,5)；命中区间 [0,5)
        reports = [
            receipt(1, "R1", 1, "B0", qty=5),
            shipment(10, "S1", 2, "C", ("B0", 0, 2)),
            shipment(11, "S1", 2, "C", ("B0", 3, 5)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 5),), 9, 50))
        self.assertEqual(res.recall[0].intervals, (Interval(3, 5),))
        # 旧版完全不参与：[0,3) 仍在库隔离
        self.assertEqual(res.isolation[0].intervals, (Interval(0, 3),))


class RefusalTest(unittest.TestCase):
    def _base(self) -> list[EventReport]:
        return [receipt(1, "R1", 1, "B0", qty=10)]

    def test_unknown_source_batch_refused(self) -> None:
        res = build_traceability_list(
            self._base(), TraceQuery("GHOST", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        self.assertEqual(res.contradictions[0].reason,
                         RefusalReason.UNKNOWN_SOURCE_BATCH)
        self.assertEqual((res.isolation, res.recall), ((), ()))

    def test_source_batch_revoked_is_not_visible_receipt(self) -> None:
        reports = self._base() + [EventReport(2, "R1", 1, None)]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        self.assertEqual(res.contradictions[0].reason,
                         RefusalReason.UNKNOWN_SOURCE_BATCH)

    def test_mark_out_of_range_refused(self) -> None:
        res = build_traceability_list(
            self._base(), TraceQuery("B0", (Interval(9, 11),), 9, 9))
        self.assertTrue(res.refused)
        self.assertEqual(res.contradictions[0].reason,
                         RefusalReason.MARK_OUT_OF_RANGE)

    def test_empty_mark_refused(self) -> None:
        res = build_traceability_list(
            self._base(), TraceQuery("B0", (), 9, 9))
        self.assertTrue(res.refused)
        self.assertEqual(res.contradictions[0].reason, RefusalReason.EMPTY_MARK)

    def test_invalid_interval_construction(self) -> None:
        with self.assertRaises(ValueError):
            Interval(5, 5)      # 空片段
        with self.assertRaises(ValueError):
            Interval(-1, 2)

    def test_invalid_cutoff_refused(self) -> None:
        res = build_traceability_list(
            self._base(), TraceQuery("B0", (Interval(0, 1),), -1, 9))
        self.assertTrue(res.refused)
        self.assertEqual(res.contradictions[0].reason,
                         RefusalReason.INVALID_CUTOFF)

    def test_missing_upstream_batch_refused(self) -> None:
        reports = self._base() + [
            shipment(2, "S1", 2, "C", ("NOPE", 0, 1))]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        reasons = {c.reason for c in res.contradictions}
        self.assertIn(RefusalReason.MISSING_UPSTREAM_BATCH, reasons)

    def test_duplicate_batch_refused_even_after_depletion(self) -> None:
        reports = self._base() + [
            shipment(2, "S1", 2, "C", ("B0", 0, 10)),       # B0 清空
            receipt(3, "R2", 3, "B0", qty=2),               # 复用批号
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        self.assertTrue(any(c.reason == RefusalReason.DUPLICATE_BATCH
                            and c.event_id == "R2"
                            for c in res.contradictions))

    def test_double_consumption_refused(self) -> None:
        reports = self._base() + [
            shipment(2, "S1", 2, "C1", ("B0", 0, 4)),
            shipment(3, "S2", 3, "C2", ("B0", 3, 5)),       # 与 [0,4) 重叠
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        self.assertTrue(any(c.reason == RefusalReason.DOUBLE_CONSUMPTION
                            for c in res.contradictions))

    def test_double_consumption_inside_same_event_refused(self) -> None:
        reports = self._base() + [
            shipment(2, "S1", 2, "C", ("B0", 1, 3), ("B0", 2, 4)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        self.assertTrue(any(c.reason == RefusalReason.DOUBLE_CONSUMPTION
                            for c in res.contradictions))

    def test_segment_out_of_initial_range_refused(self) -> None:
        reports = self._base() + [
            shipment(2, "S1", 2, "C", ("B0", 8, 12))]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        self.assertTrue(any(c.reason == RefusalReason.OUT_OF_RANGE_SEGMENT
                            for c in res.contradictions))

    def test_consuming_from_later_batch_refused(self) -> None:
        # B0 由业务 1 建立，B1 由业务 3 建立；业务 2 的发运不得动用 B1
        reports = [
            receipt(1, "R1", 1, "B0", qty=10),
            shipment(2, "S1", 2, "C", ("B1", 0, 1)),
            receipt(3, "R2", 3, "B1", qty=2),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        self.assertTrue(any(c.reason == RefusalReason.MISSING_UPSTREAM_BATCH
                            or c.reason == RefusalReason.LATER_BATCH_SEQ
                            for c in res.contradictions))

    def test_transfer_mixed_material_refused(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", material="M1", wh="W1", qty=4),
            receipt(2, "R2", 2, "B1", material="M2", wh="W1", qty=4),
            transfer(3, "T1", 3, "B2", "W9",
                     ("B0", 0, 2), ("B1", 0, 2)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        self.assertTrue(any(c.reason == RefusalReason.WRONG_SOURCE_MATERIAL
                            and c.event_id == "T1"
                            for c in res.contradictions))

    def test_transfer_mixed_warehouse_refused(self) -> None:
        reports = [
            receipt(1, "R1", 1, "B0", material="M", wh="W1", qty=4),
            receipt(2, "R2", 2, "B1", material="M", wh="W2", qty=4),
            transfer(3, "T1", 3, "B2", "W9",
                     ("B0", 0, 2), ("B1", 0, 2)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        self.assertTrue(any(c.reason == RefusalReason.WRONG_SOURCE_WAREHOUSE
                            and c.event_id == "T1"
                            for c in res.contradictions))

    def test_duplicate_report_seq_refused(self) -> None:
        reports = self._base() + [
            shipment(5, "S1", 2, "C", ("B0", 0, 1)),
            shipment(5, "S2", 3, "D", ("B0", 1, 2)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        self.assertTrue(any(c.reason == RefusalReason.DUPLICATE_REPORT_SEQ
                            for c in res.contradictions))

    def test_inconsistent_event_header_refused(self) -> None:
        reports = self._base() + [
            shipment(5, "S1", 2, "C", ("B0", 0, 1)),
            shipment(6, "S1", 9, "C", ("B0", 0, 1)),   # 同事件业务序号变了
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 20, 20))
        self.assertTrue(res.refused)
        self.assertTrue(
            any(c.reason == RefusalReason.INCONSISTENT_EVENT_HEADER
                for c in res.contradictions))

    def test_nonpositive_qty_payload_refused(self) -> None:
        # 来源批 B0 合法可见；另一收货事件批量为 0，属账内结构性矛盾
        reports = [
            receipt(1, "R1", 1, "B0", qty=10),
            EventReport(2, "R2", 2, ReceiptPayload("BAD", "M", "W1", 0)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 9, 9))
        self.assertTrue(res.refused)
        self.assertTrue(any(c.reason == RefusalReason.INVALID_PAYLOAD
                            and c.event_id == "R2"
                            for c in res.contradictions))

    def test_old_report_versions_skip_physical_validation(self) -> None:
        # 旧版发运本身重复耗位，但被新版完整替换；旧版不得触发矛盾
        reports = [
            receipt(1, "R1", 1, "B0", qty=10),
            shipment(10, "S1", 2, "C", ("B0", 0, 4)),
            shipment(11, "S1", 2, "C", ("B0", 0, 4)),  # 同内容替换，仍只耗一次
            shipment(12, "S2", 3, "D", ("B0", 4, 6)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 6),), 9, 50))
        self.assertFalse(res.refused)
        self.assertEqual(res.total_recall_qty, 6)

    def test_records_outside_cutoff_skip_all_validation(self) -> None:
        # 报送号 5 在截止号外且与账内报送号重复：不可见即不构成矛盾
        reports = [
            receipt(5, "R1", 1, "B0", qty=4),
            shipment(5, "S1", 8, "C", ("B0", 0, 1)),   # 业务序号也超界
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 1),), 5, 50))
        self.assertFalse(res.refused)
        self.assertNotIn("S1", dict(res.adopted_reports))

    def test_revoked_receipt_frees_nothing_and_batch_still_unknown(self) -> None:
        # 收货被撤销后，同批号由另一收货事件重建：来源按新收货事件可见
        reports = [
            EventReport(1, "R1", 1, ReceiptPayload("B0", "M", "W1", 10)),
            EventReport(2, "R1", 1, None),
            EventReport(3, "R2", 2, ReceiptPayload("B0", "M", "W1", 3)),
        ]
        res = build_traceability_list(
            reports, TraceQuery("B0", (Interval(0, 2),), 9, 9))
        self.assertFalse(res.refused)
        # 批次来自 R2（初始范围 [0,3)），隔离 2 件
        self.assertEqual(res.receipt_evidence.event_id, "R2")
        self.assertEqual(res.isolation[0].quantity, 2)


class ScaleTest(unittest.TestCase):
    def test_trillion_sized_batch_uses_intervals_only(self) -> None:
        huge = 10 ** 12
        reports = [
            receipt(1, "R1", 1, "B0", qty=huge),
            transfer(2, "T1", 2, "B1", "W2",
                     ("B0", huge // 2, huge // 2 + 10)),
            shipment(3, "S1", 3, "C", ("B1", 3, 7)),
        ]
        start = time.perf_counter()
        res = build_traceability_list(
            reports,
            TraceQuery("B0", (Interval(huge // 2 + 3, huge // 2 + 7),),
                       9, 9))
        elapsed = time.perf_counter() - start
        self.assertFalse(res.refused)
        self.assertLess(elapsed, 2.0)
        # 命中 4 件全部已发运 => 召回 4，无隔离；内存中只有端点整数
        self.assertEqual(res.recall[0].quantity, 4)
        self.assertEqual(res.isolation, ())

    def test_many_events_remain_linear_in_interval_count(self) -> None:
        n = 5000
        reports = [receipt(1, "R1", 1, "B0", qty=2 * n)]
        for i in range(n):
            reports.append(shipment(
                100 + i, f"S{i}", 2 + i, "C", ("B0", 2 * i, 2 * i + 1)))
        start = time.perf_counter()
        res = build_traceability_list(
            reports,
            TraceQuery("B0", tuple(Interval(2 * i, 2 * i + 1)
                                   for i in range(n)), n + 5, 10 ** 9))
        elapsed = time.perf_counter() - start
        self.assertFalse(res.refused)
        self.assertEqual(res.total_recall_qty, n)
        self.assertLess(elapsed, 10.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
