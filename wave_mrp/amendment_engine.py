"""更正包审包引擎：把多份更正包裁定成一份可发布的补报。

设计目标
--------
历史账可能保留多年流水，而一批更正通常只触及少数货批。初次准备时做一次
全账校验（复用追溯模块的报送筛选与实物账重放），此后每个包**不得**重读
历史、复制整份账或逐单重验无关货批。为此引擎在内存中只维护两类增量索引：

  - 现版索引：每个事件当前采用的报送序号与载荷（撤销事件载荷为 ``None``）；
  - 关系索引：事件 ↔ 批次的二部图，边来自**现版载荷**实际建立或引用的批次。

裁定一个包时，先做包级结构校验（空包、同包重复修改、事件不存在、制单
版本过期），再从被改事件出发沿关系图求**关联闭包**——撤下去的旧关系与
加进来的新关系都纳入播种——只把闭包内的事件按业务序号组成子账重放。
与本包没有共同建批、引用或占用关系的实物记录不会被再次处理。子账沿用
全账同一套实物规则（``LotTraceabilityEngine.replay_physical_ledger``），
货段始终以批内半开区间表达，不按件展开。

接纳一个包只改动上述索引并追加报送对象；拒绝包不改动任何现版、货段
占用或报送序号，后续包继续按收单顺序裁定。
"""

from __future__ import annotations

from .amendment_model import Correction, CorrectionPackage
from .amendment_result import AmendmentResult, CorrectionDecision
from .trace_engine import LotTraceabilityEngine
from .trace_model import (
    EventReport,
    Payload,
    ProductionPayload,
    ReceiptPayload,
    ShipmentPayload,
    TransferPayload,
)
from .trace_result import LedgerContradiction, RefusalReason


def _dedup(errors: list[LedgerContradiction]) -> list[LedgerContradiction]:
    """保持首次出现顺序去重（矛盾对象本身不可变，可直接比较）。"""
    seen: set[LedgerContradiction] = set()
    out: list[LedgerContradiction] = []
    for err in errors:
        if err not in seen:
            seen.add(err)
            out.append(err)
    return out


