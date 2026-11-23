"""更正包审包验收测试（手工可复核的小流水 + 关联闭包/版本/规模场景）。

运行： python3 -I /workspace/run_tests.py
"""

from __future__ import annotations

import time
import unittest

from wave_mrp import (
    AmendmentResult,
    Correction,
    CorrectionAcceptanceEngine,
    CorrectionPackage,
    EventReport,
    Interval,
    LotTraceabilityEngine,
    ProductionPayload,
    ReceiptPayload,
    RefusalReason,
    Segment,
    ShipmentPayload,
    TraceQuery,
    TransferPayload,
    TransferSegment,
    adjudicate_correction_packages,
)


def receipt(seq: int, event: str, biz: int, batch: str,
            material: str = "M", wh: str = "W1", qty: int = 10) -> EventReport:
    return EventReport(seq, event, biz,
                       ReceiptPayload(batch, material, wh, qty))


def transfer(seq: int, event: str, biz: int, target: str, wh: str,
             *sources: tuple[str, int, int]) -> EventReport:
    return EventReport(seq, event, biz, TransferPayload(
        target, wh,
        tuple(TransferSegment(b, Interval(s, e)) for b, s, e in sources)))


def production(seq: int, event: str, biz: int, out_batch: str,
               out_qty: int, *inputs: tuple[str, int, int],
               material: str = "M", wh: str = "W1") -> EventReport:
    return EventReport(seq, event, biz, ProductionPayload(
        out_batch, material, wh, out_qty,
        tuple(Segment(b, Interval(s, e)) for b, s, e in inputs)))


def shipment(seq: int, event: str, biz: int, consignee: str,
             *sources: tuple[str, int, int]) -> EventReport:
    return EventReport(seq, event, biz, ShipmentPayload(
        consignee, tuple(Segment(b, Interval(s, e)) for b, s, e in sources)))


def correction(event: str, observed: int, replacement: object) -> Correction:
    return Correction(event, observed, replacement)  # type: ignore[arg-type]


def pkg(package_id: str, *corrections: Correction) -> CorrectionPackage:
    return CorrectionPackage(package_id, tuple(corrections))


def base_history() -> list[EventReport]:
    """两张发运单各占 B0 一段；更正它们互换领用货段的基线账。"""
    return [
        receipt(100, "R1", 1, "B0", qty=10),
        shipment(110, "S1", 2, "CUST-A", ("B0", 0, 3)),
        shipment(120, "S2", 3, "CUST-B", ("B0", 3, 6)),
    ]


class HappyPathTest(unittest.TestCase):
    def test_accepted_package_emits_continuous_seqs_and_publish_cutoff(self) -> None:
        result = adjudicate_correction_packages(
            base_history(),
            [pkg("PKG-1",
                 correction("S1", 110, ShipmentPayload(
                     "CUST-A", (Segment("B0", Interval(3, 6)),))),
                 correction("S2", 120, ShipmentPayload(
                     "CUST-B", (Segment("B0", Interval(0, 3)),))))],
        )
        self.assertFalse(result.history_refused)
        self.assertEqual(len(result.package_results), 1)
        decision = result.package_results[0]
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.new_report_seqs, (121, 122))
        self.assertEqual([r.report_seq for r in result.new_reports], [121, 122])
        self.assertEqual(result.publish_cutoff, 122)
        # 业务序号与事件身份不变，只是报送版本替换
        first = result.new_reports[0]
        self.assertEqual((first.event_id, first.business_seq), ("S1", 2))
        self.assertEqual(result.new_reports[1].event_id, "S2")

    def test_identical_replacement_still_creates_new_report(self) -> None:
        same = ShipmentPayload("CUST-A", (Segment("B0", Interval(0, 3)),))
        result = adjudicate_correction_packages(
            base_history(), [pkg("PKG-1", correction("S1", 110, same))])
        self.assertTrue(result.package_results[0].accepted)
        self.assertEqual(result.package_results[0].new_report_seqs, (121,))
        self.assertEqual(result.publish_cutoff, 121)

    def test_original_list_not_mutated(self) -> None:
        history = base_history()
        before = list(history)
        adjudicate_correction_packages(
            history, [pkg("PKG-1", correction(
                "S1", 110, ShipmentPayload(
                    "CUST-A", (Segment("B0", Interval(6, 9)),))))])
        self.assertEqual(history, before)
        self.assertEqual(len(history), 3)

    def test_appended_ledger_reads_new_at_new_cutoff_old_at_old_cutoff(self) -> None:
        result = adjudicate_correction_packages(
            base_history(),
            [pkg("PKG-1", correction(
                "S1", 110, ShipmentPayload(
                    "CUST-A", (Segment("B0", Interval(6, 9)),))))],
        )
        extended = list(base_history()) + list(result.new_reports)
        # 旧截止号仍读到旧账：S1 采用 110，发的是 B0[0,3)
        old = LotTraceabilityEngine(extended).build_traceability_list(
            TraceQuery("B0", (Interval(0, 3),), 9, 120))
        self.assertFalse(old.refused)
        self.assertEqual(dict(old.adopted_reports)["S1"], 110)
        # 新截止号读到新账：S1 采用 121，发的是 B0[6,9)
        new = LotTraceabilityEngine(extended).build_traceability_list(
            TraceQuery("B0", (Interval(6, 9),), 9, result.publish_cutoff))
        self.assertFalse(new.refused)
        self.assertEqual(dict(new.adopted_reports)["S1"], 121)
        self.assertEqual(new.recall[0].intervals, (Interval(6, 9),))


