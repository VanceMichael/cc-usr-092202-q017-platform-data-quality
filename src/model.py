"""平台消费数据可信度领域：枚举、数据结构与门槛常量。

设计原则：
- 迟报、撤回、接口改版、历史回补都只产生“新批次”，历史批次只追加、不改写；
- 异常波动先进入核查（隔离），不直接进入公开统计；
- 采用任一平台指标前，必须能输出版本化口径快照、质量评分与审批范围。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ---------------------------------------------------------------- 角色

class Role(str, Enum):
    PLATFORM = "platform"        # 平台数据贡献方：只能查看本平台问题
    STATISTICIAN = "statistician"  # 统计分析人员：可比较来源差异
    REVIEWER = "reviewer"        # 口径审查人员
    PUBLISHER = "publisher"      # 公开发布人员


INTERNAL_ROLES = {Role.STATISTICIAN, Role.REVIEWER, Role.PUBLISHER}


# ---------------------------------------------------------------- 批次

class BatchKind(str, Enum):
    SCHEDULED = "scheduled"        # 按期报送
    LATE = "late"                  # 迟报（仍形成独立批次）
    API_REVISION = "api_revision"  # 接口改版后的重新报送
    BACKFILL = "backfill"          # 历史回补
    WITHDRAWAL = "withdrawal"      # 撤回


# 不参与“迟报”判定的批次类型：改版/回补/撤回按自身事件时间入账
TIMELY_KINDS = {BatchKind.SCHEDULED, BatchKind.LATE}


# ---------------------------------------------------------------- 校验

class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


# 工单严重度与校验级别对应
class TicketSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_TO_TICKET = {
    Severity.WARNING: TicketSeverity.MEDIUM,
    Severity.ERROR: TicketSeverity.HIGH,
    Severity.BLOCKER: TicketSeverity.CRITICAL,
}


class TicketStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"                  # 核查后关闭
    VOIDED_SUPERSEDED = "voided_superseded"  # 批次被新批次替代，问题自然作废
    VOIDED_WITHDRAWN = "voided_withdrawn"    # 批次被撤回


@dataclass
class Finding:
    """一次自动校验发现。blocking=True 时该观测值不得进入公开统计。"""

    code: str
    severity: Severity
    message: str
    obs_id: str | None = None
    category: str | None = None
    blocking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "obs_id": self.obs_id,
            "category": self.category,
            "blocking": self.blocking,
        }


@dataclass
class Ticket:
    """由校验发现自动生成、对平台可见的问题工单。"""

    id: str
    platform_id: str
    batch_id: str
    code: str
    title: str
    severity: TicketSeverity
    obs_id: str | None
    blocking: bool
    created_at: str
    status: TicketStatus = TicketStatus.OPEN
    resolution: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        data["status"] = self.status.value
        return data


# ---------------------------------------------------------------- 评分

# 六个评分维度与权重：覆盖、映射、及时性、口径稳定、文档完备、异常占比
SCORE_WEIGHTS = {
    "coverage": 0.20,
    "mapping": 0.20,
    "timeliness": 0.15,
    "stability": 0.20,
    "documentation": 0.10,
    "anomaly": 0.15,
}


class Grade(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


def grade_for(score: float) -> Grade:
    if score >= 85:
        return Grade.A
    if score >= 70:
        return Grade.B
    if score >= 50:
        return Grade.C
    return Grade.D


@dataclass
class QualityScore:
    batch_id: str
    platform_id: str
    period: str
    score: float
    grade: Grade
    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=lambda: dict(SCORE_WEIGHTS))

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "platform_id": self.platform_id,
            "period": self.period,
            "score": round(self.score, 1),
            "grade": self.grade.value,
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "weights": self.weights,
        }


# ---------------------------------------------------------------- 审批与置信

class UsageScope(str, Enum):
    INTERNAL_REFERENCE = "internal_reference"  # 内部参考
    AGGREGATE_TREND = "aggregate_trend"      # 进入汇总趋势
    PUBLIC_RELEASE = "public_release"        # 公开发布


# 各可用范围的最低分数
SCORE_GATES = {
    UsageScope.INTERNAL_REFERENCE: 50,
    UsageScope.AGGREGATE_TREND: 70,
    UsageScope.PUBLIC_RELEASE: 85,
}

# 各范围的审批角色
SCORE_APPROVERS = {
    UsageScope.INTERNAL_REFERENCE: Role.REVIEWER,
    UsageScope.AGGREGATE_TREND: Role.REVIEWER,
    UsageScope.PUBLIC_RELEASE: Role.PUBLISHER,
}

# 范围序号，便于“高范围覆盖低范围”判断
SCOPE_ORDER = [
    UsageScope.INTERNAL_REFERENCE,
    UsageScope.AGGREGATE_TREND,
    UsageScope.PUBLIC_RELEASE,
]


class Confidence(str, Enum):
    HIGH = "高"
    MEDIUM = "中"
    LOW = "低"
    UNUSABLE = "不可用"


@dataclass
class Approval:
    platform_id: str
    period: str
    scope: UsageScope
    batch_id: str  # 审批绑定具体批次；批次被替代即自动失效
    approver: str
    granted_at: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_id": self.platform_id,
            "period": self.period,
            "scope": self.scope.value,
            "batch_id": self.batch_id,
            "approver": self.approver,
            "granted_at": self.granted_at,
            "note": self.note,
        }


# ---------------------------------------------------------------- 口径字段

# 每次报送必须随附的六项口径 + 质量声明
METHODOLOGY_FIELDS = (
    "coverage_note",        # 样本覆盖
    "dictionary_version",   # 类目字典版本（映射表另做非空校验）
    "dedup_method",         # 去重方法
    "refund_handling",      # 退款处理
    "price_basis",          # 价格口径
    "promo_basis",          # 促销口径
)
QUALITY_STATEMENT_FIELD = "quality_statement"

# 参与“可比性”比对的口径签名（覆盖率是实现结果，不参与口径签名）
CALIBRE_SIGNATURE_FIELDS = (
    "coverage_note",
    "dictionary_version",
    "dedup_method",
    "refund_handling",
    "price_basis",
    "promo_basis",
)

CALIBRE_LABELS = {
    "coverage_note": "样本覆盖口径",
    "dictionary_version": "类目字典版本",
    "dedup_method": "去重方法",
    "refund_handling": "退款处理",
    "price_basis": "价格口径",
    "promo_basis": "促销口径",
}