class CorrectionAcceptanceEngine:
    """固定历史报送流水的更正包审包引擎；同一流水可反复受理多批更正包。"""

    def __init__(self, reports: list[EventReport] | tuple[EventReport, ...]) -> None:
        self._reports = tuple(reports)
        self._selector = LotTraceabilityEngine(self._reports)

        # 现版索引：事件 -> (现版报送序号, 现版载荷|None, 业务序号)
        self._current_seq: dict[str, int] = {}
        self._current_payload: dict[str, Payload | None] = {}
        self._business_seq: dict[str, int] = {}
        # 关系索引：事件 -> 相关批次；批次 -> 相关事件（只含现版非撤销载荷）
        self._event_batches: dict[str, set[str]] = {}
        self._batch_events: dict[str, set[str]] = {}

        self._history_errors: list[LedgerContradiction] = []
        self._next_seq = 0
        self._bootstrap()

    # ------------------------------------------------------------------ #
    # 初次准备：全账取现版并做一次全量实物校验，建立增量索引
    # ------------------------------------------------------------------ #
    def _bootstrap(self) -> None:
        valid_seq = [r.report_seq for r in self._reports
                     if isinstance(r.report_seq, int)]
        valid_biz = [r.business_seq for r in self._reports
                     if isinstance(r.business_seq, int)]
        report_cutoff = max(valid_seq, default=0)
        business_cutoff = max(valid_biz, default=0)
        self._next_seq = report_cutoff

        events, select_errors, _audit = self._selector.select_reports(
            business_cutoff, report_cutoff)
        _batches, replay_errors = self._selector.replay_physical_ledger(events)
        self._history_errors = _dedup(select_errors + replay_errors)
        if self._history_errors:
            # 历史本身有矛盾：本次不生成发布结果，也不建立可裁定状态
            return

        for biz, eid, rseq, payload in events:
            self._current_seq[eid] = rseq
            self._current_payload[eid] = payload
            self._business_seq[eid] = biz
            if payload is not None:
                self._index_event(eid, payload)

    @staticmethod
    def _relations(payload: Payload | None) -> tuple[str | None, tuple[str, ...]]:
        """返回一个现版载荷 (建立的批次, 引用的批次)；撤销载荷没有关系。"""
        if payload is None:
            return None, ()
        if isinstance(payload, ReceiptPayload):
            return payload.batch, ()
        if isinstance(payload, TransferPayload):
            return (payload.target_batch,
                    tuple(seg.source_batch for seg in payload.sources))
        if isinstance(payload, ProductionPayload):
            return (payload.output_batch,
                    tuple(seg.batch for seg in payload.inputs))
        if isinstance(payload, ShipmentPayload):
            return None, tuple(seg.batch for seg in payload.sources)
        return None, ()  # 未知类型留给子账重放报 INVALID_PAYLOAD

    def _index_event(self, eid: str, payload: Payload) -> None:
        created, refs = self._relations(payload)
        batches: set[str] = set()
        if created is not None:
            batches.add(created)
        batches.update(refs)
        self._event_batches[eid] = batches
        for batch in batches:
            self._batch_events.setdefault(batch, set()).add(eid)

    def _drop_event_index(self, eid: str) -> None:
        for batch in self._event_batches.pop(eid, ()):
            events_of_batch = self._batch_events.get(batch)
            if events_of_batch is not None:
                events_of_batch.discard(eid)
                if not events_of_batch:
                    del self._batch_events[batch]

    # ------------------------------------------------------------------ #
    # 关联闭包：被改事件（旧关系+新关系）播种，沿事件↔批次二部图扩展
    # ------------------------------------------------------------------ #
    def _related_events(
        self,
        targets: dict[str, Payload | None],
    ) -> set[str]:
        touched: set[str] = set()
        batch_queue: list[str] = []

        def add_batches_of(payload: Payload | None) -> None:
            created, refs = self._relations(payload)
            if created is not None:
                batch_queue.append(created)
            batch_queue.extend(refs)

        def seed_event(eid: str, payload: Payload | None) -> None:
            if eid in touched:
                return
            touched.add(eid)
            add_batches_of(payload)

        # 被改事件用“旧现版载荷 + 生效后载荷”双重播种：撤下去的旧关系
        # （例如撤销建批事件后仍引用该批的下游单据）与加进来的新关系都
        # 必须进入闭包，不能只看替换后的载荷。
        for eid, replacement in targets.items():
            touched.add(eid)
            add_batches_of(self._current_payload.get(eid))
            add_batches_of(replacement)

        head = 0
        while head < len(batch_queue):
            batch = batch_queue[head]
            head += 1
            for eid in tuple(self._batch_events.get(batch, ())):
                seed_event(eid, self._current_payload.get(eid))
        return touched

    # ------------------------------------------------------------------ #
    # 逐包裁定
    # ------------------------------------------------------------------ #
    def adjudicate(
        self, packages: list[CorrectionPackage] | tuple[CorrectionPackage, ...]
    ) -> AmendmentResult:
        """按收单顺序裁定整批更正包，返回补报与逐包接纳结果。"""
        if self._history_errors:
            return AmendmentResult(
                history_refused=True,
                publish_cutoff=self._next_seq,
                history_contradictions=tuple(self._history_errors))

        decisions: list[CorrectionDecision] = []
        new_reports: list[EventReport] = []
        for package in packages:
            decision, built = self._adjudicate_one(package)
            decisions.append(decision)
            new_reports.extend(built)

        return AmendmentResult(
            history_refused=False,
            publish_cutoff=self._next_seq,
            package_results=tuple(decisions),
            new_reports=tuple(new_reports))

    def _adjudicate_one(
        self, package: CorrectionPackage
    ) -> tuple[CorrectionDecision, list[EventReport]]:
        errors: list[LedgerContradiction] = []

        # ---- 1. 包级结构校验（整包拒绝，不改任何现版）----
        if not package.corrections:
            errors.append(LedgerContradiction(
                RefusalReason.EMPTY_CORRECTION_PACKAGE, None,
                f"更正包 {package.package_id} 为空，包内至少需要一条更正"))

        seen_targets: set[str] = set()
        replacements: dict[str, Payload | None] = {}
        for correction in package.corrections:
            eid = correction.event_id
            if eid in seen_targets:
                errors.append(LedgerContradiction(
                    RefusalReason.DUPLICATE_CORRECTION_TARGET, eid,
                    f"更正包 {package.package_id} 内重复修改事件 {eid}，"
                    "同一事件在一包内只能更正一次"))
            seen_targets.add(eid)
            replacements.setdefault(eid, correction.replacement)

            if eid not in self._current_seq:
                errors.append(LedgerContradiction(
                    RefusalReason.UNKNOWN_CORRECTION_EVENT, eid,
                    f"事件 {eid} 不在历史现版账中（不存在或从未可见），"
                    "更正不新建事件身份"))
                continue
            observed = correction.observed_report_seq
            current = self._current_seq[eid]
            if not isinstance(observed, int) or observed < 0:
                errors.append(LedgerContradiction(
                    RefusalReason.STALE_CORRECTION_VERSION, eid,
                    f"事件 {eid} 的制单所见报送序号非法: {observed!r}，"
                    f"现版报送序号为 {current}"))
            elif observed != current:
                errors.append(LedgerContradiction(
                    RefusalReason.STALE_CORRECTION_VERSION, eid,
                    f"事件 {eid} 的制单所见报送序号 {observed} 已过期，"
                    f"现版报送序号为 {current}（恢复已撤销单据须引用撤销报送）"))

        if errors:
            return (CorrectionDecision(
                package_id=package.package_id, accepted=False,
                contradictions=tuple(_dedup(errors))), [])

        # ---- 2. 关联闭包 + 子账重放（只重验与本包相连的实物记录）----
        touched = self._related_events(replacements)
        sub_events: list[tuple[int, str, int, Payload | None]] = []
        for eid in touched:
            payload = replacements.get(eid, self._current_payload[eid])
            sub_events.append(
                (self._business_seq[eid], eid, self._current_seq[eid], payload))
        sub_events.sort(key=lambda item: item[0])

        _batches, replay_errors = self._selector.replay_physical_ledger(sub_events)
        if replay_errors:
            return (CorrectionDecision(
                package_id=package.package_id, accepted=False,
                contradictions=tuple(_dedup(replay_errors))), [])

        # ---- 3. 接纳：按包内顺序连续发新报送号并更新现版/关系索引 ----
        built: list[EventReport] = []
        for correction in package.corrections:
            eid = correction.event_id
            self._next_seq += 1
            report = EventReport(
                self._next_seq, eid, self._business_seq[eid],
                correction.replacement)
            built.append(report)

            old_payload = self._current_payload[eid]
            if old_payload is not None:
                self._drop_event_index(eid)
            self._current_seq[eid] = self._next_seq
            self._current_payload[eid] = correction.replacement
            if correction.replacement is not None:
                self._index_event(eid, correction.replacement)

        return (CorrectionDecision(
            package_id=package.package_id, accepted=True,
            new_reports=tuple(built)), built)


def adjudicate_correction_packages(
    reports: list[EventReport] | tuple[EventReport, ...],
    packages: list[CorrectionPackage] | tuple[CorrectionPackage, ...],
) -> AmendmentResult:
    """模块级便捷接口：给定历史报送流水与按收单顺序排列的更正包。

    返回本次可追加的补报（``new_reports``）、逐包接纳结果与发布截止号；
    原始 ``reports`` 列表不被改动，把新增记录追加到历史后，原有实物账
    接口可按发布截止号读取新账、用原截止号仍读到旧账。
    """
    return CorrectionAcceptanceEngine(reports).adjudicate(packages)
