"""验收测试（全部为手工可复核的小 DAG + 一个深层共享 DAG）。

运行： python3 -I /workspace/run_tests.py
"""

from __future__ import annotations

import time
import unittest

from wave_mrp import (
    Inventory,
    InventoryOverride,
    MaterialKind,
    NodeKey,
    OrderLine,
    OrderOverride,
    Recipe,
    RecipeInput,
    Revision,
    Wave,
    WaveMRPEngine,
)


def nk(material: str, wh: str) -> NodeKey:
    return NodeKey(material, wh)


# --------------------------------------------------------------------- #
# 场景一：手工小 DAG —— 共用半成品被多个父项需要，先扣库存再整组取整
# --------------------------------------------------------------------- #
def build_small_wave() -> Wave:
    materials = {
        "FG_A": MaterialKind.SUBASSEMBLY,
        "FG_B": MaterialKind.SUBASSEMBLY,
        "FG_C": MaterialKind.SUBASSEMBLY,
        "S": MaterialKind.SUBASSEMBLY,    # 公用半成品
        "R1": MaterialKind.RAW,
        "R2": MaterialKind.RAW,
        "R3": MaterialKind.RAW,
    }
    recipes = {
        nk("FG_A", "WH"): Recipe("FG_A", "WH", 1, (
            RecipeInput("S", "WH", 1), RecipeInput("R1", "WH", 1))),
        nk("FG_B", "WH"): Recipe("FG_B", "WH", 1, (
            RecipeInput("S", "WH", 1), RecipeInput("R2", "WH", 1))),
        nk("FG_C", "WH"): Recipe("FG_C", "WH", 1, (
            RecipeInput("S", "WH", 1), RecipeInput("R3", "WH", 1))),
        # S 每组产出 4，每个父项各消耗 1
        nk("S", "WH"): Recipe("S", "WH", 4, (
            RecipeInput("R1", "WH", 2), RecipeInput("R2", "WH", 1))),
    }
    orders = [
        OrderLine("O-A", "FG_A", "WH", 2),
        OrderLine("O-B", "FG_B", "WH", 1),
        OrderLine("O-C", "FG_C", "WH", 1),
    ]
    inventory = {
        nk("S", "WH"): Inventory(available=2, isolated=0),
        nk("R1", "WH"): Inventory(available=5, isolated=0),
        nk("R2", "WH"): Inventory(available=3, isolated=0),
        # R3 无库存
    }
    return Wave("small", materials, recipes, orders, inventory)


