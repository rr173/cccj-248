"""更正包审包手算演示：把多份更正包审成一份可发布的补报。

运行： python3 -I /workspace/amendment_demo.py

场景（整数计量，批内位置从 0 起、不随出库重编号）：
  收货 R1 建批 B0（10 件，物料 M，仓点 W1）；
  发运 S1 领用 B0[0,3) 发给 CUST-A，发运 S2 领用 B0[3,6) 发给 CUST-B。
收单员依次收到三个更正包：
  PKG-1 只把 S1 改到 B0[3,6) —— S2 现版仍占该段，单改一张即重复占货，
        整包拒绝，不占报送号、不改任何现版；
  PKG-2 在同一包内同时改 S1、S2 互换领用货段 —— 更正同时生效，整份账
        仍满足实物规则，接纳并取得连续报送号 121、122；
  PKG-3 引用旧序号 110 改 S1 —— S1 现版已是 121，制单版本过期，拒绝；
        随后按正确现版 121 重提即接纳（报送号 123）。
最后把新增报送追加到历史后，用发布截止号 123 读新账、用原截止号 120
读旧账，验证两份账各自完整可读。
"""

import sys

sys.path.insert(0, "/workspace")

from wave_mrp import (
    Correction,
    CorrectionAcceptanceEngine,
    CorrectionPackage,
    EventReport,
    Interval,
    LotTraceabilityEngine,
    ReceiptPayload,
    Segment,
    ShipmentPayload,
    TraceQuery,
)


def build_history() -> list[EventReport]:
    return [
        EventReport(100, "R1", 1, ReceiptPayload("B0", "M", "W1", 10)),
        EventReport(110, "S1", 2, ShipmentPayload(
            "CUST-A", (Segment("B0", Interval(0, 3)),))),
        EventReport(120, "S2", 3, ShipmentPayload(
            "CUST-B", (Segment("B0", Interval(3, 6)),))),
    ]


def ship(consignee: str, start: int, end: int) -> ShipmentPayload:
    return ShipmentPayload(consignee, (Segment("B0", Interval(start, end)),))


def show(title: str, result) -> None:
    print(f"== {title} ==")
    for decision in result.package_results:
        verdict = "接纳" if decision.accepted else "拒绝"
        print(f"  {decision.package_id}: {verdict}")
        if decision.accepted:
            for report in decision.new_reports:
                print(f"    新报送 seq={report.report_seq} "
                      f"event={report.event_id} 业务序号={report.business_seq}")
        for contradiction in decision.contradictions:
            print(f"    原因={contradiction.reason.value} "
                  f"事件={contradiction.event_id} {contradiction.detail}")
    print(f"  发布截止号: {result.publish_cutoff}")
    print()


def main() -> None:
    history = build_history()
    engine = CorrectionAcceptanceEngine(history)

    # 第一批：单改 S1 去占 S2 的货段 —— 重复占货，拒绝
    batch_one = engine.adjudicate([CorrectionPackage(
        "PKG-1", (Correction("S1", 110, ship("CUST-A", 3, 6)),))])
    show("单改一张发运单（重复占货）", batch_one)

    # 第二批：一包内 S1、S2 互换货段，同时生效 —— 接纳
    batch_two = engine.adjudicate([CorrectionPackage("PKG-2", (
        Correction("S1", 110, ship("CUST-A", 3, 6)),
        Correction("S2", 120, ship("CUST-B", 0, 3)),
    ))])
    show("同包互换两张发运单的领用货段", batch_two)

    # 第三批：版本过期先拒绝，按新现版重提再接纳
    stale = engine.adjudicate([CorrectionPackage(
        "PKG-3-STALE", (Correction("S1", 110, ship("CUST-A", 6, 9)),))])
    show("引用已过期的制单版本", stale)

    fresh = engine.adjudicate([CorrectionPackage(
        "PKG-3", (Correction("S1", 121, ship("CUST-A", 6, 9)),))])
    show("按现版序号重提同一更正", fresh)

    # 追加补报到历史之后：新截止号读新账，原截止号读旧账
    extended = history + list(fresh.new_reports)
    old_ledger = LotTraceabilityEngine(extended).build_traceability_list(
        TraceQuery("B0", (Interval(0, 9),), 9, 120))
    new_ledger = LotTraceabilityEngine(extended).build_traceability_list(
        TraceQuery("B0", (Interval(0, 9),), 9, fresh.publish_cutoff))
    print("== 追加补报后的两份账 ==")
    print("  原截止号 120 采用的 S1 报送:",
          dict(old_ledger.adopted_reports)["S1"])
    print("  新截止号", fresh.publish_cutoff, "采用的 S1 报送:",
          dict(new_ledger.adopted_reports)["S1"])
    print("  新账 S1 实际发出区间:",
          next(r for r in new_ledger.recall
               if r.shipment_event_id == "S1").intervals)


if __name__ == "__main__":
    main()
