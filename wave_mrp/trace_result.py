"""来料抽检追溯的不可变输出模型（隔离/召回清单 + 直接依据 + 拒绝结论）。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .trace_model import Interval, Segment


class RefusalReason(Enum):
    """整单拒绝的原因类别；拒绝时不出具任何部分清单。"""

    UNKNOWN_SOURCE_BATCH = "unknown_source_batch"      # 指定来源不是可见收货批
    MARK_OUT_OF_RANGE = "mark_out_of_range"            # 圈选区间超出批次初始范围
    EMPTY_MARK = "empty_mark"                           # 未圈选任何片段
    INVALID_CUTOFF = "invalid_cutoff"                   # 截止号不是非负整数
    DUPLICATE_REPORT_SEQ = "duplicate_report_seq"       # 报送序号不唯一
    DUPLICATE_BUSINESS_SEQ = "duplicate_business_seq"   # 业务序号不唯一
    INCONSISTENT_EVENT_HEADER = "inconsistent_event_header"  # 同事件业务序号不一致
    INVALID_PAYLOAD = "invalid_payload"                 # 数量非正、空片段等结构问题
    DUPLICATE_BATCH = "duplicate_batch"                 # 批号重复建批（含清空后复用）
    MISSING_UPSTREAM_BATCH = "missing_upstream_batch"   # 引用了当时不存在的上游批
    OUT_OF_RANGE_SEGMENT = "out_of_range_segment"       # 片段超出该批初始范围
    DOUBLE_CONSUMPTION = "double_consumption"           # 同一位置被二次消耗
    WRONG_SOURCE_MATERIAL = "wrong_source_material"     # 调拨源批物料不一致
    WRONG_SOURCE_WAREHOUSE = "wrong_source_warehouse"   # 调拨源批原仓点不一致
    LATER_BATCH_SEQ = "later_batch_seq"                 # 从业务序号更晚的批次取货


@dataclass(frozen=True)
class IsolationRow:
    """隔离清单一行：某仓点某当前批次实际尚存的受影响区间。

    同一位置在同一行只计一次（区间互不重叠）；已被生产/调拨/发运消耗的
    位置不在其中，因此不会出现零数量行。
    """

    warehouse: str
    material: str
    batch: str
    intervals: tuple[Interval, ...]
    quantity: int


@dataclass(frozen=True)
class RecallRow:
    """召回清单一行：某收货方在某次发运事件中实际发出的受影响片段。"""

    consignee: str
    shipment_event_id: str
    material: str
    batch: str
    intervals: tuple[Interval, ...]
    quantity: int


@dataclass(frozen=True)
class IntervalMapping:
    """依据中的一对对应区间：源批区间 -> 目标/产出批区间。"""

    source_batch: str
    source_interval: Interval
    target_batch: str
    target_interval: Interval


@dataclass(frozen=True)
class ReceiptMarkEvidence:
    """直接依据①：命中的原收货区间（含采用的报送版本）。"""

    event_id: str
    report_seq: int
    batch: str
    marked_intervals: tuple[Interval, ...]   # 合并去重后的命中区间


@dataclass(frozen=True)
class TransferEvidence:
    """直接依据②：每次命中调拨的源批区间与对应目标批区间。

    只记录与受影响位置有交集的片段；mappings 在目标批坐标系下互不重叠。
    目标批可能是中间批——它出现在依据里，但只要已被后续业务消耗就不会
    误计为待处理货物。
    """

    event_id: str
    report_seq: int
    target_batch: str
    mappings: tuple[IntervalMapping, ...]


@dataclass(frozen=True)
class ProductionEvidence:
    """直接依据③：每次受影响生产的产出批与实际命中的投入区间。

    投入只要有一个位置受影响，产出整批受影响；hit_inputs 只列真正命中
    受影响位置的投入片段，其余投入不影响结论。
    """

    event_id: str
    report_seq: int
    output_batch: str
    output_interval: Interval                # 通常为整批 [0, 实报产量)
    hit_inputs: tuple[Segment, ...]          # 实际命中受影响位置的投入片段


@dataclass(frozen=True)
class LedgerContradiction:
    """一条拒绝依据：原因、涉及事件（或查询本身）与可读说明。"""

    reason: RefusalReason
    event_id: str | None                     # None 表示问题出在查询参数本身
    detail: str


@dataclass(frozen=True)
class TraceabilityResult:
    """清单查询结论。

    refused=True 时所有清单与依据为空，contradictions 给出全部拒绝来源；
    refused=False 时 contradictions 为空，清单与依据可直接交付。
    evidence 只含直接的一跳依据，不展开同一依据通往收货的所有路径。
    """

    refused: bool
    business_cutoff: int
    report_cutoff: int
    isolation: tuple[IsolationRow, ...] = ()
    recall: tuple[RecallRow, ...] = ()
    receipt_evidence: ReceiptMarkEvidence | None = None
    transfer_evidence: tuple[TransferEvidence, ...] = ()
    production_evidence: tuple[ProductionEvidence, ...] = ()
    # 账内每项业务实际采用的报送版本（撤销事件 report_seq 为 None），
    # 让依据引用的版本可以逐项回查；按业务序号排序。
    adopted_reports: tuple[tuple[str, int | None], ...] = ()
    contradictions: tuple[LedgerContradiction, ...] = ()

    @property
    def total_isolation_qty(self) -> int:
        return sum(row.quantity for row in self.isolation)

    @property
    def total_recall_qty(self) -> int:
        return sum(row.quantity for row in self.recall)
