"""更正包审包的不可变输入模型（补报裁定）。

收单员收进的不是单条更正，而是一**包**更正：每包有唯一单号，包内更正
按单内顺序排列，被视为**同时生效**。更正只针对历史账中已经存在的事件，
是对该事件报送内容的完整替换或撤销：

  - 不新建事件身份（``event_id`` 必须已在历史账中出现）；
  - 不改业务序号（新报送沿用该事件历史上登记的 ``business_seq``）；
  - 每条更正必须写明制单时看到的报送序号 ``observed_report_seq``，
    该序号须与裁定当时的现版报送序号一致，否则视为版本已过期，整包拒绝；
  - 撤销也是一个现版：``replacement=None`` 产生一条撤销报送；要恢复一张
    已撤销的单据，``observed_report_seq`` 须引用那条撤销报送的序号。
"""

from __future__ import annotations

from dataclasses import dataclass

from .trace_model import Payload


@dataclass(frozen=True)
class Correction:
    """一条针对既有事件的更正。

    ``replacement`` 为完整替换载荷；``replacement=None`` 表示撤销该事件。
    ``observed_report_seq`` 是制单时看到的现版报送序号，用于乐观版本校验。
    """

    event_id: str
    observed_report_seq: int
    replacement: Payload | None


@dataclass(frozen=True)
class CorrectionPackage:
    """一个更正包：唯一单号 + 按单内顺序排列的一组更正。

    包内所有更正在裁定时同时生效；空包、同包重复修改同一事件等结构问题
    由审包引擎整包拒绝，不产生任何报送。
    """

    package_id: str
    corrections: tuple[Correction, ...]
