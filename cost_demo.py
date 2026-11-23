"""存货成本结账手算演示：期初 / 期内增减 / 期末，含拆单余分与调拨。

运行： python3 -I /workspace/cost_demo.py

场景（整数分，批内位置从 0 起、不随出库重编号）：
  业务 1  收货 R1：B0 共 4 件、整批入账 2 分（按 floor(C*b/Q)-floor(C*a/Q)
          分位置，位置价值依次为 0、1、0、1）。
  业务 2  收货 R2：B1 共 10 件、入账 10 分。
  业务 3  调拨 T1：把 B0 的 [1,2) 接在 [2,3) 之前拼成 W2 的 B2；
          目标第 1 件仍值 1 分、第 2 件值 0 分（不重新摊平）。
  业务 4  生产 P1：投 B1[0,4)（值 4 分），加工金额 6 分，产出 B3 共 2 件
          （产出批成本 = 4 + 6 = 10 分，与投入/产出数量是否相等无关）。
  业务 5  发运 S1：发 B2[0,1)，减记 1 分（发运减记就是这笔发出成本）。
  业务 6  发运 S2：发 B0[0,1)，减记 0 分（零元发运也要按实际位置登记）。

结账口径：期初业务截止号 2（业务 1、2 进期初），期末业务截止号 6，
报送截止号 500。期内业务 3..6 逐笔列出。
"""

import sys

sys.path.insert(0, "/workspace")

from wave_mrp import (
    ClosingQuery,
    CostTable,
    EventReport,
    Interval,
    ProductionCost,
    ProductionPayload,
    ReceiptCost,
    ReceiptPayload,
    Segment,
    ShipmentPayload,
    TransferPayload,
    TransferSegment,
    close_inventory_costs,
)


def build_reports() -> list[EventReport]:
    return [
        EventReport(100, "R1", 1, ReceiptPayload("B0", "M", "W1", 4)),
        EventReport(101, "R2", 2, ReceiptPayload("B1", "M", "W1", 10)),
        EventReport(110, "T1", 3, TransferPayload(
            "B2", "W2",
            (TransferSegment("B0", Interval(1, 2)),
             TransferSegment("B0", Interval(2, 3))))),
        EventReport(120, "P1", 4, ProductionPayload(
            "B3", "M", "W2", 2,
            (Segment("B1", Interval(0, 4)),))),
        EventReport(130, "S1", 5, ShipmentPayload(
            "CUST-A", (Segment("B2", Interval(0, 1)),))),
        EventReport(140, "S2", 6, ShipmentPayload(
            "CUST-B", (Segment("B0", Interval(0, 1)),))),
    ]


def build_costs() -> CostTable:
    return CostTable(
        receipt_costs=(ReceiptCost("R1", 100, 2), ReceiptCost("R2", 101, 10)),
        production_costs=(ProductionCost("P1", 120, 6),),
    )


def render(stmt) -> None:
    print(f"期初业务截止号={stmt.opening_business_cutoff} "
          f"期末业务截止号={stmt.closing_business_cutoff} "
          f"报送截止号={stmt.report_cutoff} "
          f"结论={'拒绝' if stmt.refused else '出具结账单'}")
    if stmt.refused:
        for c in stmt.contradictions:
            print(f"  拒绝[{c.reason.value}] 事件={c.event_id} "
                  f"报送={c.report_seq}: {c.detail}")
        return

    print("\n-- 期初余额（仓点 | 物料 | 批次 | 数量 | 金额分）--")
    for row in stmt.opening:
        print(f"  {row.warehouse} | {row.material} | {row.batch} | "
              f"{row.quantity} | {row.amount}")
    print(f"  期初合计金额={stmt.total_opening_amount}")

    print("\n-- 期内增减（业务顺序；调拨组内金额之和为零）--")
    for m in stmt.movements:
        group = f" [调拨组 {m.transfer_group}]" if m.transfer_group else ""
        print(f"  业务{m.business_seq} {m.kind:<10} {m.event_id}"
              f"@报送{m.report_seq} | {m.warehouse} | {m.material} | "
              f"{m.batch} | 数量{m.quantity:+d} | 金额{m.amount:+d}分{group}")
    print(f"  期内合计金额={stmt.total_movement_amount}")

    print("\n-- 期末余额 --")
    for row in stmt.closing:
        print(f"  {row.warehouse} | {row.material} | {row.batch} | "
              f"{row.quantity} | {row.amount}")
    print(f"  期末合计金额={stmt.total_closing_amount}")

    print("\n-- 逐批核对：期初 + 期内 = 期末 --")
    keys = sorted({(r.warehouse, r.material, r.batch) for r in stmt.closing}
                  | {(r.warehouse, r.material, r.batch) for r in stmt.opening})
    for key in keys:
        oq, oa = next(((r.quantity, r.amount) for r in stmt.opening
                       if (r.warehouse, r.material, r.batch) == key), (0, 0))
        moves = [m for m in stmt.movements
                 if (m.warehouse, m.material, m.batch) == key]
        mq, ma = sum(m.quantity for m in moves), sum(m.amount for m in moves)
        row = stmt.balance(*key)
        cq, ca = (row.quantity, row.amount) if row else (0, 0)
        ok = (oq + mq, oa + ma) == (cq, ca)
        print(f"  {key}: {oq}+{mq}={cq} 件, {oa}+{ma}={ca} 分"
              f" {'OK' if ok else 'MISMATCH'}")

    print("\n-- 业务核对 --")
    if not stmt.movements:
        print("  期内没有任何可见生效业务，无调拨/生产可核对")
    else:
        t1 = [m.amount for m in stmt.movements if m.event_id == "T1"]
        print(f"  T1 调拨增减金额之和 = {sum(t1)}（应为 0）")
        p1_net = sum(m.amount for m in stmt.movements if m.event_id == "P1")
        print(f"  P1 生产净增金额 = {p1_net}（应等于加工金额 6 分）")


def main() -> None:
    stmt = close_inventory_costs(
        build_reports(), build_costs(), ClosingQuery(2, 6, 500))
    render(stmt)

    print("\n--- 手工核验 ---")
    print("  B0 4 件 2 分：位置价值 0,1,0,1；期初 B0 完整 4 件 2 分")
    print("  T1 只转 [1,2)+[2,3)（值 1+0=1 分），B2 目标第 1 件 1 分、"
          "第 2 件 0 分")
    print("  P1 投 B1[0,4) 值 4 分 + 加工 6 分 = B3 两件共 10 分"
          "（每件 5 分）")
    print("  S1 发 B2 第 1 件减 1 分；S2 发 B0 第 1 件减 0 分，"
          "零金额仍按实际位置登记")
    print("  期末 B0 余 3 件 1 分、B1 余 6 件 6 分、B2 余 1 件 0 分、"
          "B3 两件 10 分")

    print("\n--- 报送截止号=100：只剩第一笔收货可见 ---")
    old = close_inventory_costs(
        build_reports(), build_costs(), ClosingQuery(2, 6, 100))
    render(old)
    print("  说明：业务截止号 6 内的其余事件没有任何可见报送版本，"
          "既不发生也不需要成本记录；缺失的 R2/P1 成本记录不在校验范围。")

    print("\n--- 错误成本表：P1 漏价（不以零补齐，整单拒绝）---")
    bad = close_inventory_costs(
        build_reports(),
        CostTable(receipt_costs=(ReceiptCost("R1", 100, 2),
                                 ReceiptCost("R2", 101, 10))),
        ClosingQuery(2, 6, 500))
    render(bad)


if __name__ == "__main__":
    main()
