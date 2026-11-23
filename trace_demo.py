"""来料抽检追溯手算演示：隔离 + 召回清单及可回查依据。

运行： python3 -I /workspace/trace_demo.py

场景（整数计量，批内位置从 0 起、不随出库重编号）：
  收货 R1 建批 B0（10 件，物料 M，仓点 W1），抽检圈出 B0 的 [2,5) 与 [4,6)
  （重叠，合并为命中区间 [2,6)，共 4 件）。
  调拨 T1 把 B0 的 [1,3)、[7,9) 按单序拼成 W2 新批 B1（共 4 件）；
  生产 P1 投入 B1 的 [0,2)（其中目标位置 1..2 来自 B0 的 2..3，命中），
       实报产出 B2 共 4 件——投入任一位置受影响，产出整批受影响；
  发运 S1 把 B2 的 [0,3) 发给客户 CUST-A；
  发运 S2 把 B1 的 [2,4)（来自 B0 的 [7,9)，未命中）发给 CUST-B，无召回；
  撤销事件 X1 原本要再发运 B0 的 [5,6)，其最后版本是撤销，故不发生；
  业务 F1 在业务截止号之外，重查时不可见。
报送采用多版本：S2 有两版完整替换报送，截止号固定时只认最后可见版本。
"""

import sys

sys.path.insert(0, "/workspace")

from wave_mrp import (
    EventReport,
    Interval,
    LotTraceabilityEngine,
    ProductionPayload,
    ReceiptPayload,
    Segment,
    ShipmentPayload,
    TraceQuery,
    TransferPayload,
    TransferSegment,
)


def build_reports() -> list[EventReport]:
    return [
        # 收货：B0 = 10 件 M @ W1
        EventReport(100, "R1", 1, ReceiptPayload("B0", "M", "W1", 10)),
        # 调拨：B0[1,3)+B0[7,9) -> B1 @ W2，B1 共 4 件
        EventReport(110, "T1", 2, TransferPayload(
            "B1", "W2",
            (TransferSegment("B0", Interval(1, 3)),
             TransferSegment("B0", Interval(7, 9))))),
        # 生产：投 B1[0,2)，实报产出 B2 4 件（产量与投入量不必相等）
        EventReport(120, "P1", 3, ProductionPayload(
            "B2", "M", "W2", 4,
            (Segment("B1", Interval(0, 2)),))),
        # 发运 S1：B2[0,3) -> CUST-A（B2 整批受影响，召回 3 件）
        EventReport(130, "S1", 4, ShipmentPayload(
            "CUST-A", (Segment("B2", Interval(0, 3)),))),
        # 发运 S2 第一版：耗用 B1[1,4)（其中 B1[1,2) 已被 P1 消耗 => 若此版
        # 生效将构成重复耗用；第二版改为合法的 B1[2,4) 完整替换）
        EventReport(140, "S2", 5, ShipmentPayload(
            "CUST-B", (Segment("B1", Interval(1, 4)),))),
        # S2 第二版（完整替换）：改为 B1[2,4)；同位置不会二次消耗
        EventReport(141, "S2", 5, ShipmentPayload(
            "CUST-B", (Segment("B1", Interval(2, 4)),))),
        # X1：先报送一次发运，随后撤销（撤销是最后可见版本 => 事件不发生）
        EventReport(150, "X1", 6, ShipmentPayload(
            "CUST-X", (Segment("B0", Interval(5, 6)),))),
        EventReport(151, "X1", 6, None),
        # F1：业务序号 9 在业务截止号 6 之外，任何截止账内都不可见
        EventReport(160, "F1", 9, ShipmentPayload(
            "CUST-F", (Segment("B0", Interval(2, 3)),))),
    ]