class StructuralRefusalTest(unittest.TestCase):
    def test_empty_package_refused(self) -> None:
        result = adjudicate_correction_packages(
            base_history(), [pkg("EMPTY")])
        decision = result.package_results[0]
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.contradictions[0].reason,
                         RefusalReason.EMPTY_CORRECTION_PACKAGE)
        self.assertEqual(result.publish_cutoff, 120)     # 无新增，仍是历史最大号
        self.assertEqual(result.new_reports, ())

    def test_duplicate_target_in_package_refused(self) -> None:
        result = adjudicate_correction_packages(
            base_history(),
            [pkg("PKG-1",
                 correction("S1", 110, ShipmentPayload(
                     "CUST-A", (Segment("B0", Interval(6, 7)),))),
                 correction("S1", 110, ShipmentPayload(
                     "CUST-A", (Segment("B0", Interval(7, 8)),))))],
        )
        decision = result.package_results[0]
        self.assertFalse(decision.accepted)
        reasons = {c.reason for c in decision.contradictions}
        self.assertIn(RefusalReason.DUPLICATE_CORRECTION_TARGET, reasons)
        self.assertTrue(any(c.event_id == "S1"
                            for c in decision.contradictions))
        self.assertEqual(result.publish_cutoff, 120)

    def test_unknown_event_refused(self) -> None:
        result = adjudicate_correction_packages(
            base_history(),
            [pkg("PKG-1", correction(
                "GHOST", 1, ShipmentPayload(
                    "C", (Segment("B0", Interval(0, 1)),))))],
        )
        decision = result.package_results[0]
        self.assertFalse(decision.accepted)
        contradiction = next(
            c for c in decision.contradictions
            if c.reason == RefusalReason.UNKNOWN_CORRECTION_EVENT)
        self.assertEqual(contradiction.event_id, "GHOST")

    def test_stale_observed_version_refused(self) -> None:
        result = adjudicate_correction_packages(
            base_history(),
            [pkg("PKG-1", correction(
                "S1", 99, ShipmentPayload(
                    "CUST-A", (Segment("B0", Interval(6, 7)),))))],
        )
        decision = result.package_results[0]
        self.assertFalse(decision.accepted)
        contradiction = next(
            c for c in decision.contradictions
            if c.reason == RefusalReason.STALE_CORRECTION_VERSION)
        self.assertEqual(contradiction.event_id, "S1")

    def test_stale_check_uses_current_version_after_earlier_accepted_package(self) -> None:
        engine = CorrectionAcceptanceEngine(base_history())
        first = engine.adjudicate([pkg(
            "PKG-1", correction("S1", 110, ShipmentPayload(
                "CUST-A", (Segment("B0", Interval(6, 9)),))))])
        self.assertTrue(first.package_results[0].accepted)
        # 第二包仍引用旧序号 110 => 现版已是 121，过期拒绝
        second = engine.adjudicate([pkg(
            "PKG-2", correction("S1", 110, ShipmentPayload(
                "CUST-A", (Segment("B0", Interval(0, 1)),))))])
        self.assertFalse(second.package_results[0].accepted)
        self.assertIn(RefusalReason.STALE_CORRECTION_VERSION,
                      {c.reason for c in second.package_results[0].contradictions})
        # 引用新现版 121 即可继续裁定
        third = engine.adjudicate([pkg(
            "PKG-3", correction("S1", 121, ShipmentPayload(
                "CUST-A", (Segment("B0", Interval(0, 1)),))))])
        self.assertTrue(third.package_results[0].accepted)
        self.assertEqual(third.publish_cutoff, 122)


