"""问题工单与可见性控制。

工单来源：自动校验发现（finding）与异常波动（anomaly）。
可见性规则：
- platform 角色只能看到与本平台 platform_id 相同的工单；
- statistician / caliber_reviewer / publisher 可查看全部来源的工单，
  以便统计人员比较不同平台的问题差异。
平台之间互不可见，越权访问直接抛 PermissionError。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .anomaly import Anomaly
from .validation import Finding

INTERNAL_ROLES = {"statistician", "caliber_reviewer", "publisher"}
TICKET_SEVERITY = ("critical", "major", "minor")


@dataclass
class TicketEvent:
    at: str
    actor: str
    actor_role: str
    action: str
    note: str


@dataclass
class Ticket:
    ticket_id: str
    platform_id: str
    category_code: str | None
    batch_id: str
    source: str           # validation / anomaly
    severity: str
    title: str
    status: str = "open"  # open / investigating / resolved / dismissed
    created_at: str = ""
    resolution: str | None = None
    resolution_kind: str | None = None  # confirmed_real / corrected /口径说明/ rejected ...
    events: list[TicketEvent] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "platform_id": self.platform_id,
            "category_code": self.category_code,
            "batch_id": self.batch_id,
            "source": self.source,
            "severity": self.severity,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "resolution": self.resolution,
            "resolution_kind": self.resolution_kind,
            "history": [e.__dict__ for e in self.events],
        }


def severity_for_finding(finding: Finding) -> str:
    return {"blocker": "critical", "major": "major", "minor": "minor"}.get(
        finding.severity, "minor"
    )


class TicketStore:
    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._seq = 0

    def _new_id(self) -> str:
        self._seq += 1
        return f"TK-{self._seq:04d}"

    def open_from_finding(self, finding: Finding, today: str | None = None) -> Ticket:
        ticket = Ticket(
            ticket_id=self._new_id(),
            platform_id=finding.platform_id,
            category_code=finding.category_code,
            batch_id=finding.batch_id,
            source="validation",
            severity=severity_for_finding(finding),
            title=f"[{finding.code}] {finding.message}",
            created_at=today or date.today().isoformat(),
        )
        ticket.events.append(TicketEvent(
            ticket.created_at, "system", "system", "open",
            f"自动校验发现 {finding.code}（{finding.severity}）",
        ))
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def open_from_anomaly(self, anomaly: Anomaly, today: str | None = None) -> Ticket:
        ticket = Ticket(
            ticket_id=self._new_id(),
            platform_id=anomaly.platform_id,
            category_code=anomaly.category_code,
            batch_id=anomaly.batch_id,
            source="anomaly",
            severity="major",
            title=(
                f"{anomaly.report_period} {anomaly.category_code} 环比 "
                f"{anomaly.mom_change:+.1%} 异常，进入核查隔离；"
                + "；".join(anomaly.reasons)
            ),
            created_at=today or date.today().isoformat(),
        )
        ticket.events.append(TicketEvent(
            ticket.created_at, "system", "system", "open",
            "异常波动自动隔离，核查结论出具前不得写入公开统计",
        ))
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def get(self, ticket_id: str, *, role: str, platform_id: str | None = None) -> Ticket:
        ticket = self._tickets[ticket_id]
        self._authorize(ticket, role, platform_id)
        return ticket

    def list_for(self, *, role: str, platform_id: str | None = None) -> list[Ticket]:
        if role == "platform":
            if not platform_id:
                raise PermissionError("平台账号缺少 platform_id，不能列出工单")
            return [t for t in self._tickets.values() if t.platform_id == platform_id]
        if role in INTERNAL_ROLES:
            return sorted(self._tickets.values(), key=lambda t: t.ticket_id)
        raise PermissionError(f"角色 {role} 无权访问工单")

    def open_ticket_for(self, batch_id: str, category_code: str | None = None,
                        source: str | None = None) -> Ticket | None:
        for ticket in self._tickets.values():
            if ticket.status in ("resolved", "dismissed"):
                continue
            if ticket.batch_id != batch_id:
                continue
            if category_code is not None and ticket.category_code != category_code:
                continue
            if source is not None and ticket.source != source:
                continue
            return ticket
        return None

    def transition(self, ticket_id: str, *, role: str, platform_id: str | None,
                   action: str, actor: str, note: str,
                   resolution_kind: str | None = None,
                   today: str | None = None) -> Ticket:
        ticket = self.get(ticket_id, role=role, platform_id=platform_id)
        if action not in ("investigate", "resolve", "dismiss"):
            raise ValueError(f"未知工单动作: {action}")
        if action == "investigate" and role not in ("statistician", "caliber_reviewer", "platform"):
            raise PermissionError(f"角色 {role} 不能承接核查")
        if action in ("resolve", "dismiss") and role not in ("statistician", "caliber_reviewer"):
            raise PermissionError("仅统计/口径审查角色可以关闭工单")

        if action == "investigate":
            ticket.status = "investigating"
        elif action == "resolve":
            ticket.status = "resolved"
            ticket.resolution = note
            ticket.resolution_kind = resolution_kind or "resolved"
        else:
            ticket.status = "dismissed"
            ticket.resolution = note
            ticket.resolution_kind = "dismissed"
        ticket.events.append(TicketEvent(
            today or date.today().isoformat(), actor, role, action, note
        ))
        return ticket

    @staticmethod
    def _authorize(ticket: Ticket, role: str, platform_id: str | None) -> None:
        if role == "platform":
            if not platform_id or ticket.platform_id != platform_id:
                raise PermissionError(
                    f"平台 {platform_id} 无权查看平台 {ticket.platform_id} 的工单 {ticket.ticket_id}"
                )
        elif role not in INTERNAL_ROLES:
            raise PermissionError(f"角色 {role} 无权访问工单")