def render(result) -> None:
    print(f"截止号: 业务<={result.business_cutoff} 报送<={result.report_cutoff} "
          f"结论={'拒绝' if result.refused else '出具清单'}")
    if result.refused:
        for c in result.contradictions:
            print(f"  拒绝[{c.reason.value}] 事件={c.event_id}: {c.detail}")
        return

    print("\n-- 隔离清单（在库，按仓点/物料/当前批次）--")
    for row in result.isolation:
        ivs = ", ".join(str(i) for i in row.intervals)
        print(f"  {row.warehouse} | {row.material} | {row.batch} | "
              f"区间={ivs} | 数量={row.quantity}")
    print(f"  隔离合计={result.total_isolation_qty}")

    print("\n-- 召回清单（发出，按收货方/发运事件/发货批次）--")
    for row in result.recall:
        ivs = ", ".join(str(i) for i in row.intervals)
        print(f"  {row.consignee} | {row.shipment_event_id} | {row.material} | "
              f"{row.batch} | 区间={ivs} | 数量={row.quantity}")
    print(f"  召回合计={result.total_recall_qty}")

    print("\n-- 可回查的直接依据（一跳，不展开全路径）--")
    mark = result.receipt_evidence
    print(f"  收货命中: 事件{mark.event_id}@报送{mark.report_seq} 批{mark.batch} "
          f"区间={[str(i) for i in mark.marked_intervals]}")
    for ev in result.transfer_evidence:
        for m in ev.mappings:
            print(f"  调拨{ev.event_id}@报送{ev.report_seq}: "
                  f"{m.source_batch}{m.source_interval} -> "
                  f"{m.target_batch}{m.target_interval}")
    for ev in result.production_evidence:
        hits = ", ".join(f"{s.batch}{s.interval}" for s in ev.hit_inputs)
        print(f"  生产{ev.event_id}@报送{ev.report_seq}: 命中投入[{hits}] "
              f"=> 产出 {ev.output_batch}{ev.output_interval}（整批受影响）")
    print("\n  采用的报送版本（撤销事件版本号为 None）:")
    for eid, rseq in result.adopted_reports:
        print(f"    {eid}: report_seq={rseq}")


def main() -> None:
    engine = LotTraceabilityEngine(build_reports())
    query = TraceQuery(
        receipt_batch="B0",
        marked_intervals=(Interval(2, 5), Interval(4, 6)),  # 重叠圈选
        business_cutoff=6,
        report_cutoff=350,
    )
    result = engine.build_traceability_list(query)
    render(result)

    print("\n--- 手工核验 ---")
    print("命中 B0[2,6) 共 4 件，去向：")
    print("  T1 只带走 B0[2,3)（映射为 B1[1,2)）；B0[3,6) 从未出库，留 W1")
    print("  P1 投 B1[0,2) 与受影响 B1[1,2) 相交 => B2 整批 4 件受影响")
    print("  S1 发 B2[0,3) => 召回 3；B2[3,4) 尚存 W2 => 隔离 1")
    print("  S2 发 B1[2,4)，源是 B0[7,9) 未命中 => 无召回；B1 受影响的 [1,2)")
    print("  已被 P1 消耗，B1 不出隔离行（中间批不误计为待处理货物）")
    print("  B0[3,6) 共 3 件在库隔离；X1 已撤销、F1 超业务截止号，均不发生")

    # 固定截止号重查：后来的报送更正不能改变旧清单
    again = engine.build_traceability_list(query)
    same = (again.isolation == result.isolation
            and again.recall == result.recall)
    print(f"\n固定截止号重查结果一致: {same}")

    # 报送截止号降到 140：S2 只看得见第一版（耗用 B1[2,5)）
    old_query = TraceQuery(
        receipt_batch="B0",
        marked_intervals=(Interval(2, 5), Interval(4, 6)),
        business_cutoff=6,
        report_cutoff=140,
    )
    old = engine.build_traceability_list(old_query)
    print("\n--- 报送截止号=140（S2 仅第一版可见；X1 无任何可见报送，事件不存在）---")
    render(old)
    print("\n说明：旧截止账里 S2 第一版要发 B1[1,4)，与 P1 已耗的 B1[1,2) 冲突，")
    print("按最后可见版本做实物校验即构成重复耗用，整单拒绝；这正体现旧报送不参与")
    print("新账校验、而每个截止账各自自洽的原则。X1 报送全在截止号外，事件不生效。")


if __name__ == "__main__":
    main()
