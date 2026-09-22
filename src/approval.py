"""可用范围审批与采用指标时的血缘/置信说明。

审批绑定“具体批次”：一旦该期出现更新批次（改版重报、回补、撤回），原审批自动失效，
须按新批次重新评分、重新审批——这保证公开发布永远锚定当前有效批次及其口径快照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .dataset import Batch, Dataset
from .model import (
    CALIBRE_LABELS,
    Confidence,
    Grade,
    QualityScore,
    Role,
    SCOPE_ORDER,
    SCORE_APPROVERS,
    SCORE_GATES,
    Ticket,
    TicketStatus,
    UsageScope,
    Approval,
)
from .validation import BatchReport


@dataclass
class ApprovalDecision:
    approved: bool
    scope: UsageScope
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "scope": self.scope.value,
            "reasons": self.reasons,
        }


def _open_tickets(tickets: list[Ticket], batch_id: str) -> list[Ticket]:
    return [t for t in tickets if t.batch_id == batch_id and t.status is TicketStatus.OPEN]


def evaluate_scope(
    scope: UsageScope,
    batch: Batch,
    score: QualityScore,
    report: BatchReport,
    tickets: list[Ticket],
) -> ApprovalDecision:
    """按门槛与阻断条件判断该批次能否获批某可用范围。"""
    reasons: list[str] = []
    open_tk = _open_tickets(tickets, batch.batch_id)
    blocking_open = [t for t in open_tk if t.blocking]

    if report.blocked:
        reasons.append("批次存在批次级阻断问题（口径资料缺失/覆盖过低/字典非法）")
    if score.score < SCORE_GATES[scope]:
        reasons.append(
            f"质量评分 {score.score}（{score.grade.value}）低于 {scope.value} 门槛 {SCORE_GATES[scope]}"
        )

    # 汇总趋势：不得有未关闭的阻断工单
    if scope in (UsageScope.AGGREGATE_TREND, UsageScope.PUBLIC_RELEASE) and blocking_open:
        reasons.append(
            f"尚有 {len(blocking_open)} 个阻断性工单未核查关闭："
            + "、".join(sorted({t.code for t in blocking_open}))
        )

    # 公开发布：不得有任何被隔离观测值，评分等级须为 A
    if scope is UsageScope.PUBLIC_RELEASE:
        if report.quarantined_obs:
            reasons.append(
                f"存在 {len(report.quarantined_obs)} 个被隔离待核查的类目观测，不得公开发布"
            )
        if score.grade is not Grade.A:
            reasons.append(f"公开发布要求质量等级 A，当前为 {score.grade.value}")
        if score.components.get("stability", 1.0) < 1.0:
            reasons.append("口径相对上一有效批次发生变化，需在发布说明中披露或恢复可比口径")

    return ApprovalDecision(approved=not reasons, scope=scope, reasons=reasons)


def approver_role_for(scope: UsageScope) -> Role:
    return SCORE_APPROVERS[scope]


class ApprovalBook:
    """审批登记册：登记、按批次失效、查询有效审批。"""

    def __init__(self) -> None:
        self._items: list[Approval] = []

    def grant(
        self,
        scope: UsageScope,
        batch: Batch,
        score: QualityScore,
        report: BatchReport,
        tickets: list[Ticket],
        approver: str,
        approver_role: Role,
        granted_at: str,
        note: str = "",
    ) -> ApprovalDecision:
        decision = evaluate_scope(scope, batch, score, report, tickets)
        if approver_role is not approver_role_for(scope):
            decision.approved = False
            decision.reasons.append(
                f"{scope.value} 须由 {approver_role_for(scope).value} 审批"
            )
        if decision.approved:
            approval = Approval(
                platform_id=batch.platform_id,
                period=batch.period,
                scope=scope,
                batch_id=batch.batch_id,
                approver=approver,
                granted_at=granted_at,
                note=note,
            )
            # 同范围旧审批（必然指向旧批次）随新登记失效
            self._items = [
                a for a in self._items
                if not (a.platform_id == batch.platform_id
                        and a.period == batch.period and a.scope is scope)
            ]
            self._items.append(approval)
        return decision

    def active_for(self, ds: Dataset, platform_id: str, period: str) -> list[Approval]:
        """返回仍锚定当前有效批次的审批；批次已被替代/撤回的自动失效。"""
        effective = ds.effective_batch(platform_id, period)
        if effective is None:
            return []
        return [
            a for a in self._items
            if a.platform_id == platform_id and a.period == period
            and a.batch_id == effective.batch_id
        ]

    def highest_active_scope(self, ds: Dataset, platform_id: str, period: str) -> UsageScope | None:
        active = self.active_for(ds, platform_id, period)
        if not active:
            return None
        idx = max(SCOPE_ORDER.index(a.scope) for a in active)
        return SCOPE_ORDER[idx]

    def all(self) -> list[Approval]:
        return list(self._items)


# ---------------------------------------------------------------- 血缘与置信

@dataclass
class LineageStatement:
    """采用某平台某期指标时给出的血缘与置信说明。"""

    platform_id: str
    period: str
    batch_id: str
    batch_kind: str
    supersedes: str | None
    approved_scope: str | None
    confidence: str
    usable: bool
    score: float
    grade: str
    calibre: dict[str, str]
    dictionary_version: str
    quality_statement: str
    open_tickets: int
    blocking_tickets: int
    quarantined_categories: list[str]
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_id": self.platform_id,
            "period": self.period,
            "batch_id": self.batch_id,
            "batch_kind": self.batch_kind,
            "supersedes": self.supersedes,
            "approved_scope": self.approved_scope,
            "confidence": self.confidence,
            "usable": self.usable,
            "score": self.score,
            "grade": self.grade,
            "calibre": self.calibre,
            "dictionary_version": self.dictionary_version,
            "quality_statement": self.quality_statement,
            "open_tickets": self.open_tickets,
            "blocking_tickets": self.blocking_tickets,
            "quarantined_categories": self.quarantined_categories,
            "reasons": self.reasons,
        }


def build_lineage(
    ds: Dataset,
    platform_id: str,
    period: str,
    reports: dict[str, BatchReport],
    scores: dict[str, QualityScore],
    tickets: list[Ticket],
    book: ApprovalBook,
) -> LineageStatement:
    batch = ds.effective_batch(platform_id, period)
    if batch is None:
        return LineageStatement(
            platform_id=platform_id, period=period, batch_id="", batch_kind="",
            supersedes=None, approved_scope=None, confidence=Confidence.UNUSABLE.value,
            usable=False, score=0.0, grade=Grade.D.value, calibre={},
            dictionary_version="", quality_statement="", open_tickets=0,
            blocking_tickets=0, quarantined_categories=[],
            reasons=["该平台该期当前无有效批次（已撤回或尚未报送）"],
        )

    report = reports[batch.batch_id]
    score = scores[batch.batch_id]
    scope = book.highest_active_scope(ds, platform_id, period)
    open_tk = _open_tickets(tickets, batch.batch_id)
    blocking = [t for t in open_tk if t.blocking]

    m = batch.methodology
    calibre = {
        CALIBRE_LABELS["coverage_note"]: m.coverage_note,
        CALIBRE_LABELS["dedup_method"]: m.dedup_method,
        CALIBRE_LABELS["refund_handling"]: m.refund_handling,
        CALIBRE_LABELS["price_basis"]: m.price_basis,
        CALIBRE_LABELS["promo_basis"]: m.promo_basis,
        f"类目字典（{m.dictionary_version}）": "已随批存档",
    }
    quarantined = sorted({
        (o.category or o.obs_id) for o in batch.observations if o.obs_id in report.quarantined_obs
    })

    confidence, usable, reasons = _confidence(scope, score, report, blocking)

    return LineageStatement(
        platform_id=platform_id,
        period=period,
        batch_id=batch.batch_id,
        batch_kind=batch.kind.value,
        supersedes=batch.supersedes,
        approved_scope=scope.value if scope else None,
        confidence=confidence.value,
        usable=usable,
        score=score.score,
        grade=score.grade.value,
        calibre=calibre,
        dictionary_version=m.dictionary_version,
        quality_statement=m.quality_statement,
        open_tickets=len(open_tk),
        blocking_tickets=len(blocking),
        quarantined_categories=quarantined,
        reasons=reasons,
    )


def _confidence(
    scope: UsageScope | None,
    score: QualityScore,
    report: BatchReport,
    blocking: list[Ticket],
) -> tuple[Confidence, bool, list[str]]:
    reasons: list[str] = []
    if report.blocked or blocking:
        reasons.append("存在批次级阻断或未关闭的阻断工单，数据不可直接采用")
        return Confidence.UNUSABLE, False, reasons
    if scope is UsageScope.PUBLIC_RELEASE and score.grade is Grade.A:
        reasons.append("已获公开发布审批，质量等级 A，无隔离观测，口径稳定")
        return Confidence.HIGH, True, reasons
    if scope in (UsageScope.AGGREGATE_TREND, UsageScope.PUBLIC_RELEASE) and score.grade in (Grade.A, Grade.B):
        reasons.append(f"已获汇总趋势审批，质量等级 {score.grade.value}")
        return Confidence.MEDIUM, True, reasons
    if scope is UsageScope.INTERNAL_REFERENCE:
        reasons.append("仅限内部参考：审批范围最低，采用时需同时附口径差异提示")
        return Confidence.LOW, True, reasons
    reasons.append("尚无有效可用范围审批，禁止纳入公开统计")
    return Confidence.UNUSABLE, False, reasons
