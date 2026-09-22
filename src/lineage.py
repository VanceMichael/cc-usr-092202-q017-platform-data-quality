"""指标血缘与置信说明。

采用某项平台指标时，系统输出该指标从批次、类目映射、方法论快照、
质量评分、异常核查、问题工单到可用范围审批的完整血缘，并给出
是否满足拟用范围的明确结论；统计人员可对同一类目跨来源比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .approvals import (
    SCOPE_NAMES,
    SCOPES,
    Approval,
    ApprovalRegistry,
    maximum_supported_scope,
    scope_satisfied,
)
from .ledger import Batch, Ledger, Observation
from .quality import (
    DIMENSION_LABELS,
    METHODOLOGY_DIMENSION,
    DIMENSIONS,
    QualityRating,
)
from .tickets import Ticket, TicketStore
from .validation import Finding


@dataclass
class LineageReport:
    platform_id: str
    platform_name: str
    category_code: str
    category_name: str
    report_period: str
    observation: Observation | None
    effective_batch: Batch | None
    batch_chain: list[dict]
    methodology_snapshot: dict
    mappings: list[dict]
    rating: QualityRating | None
    breakpoints: list[Finding]
    open_tickets: list[Ticket]
    approval: Approval | None
    supported_scope: str | None
    required_scope: str
    adoptable: bool
    confidence_statement: str
    blockers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "platform_id": self.platform_id,
            "platform_name": self.platform_name,
            "category_code": self.category_code,
            "category_name": self.category_name,
            "report_period": self.report_period,
            "value": None if self.observation is None else {
                "sales": self.observation.sales,
                "orders": self.observation.orders,
                "refunds": self.observation.refunds,
            },
            "effective_batch": None if self.effective_batch is None else {
                "batch_id": self.effective_batch.batch_id,
                "event_type": self.effective_batch.event_type,
                "submitted_at": self.effective_batch.submitted_at,
                "reason": self.effective_batch.reason,
            },
            "batch_chain": self.batch_chain,
            "methodology_snapshot": self.methodology_snapshot,
            "category_mappings": self.mappings,
            "quality": None if self.rating is None else self.rating.as_dict(),
            "breakpoints": [f.as_dict() for f in self.breakpoints],
            "open_tickets": [
                {"ticket_id": t.ticket_id, "severity": t.severity,
                 "source": t.source, "status": t.status, "title": t.title}
                for t in self.open_tickets
            ],
            "approval": None if self.approval is None else self.approval.as_dict(),
            "system_supported_scope": (
                None if self.supported_scope is None else SCOPE_NAMES[self.supported_scope]
            ),
            "required_scope": SCOPE_NAMES[self.required_scope],
            "adoptable": self.adoptable,
            "confidence_statement": self.confidence_statement,
            "blockers": self.blockers,
        }


def build_batch_chain(ledger: Ledger, batch: Batch) -> list[dict]:
    """沿 supersedes 链回溯：初报 → 撤回/回补 的完整批次留痕。"""
    by_id = {b.batch_id: b for b in ledger.batches}
    chain: list[dict] = []
    seen: set[str] = set()
    current: Batch | None = batch
    while current is not None and current.batch_id not in seen:
        seen.add(current.batch_id)
        chain.append({
            "batch_id": current.batch_id,
            "event_type": current.event_type,
            "report_period": current.report_period,
            "submitted_at": current.submitted_at,
            "supersedes": current.supersedes,
            "reason": current.reason,
            "with_data": current.is_data_batch,
        })
        current = by_id.get(current.supersedes) if current.supersedes else None
    # 还要纳入“撤回当前链上任一节点”的撤回批次
    current_ids = {item["batch_id"] for item in chain}
    for other in ledger.batches:
        if other.event_type == "withdrawal" and other.supersedes in current_ids:
            chain.append({
                "batch_id": other.batch_id,
                "event_type": other.event_type,
                "report_period": other.report_period,
                "submitted_at": other.submitted_at,
                "supersedes": other.supersedes,
                "reason": other.reason,
                "with_data": other.is_data_batch,
            })
    chain.sort(key=lambda item: item["submitted_at"])
    return chain


def build_lineage(
    *,
    ledger: Ledger,
    directory,
    catalog,
    platform_id: str,
    category_code: str,
    report_period: str,
    ratings: dict[tuple[str, str, str], QualityRating],
    breakpoint_index: dict[tuple[str, str], list[Finding]],
    tickets: TicketStore,
    approvals: ApprovalRegistry,
    required_scope: str,
) -> LineageReport:
    category = catalog.get(category_code)
    platform = directory.platforms[platform_id]
    ledger_batches = {b.batch_id: b for b in ledger.batches}
    batch = ledger.effective_batch(platform_id, report_period)
    observation = batch.observation_for(category_code) if batch else None

    rating = ratings.get((platform_id, category_code, report_period))
    breakpoints = breakpoint_index.get((platform_id, category_code), [])
    # 断点发生在本报告期或更早，即影响该期数据的跨期可比性
    relevant_breaks = [
        f for f in breakpoints
        if (ledger_batches.get(f.batch_id).report_period if f.batch_id in ledger_batches else "")
        <= report_period
    ]
    open_tickets = [
        t for t in tickets.list_for(role="statistician")
        if t.platform_id == platform_id
        and t.category_code == category_code
        and (batch is not None and t.batch_id == batch.batch_id)
        and t.status in ("open", "investigating")
    ]

    approval = approvals.active_for(platform_id, category_code, report_period)
    blockers: list[str] = []

    if batch is None or observation is None:
        supported_scope = None
        blockers.append("该报告期无有效数据批次（可能已撤回且尚未回补）")
    else:
        has_open_critical = any(t.severity == "critical" for t in open_tickets)
        has_open_major = any(t.severity == "major" for t in open_tickets)
        has_breakpoint = any(
            f.category_code == category_code for f in relevant_breaks
        )
        supported_scope = maximum_supported_scope(
            grade=rating.grade if rating else "D",
            blocked=rating.blocked if rating else True,
            quarantined=rating.quarantined if rating else False,
            has_open_critical=has_open_critical,
            has_open_major=has_open_major,
            has_breakpoint=has_breakpoint,
        )
        if rating and rating.blocked:
            blockers.append("批次存在阻断性校验问题（见质量评分扣分）")
        if rating and rating.quarantined:
            blockers.append("异常波动核查未关闭，观测处于隔离状态")
        if has_breakpoint:
            blockers.append("存在接口改版/口径变更断点且历史未回溯重述，跨期不可比")
        if supported_scope is None:
            blockers.append("质量等级为D，任何统计用途均不予支持")

    required_rank = SCOPES.index(required_scope)
    system_rank_ok = (
        supported_scope is not None and SCOPES.index(supported_scope) >= required_rank
    )
    adoptable = (
        batch is not None
        and observation is not None
        and supported_scope is not None
        and system_rank_ok
        and scope_satisfied(approval, required_scope)
    )
    if approval is None:
        blockers.append(f"缺少覆盖 {report_period} 的有效审批")
    elif not scope_satisfied(approval, required_scope):
        blockers.append(
            f"当前批准范围为{SCOPE_NAMES[approval.scope]}，"
            f"低于拟用范围{SCOPE_NAMES[required_scope]}"
        )
    if not system_rank_ok and supported_scope is not None:
        blockers.append(
            f"系统当前仅支持到{SCOPE_NAMES[supported_scope]}，"
            f"低于拟用范围{SCOPE_NAMES[required_scope]}"
        )

    method = batch.methodology if batch else {}
    statement_parts = []
    method_keys = (
        "sample_coverage", "category_dictionary", "dedup_method",
        "refund_treatment", "price_caliber",
    )
    if rating:
        statement_parts.append(
            f"综合质量评分 {rating.total} 分，等级 {rating.grade}（{rating.confidence}）"
        )
    if batch:
        statement_parts.append(
            "采用值的口径：" + "；".join(
                f"{DIMENSION_LABELS[METHODOLOGY_DIMENSION[key]]}={method.get(key, '缺失')}"
                for key in method_keys
            )
        )
    if adoptable:
        statement_parts.append(
            f"结论：可用于{SCOPE_NAMES[required_scope]}；审批 {approval.approval_id}，"
            f"生效期 {approval.valid_period_from}~{approval.valid_period_to}"
        )
    else:
        statement_parts.append("结论：暂不可采用——" + "；".join(blockers))

    return LineageReport(
        platform_id=platform_id,
        platform_name=platform["name"],
        category_code=category_code,
        category_name=category.name,
        report_period=report_period,
        observation=observation,
        effective_batch=batch,
        batch_chain=build_batch_chain(ledger, batch) if batch else [],
        methodology_snapshot={
            DIMENSION_LABELS[METHODOLOGY_DIMENSION[key]]: method.get(key)
            for key in ("sample_coverage", "category_dictionary", "dedup_method",
                        "refund_treatment", "price_caliber", "quality_statement")
        } if batch else {},
        mappings=method.get("category_mappings", []) if batch else [],
        rating=rating,
        breakpoints=relevant_breaks,
        open_tickets=open_tickets,
        approval=approval,
        supported_scope=supported_scope,
        required_scope=required_scope,
        adoptable=adoptable,
        confidence_statement="。".join(part for part in statement_parts if part),
        blockers=blockers,
    )


def compare_sources(
    *,
    ledger: Ledger,
    directory,
    catalog,
    category_code: str,
    report_period: str,
    ratings: dict[tuple[str, str, str], QualityRating],
    tickets: TicketStore,
    approvals: ApprovalRegistry,
) -> dict:
    """统计人员视角：同一类目同一报告期各来源数值、评分、范围与口径差异。"""
    sources = []
    for platform_id, platform in directory.platforms.items():
        batch = ledger.effective_batch(platform_id, report_period)
        obs = batch.observation_for(category_code) if batch else None
        rating = ratings.get((platform_id, category_code, report_period))
        approval = approvals.active_for(platform_id, category_code, report_period)
        chain_notes = []
        if batch is not None:
            for item in build_batch_chain(ledger, batch):
                if item["event_type"] in ("withdrawal", "backfill", "late", "api_change"):
                    chain_notes.append(f"{item['batch_id']}:{item['event_type']}")
        sources.append({
            "platform_id": platform_id,
            "platform_name": platform["name"],
            "sales": None if obs is None else obs.sales,
            "orders": None if obs is None else obs.orders,
            "refunds": None if obs is None else obs.refunds,
            "grade": None if rating is None else rating.grade,
            "score": None if rating is None else rating.total,
            "quarantined": None if rating is None else rating.quarantined,
            "approved_scope": None if approval is None else SCOPE_NAMES[approval.scope],
            "batch_id": None if batch is None else batch.batch_id,
            "batch_event": None if batch is None else batch.event_type,
            "chain_notes": chain_notes,
            "covers_category": category_code in directory.covered_categories(platform_id),
            "price_caliber": batch.methodology.get("price_caliber") if batch else None,
            "refund_treatment": batch.methodology.get("refund_treatment") if batch else None,
            "dedup_method": batch.methodology.get("dedup_method") if batch else None,
        })

    values = [s["sales"] for s in sources if s["sales"] is not None]
    spread = None
    if len(values) >= 2:
        spread = {
            "min": min(values),
            "max": max(values),
            "max_min_ratio": round(max(values) / min(values), 3),
        }
    caliber_diffs = _caliber_differences(sources)
    return {
        "category_code": category_code,
        "category_name": catalog.get(category_code).name,
        "report_period": report_period,
        "sources": sources,
        "value_spread": spread,
        "caliber_differences": caliber_diffs,
        "note": "口径差异未消除前，各来源增速与绝对值不得直接拼接为同一序列",
    }


def _caliber_differences(sources: list[dict]) -> list[str]:
    diffs: list[str] = []
    for field_name, label in (
        ("price_caliber", "价格口径"),
        ("refund_treatment", "退款处理"),
        ("dedup_method", "去重方法"),
    ):
        distinct = {s[field_name] for s in sources if s.get(field_name)}
        if len(distinct) > 1:
            diffs.append(f"{label}存在 {len(distinct)} 种口径，跨来源比较需加注")
    return diffs
