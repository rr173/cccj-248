"""更正包审包的不可变输出模型（每包接纳结果 + 本次可追加的补报）。"""

from __future__ import annotations

from dataclasses import dataclass

from .trace_model import EventReport
from .trace_result import LedgerContradiction


@dataclass(frozen=True)
class CorrectionDecision:
    """单个更正包的裁定结果，按收单顺序返回。

    接纳时：``accepted=True``，``new_reports`` 给出本包产生的连续新报送
    （按包内更正顺序），``contradictions`` 为空。
    拒绝时：``accepted=False``，``new_reports`` 为空，``contradictions``
    给出所涉事件与具体原因；结构原因（空包、重复修改、事件不存在、
    版本过期）与实物矛盾（沿用过账账的 ``RefusalReason``，定位到事件与
    批次）都在此列出，不要求列完所有矛盾。
    """

    package_id: str
    accepted: bool
    new_reports: tuple[EventReport, ...] = ()
    contradictions: tuple[LedgerContradiction, ...] = ()

    @property
    def new_report_seqs(self) -> tuple[int, ...]:
        """本包取得的新报送序号（按包内顺序）；拒绝包为空。"""
        return tuple(rep.report_seq for rep in self.new_reports)


@dataclass(frozen=True)
class AmendmentResult:
    """一批更正包的整体裁定结论。

    历史账本身有矛盾时不生成发布结果：``history_refused=True``，包级结果
    与新增报送均为空，``history_contradictions`` 指出历史矛盾原因。

    历史账可用时：
      - ``package_results`` 与输入包同序，逐包给出接纳/拒绝；
      - ``new_reports`` 是全部已接纳包产生的新 ``EventReport``（追加到
        历史之后即可在原实物账接口上按 ``publish_cutoff`` 读到新账）；
      - ``publish_cutoff`` 是发布截止号：接纳的更正按包及包内顺序取得
        连续报送号，从历史最大报送号之后开始；无新增时仍为历史最大
        报送号（空历史以 0 为起点）。拒绝包不占用任何报送号，因此
        ``publish_cutoff`` 永远不会落在一个只生效了一部分的包中间。
    """

    history_refused: bool
    publish_cutoff: int
    package_results: tuple[CorrectionDecision, ...] = ()
    new_reports: tuple[EventReport, ...] = ()
    history_contradictions: tuple[LedgerContradiction, ...] = ()

    @property
    def accepted_packages(self) -> tuple[CorrectionDecision, ...]:
        return tuple(d for d in self.package_results if d.accepted)

    @property
    def refused_packages(self) -> tuple[CorrectionDecision, ...]:
        return tuple(d for d in self.package_results if not d.accepted)