class SmallDagHandCalcTest(unittest.TestCase):
    """手算（见每个 assert 旁注释）：
    S 总毛需求 = 2+1+1 = 4；抵可用库存 2 → 净缺 2；
    错误做法（每成品各自取整再相加）= ceil(2/4)+ceil(1/4)+ceil(1/4)=3 组；
    正确做法（汇总扣库存后整体取整）= ceil(2/4)=1 组。
    """

    def setUp(self) -> None:
        self.engine = WaveMRPEngine(build_small_wave())
        self.result = self.engine.solve_picking_list()

    def test_warehouse_document_available(self) -> None:
        doc = self.result.documents["WH"]
        self.assertTrue(doc.available)
        self.assertEqual(doc.defects, ())

    def test_shared_subassembly_net_before_round(self) -> None:
        line_s = self.result.line("S", "WH")
        # 毛需求 4（三个父项汇总），抵 2，净缺 2，向上整 1 组，组产出 4，尾数 2
        self.assertEqual(line_s.gross_demand, 4)
        self.assertEqual(len(line_s.contributions), 3)   # 三个父项批件身份
        self.assertEqual(line_s.netted_qty, 2)
        self.assertEqual(line_s.available_remainder, 0)
        self.assertEqual(line_s.net_shortage, 2)
        self.assertEqual(line_s.lots_to_issue, 1)
        self.assertEqual(line_s.lot_output_qty, 4)
        self.assertEqual(line_s.lot_remainder, 2)         # 取整尾数留批次，不摊平
        self.assertEqual(line_s.issue_qty, 0)

    def test_raw_demand_from_real_lots(self) -> None:
        # R1 = FG_A 直接 2 + S 投 1 组 ×2 = 4；R2 = FG_B 1 + S 1×1 = 2；
        # R3 = FG_C 1。
        r1 = self.result.line("R1", "WH")
        r2 = self.result.line("R2", "WH")
        r3 = self.result.line("R3", "WH")
        self.assertEqual(r1.gross_demand, 4)
        self.assertEqual(r1.netted_qty, 4)               # 可用 5 抵 4
        self.assertEqual(r1.available_remainder, 1)
        self.assertEqual(r1.issue_qty, 0)
        self.assertEqual(r2.gross_demand, 2)
        self.assertEqual(r2.netted_qty, 2)
        self.assertEqual(r2.issue_qty, 0)
        self.assertEqual(r3.gross_demand, 1)
        self.assertEqual(r3.netted_qty, 0)
        self.assertEqual(r3.issue_qty, 1)

    def test_raw_shortage_delivery_and_sources(self) -> None:
        doc = self.result.documents["WH"]
        self.assertEqual(len(doc.raw_shortages), 1)
        short = doc.raw_shortages[0]
        self.assertEqual(short.node, nk("R3", "WH"))
        self.assertEqual(short.shortage_qty, 1)
        # R3 的唯一需求由父项 FG_C 的整组投料形成（贡献以父项批件身份引用，
        # 不摊平回订单 O-C）
        self.assertEqual(len(short.sources), 1)
        self.assertIn("FG_C@WH", short.sources[0].batch_ref)
        self.assertEqual(short.sources[0].required_qty, 1)

    def test_finished_goods_lines_aggregated(self) -> None:
        fga = self.result.line("FG_A", "WH")
        self.assertEqual(fga.gross_demand, 2)
        self.assertEqual(fga.contributions[0].source_kind, "order")
        self.assertEqual(fga.contributions[0].batch_ref, "O-A")
        # FG_A 自身也是半成品：净缺 2，组产出 1 → 投 2 组，无尾数
        self.assertEqual(fga.lots_to_issue, 2)
        self.assertEqual(fga.lot_remainder, 0)

    def test_isolated_stock_never_netted(self) -> None:
        # 同一波次，仅把 R1 的 5 可用改为 0 可用 + 5 隔离
        rev = Revision(
            "isolated-r1",
            inventory_overrides=(
                InventoryOverride(nk("R1", "WH"), 0, 5),
            ),
        )
        revised = self.engine.solve_picking_list(rev)
        r1 = revised.line("R1", "WH")
        self.assertEqual(r1.gross_demand, 4)
        self.assertEqual(r1.netted_qty, 0)                # 隔离库存不得抵用
        self.assertEqual(r1.available_remainder, 0)
        self.assertEqual(r1.isolated_remainder, 5)        # 余数拆回：隔离原样保留
        self.assertEqual(r1.issue_qty, 4)
        short = next(s for s in revised.documents["WH"].raw_shortages
                     if s.node == nk("R1", "WH"))
        self.assertEqual(short.shortage_qty, 4)
        refs = {s.batch_ref for s in short.sources}
        # 形成需求的领料项均以父项批件身份引用：FG_A 的投料 + S 的投料，
        # 不摊平回订单、也不拆成逐单位路径
        self.assertEqual(len(refs), 2)
        self.assertTrue(any("FG_A@WH" in r for r in refs))
        self.assertTrue(any("S@WH" in r for r in refs))
        self.assertEqual(sum(s.required_qty for s in short.sources), 4)

    def test_no_per_unit_rows(self) -> None:
        # 聚合行：每个 (物料, 仓点) 仅一行，不按实体单位拆行
        nodes = [line.node for line in self.result.all_lines()]
        self.assertEqual(len(nodes), len(set(nodes)))
        self.assertEqual(len(nodes), 7)


