"""异常波动检测：命中阈值或历史离散度的观测先进入核查隔离，不写入公开统计。

口径断点（接口改版/方法论变更）批次不参与波动判定——其环比变化不具
可比性，由断点工单单独处置；断点之后在新口径下积累的环比重新构成
比较基准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean, pstdev

from .ledger import Batch, Ledger
from .validation import Finding

# 环比绝对变化阈值；z 分数阈值与启用条件
MOM_CHANGE_THRESHOLD = 0.30
Z_SCORE_THRESHOLD = 2.0
Z_MIN_HISTORY = 3      # 至少 3 个同口径历史环比才启用 z 分数
Z_MIN_EFFECT = 0.10    # z 分数判定同时要求环比变化不小于 10%，避免平滑序列误报


@dataclass(frozen=True)
class Anomaly:
    platform_id: str
    category_code: str
    report_period: str
    batch_id: str
    sales: float
    previous_sales: float
    mom_change: float
    z_score: float | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "platform_id": self.platform_id,
            "category_code": self.category_code,
            "report_period": self.report_period,
            "batch_id": self.batch_id,
            "sales": self.sales,
            "previous_sales": self.previous_sales,
            "mom_change": round(self.mom_change, 4),
            "z_score": None if self.z_score is None else round(self.z_score, 3),
            "reasons": list(self.reasons),
            "state": "quarantined_pending_review",
        }


@dataclass
class AnomalyDetector:
    mom_threshold: float = MOM_CHANGE_THRESHOLD
    z_threshold: float = Z_SCORE_THRESHOLD
    breakpoints: dict[tuple[str, str, str], list[Finding]] = field(default_factory=dict)

    def note_breakpoints(self, findings: list[Finding]) -> None:
        for f in findings:
            if f.code in ("API_CALIBER_BREAK", "MAPPING_SCOPE_BREAK", "METHODOLOGY_DRIFT"):
                self.breakpoints.setdefault(
                    (f.platform_id, f.category_code or "", f.batch_id), []
                ).append(f)

    def _is_break_batch(self, platform_id: str, category_code: str, batch: Batch) -> bool:
        return bool(self.breakpoints.get((platform_id, category_code, batch.batch_id)))

    def detect(self, ledger: Ledger, platform_id: str, category_code: str) -> list[Anomaly]:
        timeline = ledger.category_timeline(platform_id, category_code)
        anomalies: list[Anomaly] = []
        changes: list[float] = []

        for index, batch in enumerate(timeline):
            obs = batch.observation_for(category_code)
            if obs is None or index == 0:
                continue
            previous = timeline[index - 1]
            prev_obs = previous.observation_for(category_code)
            if prev_obs is None or prev_obs.sales <= 0:
                continue

            if self._is_break_batch(platform_id, category_code, batch):
                # 断点批次：环比跨口径，不做波动判定，旧基准作废重新积累
                changes = []
                continue

            mom = (obs.sales - prev_obs.sales) / prev_obs.sales
            z_score: float | None = None
            reasons: list[str] = []

            if abs(mom) >= self.mom_threshold:
                reasons.append(
                    f"环比变化 {mom:+.1%} 超过阈值 {self.mom_threshold:.0%}"
                )
            if len(changes) >= Z_MIN_HISTORY:
                mean = fmean(changes)
                sd = pstdev(changes)
                if sd > 0:
                    z_score = (mom - mean) / sd
                    if abs(z_score) >= self.z_threshold and abs(mom) >= Z_MIN_EFFECT:
                        reasons.append(
                            f"环比 z 分数 {z_score:.2f}（阈值 ±{self.z_threshold:.1f}），"
                            f"明显偏离历史 {len(changes)} 期同口径波动形态"
                        )

            if reasons:
                anomalies.append(
                    Anomaly(
                        platform_id=platform_id,
                        category_code=category_code,
                        report_period=batch.report_period,
                        batch_id=batch.batch_id,
                        sales=obs.sales,
                        previous_sales=prev_obs.sales,
                        mom_change=mom,
                        z_score=z_score,
                        reasons=tuple(reasons),
                    )
                )
            changes.append(mom)
        return anomalies

    def detect_all(
        self, ledger: Ledger, platform_category_pairs: list[tuple[str, str]]
    ) -> list[Anomaly]:
        result: list[Anomaly] = []
        for platform_id, category_code in platform_category_pairs:
            result.extend(self.detect(ledger, platform_id, category_code))
        result.sort(key=lambda a: (a.report_period, a.platform_id, a.category_code))
        return result
