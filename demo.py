"""手算演示：打印小 DAG 波次的完整领料解释与库存更正对照。

运行： python3 -I /workspace/demo.py
"""

import sys

sys.path.insert(0, "/workspace")

from wave_mrp import (
    Inventory,
    InventoryOverride,
    MaterialKind,
    NodeKey,
    OrderLine,
    Recipe,
    RecipeInput,
    Revision,
    Wave,
    WaveMRPEngine,
)


def nk(m: str, w: str) -> NodeKey:
    return NodeKey(m, w)


def render(result) -> None:
    print(f"\n=== 领料单 {result.wave_name} ({result.revision_label}) ===")
    for wh, doc in sorted(result.documents.items()):
        print(f"\n[仓点 {wh}] 单据状态: {'可用' if doc.available else '不可用'}")
        if not doc.available:
            for d in doc.defects:
                chain = " -> ".join(str(n) for n in d.chain)
                print(f"  缺陷 {d.kind}: {chain} | {d.detail}")
            continue
        for line in doc.picking_lines:
            src = ", ".join(
                f"{c.batch_ref}:{c.qty}" for c in line.contributions
            ) or "-"
            print(
                f"  {line.node!s:<8} 类型={line.kind:<12} 毛需求={line.gross_demand:<6} "
                f"来源=[{src}]"
            )
            print(
                f"           可用库存={line.available_stock} 隔离={line.isolated_stock} "
                f"抵用={line.netted_qty} 可用余数={line.available_remainder} "
                f"隔离余数={line.isolated_remainder} 净缺={line.net_shortage}"
            )
            if line.kind == "subassembly":
                print(
                    f"           投组数={line.lots_to_issue} 组产出={line.output_per_lot} "
                    f"总产出={line.lot_output_qty} 批次尾数={line.lot_remainder}"
                )
            else:
                print(f"           原料领用/缺料={line.issue_qty}")
        for short in doc.raw_shortages:
            refs = ", ".join(
                f"{s.batch_ref}需{s.required_qty}" for s in short.sources
            )
            print(f"  *** 缺料 {short.node}: 缺 {short.shortage_qty}（形成项: {refs}）")


def main() -> None:
    materials = {
        "FG_A": MaterialKind.SUBASSEMBLY, "FG_B": MaterialKind.SUBASSEMBLY,
        "FG_C": MaterialKind.SUBASSEMBLY, "S": MaterialKind.SUBASSEMBLY,
        "R1": MaterialKind.RAW, "R2": MaterialKind.RAW, "R3": MaterialKind.RAW,
    }
    recipes = {
        nk("FG_A", "WH"): Recipe("FG_A", "WH", 1, (
            RecipeInput("S", "WH", 1), RecipeInput("R1", "WH", 1))),
        nk("FG_B", "WH"): Recipe("FG_B", "WH", 1, (
            RecipeInput("S", "WH", 1), RecipeInput("R2", "WH", 1))),
        nk("FG_C", "WH"): Recipe("FG_C", "WH", 1, (
            RecipeInput("S", "WH", 1), RecipeInput("R3", "WH", 1))),
        nk("S", "WH"): Recipe("S", "WH", 4, (
            RecipeInput("R1", "WH", 2), RecipeInput("R2", "WH", 1))),
    }
    wave = Wave(
        "demo", materials, recipes,
        [OrderLine("O-A", "FG_A", "WH", 2),
         OrderLine("O-B", "FG_B", "WH", 1),
         OrderLine("O-C", "FG_C", "WH", 1)],
        {nk("S", "WH"): Inventory(2, 0),
         nk("R1", "WH"): Inventory(5, 0),
         nk("R2", "WH"): Inventory(3, 0)},
    )
    engine = WaveMRPEngine(wave)
    base = engine.solve_picking_list()
    render(base)

    s_line = base.line("S", "WH")
    print("\n--- 手工核验：先扣库存再取整 vs 逐成品取整 ---")
    print(f"S 毛需求=4（2+1+1），抵库存2，净缺2 → 整组 ceil(2/4)={s_line.lots_to_issue} 组")
    wrong = -((-2 // 4)) + -((-1 // 4)) + -((-1 // 4))
    print(f"错误做法（每成品独立取整后相加）= {wrong} 组，会多投 2 组、多耗 R1=4/R2=2")

    # 库存更正：S 可用库存从 2 提到 3，底层领料进一步下降
    rev = Revision(
        "stock-fix",
        inventory_overrides=(InventoryOverride(nk("S", "WH"), 3, 0),),
    )
    revised = engine.solve_picking_list(rev)
    render(revised)
    r1_base = base.line("R1", "WH").gross_demand
    r1_rev = revised.line("R1", "WH").gross_demand
    print(f"\n--- 更正解释：S 多抵 1 单位可用库存 → 仍只需 1 组；"
          f"若库存提到 4 则 0 组")
    print(f"R1 毛需求基线={r1_base}，本次修订={r1_rev}（修订未跨组界时底层不变）")
    rev2 = Revision(
        "stock-fix2",
        inventory_overrides=(InventoryOverride(nk("S", "WH"), 4, 0),),
    )
    revised2 = engine.solve_picking_list(rev2)
    print(f"S 库存=4 时: 投组数={revised2.line('S', 'WH').lots_to_issue}, "
          f"R1 毛需求={revised2.line('R1', 'WH').gross_demand}, "
          f"R2 毛需求={revised2.line('R2', 'WH').gross_demand}（底层领料骤减）")
    print(f"缓存命中={engine.cache_hits} 次，未重算无关连通组件")


if __name__ == "__main__":
    main()
