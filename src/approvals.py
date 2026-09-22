"""可用范围分级审批。

三级可用范围逐级包含：内部参考 < 趋势比对 < 公开统计。
审批由统计人员发起、口径审查人员决定；系统按质量评分、未关闭工单、
异常隔离状态与口径断点计算“系统可支持的最高范围”，审查人员
不得批准高于该范围的用途。已批准事项可撤回（如接口改版后暂停公开）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

SCOPES = ("internal_reference", "trend_comparison", "public_statistic")
SCOPE_NAMES = {
    "internal_reference": "内部参考",
    "trend_comparison": "趋势比对",
    "public_statistic": "公开统计",
}
# 各级范围对应的最低质量等级
MIN_GRADE = {
    "internal_reference": "C",
    "trend_comparison": "C",
    "public_statistic": "B",
}
_GRADE_RANK = {"D": 0, "C": 1, "B": 2, "A": 3}


@dataclass
class Approval:
    approval_id: str
    platform_id: str
    category_code: str
    scope: str
    status: str  # approved / rejected / revoked
    requested_by: str
    requested_at: str
    decided_by: str | None = None
    decided_at: str | None = None
    revoked_by: str | None = None
    revoked_at: str | None = None
    valid_period_from: str = ""
    valid_period_to: str = ""
    conditions: list[str] = field(default_factory=list)
    rationale: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> "Approval":
        return cls(**raw)

    def covers_period(self, report_period: str) -> bool:
        return self.valid_period_from <= report_period <= self.valid_period_to

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class ApprovalRegistry:
    def __init__(self, approvals: list[Approval]):
        self.approvals = approvals
        self._seq = len(approvals)

    @classmethod
    def load(cls, path: Path) -> "ApprovalRegistry":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls([Approval.from_dict(item) for item in raw["approvals"]])

    def request(self, platform_id: str, category_code: str, scope: str,
                *, requested_by: str, today: str | None = None,
                conditions: list[str] | None = None) -> Approval:
        if scope not in SCOPES:
            raise ValueError(f"未知可用范围: {scope}")
        self._seq += 1
        approval = Approval(
            approval_id=f"AP-{self._seq:04d}",
            platform_id=platform_id,
            category_code=category_code,
            scope=scope,
            status="pending",
            requested_by=requested_by,
            requested_at=today or date.today().isoformat(),
            conditions=conditions or [],
        )
        self.approvals.append(approval)
        return approval

    def decide(self, approval_id: str, *, approver_role: str, approver: str,
               approve: bool, rationale: str,
               max_supported_scope: str | None,
               today: str | None = None) -> Approval:
        if approver_role != "caliber_reviewer":
            raise PermissionError("仅口径审查人员可以决定可用范围审批")
        approval = self._get(approval_id)
        if approval.status != "pending":
            raise ValueError(f"审批 {approval_id} 已决定，不能重复审批")
        if approve:
            if max_supported_scope is not None and _rank(approval.scope) > _rank(max_supported_scope):
                    raise PermissionError(
                        f"申请范围 {SCOPE_NAMES[approval.scope]} 高于系统当前可支持的最高范围 "
                        f"{SCOPE_NAMES[max_supported_scope]}，不得批准"
                    )
            approval.status = "approved"
        else:
            approval.status = "rejected"
        approval.decided_by = approver
        approval.decided_at = today or date.today().isoformat()
        approval.rationale = rationale
        return approval

    def revoke(self, approval_id: str, *, actor_role: str, actor: str,
               reason: str, today: str | None = None) -> Approval:
        if actor_role not in ("caliber_reviewer", "publisher"):
            raise PermissionError("仅口径审查或发布角色可以撤回批准")
        approval = self._get(approval_id)
        approval.status = "revoked"
        approval.revoked_by = actor
        approval.revoked_at = today or date.today().isoformat()
        approval.rationale = f"撤回：{reason}。原理由：{approval.rationale}"
        return approval

    def active_for(self, platform_id: str, category_code: str,
                   report_period: str) -> Approval | None:
        candidates = [
            a for a in self.approvals
            if a.platform_id == platform_id
            and a.category_code == category_code
            and a.status == "approved"
            and a.covers_period(report_period)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda a: _rank(a.scope))

    def _get(self, approval_id: str) -> Approval:
        for approval in self.approvals:
            if approval.approval_id == approval_id:
                return approval
        raise KeyError(f"审批不存在: {approval_id}")


def _rank(scope: str) -> int:
    return SCOPES.index(scope)


def maximum_supported_scope(
    *, grade: str, blocked: bool, quarantined: bool,
    has_open_critical: bool, has_open_major: bool, has_breakpoint: bool,
) -> str | None:
    """根据质量与流程状态计算系统可支持的最高可用范围。"""
    if blocked or grade == "D":
        return None
    if quarantined or has_open_critical:
        # 异常核查未关闭：隔离，不允许趋势比对与公开统计
        return "internal_reference"
    if grade == "C" or has_open_major:
        return "trend_comparison"
    if has_breakpoint:
        # 口径未回溯：与历史不可比，不得公开
        return "trend_comparison"
    if _GRADE_RANK[grade] >= _GRADE_RANK[MIN_GRADE["public_statistic"]]:
        return "public_statistic"
    return "trend_comparison"


def scope_satisfied(approved: Approval | None, required_scope: str) -> bool:
    if approved is None:
        return False
    return _rank(approved.scope) >= _rank(required_scope)