# --------------------------------------------------------------------- #
# 场景二：库存更正后底层领料骤减；无关连通组件保持不变
# --------------------------------------------------------------------- #
def build_revision_wave() -> Wave:
    materials = {
        "D1": MaterialKind.SUBASSEMBLY,
        "SHARED": MaterialKind.SUBASSEMBLY,
        "BOTTOM": MaterialKind.RAW,
        # 独立组件（不同仓点 + 不同物料树）
        "D2": MaterialKind.SUBASSEMBLY,
        "INDEP_RAW": MaterialKind.RAW,
    }
    recipes = {
        nk("D1", "W1"): Recipe("D1", "W1", 1, (RecipeInput("SHARED", "W1", 1),)),
        nk("SHARED", "W1"): Recipe("SHARED", "W1", 1,
                                   (RecipeInput("BOTTOM", "W1", 1),)),
        nk("D2", "W2"): Recipe("D2", "W2", 1,
                               (RecipeInput("INDEP_RAW", "W2", 1),)),
    }
    orders = [
        OrderLine("O-1", "D1", "W1", 10),
        OrderLine("O-2", "D2", "W2", 4),
    ]
    inventory = {
        nk("SHARED", "W1"): Inventory(available=0, isolated=0),
        nk("BOTTOM", "W1"): Inventory(available=0, isolated=0),
        nk("INDEP_RAW", "W2"): Inventory(available=0, isolated=0),
    }
    return Wave("revision", materials, recipes, orders, inventory)


class RevisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = WaveMRPEngine(build_revision_wave())
        self.base = self.engine.solve_picking_list()

    def test_baseline_hand_calc(self) -> None:
        # 基线：SHARED 毛需求 10，无库存 → 投 10 组 → BOTTOM 缺 10
        shared = self.base.line("SHARED", "W1")
        bottom = self.base.line("BOTTOM", "W1")
        self.assertEqual(shared.gross_demand, 10)
        self.assertEqual(shared.lots_to_issue, 10)
        self.assertEqual(bottom.gross_demand, 10)
        self.assertEqual(bottom.issue_qty, 10)
        indep = self.base.line("INDEP_RAW", "W2")
        self.assertEqual(indep.gross_demand, 4)
        self.assertEqual(indep.issue_qty, 4)

    def test_stock_correction_collapses_bottom_picking(self) -> None:
        # 更正：SHARED 现有可用库存 8（一次提交的修订快照）
        rev = Revision(
            "stock-fix",
            inventory_overrides=(
                InventoryOverride(nk("SHARED", "W1"), new_available=8),
            ),
        )
        revised = self.engine.solve_picking_list(rev)
        shared = revised.line("SHARED", "W1")
        bottom = revised.line("BOTTOM", "W1")
        # 毛需求仍 10，但先抵库存 8 → 净缺 2 → 只投 2 组 → BOTTOM 骤减为 2
        self.assertEqual(shared.gross_demand, 10)
        self.assertEqual(shared.netted_qty, 8)
        self.assertEqual(shared.available_remainder, 0)
        self.assertEqual(shared.lots_to_issue, 2)
        self.assertEqual(bottom.gross_demand, 2)       # 10 → 2 的骤减来源
        self.assertEqual(bottom.issue_qty, 2)

    def test_unrelated_component_unchanged_and_reused(self) -> None:
        rev = Revision(
            "stock-fix",
            inventory_overrides=(
                InventoryOverride(nk("SHARED", "W1"), new_available=8),
            ),
        )
        revised = self.engine.solve_picking_list(rev)
        # W2 独立组件：数量完全不变，且必须来自缓存复用而非重算
        indep = revised.line("INDEP_RAW", "W2")
        d2 = revised.line("D2", "W2")
        self.assertEqual(indep.gross_demand, 4)
        self.assertEqual(indep.issue_qty, 4)
        self.assertEqual(d2.gross_demand, 4)
        self.assertGreaterEqual(self.engine.cache_hits, 1)
        # 修订是独立快照：基线结果不得被污染
        self.assertEqual(self.base.line("SHARED", "W1").lots_to_issue, 10)
        self.assertEqual(self.base.line("BOTTOM", "W1").issue_qty, 10)

    def test_order_override_is_atomic_replacement(self) -> None:
        rev = Revision(
            "order-cut",
            order_overrides=(OrderOverride("O-1", new_qty=3),),
        )
        revised = self.engine.solve_picking_list(rev)
        self.assertEqual(revised.line("D1", "W1").gross_demand, 3)
        self.assertEqual(revised.line("SHARED", "W1").gross_demand, 3)
        self.assertEqual(revised.line("BOTTOM", "W1").issue_qty, 3)
        # 未触及的 W2 依旧
        self.assertEqual(revised.line("INDEP_RAW", "W2").issue_qty, 4)
        # 基线快照不变（不混用前后批）
        self.assertEqual(self.base.line("D1", "W1").gross_demand, 10)

    def test_unknown_order_in_revision_rejected(self) -> None:
        rev = Revision("bad", order_overrides=(OrderOverride("NOPE", 1),))
        with self.assertRaises(KeyError):
            self.engine.solve_picking_list(rev)


