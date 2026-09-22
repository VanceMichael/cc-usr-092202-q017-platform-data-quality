"""append-only 平台报送批次台账。

迟报、撤回、接口改版、历史回补均登记为新批次，历史批次永不被覆盖修改：
- withdrawal 批次不含观测，并将其 supersedes 指向的批次标记为作废；
- backfill 批次替代同报告期的旧数据批次；
- api_change 批次携带数据，同时在口径时间轴上形成可比性断点。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

EVENT_TYPES = {"regular", "late", "withdrawal", "api_change", "backfill"}
DATA_EVENTS = {"regular", "late", "api_change", "backfill"}

METHODOLOGY_KEYS = (
    "sample_coverage",
    "category_dictionary",
    "dedup_method",
    "refund_treatment",
    "price_caliber",
    "quality_statement",
)


@dataclass(frozen=True)
class Observation:
    category_code: str
    platform_path: str
    sales: float
    orders: int
    refunds: float
    period_start: str
    period_end: str

    @classmethod
    def from_dict(cls, raw: dict) -> "Observation":
        return cls(
            category_code=raw["category_code"],
            platform_path=raw["platform_path"],
            sales=float(raw["sales"]),
            orders=int(raw["orders"]),
            refunds=float(raw["refunds"]),
            period_start=raw["period_start"],
            period_end=raw["period_end"],
        )


@dataclass(frozen=True)
class Batch:
    batch_id: str
    platform_id: str
    report_period: str
    event_type: str
    submitted_at: str
    supersedes: str | None
    reason: str
    methodology: dict
    observations: tuple[Observation, ...]
    quality_declaration: dict

    @classmethod
    def from_dict(cls, raw: dict) -> "Batch":
        return cls(
            batch_id=raw["batch_id"],
            platform_id=raw["platform_id"],
            report_period=raw["report_period"],
            event_type=raw["event_type"],
            submitted_at=raw["submitted_at"],
            supersedes=raw.get("supersedes"),
            reason=raw["reason"],
            methodology=raw["methodology"],
            observations=tuple(Observation.from_dict(o) for o in raw.get("observations", [])),
            quality_declaration=raw["quality_declaration"],
        )

    @property
    def is_data_batch(self) -> bool:
        return self.event_type in DATA_EVENTS and bool(self.observations)

    def observation_for(self, category_code: str) -> Observation | None:
        for obs in self.observations:
            if obs.category_code == category_code:
                return obs
        return None


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def deadline_for_period(report_period: str, deadline_day: int = 15) -> date:
    """报告期 YYYY-MM 的报送截止日：次月 deadline_day 日。"""
    year, month = (int(part) for part in report_period.split("-"))
    year += month // 12
    month = month % 12 + 1
    return date(year, month, deadline_day)


@dataclass
class Ledger:
    version: int
    batches: list[Batch]
    unit: str = "万元"
    deadline_rule: str = ""

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        raw = json.loads(path.read_text(encoding="utf-8"))
        batches = [Batch.from_dict(item) for item in raw["batches"]]
        ledger = cls(
            version=raw["ledger_version"],
            batches=batches,
            unit=raw.get("currency_unit", "万元"),
            deadline_rule=raw.get("submission_deadline_rule", ""),
        )
        ledger.validate_structure()
        return ledger

    def validate_structure(self) -> None:
        """台账级结构约束：标识唯一、事件类型合法、替代链指向同平台同期批次。"""
        ids = [b.batch_id for b in self.batches]
        if len(set(ids)) != len(ids):
            raise ValueError("台账中存在重复批次标识")
        by_id = {b.batch_id: b for b in self.batches}
        for batch in self.batches:
            if batch.event_type not in EVENT_TYPES:
                raise ValueError(f"批次 {batch.batch_id} 事件类型非法: {batch.event_type}")
            if batch.supersedes:
                target = by_id.get(batch.supersedes)
                if target is None:
                    raise ValueError(
                        f"批次 {batch.batch_id} 的 supersedes 指向不存在的批次 {batch.supersedes}"
                    )
                if target.platform_id != batch.platform_id:
                    raise ValueError(f"批次 {batch.batch_id} 跨平台替代，不允许")
                if target.report_period != batch.report_period:
                    raise ValueError(f"批次 {batch.batch_id} 跨报告期替代，不允许")
                if _parse_day(batch.submitted_at) < _parse_day(target.submitted_at):
                    raise ValueError(f"批次 {batch.batch_id} 早于被替代批次，台账时序非法")

    def for_platform(self, platform_id: str) -> list[Batch]:
        return [b for b in self.batches if b.platform_id == platform_id]

    def withdrawn_ids(self) -> set[str]:
        return {
            b.supersedes
            for b in self.batches
            if b.event_type == "withdrawal" and b.supersedes
        }

    def backfilled_ids(self) -> set[str]:
        return {
            b.supersedes
            for b in self.batches
            if b.event_type == "backfill" and b.supersedes
        }

    def is_late(self, batch: Batch) -> bool:
        if batch.event_type == "late":
            return True
        return _parse_day(batch.submitted_at) > deadline_for_period(batch.report_period)

    def effective_batches(self, platform_id: str | None = None) -> list[Batch]:
        """每个报告期当前有效的数据批次（排除撤回与被回补替代的批次）。"""
        withdrawn = self.withdrawn_ids()
        backfilled = self.backfilled_ids()
        result = [
            b
            for b in self.batches
            if b.is_data_batch
            and b.batch_id not in withdrawn
            and b.batch_id not in backfilled
            and (platform_id is None or b.platform_id == platform_id)
        ]
        result.sort(key=lambda b: b.report_period)
        return result

    def effective_batch(self, platform_id: str, report_period: str) -> Batch | None:
        for batch in self.effective_batches(platform_id):
            if batch.report_period == report_period:
                return batch
        return None

    def category_timeline(self, platform_id: str, category_code: str) -> list[Batch]:
        """该平台该类目按报告期排列的有效数据批次，供波动检测与口径断点判断。"""
        timeline = [
            b
            for b in self.effective_batches(platform_id)
            if b.observation_for(category_code) is not None
        ]
        timeline.sort(key=lambda b: b.report_period)
        return timeline
