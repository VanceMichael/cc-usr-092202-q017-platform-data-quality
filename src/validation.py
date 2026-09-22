"""平台报送数据自动校验。

校验分四级：blocker（禁止进入任何统计用途）/ major（必须核查处置）/
minor（提示修正）/ info（覆盖与口径披露信息）。校验只产出问题清单，
不改动批次，也不直接产生公开统计结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .catalog import UNMAPPED_CODE, Catalog
from .ledger import METHODOLOGY_KEYS, Batch, Ledger, _parse_day, deadline_for_period

SEVERITIES = ("blocker", "major", "minor", "info")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    batch_id: str
    platform_id: str
    message: str
    category_code: str | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "batch_id": self.batch_id,
            "platform_id": self.platform_id,
            "category_code": self.category_code,
            "message": self.message,
        }


@dataclass
class BatchCheck:
    batch: Batch
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_blocker(self) -> bool:
        return any(f.severity == "blocker" for f in self.findings)

    @property
    def major_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "major")

    @property
    def minor_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "minor")

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch.batch_id,
            "platform_id": self.batch.platform_id,
            "report_period": self.batch.report_period,
            "event_type": self.batch.event_type,
            "has_blocker": self.has_blocker,
            "findings": [f.as_dict() for f in self.findings],
        }


def _period_bounds(report_period: str) -> tuple[date, date]:
    year, month = (int(part) for part in report_period.split("-"))
    start = date(year, month, 1)
    end_year, end_month = (year + month // 12, month % 12 + 1)
    from datetime import timedelta

    end = date(end_year, end_month, 1) - timedelta(days=1)
    return start, end


class Validator:
    def __init__(self, catalog: Catalog, covered: dict[str, list[str]] | None = None):
        self.catalog = catalog
        # platform_id -> 平台声明可覆盖的标准类目编码
        self.covered = covered or {}

    def validate_all(self, ledger: Ledger) -> list[BatchCheck]:
        checks = [self.validate_batch(ledger, batch) for batch in ledger.batches]
        self._validate_chains(ledger, checks)
        return checks

    def validate_batch(self, ledger: Ledger, batch: Batch) -> BatchCheck:
        check = BatchCheck(batch=batch)
        self._check_methodology(batch, check)
        self._check_timeliness(ledger, batch, check)

        if batch.event_type == "withdrawal":
            if batch.observations:
                check.findings.append(
                    Finding(
                        "WD_WITH_OBS", "major", batch.batch_id, batch.platform_id,
                        "撤回批次不得携带观测数据",
                    )
                )
            return check

        self._check_mappings(batch, check)
        self._check_observations(batch, check)
        self._check_coverage(batch, check)
        return check

    def _check_methodology(self, batch: Batch, check: BatchCheck) -> None:
        method = batch.methodology
        for key in METHODOLOGY_KEYS:
            if not str(method.get(key, "")).strip():
                check.findings.append(
                    Finding(
                        "METHODOLOGY_MISSING", "blocker", batch.batch_id, batch.platform_id,
                        f"方法论要素缺失或为空: {key}",
                    )
                )
        mappings = method.get("category_mappings", [])
        if batch.event_type != "withdrawal" and not mappings:
            check.findings.append(
                Finding(
                    "MAPPING_TABLE_EMPTY", "blocker", batch.batch_id, batch.platform_id,
                    "类目映射表为空，无法确认类目字典口径",
                )
            )
        if not batch.quality_declaration.get("statement", "").strip():
            check.findings.append(
                Finding(
                    "QUALITY_DECL_EMPTY", "major", batch.batch_id, batch.platform_id,
                    "质量声明为空",
                )
            )

    def _check_timeliness(self, ledger: Ledger, batch: Batch, check: BatchCheck) -> None:
        if batch.event_type == "withdrawal":
            return
        deadline = deadline_for_period(batch.report_period)
        submitted = _parse_day(batch.submitted_at)
        if batch.event_type == "late" or submitted > deadline:
            days = (submitted - deadline).days
            check.findings.append(
                Finding(
                    "LATE_SUBMISSION", "minor", batch.batch_id, batch.platform_id,
                    f"迟报 {days} 天（截止 {deadline.isoformat()}，实际 {batch.submitted_at}）",
                )
            )

    def _check_mappings(self, batch: Batch, check: BatchCheck) -> None:
        method = batch.methodology
        registered: dict[str, str] = {}
        for mapping in method.get("category_mappings", []):
            code = mapping.get("standard_code", "")
            path = mapping.get("platform_path", "")
            if code == UNMAPPED_CODE:
                check.findings.append(
                    Finding(
                        "UNMAPPED_CATEGORY", "blocker", batch.batch_id, batch.platform_id,
                        f"平台类目“{path}”落入未映射占位，不得用于汇总",
                    )
                )
                continue
            try:
                category = self.catalog.get(code)
            except KeyError:
                check.findings.append(
                    Finding(
                        "UNKNOWN_STANDARD_CODE", "blocker", batch.batch_id, batch.platform_id,
                        f"映射目标编码不在分类表中: {code}（平台类目 {path}）",
                        category_code=code,
                    )
                )
                continue
            allowed = category.mapped_paths(batch.platform_id)
            if allowed and path not in allowed:
                check.findings.append(
                    Finding(
                        "MAPPING_NOT_IN_DICTIONARY", "major", batch.batch_id, batch.platform_id,
                        f"平台类目“{path}”-> {code} 不在本平台分类表映射登记中",
                        category_code=code,
                    )
                )
            registered[path] = code

        seen_codes: set[str] = set()
        for obs in batch.observations:
            if obs.platform_path not in registered:
                check.findings.append(
                    Finding(
                        "OBS_WITHOUT_MAPPING", "blocker", batch.batch_id, batch.platform_id,
                        f"观测使用的平台类目“{obs.platform_path}”未出现在本批映射表中",
                        category_code=obs.category_code,
                    )
                )
            elif registered[obs.platform_path] != obs.category_code:
                check.findings.append(
                    Finding(
                        "OBS_CODE_MISMATCH", "blocker", batch.batch_id, batch.platform_id,
                        f"观测类目编码 {obs.category_code} 与映射表中“{obs.platform_path}”"
                        f"的目标 {registered[obs.platform_path]} 不一致",
                        category_code=obs.category_code,
                    )
                )
            if obs.category_code in seen_codes:
                check.findings.append(
                    Finding(
                        "DUP_CATEGORY_OBS", "major", batch.batch_id, batch.platform_id,
                        f"同一标准类目在批次内出现多条观测: {obs.category_code}",
                        category_code=obs.category_code,
                    )
                )
            seen_codes.add(obs.category_code)

    def _check_observations(self, batch: Batch, check: BatchCheck) -> None:
        start, end = _period_bounds(batch.report_period)
        for obs in batch.observations:
            try:
                self.catalog.get(obs.category_code)
            except KeyError:
                check.findings.append(
                    Finding(
                        "UNKNOWN_STANDARD_CODE", "blocker", batch.batch_id, batch.platform_id,
                        f"观测类目编码不在分类表中: {obs.category_code}",
                        category_code=obs.category_code,
                    )
                )
            if obs.refunds > obs.sales:
                check.findings.append(
                    Finding(
                        "REFUND_EXCEEDS_SALES", "major", batch.batch_id, batch.platform_id,
                        f"{obs.category_code} 退款额 {obs.refunds} 大于销售额 {obs.sales}",
                        category_code=obs.category_code,
                    )
                )
            if obs.orders <= 0:
                check.findings.append(
                    Finding(
                        "ZERO_ORDERS", "minor", batch.batch_id, batch.platform_id,
                        f"{obs.category_code} 订单量为零，无法支撑增速测算",
                        category_code=obs.category_code,
                    )
                )
            obs_start = _parse_day(obs.period_start)
            obs_end = _parse_day(obs.period_end)
            if obs_start != start or obs_end != end:
                check.findings.append(
                    Finding(
                        "PERIOD_MISMATCH", "major", batch.batch_id, batch.platform_id,
                        f"{obs.category_code} 观测区间 {obs.period_start}~{obs.period_end}"
                        f"与报告期 {batch.report_period} 不一致",
                        category_code=obs.category_code,
                    )
                )

    def _check_coverage(self, batch: Batch, check: BatchCheck) -> None:
        declared = set(self.covered.get(batch.platform_id, []))
        observed = {o.category_code for o in batch.observations}
        for code in sorted(declared - observed):
            check.findings.append(
                Finding(
                    "COVERAGE_GAP", "minor", batch.batch_id, batch.platform_id,
                    f"平台声明覆盖的类目 {code}（{self.catalog.get(code).name}）本批缺报",
                    category_code=code,
                )
            )
        if not declared:
            return
        for obs in batch.observations:
            if obs.category_code not in declared:
                check.findings.append(
                    Finding(
                        "OUT_OF_SCOPE_CATEGORY", "major", batch.batch_id, batch.platform_id,
                        f"观测类目 {obs.category_code} 不在平台登记覆盖范围内",
                        category_code=obs.category_code,
                    )
                )

    def _validate_chains(self, ledger: Ledger, checks: list[BatchCheck]) -> None:
        by_id = {c.batch.batch_id: c for c in checks}
        for batch in ledger.batches:
            if batch.event_type not in ("withdrawal", "backfill") or not batch.supersedes:
                continue
            target = by_id.get(batch.supersedes)
            if target is None:
                continue
            if batch.event_type == "withdrawal" and target.batch.event_type not in (
                "regular", "late", "api_change",
            ):
                target.findings.append(
                    Finding(
                        "WD_TARGET_INVALID", "major", batch.supersedes, batch.platform_id,
                        f"撤回批次 {batch.batch_id} 的撤回对象不是常规数据批次",
                    )
                )
            if batch.event_type == "backfill" and target.has_blocker:
                check = by_id[batch.batch_id]
                check.findings.append(
                    Finding(
                        "BACKFILL_AFTER_BLOCKER", "info", batch.batch_id, batch.platform_id,
                        "回补批次替代了含阻断问题的旧批次，引用时应以本批为准",
                    )
                )

    def comparability_breakpoints(
        self, ledger: Ledger, platform_id: str, category_code: str
    ) -> list[Finding]:
        """沿有效时间轴检测口径断点。

        - 接口改版且该类目的映射路径集合发生变化：API_CALIBER_BREAK；
        - 映射路径集合在非改版批次上变化：MAPPING_SCOPE_BREAK；
        - 去重/退款/价格口径文本变化：METHODOLOGY_DRIFT。
        类目字典版本的差异最终体现为映射路径变化，不单独计断点，
        以免把与本类目无关的字典改版误判为不可比。
        """
        timeline = ledger.category_timeline(platform_id, category_code)
        breakpoints: list[Finding] = []
        watched = ("dedup_method", "refund_treatment", "price_caliber")
        previous: Batch | None = None
        for batch in timeline:
            if previous is not None:
                prev_paths = self._mapping_paths(previous, category_code)
                curr_paths = self._mapping_paths(batch, category_code)
                if curr_paths != prev_paths:
                    code = (
                        "API_CALIBER_BREAK" if batch.event_type == "api_change"
                        else "MAPPING_SCOPE_BREAK"
                    )
                    added = sorted(curr_paths - prev_paths)
                    removed = sorted(prev_paths - curr_paths)
                    detail = []
                    if added:
                        detail.append(f"并入 {added}")
                    if removed:
                        detail.append(f"移出 {removed}")
                    breakpoints.append(
                        Finding(
                            code, "major", batch.batch_id, platform_id,
                            f"{category_code} 类目映射口径变化（{'，'.join(detail)}），"
                            f"与 {previous.report_period} 及以前不可直接比较",
                            category_code=category_code,
                        )
                    )
                for key in watched:
                    if batch.methodology.get(key) != previous.methodology.get(key):
                        breakpoints.append(
                            Finding(
                                "METHODOLOGY_DRIFT", "major", batch.batch_id, platform_id,
                                f"{category_code} 口径要素 {key} 较 {previous.batch_id} 发生变化，"
                                "跨期比较需重述或加注",
                                category_code=category_code,
                            )
                        )
            previous = batch
        return breakpoints

    @staticmethod
    def _mapping_paths(batch: Batch, category_code: str) -> set[str]:
        return {
            mapping["platform_path"]
            for mapping in batch.methodology.get("category_mappings", [])
            if mapping.get("standard_code") == category_code
        }
