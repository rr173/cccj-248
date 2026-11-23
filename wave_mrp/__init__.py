"""波次领料 MRP 包：固定配方下的净需求汇总与整组投料求解。

另含两个以过账流水为准的子模块：
  - 来料抽检追溯（trace_*）：按固定截止号重放收货/调拨/生产/发运流水，
    输出在库隔离与发出召回清单及可回查的直接依据；
  - 存货成本结账（cost_*）：在同一份实物账上贴整批入账金额与加工金额，
    输出期初/期内/期末三张可核对的成本表。
"""

from .amendment_engine import (
    CorrectionAcceptanceEngine,
    adjudicate_correction_packages,
)
from .amendment_model import Correction, CorrectionPackage
from .amendment_result import AmendmentResult, CorrectionDecision
from .cost_engine import (
    CostClosingEngine,
    InventoryCostClosingEngine,
    build_closing_statement,
    close_inventory_costs,
)
from .cost_model import ClosingQuery, CostTable, ProductionCost, ReceiptCost
from .cost_result import ClosingStatement, MovementLine, StockBalance
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
    # 存货成本结账
    "InventoryCostClosingEngine",
    "CostClosingEngine",
    "close_inventory_costs",
    "build_closing_statement",
    "ClosingQuery",
    "CostTable",
    "ProductionCost",
    "ReceiptCost",
    "ClosingStatement",
    "MovementLine",
    "StockBalance",
    # 更正包审包（补报裁定）
    "CorrectionAcceptanceEngine",
    "adjudicate_correction_packages",
    "Correction",
    "CorrectionPackage",
    "AmendmentResult",
    "CorrectionDecision",
]