# --------------------------------------------------------------------- #
# 场景三：层层共享的深 DAG（指数条成品→原料路径，线性去重节点）
# 每层 3 个共享件、每个父件吃全部 3 个子件；L=14 时路径 3^14 ≈ 478 万，
# 但去重节点仅 1 + 3*14 = 43 个，每个节点只求解一次。
# --------------------------------------------------------------------- #
def build_deep_sharing_wave(layers: int = 14) -> Wave:
    materials: dict[str, MaterialKind] = {"F": MaterialKind.SUBASSEMBLY}
    recipes: dict[NodeKey, Recipe] = {}
    for layer in range(1, layers + 1):
        for i in range(3):
            name = f"L{layer}_{i}"
            if layer == layers:
                materials[name] = MaterialKind.RAW
            else:
                materials[name] = MaterialKind.SUBASSEMBLY
    # 根 F 吃第 1 层三个节点
    recipes[nk("F", "WH")] = Recipe(
        "F", "WH", 1,
        tuple(RecipeInput(f"L1_{i}", "WH", 1) for i in range(3)),
    )
    for layer in range(1, layers):
        for i in range(3):
            parent = f"L{layer}_{i}"
            recipes[nk(parent, "WH")] = Recipe(
                parent, "WH", 1,
                tuple(RecipeInput(f"L{layer + 1}_{j}", "WH", 1) for j in range(3)),
            )
    return Wave(
        "deep",
        materials,
        recipes,
        [OrderLine("O-DEEP", "F", "WH", 1)],
        {},
    )


class DeepSharingTest(unittest.TestCase):
    def test_linear_nodes_exponential_paths(self) -> None:
        engine = WaveMRPEngine(build_deep_sharing_wave(14))
        start = time.perf_counter()
        result = engine.solve_picking_list()
        elapsed = time.perf_counter() - start

        self.assertTrue(result.documents["WH"].available)
        self.assertEqual(result.nodes_expanded, 43)
        self.assertEqual(result.nodes_solved, 43)
        self.assertEqual(len(result.all_lines()), 43)
        self.assertLess(elapsed, 5.0)
        # 第 k 层每个共享件毛需求 = 3^(k-1)；最底层原料 = 3^13 = 1594323
        bottom = result.line("L14_0", "WH")
        self.assertEqual(bottom.gross_demand, 3 ** 13)
        self.assertEqual(bottom.issue_qty, 3 ** 13)
        mid = result.line("L7_2", "WH")
        self.assertEqual(mid.gross_demand, 3 ** 6)
        # 每个共享节点只有一张聚合领料行（证明没有按路径反复展开）
        counts: dict[NodeKey, int] = {}
        for line in result.all_lines():
            counts[line.node] = counts.get(line.node, 0) + 1
        self.assertTrue(all(c == 1 for c in counts.values()))

    def test_repeated_shared_solve_uses_cache(self) -> None:
        engine = WaveMRPEngine(build_deep_sharing_wave(6))
        first = engine.solve_picking_list()
        second = engine.solve_picking_list()  # 完全相同的再次求解
        self.assertEqual(first.nodes_expanded, second.nodes_expanded)
        self.assertGreaterEqual(engine.cache_hits, engine.cache_misses - 1)
        self.assertEqual(second.line("L6_0", "WH").gross_demand, 3 ** 5)


