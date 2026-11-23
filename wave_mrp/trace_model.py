"""来料抽检追溯的不可变输入模型（批片段过账流水）。

口径与主模型一致：物料、仓点为字符串标识，数量全部为精确整数，不做单位
转换、不做浮点拆分。批次内位置从 0 开始连续编号，且**不随出库重编号**；
所有片段都用半开整数区间 ``[start, end)`` 表达，即使批量超过万亿也只存
区间端点，绝不逐件分配内存。

流水组织：
  - 每项业务有稳定的 ``event_id`` 与唯一的 ``business_seq``（业务序号）；
  - 同一事件可多次报送，每次报送是一次完整替换，``payload=None`` 表示撤销；
  - ``report_seq``（报送序号）全局唯一，报送列表的排列不代表先后。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Union


class PayloadType(Enum):
    """业务事件类型：收货 / 调拨 / 生产 / 发运。"""

    RECEIPT = "receipt"
    TRANSFER = "transfer"
    PRODUCTION = "production"
    SHIPMENT = "shipment"


@dataclass(frozen=True)
class Interval:
    """批内半开整数区间 ``[start, end)``，端点为批内位置（从 0 起）。"""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise TypeError("区间端点必须是整数")
        if self.start < 0:
            raise ValueError(f"非法区间 {self}: 起点不能为负")
        if self.end <= self.start:
            raise ValueError(f"非法区间 {self}: 片段必须非空")

    @property
    def length(self) -> int:
        return self.end - self.start

    def shifted(self, delta: int) -> "Interval":
        """整体平移 delta（用于调拨源批位置映射到目标批位置）。"""
        return Interval(self.start + delta, self.end + delta)

    def __str__(self) -> str:
        return f"[{self.start},{self.end})"


@dataclass(frozen=True)
class Segment:
    """对某批一段半开区间的引用（生产投入、发运耗用通用）。"""

    batch: str
    interval: Interval


@dataclass(frozen=True)
class ReceiptPayload:
    """收货过账：建立一个新批次，初始范围固定为 ``[0, quantity)``。"""

    batch: str
    material: str
    warehouse: str
    quantity: int
    kind: PayloadType = field(default=PayloadType.RECEIPT, compare=False)


@dataclass(frozen=True)
class TransferSegment:
    """调拨单上的一个源批片段（单据顺序即 tuple 顺序）。"""

    source_batch: str
    source_interval: Interval


@dataclass(frozen=True)
class TransferPayload:
    """调拨过账：同物料、同原仓点的若干源批片段按单上顺序拼成目标仓点新批。

    目标批内位置由片段顺序累加确定：第 k 个片段的目标起点等于此前各片段
    长度之和，片段在新批中的相对位置保持不变。
    """

    target_batch: str
    target_warehouse: str
    sources: tuple[TransferSegment, ...]
    kind: PayloadType = field(default=PayloadType.TRANSFER, compare=False)


@dataclass(frozen=True)
class ProductionPayload:
    """生产过账：在登记仓点生成一个新批，产量取实报值。

    产量不要求与投入数量相等；投入片段可来自任意此前尚有余额的批次，
    不要求从某张调拨单的开头取货。
    """

    output_batch: str
    output_material: str
    registration_warehouse: str
    reported_output_qty: int
    inputs: tuple[Segment, ...]
    kind: PayloadType = field(default=PayloadType.PRODUCTION, compare=False)


@dataclass(frozen=True)
class ShipmentPayload:
    """发运过账：消耗登记的批片段，记在指定收货方名下。"""

    consignee: str
    sources: tuple[Segment, ...]
    kind: PayloadType = field(default=PayloadType.SHIPMENT, compare=False)


Payload = Union[ReceiptPayload, TransferPayload, ProductionPayload, ShipmentPayload]


@dataclass(frozen=True)
class EventReport:
    """一次报送。

    ``payload=None`` 是撤销报送：该事件在截止账内视为不发生。
    同一 ``event_id`` 的多次报送以报送序号最大者为最后可见版本。
    """

    report_seq: int
    event_id: str
    business_seq: int
    payload: Payload | None


@dataclass(frozen=True)
class TraceQuery:
    """一次清单查询的固定口径（两个截止号含端点）。

    marked_intervals 允许重叠，引擎负责合并；它们必须全部落在收货批次的
    初始范围 ``[0, 初始批量)`` 内，否则整单拒绝、不出具部分清单。
    """

    receipt_batch: str
    marked_intervals: tuple[Interval, ...]
    business_cutoff: int
    report_cutoff: int
