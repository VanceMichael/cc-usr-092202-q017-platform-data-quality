"""服务门面：装配校验/评分/工单/审批，落实角色可见范围与来源对比。

角色边界：
- platform（平台数据贡献方）只能看到本平台批次与本平台问题工单；
- statistician/reviewer/publisher 可见全部，统计人员另可做跨来源差异比较。

聚合入口保证：被隔离观测、未达审批范围的批次都不会进入公开统计。
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any

from .dataset import Batch, Dataset
from .model import (
    INTERNAL_ROLES,
    BatchKind,
    Role,
    Ticket,
    TicketStatus,
    UsageScope,
)
from .approval import ApprovalBook, build_lineage
from .scoring import build_tickets, score_all, score_batch
from .validation import BatchReport, Validator


class AccessDenied(Exception):
    pass


@dataclass
class SourceComparisonRow:
    period: str
    category: str
    platform_id: str
    yoy_growth: float
    median: float
    deviation: float
    quarantined: bool
    usable_scope: str | None


class QualityService:
    def __init__(self, dataset: Dataset):
        self.ds = dataset
        self.validator = Validator(dataset)
        self.reports: dict[str, BatchReport] = self.validator.validate_all()
        self.scores = score_all(dataset, self.reports)
        self.tickets = self._materialize_tickets()
        self.approvals = ApprovalBook()
        self._void_stale_tickets()

    # ------------------------------------------------------------ 工单

    def _materialize_tickets(self) -> list[Ticket]:
        tickets = build_tickets(self.reports)
        for t in tickets:
            batch = next(b for b in self.ds.batches if b.batch_id == t.batch_id)
            t.created_at = batch.received_at
        return tickets

    def _void_stale_tickets(self) -> None:
        """批次被改版/回补新批次替代或被撤回时，其未关闭工单自然作废。"""
        superseded = self.ds.superseded_batch_ids()
        withdrawn = self.ds.withdrawn_batch_ids()
        for t in self.tickets:
            if t.status is not TicketStatus.OPEN:
                continue
            if t.batch_id in superseded:
                t.status = TicketStatus.VOIDED_SUPERSEDED
                t.resolution = {"reason": "批次已被新报送批次替代"}
            elif t.batch_id in withdrawn:
                t.status = TicketStatus.VOIDED_WITHDRAWN
                t.resolution = {"reason": "批次已被平台撤回"}

    def resolve_ticket(self, ticket_id: str, note: str, reviewer: str) -> Ticket:
        ticket = self._require_ticket(ticket_id)
        ticket.status = TicketStatus.RESOLVED
        ticket.resolution = {"note": note, "by": reviewer}
        # 关闭工单后评分/审批结论依赖工单状态，评分本身不需重算
        return ticket

    def confirm_quarantine(
        self,
        batch_id: str,
        obs_id: str,
        reviewer: str,
        note: str,
        data_sound: bool,
    ) -> dict[str, Any]:
        """核查异常隔离观测。

        data_sound=True  核查后确认数据真实（如促销活动确实拉高增速）：
                         解除隔离、重算评分，该观测恢复候选资格，仍保留核查记录；
        data_sound=False 确认数据有误：维持隔离，平台须以新批次重新报送。
        """
        report = self.reports.get(batch_id)
        if report is None:
            raise KeyError(f"批次不存在：{batch_id}")
        if obs_id not in report.quarantined_obs:
            raise ValueError(f"观测 {obs_id} 不在隔离核查名单中")
        if not data_sound:
            return {
                "obs_id": obs_id,
                "cleared": False,
                "action": "维持隔离，等待平台以新批次重新报送",
                "note": note, "by": reviewer,
            }
        report.quarantined_obs.discard(obs_id)
        batch = next(b for b in self.ds.batches if b.batch_id == batch_id)
        self.scores[batch_id] = score_batch(batch, self.ds, report)
        # 关联工单标记已核查关闭
        for t in self.tickets:
            if t.batch_id == batch_id and t.obs_id == obs_id and t.status is TicketStatus.OPEN:
                t.status = TicketStatus.RESOLVED
                t.resolution = {
                    "note": f"核查通过解除隔离：{note}", "by": reviewer,
                }
        return {
            "obs_id": obs_id, "cleared": True,
            "action": "解除隔离并重算质量评分",
            "new_score": self.scores[batch_id].to_dict(),
            "note": note, "by": reviewer,
        }

    def _require_ticket(self, ticket_id: str) -> Ticket:
        for t in self.tickets:
            if t.id == ticket_id:
                return t
        raise KeyError(f"工单不存在：{ticket_id}")

    # ------------------------------------------------------------ 可见范围

    def visible_tickets(self, role: Role, platform_id: str | None = None) -> list[Ticket]:
        if role is Role.PLATFORM:
            if not platform_id:
                raise AccessDenied("平台身份必须指定所属平台")
            return [t for t in self.tickets if t.platform_id == platform_id]
        if role in INTERNAL_ROLES:
            return list(self.tickets)
        raise AccessDenied(f"未知角色 {role}")

    def visible_batches(self, role: Role, platform_id: str | None = None) -> list[Batch]:
        if role is Role.PLATFORM:
            if not platform_id:
                raise AccessDenied("平台身份必须指定所属平台")
            return [b for b in self.ds.batches if b.platform_id == platform_id]
        return list(self.ds.batches)

    # ------------------------------------------------------------ 审批

    def request_approval(
        self,
        platform_id: str,
        period: str,
        scope: UsageScope,
        approver: str,
        approver_role: Role,
        granted_at: str,
        note: str = "",
    ):
        batch = self.ds.effective_batch(platform_id, period)
        if batch is None:
            raise ValueError("该平台该期无有效批次，无法审批")
        return self.approvals.grant(
            scope, batch, self.scores[batch.batch_id],
            self.reports[batch.batch_id], self.tickets,
            approver, approver_role, granted_at, note,
        )

    def lineage(self, platform_id: str, period: str):
        return build_lineage(
            self.ds, platform_id, period,
            self.reports, self.scores, self.tickets, self.approvals,
        )

    # ------------------------------------------------------------ 来源对比（统计人员）

    def compare_sources(
        self,
        role: Role,
        period: str,
        category: str | None = None,
    ) -> list[SourceComparisonRow]:
        if role is not Role.STATISTICIAN:
            raise AccessDenied("仅统计分析人员可比较平台来源差异")
        rows: list[SourceComparisonRow] = []
        per_category: dict[str, list[float]] = {}
        effective = {
            (pid, p): b for (pid, p), b in self.ds.effective_batches().items() if p == period
        }
        for b in effective.values():
            for o in b.observations:
                per_category.setdefault(o.category, []).append(o.yoy_growth)

        for (pid, p), b in sorted(effective.items()):
            for o in sorted(b.observations, key=lambda x: x.category):
                if category and o.category != category:
                    continue
                values = per_category.get(o.category, [o.yoy_growth])
                med = median(values)
                scope = self.approvals.highest_active_scope(self.ds, pid, period)
                rows.append(SourceComparisonRow(
                    period=period,
                    category=o.category,
                    platform_id=pid,
                    yoy_growth=o.yoy_growth,
                    median=med,
                    deviation=o.yoy_growth - med,
                    quarantined=o.obs_id in self.reports[b.batch_id].quarantined_obs,
                    usable_scope=scope.value if scope else None,
                ))
        return rows

    # ------------------------------------------------------------ 公开统计口径

    def public_observations(self, period: str) -> list[dict[str, Any]]:
        """返回可进入公开统计的观测：有效批次 + 公开发布审批 + 未被隔离。

        任何不满足条件的观测都被挡在此处之外——异常波动先核查、不直接发布。
        """
        out: list[dict[str, Any]] = []
        for (pid, p), b in self.ds.effective_batches().items():
            if p != period:
                continue
            scope = self.approvals.highest_active_scope(self.ds, pid, period)
            if scope is not UsageScope.PUBLIC_RELEASE:
                continue
            report = self.reports[b.batch_id]
            for o in b.observations:
                if o.obs_id in report.quarantined_obs:
                    continue
                out.append({
                    "platform_id": pid,
                    "period": period,
                    "category": o.category,
                    "yoy_growth": o.yoy_growth,
                    "sales_index": o.sales_index,
                    "batch_id": b.batch_id,
                    "dictionary_version": b.methodology.dictionary_version,
                })
        return out

    def summary(self) -> dict[str, Any]:
        """便于巡检的总览。"""
        return {
            "platforms": len(self.ds.platforms),
            "batches_total": len(self.ds.batches),
            "batches_effective": len(self.ds.effective_batches()),
            "batches_superseded": sorted(self.ds.superseded_batch_ids()),
            "batches_withdrawn": sorted(self.ds.withdrawn_batch_ids()),
            "tickets": {
                "total": len(self.tickets),
                "open": sum(1 for t in self.tickets if t.status is TicketStatus.OPEN),
            },
            "scores": [s.to_dict() for s in self.scores.values()],
        }