class PhysicalRuleTest(unittest.TestCase):
    def test_swap_in_one_package_takes_effect_simultaneously(self) -> None:
        # S1、S2 互换货段：拆成两包各自都会与对方现版重复占货，
        # 一包内同时生效则整份账合法。
        result = adjudicate_correction_packages(
            base_history(),
            [pkg("PKG-1",
                 correction("S1", 110, ShipmentPayload(
                     "CUST-A", (Segment("B0", Interval(3, 6)),))),
                 correction("S2", 120, ShipmentPayload(
                     "CUST-B", (Segment("B0", Interval(0, 3)),))))],
        )
        self.assertTrue(result.package_results[0].accepted)
        self.assertEqual(result.publish_cutoff, 122)

    def test_half_swap_refused_for_double_consumption(self) -> None:
        # 只改 S1 去占 S2 的货段，S2 现版仍占 [3,6) => 重复耗用
        result = adjudicate_correction_packages(
            base_history(),
            [pkg("PKG-1", correction(
                "S1", 110, ShipmentPayload(
                    "CUST-A", (Segment("B0", Interval(3, 6)),))))],
        )
        decision = result.package_results[0]
        self.assertFalse(decision.accepted)
        # 子账按业务序号重放：新版 S1 先占 [3,6)，旧 S2 再取同段即重复耗用，
        # 矛盾定位到具体事件（S2）与批次（B0）即满足定位要求。
        self.assertTrue(any(
            c.reason == RefusalReason.DOUBLE_CONSUMPTION
            and c.event_id in ("S1", "S2")
            and "B0" in c.detail
            for c in decision.contradictions))
        # 拒绝不占报送号、不改现版
        self.assertEqual(result.publish_cutoff, 120)

    def test_refused_package_does_not_change_current_version(self) -> None:
        engine = CorrectionAcceptanceEngine(base_history())
        result = engine.adjudicate([pkg(
            "PKG-1", correction("S1", 110, ShipmentPayload(
                "CUST-A", (Segment("B0", Interval(3, 6)),))))])  # 与 S2 冲突
        self.assertFalse(result.package_results[0].accepted)
        # 后续包仍按旧现版 110 校验版本，且 S2 仍占 [3,6)
        follow = engine.adjudicate([pkg(
            "PKG-2", correction("S1", 110, ShipmentPayload(
                "CUST-A", (Segment("B0", Interval(6, 9)),))))])
        self.assertTrue(follow.package_results[0].accepted)

    def test_transfer_shortened_broke_legacy_shipment_refused(self) -> None:
        history = [
            receipt(100, "R1", 1, "B0", qty=10),
            transfer(110, "T1", 2, "B1", "W2", ("B0", 0, 6)),
            shipment(120, "S1", 3, "CUST-A", ("B1", 2, 5)),
        ]
        # 调拨缩短为只搬 2 件（B1 只剩 [0,2)），旧发运仍引用 B1[2,5)
        result = adjudicate_correction_packages(
            history,
            [pkg("PKG-1", correction(
                "T1", 110, TransferPayload(
                    "B1", "W2",
                    (TransferSegment("B0", Interval(0, 2)),))))],
        )
        decision = result.package_results[0]
        self.assertFalse(decision.accepted)
        self.assertTrue(any(
            c.reason == RefusalReason.OUT_OF_RANGE_SEGMENT
            and c.event_id == "S1"
            and "B1" in c.detail
            for c in decision.contradictions))

    def test_revocation_is_a_version_and_revive_must_reference_it(self) -> None:
        engine = CorrectionAcceptanceEngine(base_history())
        # 撤销 S1：新报送是一条 payload=None
        revoked = engine.adjudicate(
            [pkg("PKG-1", correction("S1", 110, None))])
        self.assertTrue(revoked.package_results[0].accepted)
        self.assertIsNone(revoked.new_reports[0].payload)
        # 恢复时引用旧序号 110 => 过期（现版是撤销报送 121）
        stale_restore = engine.adjudicate([pkg(
            "PKG-2", correction("S1", 110, ShipmentPayload(
                "CUST-A", (Segment("B0", Interval(0, 3)),))))])
        self.assertFalse(stale_restore.package_results[0].accepted)
        # 引用撤销报送序号 121 即可恢复
        revived = engine.adjudicate([pkg(
            "PKG-3", correction("S1", 121, ShipmentPayload(
                "CUST-A", (Segment("B0", Interval(0, 3)),))))])
        self.assertTrue(revived.package_results[0].accepted)
        self.assertEqual(revived.publish_cutoff, 122)

    def test_revoke_receipt_still_referenced_by_shipment_refused(self) -> None:
        # 撤掉收货 R1，旧发运引用的 B0 随之消失 => 闭包必须带上旧关系
        result = adjudicate_correction_packages(
            base_history(), [pkg("PKG-1", correction("R1", 100, None))])
        decision = result.package_results[0]
        self.assertFalse(decision.accepted)
        self.assertTrue(any(
            c.reason == RefusalReason.MISSING_UPSTREAM_BATCH
            and c.event_id in ("S1", "S2")
            and "B0" in c.detail
            for c in decision.contradictions))


