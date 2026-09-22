"""质量评分与问题工单。

评分采用六维加权（见 model.SCORE_WEIGHTS），每维先算 0~1 的得分再折算百分制：
- coverage       覆盖：声明份额（满份额 50%）与类目覆盖率各占一半；
- mapping        映射：通过映射校验的观测占比，字典目标非法时重罚；
- timeliness     及时性：按时 1，迟报按延迟天数折减；
- stability      口径稳定：与上一有效批次相比，六项口径与映射条目的保持比例；
- documentation  文档完备：六项口径 + 质量声明的填全比例；
- anomaly        异常：未被隔离的观测占比。

批次级阻断（缺口径、覆盖过低、字典目标非法）时总分封顶为 D。
"""

from __future__ import annotations

from datetime import date

from .dataset import Batch, Dataset
from .model import (
    CALIBRE_SIGNATURE_FIELDS,
    Grade,
    METHODOLOGY_FIELDS,
    QUALITY_STATEMENT_FIELD,
    QualityScore,
    SEVERITY_TO_TICKET,
    Severity,
    Ticket,
    TicketSeverity,
    TIMELY_KINDS,
    grade_for,
)
from .validation import BatchReport

FULL_SHARE = 0.50            # 覆盖份额达到 50% 即视为该子项满分
LATE_FULL_DEDUCT_DAYS = 10   # 迟报 10 天及时性归零
BLOCKED_SCORE_CAP = 49.0     # 批次级阻断时的总分上限


def _days_between(a: str, b: str) -> int:
    da = date.fromisoformat(a[:10])
    db = date.fromisoformat(b[:10])
    return (db - da).days


def _coverage_component(batch: Batch, ds: Dataset) -> float:
    share = min(batch.methodology.expected_share / FULL_SHARE, 1.0)
    reported = {o.category for o in batch.observations}
    cat_rate = len(reported) / len(ds.categories) if ds.categories else 0.0
    return 0.5 * share + 0.5 * min(cat_rate, 1.0)


def _mapping_component(batch: Batch, report: BatchReport) -> float:
    total = len(batch.observations)
    if total == 0:
        return 0.0
    bad = {
        f.obs_id for f in report.findings
        if f.code in {"CATEGORY_UNMAPPED", "CATEGORY_MAP_MISMATCH"} and f.obs_id
    }
    score = 1 - len(bad) / total
    if any(f.code == "DICTIONARY_TARGET_UNKNOWN" for f in report.findings):
        score = min(score, 0.3)
    return score


def _timeliness_component(batch: Batch) -> float:
    # 改版重报/历史回补按自身事件时间入账，及时性维度不扣分
    if batch.kind not in TIMELY_KINDS:
        return 1.0
    if batch.received_at <= batch.due_at:
        return 1.0
    late_days = _days_between(batch.due_at, batch.received_at)
    return max(0.0, 1 - late_days / LATE_FULL_DEDUCT_DAYS)


def _stability_component(batch: Batch, ds: Dataset) -> tuple[float, list[str]]:
    priors = ds.previous_periods(batch.period)
    prior = None
    for period in reversed(priors):
        prior = ds.effective_batch(batch.platform_id, period)
        if prior is not None:
            break
    if prior is None:
        return 1.0, []  # 首报无对比基准，稳定性不扣分

    old, new = prior.methodology, batch.methodology
    checks: list[tuple[str, bool]] = []
    changed: list[str] = []
    for key in CALIBRE_SIGNATURE_FIELDS:
        same = getattr(old, key) == getattr(new, key)
        checks.append((key, same))
        if not same:
            changed.append(key)

    old_map, new_map = old.category_map, new.category_map
    all_keys = set(old_map) | set(new_map)
    same_entries = sum(1 for k in all_keys if old_map.get(k) == new_map.get(k) and k in old_map and k in new_map)
    map_score = same_entries / len(all_keys) if all_keys else 1.0
    if all_keys and map_score < 1.0:
        changed.append("category_map")

    field_score = sum(1 for _, ok in checks if ok) / len(checks)
    return 0.6 * field_score + 0.4 * map_score, changed


