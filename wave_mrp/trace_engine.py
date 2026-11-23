"""来料抽检追溯引擎：按固定截止号重放过账流水，生成隔离/召回清单。

实现分三遍，所有集合都以**区间列表**（排序、互不重叠的半开整数区间）
表示，批量再大也只存端点，不逐件分配内存；依据只记一跳，不展开通往
收货的所有路径。

  1. 报送筛选：只看两个截止号（含端点）以内的报送，按报送序号取每个
     事件最后可见版本，撤销事件不发生。旧版本与截止号外记录完全不参与
     后续实物校验，因此以固定截止号重查必然得到同一份清单。
  2. 实物账重放：按业务序号应用有效事件，建批/耗用逐位置记账，缺失上游
     批、重复建批、重复耗用、从更晚批次取货等矛盾全部在此发现；有任何
     矛盾即整单拒绝，不出具部分清单。
  3. 受影响传播：圈选区间播种在原收货批上，沿调拨（仅命中片段映射）、
     生产（任一投入位置命中则整批产出受影响）、发运（命中片段转召回）
     正向传播；最后用“受影响区间 − 已消耗区间”得在库隔离清单。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .trace_model import (
    EventReport,
    Interval,
    PayloadType,
    ProductionPayload,
    ReceiptPayload,
    Segment,
    ShipmentPayload,
    TraceQuery,
    TransferPayload,
    TransferSegment,
)
from .trace_result import (
    IntervalMapping,
    IsolationRow,
    LedgerContradiction,
    ProductionEvidence,
    RecallRow,
    ReceiptMarkEvidence,
    RefusalReason,
    TraceabilityResult,
    TransferEvidence,
)


# --------------------------------------------------------------------- #
# 区间列表工具：所有列表均为排序、互不重叠，元素是半开区间
# --------------------------------------------------------------------- #
def merge_intervals(intervals: list[Interval] | tuple[Interval, ...]) -> list[Interval]:
    """合并重叠/相邻区间，保持端点整数口径。"""
    ordered = sorted(intervals, key=lambda iv: (iv.start, iv.end))
    merged: list[Interval] = []
    for iv in ordered:
        if merged and iv.start <= merged[-1].end:
            if iv.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, iv.end)
        else:
            merged.append(Interval(iv.start, iv.end))
    return merged


def intersect_lists(a: list[Interval], b: list[Interval]) -> list[Interval]:
    """两个排序不重叠区间列表的交集（双指针，线性时间）。"""
    out: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i].start, b[j].start)
        hi = min(a[i].end, b[j].end)
        if lo < hi:
            out.append(Interval(lo, hi))
        if a[i].end < b[j].end:
            i += 1
        else:
            j += 1
    return out


def subtract_lists(a: list[Interval], b: list[Interval]) -> list[Interval]:
    """从 a 中扣除 b 覆盖的位置，结果仍排序不重叠。

    对 a 的每个片段独立扫描 b：b 中完全位于片段左侧的区间直接跳过，
    与片段相交的区间逐段切割；不把指针带到下一个 a 片段，避免漏判。
    两端列表都排序，实际为线性复杂度。
    """
    out: list[Interval] = []
    for iv in a:
        cur_start, cur_end = iv.start, iv.end
        for cut in b:
            if cut.end <= cur_start:
                continue
            if cut.start >= cur_end:
                break
            if cut.start > cur_start:
                out.append(Interval(cur_start, cut.start))
            cur_start = max(cur_start, cut.end)
            if cur_start >= cur_end:
                break
        if cur_start < cur_end:
            out.append(Interval(cur_start, cur_end))
    return out


def total_length(intervals: list[Interval] | tuple[Interval, ...]) -> int:
    return sum(iv.length for iv in intervals)


def overlaps_sorted(sorted_intervals: list[Interval], iv: Interval) -> bool:
    """iv 是否与排序不重叠区间列表中的任一区间相交（二分，O(log n)）。

    不缓存端点数组：区间列表在重放中会被整体替换，缓存会失效。
    找到起点不小于 iv.start 的首个区间后，只需检查它和它的前驱。
    """
    if not sorted_intervals:
        return False
    lo, hi = 0, len(sorted_intervals)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_intervals[mid].start < iv.start:
            lo = mid + 1
        else:
            hi = mid
    if lo < len(sorted_intervals) and sorted_intervals[lo].start < iv.end:
        return True
    return lo > 0 and sorted_intervals[lo - 1].end > iv.start


def add_interval(sorted_intervals: list[Interval], iv: Interval) -> list[Interval]:
    """向排序不重叠列表并入单个区间，吸收重叠/相邻段，返回新的排序列表。"""
    result: list[Interval] = []
    cur_start, cur_end = iv.start, iv.end
    inserted = False
    for existing in sorted_intervals:
        if existing.end < cur_start:
            result.append(existing)                    # 完全在左侧
        elif existing.start > cur_end:
            if not inserted:
                result.append(Interval(cur_start, cur_end))
                inserted = True
            result.append(existing)                    # 完全在右侧
        else:                                          # 相交或相邻，吸收
            cur_start = min(cur_start, existing.start)
            cur_end = max(cur_end, existing.end)
    if not inserted:
        result.append(Interval(cur_start, cur_end))
    return result


@dataclass
class _BatchState:
    """重放期一个批次的实物账。"""

    batch: str
    material: str
    warehouse: str
    size: int
    created_seq: int
    created_event: str
    consumed: list[Interval] = field(default_factory=list)   # 已耗用位置（累计并集）
    affected: list[Interval] = field(default_factory=list)   # 受影响位置（传播期累计并集）


class LotTraceabilityEngine:
    """固定一批报送流水的追溯引擎；同一份流水可按不同截止号反复查询。"""

    def __init__(self, reports: list[EventReport] | tuple[EventReport, ...]) -> None:
        self._reports = tuple(reports)

    # ------------------------------------------------------------------ #
    # 第 1 遍：报送筛选（业务/报送双截止，最后可见版本，撤销即不发生）
    # ------------------------------------------------------------------ #
    def _select_reports(
        self, query: TraceQuery
    ) -> tuple[list[tuple[int, str, int, object]], list[LedgerContradiction],
               list[tuple[str, int | None]]]:
        """返回 (按业务序号排序的有效事件, 矛盾, 采用版本审计)。

        有效事件元素为 (business_seq, event_id, report_seq, payload|None)。
        """
        errors: list[LedgerContradiction] = []
        seen_report_seq: dict[int, str] = {}
        # event_id -> (最大 report_seq, business_seq, payload)
        latest: dict[str, tuple[int, int, object]] = {}
        header_seq: dict[str, int] = {}

        for rep in self._reports:
            if not isinstance(rep.report_seq, int) or rep.report_seq < 0:
                errors.append(LedgerContradiction(
                    RefusalReason.INVALID_PAYLOAD, rep.event_id,
                    f"报送序号必须是非负整数: {rep.report_seq!r}"))
                continue
            if not isinstance(rep.business_seq, int) or rep.business_seq < 0:
                errors.append(LedgerContradiction(
                    RefusalReason.INVALID_PAYLOAD, rep.event_id,
                    f"业务序号必须是非负整数: {rep.business_seq!r}"))
                continue
            # 两个截止号以外的报送一律不可见，不参与任何校验
            if (rep.business_seq > query.business_cutoff
                    or rep.report_seq > query.report_cutoff):
                continue
            if rep.report_seq in seen_report_seq:
                errors.append(LedgerContradiction(
                    RefusalReason.DUPLICATE_REPORT_SEQ, rep.event_id,
                    f"报送序号 {rep.report_seq} 同时被事件 "
                    f"{seen_report_seq[rep.report_seq]} 与 {rep.event_id} 使用"))
            else:
                seen_report_seq[rep.report_seq] = rep.event_id
            if rep.event_id in header_seq and header_seq[rep.event_id] != rep.business_seq:
                errors.append(LedgerContradiction(
                    RefusalReason.INCONSISTENT_EVENT_HEADER, rep.event_id,
                    f"同一事件的业务序号在报送间不一致: "
                    f"{header_seq[rep.event_id]} != {rep.business_seq}"))
            header_seq.setdefault(rep.event_id, rep.business_seq)
            old = latest.get(rep.event_id)
            if old is None or rep.report_seq > old[0]:
                latest[rep.event_id] = (rep.report_seq, rep.business_seq, rep.payload)

        events = [(biz, eid, rseq, payload)
                  for eid, (rseq, biz, payload) in latest.items()]
        events.sort(key=lambda item: item[0])
        seen_biz: set[int] = set()
        for biz, eid, _rseq, _payload in events:
            if biz in seen_biz:
                errors.append(LedgerContradiction(
                    RefusalReason.DUPLICATE_BUSINESS_SEQ, eid,
                    f"业务序号 {biz} 被多个有效事件使用"))
            seen_biz.add(biz)
        audit = [(eid, None if payload is None else rseq)
                 for _biz, eid, rseq, payload in events]
        return events, errors, audit

    # ------------------------------------------------------------------ #
    # 第 2 遍：实物账重放与矛盾校验
    # ------------------------------------------------------------------ #
    @staticmethod
    def _segment_in_batch(seg: Interval, batch: _BatchState) -> bool:
        return seg.start >= 0 and seg.end <= batch.size

    def _replay(
        self,
        events: list[tuple[int, str, int, object]],
    ) -> tuple[dict[str, _BatchState], list[LedgerContradiction]]:
        batches: dict[str, _BatchState] = {}
        errors: list[LedgerContradiction] = []

        def err(reason: RefusalReason, eid: str, detail: str) -> None:
            errors.append(LedgerContradiction(reason, eid, detail))

        def check_consume(
            eid: str, batch: _BatchState, seg: Interval, tentative: list[Interval]
        ) -> bool:
            """片段非空、不超初始范围、不与既有/本单已耗位置重叠。"""
            ok = True
            if seg.start < 0 or seg.end <= seg.start:
                err(RefusalReason.OUT_OF_RANGE_SEGMENT, eid,
                    f"批次 {batch.batch} 的耗用片段 {seg} 非法（须为批内非空区间）")
                ok = False
            elif not self._segment_in_batch(seg, batch):
                err(RefusalReason.OUT_OF_RANGE_SEGMENT, eid,
                    f"批次 {batch.batch} 初始范围 [0,{batch.size})，"
                    f"耗用片段 {seg} 超界")
                ok = False
            elif (overlaps_sorted(batch.consumed, seg)
                  or overlaps_sorted(tentative, seg)):
                err(RefusalReason.DOUBLE_CONSUMPTION, eid,
                    f"批次 {batch.batch} 的位置 {seg} 已被消耗，同一位置不可二次消耗")
                ok = False
            return ok

        def require_existing(eid: str, batch_id: str, seq: int) -> _BatchState | None:
            batch = batches.get(batch_id)
            if batch is None:
                err(RefusalReason.MISSING_UPSTREAM_BATCH, eid,
                    f"引用了当时尚不存在的批次 {batch_id}")
                return None
            if batch.created_seq >= seq:
                err(RefusalReason.LATER_BATCH_SEQ, eid,
                    f"批次 {batch_id} 的建批业务序号 {batch.created_seq} "
                    f"不早于本事件 {seq}，不得从更晚批次取货")
                return None
            return batch

        for seq, eid, _rseq, payload in events:
            if payload is None:
                continue  # 撤销事件不发生

            if payload.kind is PayloadType.RECEIPT:
                assert isinstance(payload, ReceiptPayload)
                if payload.quantity <= 0:
                    err(RefusalReason.INVALID_PAYLOAD, eid,
                        f"收货批量必须为正整数: {payload.quantity}")
                    continue
                if payload.batch in batches:
                    err(RefusalReason.DUPLICATE_BATCH, eid,
                        f"批号 {payload.batch} 已存在，"
                        "批号清空后也不得复用")
                    continue
                batches[payload.batch] = _BatchState(
                    batch=payload.batch, material=payload.material,
                    warehouse=payload.warehouse, size=payload.quantity,
                    created_seq=seq, created_event=eid)

            elif payload.kind is PayloadType.TRANSFER:
                assert isinstance(payload, TransferPayload)
                if not payload.sources:
                    err(RefusalReason.INVALID_PAYLOAD, eid, "调拨单没有源片段")
                    continue
                if payload.target_batch in batches:
                    err(RefusalReason.DUPLICATE_BATCH, eid,
                        f"目标批号 {payload.target_batch} 已存在，"
                        "批号清空后也不得复用")
                    continue
                source_batches: list[tuple[TransferSegment, _BatchState]] = []
                material = warehouse = None
                structural_bad = False
                for src in payload.sources:
                    batch = require_existing(eid, src.source_batch, seq)
                    if batch is None:
                        structural_bad = True
                        continue
                    if not self._segment_in_batch(src.source_interval, batch):
                        err(RefusalReason.OUT_OF_RANGE_SEGMENT, eid,
                            f"调拨源批 {batch.batch} 初始范围 [0,{batch.size})，"
                            f"片段 {src.source_interval} 超界")
                        structural_bad = True
                        continue
                    if material is None:
                        material, warehouse = batch.material, batch.warehouse
                    elif batch.material != material:
                        err(RefusalReason.WRONG_SOURCE_MATERIAL, eid,
                            f"调拨混料：{batch.batch} 物料 {batch.material} "
                            f"!= {material}")
                        structural_bad = True
                    elif batch.warehouse != warehouse:
                        err(RefusalReason.WRONG_SOURCE_WAREHOUSE, eid,
                            f"调拨混仓：{batch.batch} 仓点 {batch.warehouse} "
                            f"!= {warehouse}")
                        structural_bad = True
                    source_batches.append((src, batch))
                if structural_bad:
                    continue  # 本事件不成立，不建批、不记账
                tentative: list[Interval] = []
                for src, batch in source_batches:
                    if not check_consume(eid, batch, src.source_interval, tentative):
                        structural_bad = True
                if structural_bad:
                    continue
                target_size = 0
                for src, batch in source_batches:
                    batch.consumed = add_interval(
                        batch.consumed, src.source_interval)
                    target_size += src.source_interval.length
                batches[payload.target_batch] = _BatchState(
                    batch=payload.target_batch, material=material,  # type: ignore[arg-type]
                    warehouse=payload.target_warehouse,
                    size=target_size, created_seq=seq, created_event=eid)

            elif payload.kind is PayloadType.PRODUCTION:
                assert isinstance(payload, ProductionPayload)
                if payload.reported_output_qty <= 0:
                    err(RefusalReason.INVALID_PAYLOAD, eid,
                        f"产量必须为正整数: {payload.reported_output_qty}")
                    continue
                if not payload.inputs:
                    err(RefusalReason.INVALID_PAYLOAD, eid, "生产登记没有投入片段")
                    continue
                if payload.output_batch in batches:
                    err(RefusalReason.DUPLICATE_BATCH, eid,
                        f"产出批号 {payload.output_batch} 已存在，"
                        "批号清空后也不得复用")
                    continue
                resolved: list[tuple[Segment, _BatchState]] = []
                structural_bad = False
                for seg in payload.inputs:
                    batch = require_existing(eid, seg.batch, seq)
                    if batch is None:
                        structural_bad = True
                        continue
                    resolved.append((seg, batch))
                if structural_bad:
                    continue
                per_batch_pending: dict[str, list[Interval]] = {}
                for seg, batch in resolved:
                    per_batch_pending.setdefault(batch.batch, [])
                    if not check_consume(eid, batch, seg.interval,
                                         per_batch_pending[batch.batch]):
                        structural_bad = True
                    per_batch_pending[batch.batch].append(seg.interval)
                if structural_bad:
                    continue
                for seg, batch in resolved:
                    batch.consumed = add_interval(batch.consumed, seg.interval)
                batches[payload.output_batch] = _BatchState(
                    batch=payload.output_batch, material=payload.output_material,
                    warehouse=payload.registration_warehouse,
                    size=payload.reported_output_qty,
                    created_seq=seq, created_event=eid)

            elif payload.kind is PayloadType.SHIPMENT:
                assert isinstance(payload, ShipmentPayload)
                if not payload.sources:
                    err(RefusalReason.INVALID_PAYLOAD, eid, "发运登记没有耗用片段")
                    continue
                resolved: list[tuple[Segment, _BatchState]] = []
                structural_bad = False
                for seg in payload.sources:
                    batch = require_existing(eid, seg.batch, seq)
                    if batch is None:
                        structural_bad = True
                        continue
                    resolved.append((seg, batch))
                if structural_bad:
                    continue
                per_batch_pending: dict[str, list[Interval]] = {}
                for seg, batch in resolved:
                    per_batch_pending.setdefault(batch.batch, [])
                    if not check_consume(eid, batch, seg.interval,
                                         per_batch_pending[batch.batch]):
                        structural_bad = True
                    per_batch_pending[batch.batch].append(seg.interval)
                if structural_bad:
                    continue
                for seg, batch in resolved:
                    batch.consumed = add_interval(batch.consumed, seg.interval)

            else:  # pragma: no cover - 防御性分支
                err(RefusalReason.INVALID_PAYLOAD, eid,
                    f"未知业务类型 {payload.kind!r}")

        return batches, errors

    # ------------------------------------------------------------------ #
    # 第 3 遍：受影响传播（播种 -> 调拨/生产/发运）
    # ------------------------------------------------------------------ #
    def _propagate(
        self,
        events: list[tuple[int, str, int, object]],
        batches: dict[str, _BatchState],
        seed_batch: str,
        seed_intervals: list[Interval],
    ) -> tuple[
        list[TransferEvidence],
        list[ProductionEvidence],
        list[tuple[str, str, str, str, list[Interval], int]],
    ]:
        """返回 (调拨依据, 生产依据, 召回原始行)。

        召回原始行元素：(收货方, 发运事件, 物料, 批次, 命中区间, 报送号)。
        """
        batches[seed_batch].affected = merge_intervals(seed_intervals)
        transfer_ev: list[TransferEvidence] = []
        production_ev: list[ProductionEvidence] = []
        recall_rows: list[tuple[str, str, str, str, list[Interval], int]] = []

        for _seq, eid, rseq, payload in events:
            if payload is None:
                continue

            if payload.kind is PayloadType.TRANSFER:
                assert isinstance(payload, TransferPayload)
                target = batches[payload.target_batch]
                offset = 0
                mappings: list[IntervalMapping] = []
                target_hits: list[Interval] = []
                for src in payload.sources:
                    source = batches[src.source_batch]
                    hit = intersect_lists(source.affected, [src.source_interval])
                    for iv in hit:
                        target_iv = iv.shifted(
                            offset - src.source_interval.start)
                        mappings.append(IntervalMapping(
                            source_batch=src.source_batch,
                            source_interval=iv,
                            target_batch=payload.target_batch,
                            target_interval=target_iv))
                        target_hits.append(target_iv)
                    offset += src.source_interval.length
                if target_hits:
                    for iv in merge_intervals(target_hits):
                        target.affected = add_interval(target.affected, iv)
                    transfer_ev.append(TransferEvidence(
                        event_id=eid, report_seq=rseq,
                        target_batch=payload.target_batch,
                        mappings=tuple(mappings)))

            elif payload.kind is PayloadType.PRODUCTION:
                assert isinstance(payload, ProductionPayload)
                hit_inputs: list[Segment] = []
                for seg in payload.inputs:
                    source = batches[seg.batch]
                    if intersect_lists(source.affected, [seg.interval]):
                        hit_inputs.append(seg)
                if hit_inputs:
                    output = batches[payload.output_batch]
                    whole = Interval(0, output.size)
                    output.affected = add_interval(output.affected, whole)
                    production_ev.append(ProductionEvidence(
                        event_id=eid, report_seq=rseq,
                        output_batch=payload.output_batch,
                        output_interval=whole,
                        hit_inputs=tuple(hit_inputs)))

            elif payload.kind is PayloadType.SHIPMENT:
                assert isinstance(payload, ShipmentPayload)
                grouped: dict[str, list[Interval]] = {}
                for seg in payload.sources:
                    source = batches[seg.batch]
                    hit = intersect_lists(source.affected, [seg.interval])
                    if hit:
                        grouped.setdefault(seg.batch, [])
                        grouped[seg.batch].extend(hit)
                for batch_id, ivs in grouped.items():
                    source = batches[batch_id]
                    recall_rows.append((
                        payload.consignee, eid, source.material, batch_id,
                        merge_intervals(ivs), rseq))

        return transfer_ev, production_ev, recall_rows

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def build_traceability_list(self, query: TraceQuery) -> TraceabilityResult:
        # 查询参数本身的结构校验
        param_errors: list[LedgerContradiction] = []
        if (not isinstance(query.business_cutoff, int)
                or not isinstance(query.report_cutoff, int)
                or query.business_cutoff < 0 or query.report_cutoff < 0):
            param_errors.append(LedgerContradiction(
                RefusalReason.INVALID_CUTOFF, None,
                f"截止号必须是非负整数: 业务={query.business_cutoff!r}, "
                f"报送={query.report_cutoff!r}"))
        if not query.marked_intervals:
            param_errors.append(LedgerContradiction(
                RefusalReason.EMPTY_MARK, None, "圈选片段不能为空"))
        if param_errors:
            return TraceabilityResult(
                refused=True,
                business_cutoff=query.business_cutoff,
                report_cutoff=query.report_cutoff,
                contradictions=tuple(param_errors))

        events, select_errors, audit = self._select_reports(query)

        # 来源必须是当时可见（最后版本未撤销）的收货批次
        receipt_event = next(
            ((eid, rseq, payload) for _biz, eid, rseq, payload in events
             if isinstance(payload, ReceiptPayload)
             and payload.batch == query.receipt_batch),
            None)
        if receipt_event is None:
            select_errors.append(LedgerContradiction(
                RefusalReason.UNKNOWN_SOURCE_BATCH, None,
                f"指定来源 {query.receipt_batch} 不是截止账内可见的收货批次"
                "（不存在、已撤销或在业务截止号之外）"))
            return TraceabilityResult(
                refused=True,
                business_cutoff=query.business_cutoff,
                report_cutoff=query.report_cutoff,
                adopted_reports=tuple(audit),
                contradictions=tuple(select_errors))
        receipt_eid, receipt_rseq, receipt_payload = receipt_event
        assert isinstance(receipt_payload, ReceiptPayload)

        # 圈选区间必须落在批次初始范围 [0, 初始批量) 内
        seeds = merge_intervals(query.marked_intervals)
        for iv in seeds:
            if iv.start < 0 or iv.end > receipt_payload.quantity:
                select_errors.append(LedgerContradiction(
                    RefusalReason.MARK_OUT_OF_RANGE, None,
                    f"圈选区间 {iv} 超出收货批 {query.receipt_batch} 的初始范围 "
                    f"[0,{receipt_payload.quantity})"))
        if select_errors:
            return TraceabilityResult(
                refused=True,
                business_cutoff=query.business_cutoff,
                report_cutoff=query.report_cutoff,
                adopted_reports=tuple(audit),
                contradictions=tuple(select_errors))

        # 实物账重放：任何矛盾都整单拒绝
        batches, replay_errors = self._replay(events)
        if replay_errors:
            return TraceabilityResult(
                refused=True,
                business_cutoff=query.business_cutoff,
                report_cutoff=query.report_cutoff,
                adopted_reports=tuple(audit),
                contradictions=tuple(select_errors + replay_errors))

        # 受影响传播（在同一份实物账上叠加受影响区间，保留已耗区间）
        transfer_ev, production_ev, recall_raw = self._propagate(
            events, batches, query.receipt_batch, seeds)

        # 在库隔离：受影响位置扣除全部已耗用位置；无余额的批次不出现
        isolation: list[IsolationRow] = []
        for batch_id in sorted(batches):
            batch = batches[batch_id]
            remaining = subtract_lists(batch.affected, batch.consumed)
            if not remaining:
                continue
            isolation.append(IsolationRow(
                warehouse=batch.warehouse,
                material=batch.material,
                batch=batch_id,
                intervals=tuple(remaining),
                quantity=total_length(remaining)))
        isolation.sort(key=lambda r: (r.warehouse, r.material, r.batch))

        # 召回：按 (收货方, 发运事件, 批次) 归并同一位置
        recall_map: dict[tuple[str, str, str, str, int], list[Interval]] = {}
        for consignee, eid, material, batch_id, ivs, rseq in recall_raw:
            recall_map.setdefault(
                (consignee, eid, material, batch_id, rseq), []).extend(ivs)
        recall: list[RecallRow] = []
        for (consignee, eid, material, batch_id, rseq), ivs in recall_map.items():
            merged = merge_intervals(ivs)
            recall.append(RecallRow(
                consignee=consignee, shipment_event_id=eid, material=material,
                batch=batch_id, intervals=tuple(merged),
                quantity=total_length(merged)))
        recall.sort(key=lambda r: (r.consignee, r.shipment_event_id, r.batch))

        return TraceabilityResult(
            refused=False,
            business_cutoff=query.business_cutoff,
            report_cutoff=query.report_cutoff,
            isolation=tuple(isolation),
            recall=tuple(recall),
            receipt_evidence=ReceiptMarkEvidence(
                event_id=receipt_eid, report_seq=receipt_rseq,
                batch=query.receipt_batch, marked_intervals=tuple(seeds)),
            transfer_evidence=tuple(transfer_ev),
            production_evidence=tuple(production_ev),
            adopted_reports=tuple(audit))


def build_traceability_list(
    reports: list[EventReport] | tuple[EventReport, ...], query: TraceQuery
) -> TraceabilityResult:
    """模块级便捷接口：给定报送流水与查询口径，返回隔离/召回清单结论。"""
    return LotTraceabilityEngine(reports).build_traceability_list(query)