class SequencingTest(unittest.TestCase):
    def test_multiple_packages_continue_seq_and_results_follow_input_order(self) -> None:
        engine = CorrectionAcceptanceEngine(base_history())
        result = engine.adjudicate([
            pkg("PKG-A", correction(
                "S1", 110, ShipmentPayload(
                    "CUST-A", (Segment("B0", Interval(6, 9)),)))),
            pkg("PKG-B", correction(
                "S2", 120, ShipmentPayload(
                    "CUST-B", (Segment("B0", Interval(0, 3)),)))),
        ])
        self.assertEqual([d.package_id for d in result.package_results],
                         ["PKG-A", "PKG-B"])
        self.assertTrue(all(d.accepted for d in result.package_results))
        self.assertEqual(result.package_results[0].new_report_seqs, (121,))
        self.assertEqual(result.package_results[1].new_report_seqs, (122,))
        self.assertEqual(result.publish_cutoff, 122)

    def test_refused_package_consumes_no_seq_between_accepted_packages(self) -> None:
        engine = CorrectionAcceptanceEngine(base_history())
        result = engine.adjudicate([
            pkg("PKG-A", correction(
                "S1", 110, ShipmentPayload(
                    "CUST-A", (Segment("B0", Interval(6, 9)),)))),
            pkg("PKG-BAD", correction(
                "S2", 120, ShipmentPayload(
                    "CUST-B", (Segment("B0", Interval(6, 9)),)))),  # 撞 S1 新货段
            pkg("PKG-C", correction(
                "S2", 120, ShipmentPayload(
                    "CUST-B", (Segment("B0", Interval(0, 3)),)))),
        ])
        accepted, refused = result.accepted_packages, result.refused_packages
        self.assertEqual([d.package_id for d in accepted], ["PKG-A", "PKG-C"])
        self.assertEqual([d.package_id for d in refused], ["PKG-BAD"])
        # 拒绝包不占号：两包接纳报送号连续 121、122，截止号不落在半包上
        self.assertEqual(result.new_reports[0].report_seq, 121)
        self.assertEqual(result.new_reports[1].report_seq, 122)
        self.assertEqual(result.publish_cutoff, 122)

    def test_empty_history_starts_at_zero(self) -> None:
        result = adjudicate_correction_packages([], [pkg("EMPTY")])
        self.assertFalse(result.history_refused)
        self.assertEqual(result.publish_cutoff, 0)
        self.assertFalse(result.package_results[0].accepted)
        self.assertEqual(result.package_results[0].contradictions[0].reason,
                         RefusalReason.EMPTY_CORRECTION_PACKAGE)


