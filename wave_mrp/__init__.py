"""波次领料 MRP 包：固定配方下的净需求汇总与整组投料求解。

另含来料抽检追溯子模块（trace_*）：按固定截止号重放收货/调拨/生产/发运
过账流水，输出在库隔离与发出召回清单及可回查的直接依据。
"""

from .engine import WaveMRPEngine
from .model import (
    Inventory,
    MaterialKind,
    NodeKey,
    OrderLine,
    Recipe,
    RecipeInput,
    Wave,
)
from .result import (
    Contribution,
    Defect,
    InventoryOverride,
    OrderOverride,
    PickingLine,
    PickingList,
    RawShortage,
    Revision,
    ShortageSource,
    WarehouseDocument,
)
from .trace_engine import LotTraceabilityEngine, build_traceability_list
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

__all__ = [
    "WaveMRPEngine",
    "Inventory",
    "MaterialKind",
    "NodeKey",
    "OrderLine",
    "Recipe",
    "RecipeInput",
    "Wave",
    "Contribution",
    "Defect",
    "InventoryOverride",
    "OrderOverride",
    "PickingLine",
    "PickingList",
    "RawShortage",
    "Revision",
    "ShortageSource",
    "WarehouseDocument",
    # 来料抽检追溯
    "LotTraceabilityEngine",
    "build_traceability_list",
    "EventReport",
    "Interval",
    "PayloadType",
    "ProductionPayload",
    "ReceiptPayload",
    "Segment",
    "ShipmentPayload",
    "TraceQuery",
    "TransferPayload",
    "TransferSegment",
    "IntervalMapping",
    "IsolationRow",
    "LedgerContradiction",
    "ProductionEvidence",
    "RecallRow",
    "ReceiptMarkEvidence",
    "RefusalReason",
    "TraceabilityResult",
    "TransferEvidence",
]
