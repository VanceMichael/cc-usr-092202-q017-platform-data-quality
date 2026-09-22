"""平台消费数据可信度服务门面。

一次装配即串起：台账载入 → 自动校验 → 口径断点检测 → 异常隔离 →
工单生成 → 质量评分 → 审批匹配 → 血缘/比较输出。
工单状态可随核查进展流转，并据此解除隔离、重算评分与可采用结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .anomaly import Anomaly, AnomalyDetector
from .approvals import ApprovalRegistry, SCOPES
from .catalog import Catalog
from .directory import Directory
from .ledger import Ledger
from .lineage import build_lineage, compare_sources
from .quality import QualityRating, QualityScorer
from .tickets import Ticket, TicketStore
from .validation import BatchCheck, Finding, Validator


@dataclass
class QualityService:
    catalog: Catalog
    directory: Directory
    ledger: Ledger
    approvals: ApprovalRegistry
    checks: list[BatchCheck] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    tickets: TicketStore = field(default_factory=TicketStore)
    ratings: dict[tuple[str, str, str], QualityRating] = field(default_factory=dict)
    breakpoint_index: dict[tuple[str, str], list[Finding]] = field(default_factory=dict)

    @classmethod
    def from_fixtures(
        cls,
        base: Path,
        *,
        catalog_name="categories.json",
        platforms_name="platforms.json",
        submissions_name="submissions.json",
        approvals_name="approvals.json",
        today: str | None = None,
    ) -> "QualityService":
        catalog = Catalog.load(base / catalog_name)
        directory = Directory.load(base / platforms_name)
        ledger = Ledger.load(base / submissions_name)
        approvals = ApprovalRegistry.load(base / approvals_name)
        service = cls(catalog=catalog, directory=directory, ledger=ledger, approvals=approvals)
        service.bootstrap(today=today)
        return service

    def bootstrap(self, today: str | None = None) -> None:
        validator = Validator(
            self.catalog,
            covered={pid: self.directory.covered_categories(pid)
                     for pid in self.directory.platforms},
        )
        self.checks = validator.validate_all(self.ledger)

        # 口径断点（接口改版/方法论漂移）
        pairs = sorted({
            (b.platform_id, obs.category_code)
            for b in self.ledger.effective_batches()
            for obs in b.observations
        })
        self.breakpoint_index = {}
        for platform_id, category_code in pairs:
            breaks = validator.comparability_breakpoints(self.ledger, platform_id, category_code)
            if breaks:
                self.breakpoint_index[(platform_id, category_code)] = breaks

        # 校验问题自动开工单
        for check in self.checks:
            if check.batch.event_type == "withdrawal":
                continue
            for finding in check.findings:
                if finding.severity in ("blocker", "major", "minor"):
                    self.tickets.open_from_finding(finding, today=today)

        # 口径断点同样生成工单（与异常波动分开来源标记）
        for findings in self.breakpoint_index.values():
            for finding in findings:
                self.tickets.open_from_finding(finding, today=today)

        # 异常波动检测（断点先喂给检测器）并自动隔离开工单
        detector = AnomalyDetector()
        for findings in self.breakpoint_index.values():
            detector.note_breakpoints(findings)
        self.anomalies = detector.detect_all(self.ledger, pairs)
        for anomaly in self.anomalies:
            self.tickets.open_from_anomaly(anomaly, today=today)

        self._rescore()

    def _rescore(self) -> None:
        self.ratings = {}
        scorer_cache: dict[str, QualityScorer] = {}
        check_by_id = {c.batch.batch_id: c for c in self.checks}
        open_anomaly_keys = {
            (a.platform_id, a.category_code, a.batch_id)
            for a in self.anomalies
            if self._ticket_still_open(a.batch_id, a.category_code, "anomaly")
        }
        # 断点历史标记：断点之后的所有时期均受“历史上未回溯”影响
        break_after: dict[tuple[str, str], str] = {}
        for (platform_id, category_code), findings in self.breakpoint_index.items():
            earliest = min(
                self._batch_period(f.batch_id) for f in findings
            )
            break_after[(platform_id, category_code)] = earliest

        for batch in self.ledger.effective_batches():
            covered = set(self.directory.covered_categories(batch.platform_id))
            scorer = scorer_cache.setdefault(
                batch.platform_id,
                QualityScorer(len(self.catalog.categories), covered),
            )
            check = check_by_id[batch.batch_id]
            for obs in batch.observations:
                anomaly_open = (
                    batch.platform_id, obs.category_code, batch.batch_id
                ) in open_anomaly_keys
                historical_break = (
                    batch.report_period
                    >= break_after.get((batch.platform_id, obs.category_code), "9999-99")
                )
                self.ratings[(batch.platform_id, obs.category_code, batch.report_period)] = (
                    scorer.rate(
                        check, obs.category_code,
                        anomaly_open=anomaly_open,
                        historical_break=historical_break,
                    )
                )

    def _ticket_still_open(self, batch_id: str, category_code: str | None,
                           source: str) -> bool:
        ticket = self.tickets.open_ticket_for(batch_id, category_code, source)
        return ticket is not None

    def _batch_period(self, batch_id: str) -> str:
        for batch in self.ledger.batches:
            if batch.batch_id == batch_id:
                return batch.report_period
        return "9999-99"

    # ---- 核查处置后重算 ----
    def close_ticket(self, ticket_id: str, *, actor: str, role: str,
                     platform_id: str | None = None, note: str,
                     resolution_kind: str = "confirmed_real",
                     today: str | None = None) -> Ticket:
        ticket = self.tickets.transition(
            ticket_id, role=role, platform_id=platform_id,
            action="resolve", actor=actor, note=note,
            resolution_kind=resolution_kind, today=today,
        )
        # 异常经核查确认真实（如大促、新品首发）后解除隔离并重算
        self._rescore()
        return ticket

    def investigate_ticket(self, ticket_id: str, *, actor: str, role: str,
                           platform_id: str | None = None, note: str,
                           today: str | None = None) -> Ticket:
        return self.tickets.transition(
            ticket_id, role=role, platform_id=platform_id,
            action="investigate", actor=actor, note=note, today=today,
        )

    # ---- 对外能力 ----
    def lineage(self, platform_id: str, category_code: str, report_period: str,
                *, required_scope: str = "public_statistic",
                role: str = "statistician",
                acting_platform_id: str | None = None):
        if role == "platform" and acting_platform_id != platform_id:
            raise PermissionError("平台角色只能查看本平台指标血缘")
        if required_scope not in SCOPES:
            raise ValueError(f"未知可用范围: {required_scope}")
        return build_lineage(
            ledger=self.ledger,
            directory=self.directory,
            catalog=self.catalog,
            platform_id=platform_id,
            category_code=category_code,
            report_period=report_period,
            ratings=self.ratings,
            breakpoint_index=self.breakpoint_index,
            tickets=self.tickets,
            approvals=self.approvals,
            required_scope=required_scope,
        )

    def compare(self, category_code: str, report_period: str):
        return compare_sources(
            ledger=self.ledger,
            directory=self.directory,
            catalog=self.catalog,
            category_code=category_code,
            report_period=report_period,
            ratings=self.ratings,
            tickets=self.tickets,
            approvals=self.approvals,
        )

    def tickets_for(self, *, role: str, platform_id: str | None = None) -> list[Ticket]:
        return self.tickets.list_for(role=role, platform_id=platform_id)

    def findings_summary(self) -> list[dict]:
        return [check.as_dict() for check in self.checks]