def _documentation_component(batch: Batch) -> float:
    m = batch.methodology
    fields = (*METHODOLOGY_FIELDS, QUALITY_STATEMENT_FIELD)
    filled = sum(1 for k in fields if str(getattr(m, k, "")).strip())
    return filled / len(fields)


def _anomaly_component(batch: Batch, report: BatchReport) -> float:
    total = len(batch.observations)
    if total == 0:
        return 0.0
    return 1 - len(report.quarantined_obs) / total


def score_batch(batch: Batch, ds: Dataset, report: BatchReport) -> QualityScore:
    stability, _changed = _stability_component(batch, ds)
    components = {
        "coverage": _coverage_component(batch, ds),
        "mapping": _mapping_component(batch, report),
        "timeliness": _timeliness_component(batch),
        "stability": stability,
        "documentation": _documentation_component(batch),
        "anomaly": _anomaly_component(batch, report),
    }
    total = 100 * sum(
        components[k] * _percent_weight(k) for k in components
    )

    # 批次级阻断：缺资料/覆盖过低/字典非法，封顶 D
    batch_blocked = any(f.blocking and f.obs_id is None for f in report.findings)
    if batch_blocked:
        total = min(total, BLOCKED_SCORE_CAP)

    return QualityScore(
        batch_id=batch.batch_id,
        platform_id=batch.platform_id,
        period=batch.period,
        score=round(total, 1),
        grade=grade_for(total),
        components={k: round(v, 3) for k, v in components.items()},
    )


def _percent_weight(key: str) -> float:
    from .model import SCORE_WEIGHTS
    return SCORE_WEIGHTS[key]


def score_all(ds: Dataset, reports: dict[str, BatchReport]) -> dict[str, QualityScore]:
    out: dict[str, QualityScore] = {}
    for batch in ds.effective_batches().values():
        report = reports[batch.batch_id]
        out[batch.batch_id] = score_batch(batch, ds, report)
    return out


# ---------------------------------------------------------------- 工单

_TICKET_TITLES = {
    "METHODOLOGY_MISSING": "随附口径说明缺失",
    "QUALITY_STATEMENT_MISSING": "缺少报送质量声明",
    "TOO_FEW_OBSERVATIONS": "报送类目数量不足",
    "CATEGORY_UNMAPPED": "平台类目未在字典中映射",
    "CATEGORY_MAP_MISMATCH": "观测映射与类目字典不一致",
    "IMPLAUSIBLE_INDEX": "销售指数异常（非正）",
    "DICTIONARY_TARGET_UNKNOWN": "类目字典映射目标不存在",
    "CATEGORY_MANY_TO_ONE": "多对一映射需核实去重",
    "COVERAGE_SHARE_UNDECLARED": "未声明样本覆盖份额",
    "COVERAGE_TOO_LOW": "样本覆盖份额过低",
    "COVERAGE_LIMITED": "样本覆盖份额有限",
    "CATEGORY_COVERAGE_LOW": "统一类目覆盖率不足",
    "LATE_SUBMISSION": "迟报",
    "CALIBRE_CHANGED": "报送口径发生变化",
    "CATEGORY_MAP_CHANGED": "类目字典映射变更",
    "ANOMALY_HISTORICAL": "同比异常偏离历史",
    "ANOMALY_CROSS_SOURCE": "同比与同期其他平台偏离",
    "NO_HISTORY_BASELINE": "缺少历史基线",
}


def build_tickets(reports: dict[str, BatchReport]) -> list[Ticket]:
    """校验发现（warning 及以上）自动转工单；info 仅记录不出单。"""
    tickets: list[Ticket] = []
    for report in reports.values():
        for idx, f in enumerate(
            sorted(report.findings, key=lambda x: x.code), start=1
        ):
            if f.severity is Severity.INFO:
                continue
            severity = SEVERITY_TO_TICKET.get(f.severity, TicketSeverity.LOW)
            tickets.append(Ticket(
                id=f"TK-{report.batch_id}-{idx:02d}",
                platform_id=report.platform_id,
                batch_id=report.batch_id,
                code=f.code,
                title=_TICKET_TITLES.get(f.code, f.code),
                severity=severity,
                obs_id=f.obs_id,
                blocking=f.blocking,
                created_at="",  # 由服务层用批次接收时间填充
            ))
    return tickets