# --------------------------------------------------------------------- #
# 场景四：需求可达的环、未知子项、缺配方 → 该仓点整单不可用；
# 其他仓点不受影响；无关配方里的环只作诊断。
# --------------------------------------------------------------------- #
def build_defect_wave() -> Wave:
    materials = {
        "A1": MaterialKind.SUBASSEMBLY,
        "A2": MaterialKind.SUBASSEMBLY,
        "A3": MaterialKind.SUBASSEMBLY,
        "U_PARENT": MaterialKind.SUBASSEMBLY,
        "M_PARENT": MaterialKind.SUBASSEMBLY,
        "W2_FG": MaterialKind.SUBASSEMBLY,
        "W2_RAW": MaterialKind.RAW,
        # 无关配方环的物料（无订单可达）
        "X1": MaterialKind.SUBASSEMBLY,
        "X2": MaterialKind.SUBASSEMBLY,
    }
    recipes = {
        # 可达环：A1 -> A2 -> A3 -> A2
        nk("A1", "W1"): Recipe("A1", "W1", 1, (RecipeInput("A2", "W1", 1),)),
        nk("A2", "W1"): Recipe("A2", "W1", 1, (RecipeInput("A3", "W1", 1),)),
        nk("A3", "W1"): Recipe("A3", "W1", 1, (RecipeInput("A2", "W1", 1),)),
        # 未知子项：GHOST 不在物料主数据
        nk("U_PARENT", "W1"): Recipe("U_PARENT", "W1", 1,
                                     (RecipeInput("GHOST", "W1", 1),)),
        # M_PARENT 是半成品但不给配方 → missing_recipe
        # 另一仓点的干净单据
        nk("W2_FG", "W2"): Recipe("W2_FG", "W2", 1,
                                  (RecipeInput("W2_RAW", "W2", 1),)),
        # 与波次无关的配方环（诊断用，不否决）
        nk("X1", "W3"): Recipe("X1", "W3", 1, (RecipeInput("X2", "W3", 1),)),
        nk("X2", "W3"): Recipe("X2", "W3", 1, (RecipeInput("X1", "W3", 1),)),
    }
    orders = [
        OrderLine("O-CYCLE", "A1", "W1", 2),
        OrderLine("O-UNKNOWN", "U_PARENT", "W1", 1),
        OrderLine("O-MISSING", "M_PARENT", "W1", 1),
        OrderLine("O-OK", "W2_FG", "W2", 3),
    ]
    return Wave("defect", materials, recipes, orders, {})


class DefectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = WaveMRPEngine(build_defect_wave())
        self.result = self.engine.solve_picking_list()

    def test_w1_whole_document_unavailable_with_chains(self) -> None:
        doc_w1 = self.result.documents["W1"]
        self.assertFalse(doc_w1.available)
        self.assertEqual(doc_w1.picking_lines, ())
        kinds = {d.kind for d in doc_w1.defects}
        self.assertIn("cycle", kinds)
        self.assertIn("unknown_child", kinds)
        self.assertIn("missing_recipe", kinds)
        cycle = next(d for d in doc_w1.defects if d.kind == "cycle")
        # 引用链必须能从需求根方向读到 A2 -> A3 -> A2
        self.assertEqual(cycle.chain[0], nk("A2", "W1"))
        self.assertEqual(cycle.chain[-1], nk("A2", "W1"))
        self.assertIn(nk("A3", "W1"), cycle.chain)
        unknown = next(d for d in doc_w1.defects if d.kind == "unknown_child")
        self.assertEqual(unknown.chain, (nk("U_PARENT", "W1"),
                                         nk("GHOST", "W1")))
        missing = next(d for d in doc_w1.defects if d.kind == "missing_recipe")
        self.assertEqual(missing.chain, (nk("M_PARENT", "W1"),))

    def test_other_warehouse_unaffected(self) -> None:
        doc_w2 = self.result.documents["W2"]
        self.assertTrue(doc_w2.available)
        self.assertEqual(doc_w2.defects, ())
        self.assertEqual(doc_w2.line("W2_RAW", "W2").gross_demand, 3)

    def test_unreachable_recipe_cycle_is_diagnostic_only(self) -> None:
        notes = self.result.unreachable_notes
        note_nodes = {n.recipe_node for n in notes}
        self.assertIn(nk("X1", "W3"), note_nodes)
        self.assertIn(nk("X2", "W3"), note_nodes)
        # W3 没有订单 → 不产生领料单据，也不影响 W1/W2 的判定
        self.assertNotIn("W3", self.result.documents)
        self.assertTrue(self.result.documents["W2"].available)

    def test_revision_does_not_clear_other_component_cache(self) -> None:
        # 修订只动 W1 的订单数量；W2 组件签名不变，必须命中缓存
        rev = Revision(
            "w1-qty",
            order_overrides=(OrderOverride("O-CYCLE", new_qty=5),),
        )
        revised = self.engine.solve_picking_list(rev)
        self.assertFalse(revised.documents["W1"].available)
        self.assertTrue(revised.documents["W2"].available)
        self.assertEqual(revised.line("W2_RAW", "W2").gross_demand, 3)
        self.assertGreaterEqual(self.engine.cache_hits, 1)


# --------------------------------------------------------------------- #
# 场景五：跨仓点独立 + 余数拆回（可用/隔离/批次尾数分别交代）
# --------------------------------------------------------------------- #
class WarehouseIndependenceTest(unittest.TestCase):
    def test_same_material_separate_warehouses(self) -> None:
        materials = {
            "FG": MaterialKind.SUBASSEMBLY,
            "S": MaterialKind.SUBASSEMBLY,
            "R": MaterialKind.RAW,
        }
        recipes = {
            nk("FG", "W1"): Recipe("FG", "W1", 1, (RecipeInput("S", "W1", 1),)),
            nk("FG", "W2"): Recipe("FG", "W2", 1, (RecipeInput("S", "W2", 1),)),
            nk("S", "W1"): Recipe("S", "W1", 5, (RecipeInput("R", "W1", 2),)),
            nk("S", "W2"): Recipe("S", "W2", 5, (RecipeInput("R", "W2", 2),)),
        }
        wave = Wave(
            "wh",
            materials,
            recipes,
            [OrderLine("O1", "FG", "W1", 7), OrderLine("O2", "FG", "W2", 7)],
            {
                # W1 有可用库存 3；W2 同物料库存是隔离的 3，不可抵用
                nk("S", "W1"): Inventory(available=3, isolated=0),
                nk("S", "W2"): Inventory(available=0, isolated=3),
            },
        )
        result = WaveMRPEngine(wave).solve_picking_list()
        s1 = result.line("S", "W1")
        s2 = result.line("S", "W2")
        # W1：毛 7 抵 3 → 净 4 → 1 组产出 5，尾数 1；W2：毛 7 无抵用 → 2 组
        self.assertEqual(s1.netted_qty, 3)
        self.assertEqual(s1.lots_to_issue, 1)
        self.assertEqual(s1.lot_remainder, 1)
        self.assertEqual(s2.netted_qty, 0)
        self.assertEqual(s2.isolated_remainder, 3)
        self.assertEqual(s2.lots_to_issue, 2)
        self.assertEqual(s2.lot_remainder, 3)
        # 底层 R 两仓点各自独立：2 vs 4
        self.assertEqual(result.line("R", "W1").gross_demand, 2)
        self.assertEqual(result.line("R", "W2").gross_demand, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
