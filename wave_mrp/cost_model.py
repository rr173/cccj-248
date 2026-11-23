"""存货成本结账的不可变输入模型。

实物数量仍完全以 ``trace_model`` 的过账流水为准；这里只补充“钱从哪里来”：
每笔生效收货一份整批入账金额，每笔生效生产一份加工金额，均以非负整数分
登记，``0`` 必须显式出现（缺记录不等于零元）。

成本记录用 ``(event_id, report_seq)`` 指向**一个具体版本**的报送；换版后的
新版本需要另有一条成本记录，旧版本的金额绝不会被借到新版本上。

金额是否合法由结账引擎统一校验：非法记录会让整次结账拒绝并指出对应事件
与报送，而不是在构造对象时抛出，避免丢失“哪条记录有问题”的信息。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReceiptCost:
    """收货事件某版本的整批入账金额（分）；同一批次不再拆分金额来源。"""

    event_id: str
    report_seq: int
    amount: int


@dataclass(frozen=True)
class ProductionCost:
    """生产事件某版本的加工金额（分），与投料成本共同构成产出批成本。

    产出批成本 = 实际耗用片段的全部成本 + 加工金额，与投入/产出数量是否
    相等无关；加工金额为 0 也必须显式登记。
    """

    event_id: str
    report_seq: int
    amount: int


@dataclass(frozen=True)
class CostTable:
    """一次结账使用的成本表。

    同一 ``(event_id, report_seq)`` 只允许一条同类记录；重复登记由结账引擎
    记为矛盾，而不是在此处静默覆盖。
    """

    receipt_costs: tuple[ReceiptCost, ...] = ()
    production_costs: tuple[ProductionCost, ...] = ()


@dataclass(frozen=True)
class ClosingQuery:
    """存货成本结账口径（业务截止号含端点）。

    ``opening_business_cutoff`` 及以前（含）的业务进期初余额；
    ``closing_business_cutoff`` 及以前（含）的业务进期末余额；
    ``report_cutoff`` 是整次结账统一使用的报送截止号，期初与期末都只看
    该编号以内的报送版本。两个业务截止号可以相同（表示本期无业务），
    但期末不得早于期初。
    """

    opening_business_cutoff: int
    closing_business_cutoff: int
    report_cutoff: int