class IncrementalClosureTest(unittest.TestCase):
    def _long_history(self, touched_events: int, untouched_events: int
                      ) -> list[EventReport]:
        reports: list[EventReport] = [
            receipt(1, "TOUCH-BATCH", 1, "TB", qty=1_000_000)]
        seq, biz = 1, 1
        # 与本包相连的流水：都引用 TB
        for i in range(touched_events):
            seq += 1
            biz += 1
            reports.append(shipment(
                seq, f"T{i}", biz, "C",
                ("TB", 10 * i, 10 * i + 5)))
        # 与本包无关的流水：独立批次 UB，保留多年
        seq += 1
        biz += 1
        reports.append(receipt(seq, "UNTOUCH-RECEIPT", biz, "UB", qty=10 ** 9))
        for i in range(untouched_events):
            seq += 1
            biz += 1
            reports.append(shipment(
                seq, f"U{i}", biz, "C",
                ("UB", 1000 * i, 1000 * i + 10)))
        return reports

    def test_unrelated_years_of_flow_not_reprocessed(self) -> None:
        # 用 monkeypatch 计数重放入口：每个包的子账重放事件数只随关联
        # 闭包增长，与无关货批的流水条数无关。
        from wave_mrp import amendment_engine

        history = self._long_history(touched_events=20, untouched_events=4000)
        engine = CorrectionAcceptanceEngine(history)
        replayed_sizes: list[int] = []
        original = engine._selector.replay_physical_ledger

        def counting_replay(events):  # type: ignore[no-untyped-def]
            replayed_sizes.append(len(events))
            return original(events)

        engine._selector.replay_physical_ledger = counting_replay  # type: ignore[method-assign]
        replayed_sizes.clear()  # 去掉 bootstrap 的全账重放
        result = engine.adjudicate([pkg(
            "PKG-1",
            correction("TOUCH-BATCH", 1,
                       ReceiptPayload("TB", "M", "W1", 2_000_000)))])
        self.assertTrue(result.package_results[0].accepted)
        # 子账只含 TB 收货 + 20 个相关发运，4000 条 UB 流水一条都不进
        self.assertEqual(replayed_sizes, [21])

    def test_removed_and_added_relations_both_seed_closure(self) -> None:
        # T1 旧版引用 B0；新版改引用 B2。两个批次上的下游都必须进闭包：
        # B0 上的旧发运 S-OLD（撤下去的旧关系），B2 上的现有发运 S-NEW
        # （加进来的新关系，检查重复占用）。
        history = [
            receipt(100, "R0", 1, "B0", qty=10),
            receipt(101, "R2", 2, "B2", qty=10),
            shipment(110, "T1", 3, "C1", ("B0", 0, 2)),
            shipment(120, "S-OLD", 4, "C2", ("B0", 2, 4)),
            shipment(130, "S-NEW", 5, "C3", ("B2", 0, 4)),
        ]
        # 新版 T1 占用 B2[0,2)，与 S-NEW 的 B2[0,4) 冲突 => 必须被闭包发现
        result = adjudicate_correction_packages(
            history,
            [pkg("PKG-1", correction(
                "T1", 110, ShipmentPayload(
                    "C1", (Segment("B2", Interval(0, 2)),))))],
        )
        decision = result.package_results[0]
        self.assertFalse(decision.accepted)
        # 重放按业务序：S-NEW 先占 B2[0,4)，新版 T1 再取 B2[0,2) 即重复耗用；
        # 矛盾由加进来的新关系（B2）上的闭包事件发现，定位到 B2 即达标。
        self.assertTrue(any(
            c.reason == RefusalReason.DOUBLE_CONSUMPTION
            and c.event_id in ("T1", "S-NEW")
            and "B2" in c.detail
            for c in decision.contradictions))

    def test_scale_intervals_not_expanded_per_piece(self) -> None:
        huge = 10 ** 12
        history = [
            receipt(100, "R1", 1, "B0", qty=huge),
            shipment(110, "S1", 2, "C", ("B0", 0, huge // 2)),
        ]
        start = time.perf_counter()
        result = adjudicate_correction_packages(
            history,
            [pkg("PKG-1", correction(
                "S1", 110, ShipmentPayload(
                    "C", (Segment("B0", Interval(huge // 2, huge)),))))],
        )
        elapsed = time.perf_counter() - start
        self.assertTrue(result.package_results[0].accepted)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(result.publish_cutoff, 111)


class HistoryContradictionTest(unittest.TestCase):
    def test_contradictory_history_blocks_all_adjudication(self) -> None:
        history = [
            receipt(100, "R1", 1, "B0", qty=10),
            shipment(110, "S1", 2, "C1", ("B0", 0, 4)),
            shipment(120, "S2", 3, "C2", ("B0", 3, 6)),   # 与 S1 重复耗位
        ]
        result = adjudicate_correction_packages(
            history,
            [pkg("PKG-1", correction(
                "S1", 110, ShipmentPayload(
                    "C1", (Segment("B0", Interval(6, 9)),))))],
        )
        self.assertIsInstance(result, AmendmentResult)
        self.assertTrue(result.history_refused)
        self.assertEqual(result.package_results, ())
        self.assertEqual(result.new_reports, ())
        self.assertEqual(result.publish_cutoff, 120)
        self.assertTrue(any(
            c.reason == RefusalReason.DOUBLE_CONSUMPTION
            for c in result.history_contradictions))

    def test_duplicate_report_seq_in_history_refused(self) -> None:
        history = [
            receipt(5, "R1", 1, "B0", qty=4),
            shipment(5, "S1", 2, "C", ("B0", 0, 1)),
        ]
        result = adjudicate_correction_packages(
            history, [pkg("PKG-1", correction(
                "R1", 5, ReceiptPayload("B0", "M", "W1", 5)))])
        self.assertTrue(result.history_refused)
        self.assertTrue(any(
            c.reason == RefusalReason.DUPLICATE_REPORT_SEQ
            for c in result.history_contradictions))


if __name__ == "__main__":
    unittest.main(verbosity=2)
