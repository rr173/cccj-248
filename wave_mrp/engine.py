"""波次领料 MRP 引擎。

求解顺序（每个连通组件只做一次，共享件不反复展开）：
  1. 从冻结订单根出发做需求图展开，迭代 DFS 检测环、未知子项、缺配方；
  2. 污染沿反向边传播：凡需求树能到达缺陷的节点全部污染，
     含污染节点的仓点整单不可用，其他仓点不受影响；
  3. 干净子图按父先子后的拓扑序汇总毛需求（订单 + 各父项整组投料），
     先以该仓点可用库存抵用（隔离库存不抵用），再对半成品净短缺向上整组，
     以真实组数 × 固定耗用把子需求传给下一层；原料净短缺直接报缺料。
所有数量用整数精确计算，输出按节点聚合，绝不拆成逐实体单位的行。
"""

from __future__ import annotations

from collections import defaultdict

from .model import (
    Inventory,
    MaterialKind,
    NodeKey,
    OrderLine,
    Recipe,
    Wave,
)
from .result import (
    Contribution,
    Defect,
    InventoryOverride,
    PickingLine,
    PickingList,
    RawShortage,
    Revision,
    ShortageSource,
    UnreachableRecipeNote,
    WarehouseDocument,
)


def _ceil_div(a: int, b: int) -> int:
    """整数向上取整（a, b 均非负，b > 0）。"""
    return -((-a) // b)


class _ComponentResult:
    """一个连通组件的求解结果，可在修订间按签名复用。"""

    __slots__ = ("lines", "shortages", "defects", "tainted", "reached_nodes")

    def __init__(
        self,
        lines: dict[NodeKey, PickingLine],
        shortages: list[RawShortage],
        defects: list[Defect],
        tainted: frozenset[NodeKey],
        reached_nodes: tuple[NodeKey, ...],
    ) -> None:
        self.lines = lines
        self.shortages = shortages
        self.defects = defects
        self.tainted = tainted
        self.reached_nodes = reached_nodes


class WaveMRPEngine:
    """固定波次的领料引擎，支持多次原子修订与组件级复用。"""

    def __init__(self, wave: Wave) -> None:
        wave.validate()
        self._base_wave = wave
        self._component_cache: dict[tuple, _ComponentResult] = {}
        # 诊断统计（最近一次 solve_picking_list 调用）
        self.last_expanded = 0
        self.last_solved = 0
        self.cache_hits = 0
        self.cache_misses = 0

    # ------------------------------------------------------------------ #
    # 修订快照：整单替换，不允许混用修订前后批次
    # ------------------------------------------------------------------ #
    def _effective_wave(self, revision: Revision | None) -> Wave:
        if revision is None:
            return self._base_wave
        orders_by_id = {o.order_id: o for o in self._base_wave.orders}
        new_orders: list[OrderLine] = []
        overridden_orders = {ov.order_id: ov.new_qty for ov in revision.order_overrides}
        for ov in revision.order_overrides:
            if ov.order_id not in orders_by_id:
                raise KeyError(f"修订 {revision.label} 引用了不存在的订单 {ov.order_id}")
            if ov.new_qty < 0:
                raise ValueError(f"修订 {revision.label} 订单数量为负")
        for o in self._base_wave.orders:
            qty = overridden_orders.get(o.order_id, o.qty)
            new_orders.append(OrderLine(o.order_id, o.material, o.warehouse, qty))
        new_inventory = dict(self._base_wave.inventory)
        for ov in revision.inventory_overrides:
            if ov.new_available < 0 or ov.new_isolated < 0:
                raise ValueError(f"修订 {revision.label} 库存为负")
            new_inventory[ov.node] = Inventory(ov.new_available, ov.new_isolated)
        wave = Wave(
            name=f"{self._base_wave.name}@{revision.label}",
            materials=dict(self._base_wave.materials),
            recipes=dict(self._base_wave.recipes),
            orders=new_orders,
            inventory=new_inventory,
        )
        wave.validate()
        return wave

    # ------------------------------------------------------------------ #
    # 第 1 步：需求图展开 + 环/未知子项/缺配方检测
    # ------------------------------------------------------------------ #
    def _expand_graph(
        self, wave: Wave
    ) -> tuple[
        dict[NodeKey, list[NodeKey]],
        dict[NodeKey, list[NodeKey]],
        dict[NodeKey, list[OrderLine]],
        list[Defect],
        set[NodeKey],
    ]:
        adjacency: dict[NodeKey, list[NodeKey]] = {}
        reverse: dict[NodeKey, list[NodeKey]] = defaultdict(list)
        order_sources: dict[NodeKey, list[OrderLine]] = defaultdict(list)
        defects: list[Defect] = []
        seen_defects: set[tuple] = set()
        reached: set[NodeKey] = set()

        roots: list[NodeKey] = []
        for o in wave.orders:
            node = o.node
            order_sources[node].append(o)
            if node not in reached:
                roots.append(node)
                reached.add(node)

        def record_defect(kind: str, chain: tuple[NodeKey, ...], detail: str) -> None:
            key = (kind, chain)
            if key in seen_defects:
                return
            seen_defects.add(key)
            defects.append(Defect(kind, chain[-1].warehouse if chain else "", chain, detail))

        def known_children(node: NodeKey) -> tuple[list[NodeKey], bool]:
            """展开节点的已知子料；未知子项与缺配方就地记缺陷。

            返回 (已知子节点, 是否存在缺陷)；存在缺陷的节点本身即被污染。
            """
            kind = wave.materials.get(node.material)
            if kind is MaterialKind.RAW:
                return [], False
            recipe = wave.recipes.get(node)
            if recipe is None:
                record_defect(
                    "missing_recipe",
                    (node,),
                    f"半成品 {node} 有需求但波次内没有可用配方",
                )
                return [], True
            children: list[NodeKey] = []
            defective = False
            for inp in recipe.inputs:
                child = NodeKey(inp.material, inp.warehouse)
                if inp.material not in wave.materials:
                    record_defect(
                        "unknown_child",
                        (node, child),
                        f"配方 {node} 引用了主数据中不存在的物料 {inp.material}",
                    )
                    defective = True
                    continue
                children.append(child)
            return children, defective

        # 迭代 DFS（显式栈 + 当前路径），避免深 DAG 递归爆炸
        state: dict[NodeKey, int] = {}  # 1 在栈路径上 / 2 已完成
        defective_nodes: set[NodeKey] = set()

        def enter(node: NodeKey) -> None:
            """节点首次入栈：注册邻接并展开配方，缺陷就地记录。"""
            known, defective = known_children(node)
            adjacency[node] = known
            if defective:
                defective_nodes.add(node)

        for root in roots:
            if state.get(root) == 2:
                continue
            state[root] = 1
            enter(root)
            stack: list[list] = [[root, 0]]
            path: list[NodeKey] = [root]
            path_index: dict[NodeKey, int] = {root: 0}
            while stack:
                node, idx = stack[-1]
                children = adjacency[node]
                if idx < len(children):
                    child = children[idx]
                    stack[-1][1] += 1
                    child_state = state.get(child, 0)
                    if child_state == 1:  # 反向边 => 需求可达的环
                        start = path_index[child]
                        chain = tuple(path[start:] + [child])
                        record_defect("cycle", chain, f"需求路径在 {child} 处成环")
                        continue
                    if child_state == 2:
                        continue
                    state[child] = 1
                    reached.add(child)
                    reverse[child].append(node)
                    enter(child)
                    path_index[child] = len(path)
                    path.append(child)
                    stack.append([child, 0])
                else:
                    state[node] = 2
                    stack.pop()
                    path_index.pop(node)
                    path.pop()

        # 反向边表补全（共享子节点可能从多条路径到达）
        for parent, children in adjacency.items():
            for child in children:
                if parent not in reverse[child]:
                    reverse[child].append(parent)
        return (adjacency, dict(reverse), dict(order_sources),
                defects, reached, frozenset(defective_nodes))

    # ------------------------------------------------------------------ #
    # 第 2 步：污染传播与连通组件划分
    # ------------------------------------------------------------------ #
    @staticmethod
    def _propagate_taint(
        defects: list[Defect],
        defective_nodes: frozenset[NodeKey],
        reverse: dict[NodeKey, list[NodeKey]],
        reached: set[NodeKey],
    ) -> frozenset[NodeKey]:
        # 种子：环引用链上的图内节点，以及缺配方/未知子项所在的父节点
        tainted: set[NodeKey] = set(defective_nodes)
        for d in defects:
            for n in d.chain:
                if n in reached:  # 幽灵（未知物料）不在图中
                    tainted.add(n)
        # 沿反向边传播：凡需求树能到达缺陷的祖先节点全部污染
        queue = list(tainted)
        while queue:
            node = queue.pop()
            for parent in reverse.get(node, ()):
                if parent not in tainted:
                    tainted.add(parent)
                    queue.append(parent)
        return frozenset(tainted)

    @staticmethod
    def _components(
        adjacency: dict[NodeKey, list[NodeKey]],
    ) -> list[frozenset[NodeKey]]:
        """无向弱连通组件：修订隔离与缓存复用的边界。"""
        undirected: dict[NodeKey, set[NodeKey]] = {n: set() for n in adjacency}
        for parent, children in adjacency.items():
            for child in children:
                undirected[parent].add(child)
                undirected.setdefault(child, set()).add(parent)
        seen: set[NodeKey] = set()
        components: list[frozenset[NodeKey]] = []
        for start in adjacency:
            if start in seen:
                continue
            comp: set[NodeKey] = set()
            queue = [start]
            seen.add(start)
            while queue:
                node = queue.pop()
                comp.add(node)
                for nxt in undirected[node]:
                    if nxt not in seen:
                        seen.add(nxt)
                        queue.append(nxt)
            components.append(frozenset(comp))
        return components

    # ------------------------------------------------------------------ #
    # 第 3 步：单组件净需求求解（先扣库存，再整组取整）
    # ------------------------------------------------------------------ #
    def _solve_component(
        self,
        wave: Wave,
        revision_label: str,
        component: frozenset[NodeKey],
        adjacency: dict[NodeKey, list[NodeKey]],
        order_sources: dict[NodeKey, list[OrderLine]],
        tainted: frozenset[NodeKey],
        defects: list[Defect],
    ) -> _ComponentResult:
        comp_defects = [d for d in defects if any(n in component for n in d.chain)]
        comp_tainted = frozenset(n for n in tainted if n in component)
        reached_tuple = tuple(sorted(component, key=lambda n: (n.warehouse, n.material)))
        if comp_tainted:
            # 污染组件不产出任何领料行，仅交付引用链
            return _ComponentResult({}, [], comp_defects, comp_tainted, reached_tuple)

        clean_nodes = component
        indegree = {n: 0 for n in clean_nodes}
        for parent in clean_nodes:
            for child in adjacency.get(parent, ()):
                if child in clean_nodes:
                    indegree[child] += 1
        # Kahn 拓扑：父先子后，保证毛需求在抵库存前已完成全部来源汇总
        ready = sorted((n for n, d in indegree.items() if d == 0),
                       key=lambda n: (n.warehouse, n.material))
        topo: list[NodeKey] = []
        indeg = dict(indegree)
        while ready:
            node = ready.pop(0)
            topo.append(node)
            for child in adjacency.get(node, ()):
                if child not in clean_nodes:
                    continue
                indeg[child] -= 1
                if indeg[child] == 0:
                    ready.append(child)
                    ready.sort(key=lambda n: (n.warehouse, n.material))
        if len(topo) != len(clean_nodes):  # 干净组件理论上无环，防御性断言
            raise RuntimeError("干净组件出现环，污染传播逻辑有误")

        gross: dict[NodeKey, int] = defaultdict(int)
        contributions: dict[NodeKey, list[Contribution]] = defaultdict(list)
        for node in topo:
            for o in sorted(order_sources.get(node, ()), key=lambda x: x.order_id):
                gross[node] += o.qty
                contributions[node].append(Contribution("order", o.order_id, o.qty))

        lines: dict[NodeKey, PickingLine] = {}
        shortages: list[RawShortage] = []
        for node in topo:
            kind = wave.materials[node.material]
            gross_demand = gross[node]
            stock = wave.stock_of(node)
            netted = min(stock.available, gross_demand)
            net_shortage = gross_demand - netted
            available_remainder = stock.available - netted
            conns = tuple(contributions.get(node, ()))

            lots = 0
            output_per_lot: int | None = None
            lot_output: int | None = None
            lot_remainder = 0
            issue_qty = 0

            if kind is MaterialKind.SUBASSEMBLY:
                recipe = wave.recipes[node]
                output_per_lot = recipe.output_per_lot
                if net_shortage > 0:
                    lots = _ceil_div(net_shortage, output_per_lot)
                    lot_output = lots * output_per_lot
                    lot_remainder = lot_output - net_shortage
                else:
                    lot_output = 0
                batch_ref = f"BATCH|{revision_label}|{node}"
                # 真实组数 × 固定耗用，形成对子料仓点的直接需求
                for inp in recipe.inputs:
                    child = NodeKey(inp.material, inp.warehouse)
                    if child not in clean_nodes:
                        continue  # 未知子项已在展开期判缺陷，不会进入干净组件
                    child_qty = lots * inp.qty_per_lot
                    if child_qty == 0:
                        continue
                    gross[child] += child_qty
                    contributions[child].append(
                        Contribution("parent", batch_ref, child_qty)
                    )
            else:
                issue_qty = net_shortage
                if net_shortage > 0:
                    sources = tuple(
                        ShortageSource(
                            source_node=node,
                            batch_ref=c.batch_ref,
                            required_qty=c.qty,
                        )
                        for c in conns
                    )
                    shortages.append(RawShortage(node, net_shortage, sources))

            lines[node] = PickingLine(
                node=node,
                kind=kind.value,
                gross_demand=gross_demand,
                contributions=conns,
                available_stock=stock.available,
                isolated_stock=stock.isolated,
                netted_qty=netted,
                available_remainder=available_remainder,
                isolated_remainder=stock.isolated,
                net_shortage=net_shortage,
                lots_to_issue=lots,
                output_per_lot=output_per_lot,
                lot_output_qty=lot_output,
                lot_remainder=lot_remainder,
                issue_qty=issue_qty,
            )
        return _ComponentResult(lines, shortages, comp_defects, comp_tainted, reached_tuple)

    # ------------------------------------------------------------------ #
    # 组件签名与缓存
    # ------------------------------------------------------------------ #
    def _component_signature(
        self,
        wave: Wave,
        component: frozenset[NodeKey],
        adjacency: dict[NodeKey, list[NodeKey]],
        order_sources: dict[NodeKey, list[OrderLine]],
    ) -> tuple:
        edge_part: list[tuple] = []
        for parent in sorted(component, key=lambda n: str(n)):
            recipe = wave.recipes.get(parent)
            opl = recipe.output_per_lot if recipe else None
            for child in adjacency.get(parent, ()):
                if child not in component:
                    continue
                qpl = next(
                    (i.qty_per_lot for i in recipe.inputs
                     if NodeKey(i.material, i.warehouse) == child),
                    None,
                )
                edge_part.append((parent, child, qpl))
            edge_part.append(("__lot__", parent, opl))
        order_part = sorted(
            (node, o.order_id, o.qty)
            for node in component
            for o in order_sources.get(node, ())
        )
        stock_part = sorted(
            (node, wave.stock_of(node).available, wave.stock_of(node).isolated)
            for node in component
        )
        # 节点身份入签名，防止两个同构但不同物料/仓点的组件错误共用缓存
        identity_part = tuple(sorted(component, key=lambda n: str(n)))
        return (identity_part, tuple(edge_part), tuple(order_part), tuple(stock_part))

    # ------------------------------------------------------------------ #
    # 与波次无关的配方诊断（不否决正常领料）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _unreachable_notes(wave: Wave, reached: set[NodeKey]) -> list[UnreachableRecipeNote]:
        notes: list[UnreachableRecipeNote] = []
        unreachable = [n for n in wave.recipes if n not in reached]
        # 在无关配方子图内单独查环，仅作诊断
        state: dict[NodeKey, int] = {}
        for root in unreachable:
            if root in state:
                continue
            stack: list[list] = [[root, 0]]
            path: list[NodeKey] = [root]
            path_index: dict[NodeKey, int] = {root: 0}
            state[root] = 1
            while stack:
                node, idx = stack[-1]
                recipe = wave.recipes.get(node)
                children = [
                    NodeKey(i.material, i.warehouse)
                    for i in recipe.inputs
                ] if recipe else []
                children = [c for c in children if c in wave.recipes and c not in reached]
                if idx < len(children):
                    child = children[idx]
                    stack[-1][1] += 1
                    st = state.get(child, 0)
                    if st == 1:
                        start = path_index[child]
                        notes.append(UnreachableRecipeNote(
                            root,
                            "无关配方子图存在环（不影响本波次领料）: "
                            + " -> ".join(str(n) for n in path[start:] + [child]),
                        ))
                        continue
                    if st == 2:
                        continue
                    state[child] = 1
                    path_index[child] = len(path)
                    path.append(child)
                    stack.append([child, 0])
                else:
                    state[node] = 2
                    stack.pop()
                    path_index.pop(node, None)
                    path.pop()
        for n in sorted(unreachable, key=lambda x: str(x)):
            notes.append(UnreachableRecipeNote(n, "配方存在但本波次无需求可达，未参与展开"))
        return notes

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def solve_picking_list(self, revision: Revision | None = None) -> PickingList:
        wave = self._effective_wave(revision)
        revision_label = revision.label if revision else "BASE"

        adjacency, reverse, order_sources, defects, reached, defective_nodes = \
            self._expand_graph(wave)
        tainted = self._propagate_taint(
            defects, defective_nodes, reverse, reached
        )
        components = self._components(adjacency)

        results: list[_ComponentResult] = []
        nodes_solved = 0
        for comp in components:
            signature = self._component_signature(wave, comp, adjacency, order_sources)
            cached = self._component_cache.get(signature)
            if cached is not None:
                self.cache_hits += 1
                results.append(cached)
                nodes_solved += len([n for n in comp if n not in cached.tainted])
                continue
            self.cache_misses += 1
            result = self._solve_component(
                wave, revision_label, comp, adjacency, order_sources, tainted, defects
            )
            self._component_cache[signature] = result
            results.append(result)
            nodes_solved += len([n for n in comp if n not in result.tainted])

        # 按仓点组装独立单据
        warehouses: dict[str, dict] = {}
        for node in sorted(reached, key=lambda n: (n.warehouse, n.material)):
            bucket = warehouses.setdefault(
                node.warehouse, {"nodes": [], "tainted": set(), "defects": []}
            )
            bucket["nodes"].append(node)
        for result in results:
            for t in result.tainted:
                warehouses[t.warehouse]["tainted"].add(t)
            for d in result.defects:
                for n in d.chain:
                    if n in reached:
                        warehouses[n.warehouse]["defects"].append(d)

        documents: dict[str, WarehouseDocument] = {}
        for wh, bucket in warehouses.items():
            reached_here = tuple(bucket["nodes"])
            wh_defects = tuple(dict.fromkeys(bucket["defects"]))
            if bucket["tainted"]:
                documents[wh] = WarehouseDocument(
                    warehouse=wh,
                    available=False,
                    defects=wh_defects,
                    picking_lines=(),
                    raw_shortages=(),
                    reached_nodes=reached_here,
                )
                continue
            lines: list[PickingLine] = []
            shorts: list[RawShortage] = []
            for result in results:
                for node in result.reached_nodes:
                    if node.warehouse == wh and node in result.lines:
                        lines.append(result.lines[node])
                shorts.extend(s for s in result.shortages if s.node.warehouse == wh)
            documents[wh] = WarehouseDocument(
                warehouse=wh,
                available=True,
                defects=(),
                picking_lines=tuple(lines),
                raw_shortages=tuple(shorts),
                reached_nodes=reached_here,
            )

        notes = self._unreachable_notes(wave, reached)
        self.last_expanded = len(reached)
        self.last_solved = nodes_solved
        return PickingList(
            wave_name=wave.name,
            revision_label=revision_label,
            documents=documents,
            unreachable_notes=tuple(notes),
            nodes_expanded=len(reached),
            nodes_solved=nodes_solved,
        )
