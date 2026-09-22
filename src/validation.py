"""自动校验引擎。

对每个平台-报告期的“当前有效批次”执行：
1. 结构与随附资料完整性（六项口径 + 质量声明）；
2. 类目映射一致性（平台原始类目须在字典中且映射目标与声明一致）；
3. 样本覆盖充分性；
4. 报送及时性；
5. 口径可比性（与上一有效批次逐项比对，覆盖/字典/去重/退款/价格/促销任一变化都告警）；
6. 异常波动（时间序列偏离 + 同期跨平台偏离）。

阻断规则：结构/映射严重问题阻断整批；异常波动仅隔离单个观测值。
被隔离的批次或观测值不会进入公开统计聚合。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from .dataset import Batch, Dataset, Observation
from .model import (
    BatchKind,
    CALIBRE_LABELS,
    CALIBRE_SIGNATURE_FIELDS,
    Finding,
    METHODOLOGY_FIELDS,
    QUALITY_STATEMENT_FIELD,
    Severity,
    TIMELY_KINDS,
)

# 阈值（可按局内规定外部化配置）
MIN_OBSERVATIONS = 3
MIN_COVERED_CATEGORY_RATE = 0.8          # 报送类目占分类表比例
MIN_SHARE_FOR_AGGREGATE = 0.30
MIN_SHARE_FOR_PUBLIC = 0.50
Z_ANOMALY = 2.5                          # 历史序列 z 值阈值
Z_STDEV_FLOOR = 0.02                     # 标准差下限（2 个百分点），避免短序列 z 值失真
DELTA_ANOMALY = 0.15                     # 同比变化绝对值阈值（15 个百分点）
CROSS_PLATFORM_DELTA = 0.20             # 跨平台同比偏离中位数阈值
HISTORY_MIN_POINTS = 2


@dataclass
class BatchReport:
    batch_id: str
    platform_id: str
    period: str
    findings: list[Finding] = field(default_factory=list)
    quarantined_obs: set[str] = field(default_factory=set)

    @property
    def blocked(self) -> bool:
        return any(f.blocking and f.obs_id is None for f in self.findings)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in (Severity.ERROR, Severity.BLOCKER)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "platform_id": self.platform_id,
            "period": self.period,
            "blocked": self.blocked,
            "quarantined_obs": sorted(self.quarantined_obs),
            "findings": [f.to_dict() for f in self.findings],
        }


class Validator:
    def __init__(self, dataset: Dataset):
        self.ds = dataset

    # ------------------------------------------------------------ 入口

    def validate_all(self) -> dict[str, BatchReport]:
        """校验全部非撤回批次（含历史批次）。

        历史批次的工单会在校验后由服务层根据批次状态自动作废，
        这样“新批次替代/撤回”不删除任何历史痕迹，又不会遗留待办。
        """
        reports: dict[str, BatchReport] = {}
        for batch in sorted(self.ds.batches, key=lambda b: (b.period, b.platform_id, b.received_at)):
            if batch.kind is BatchKind.WITHDRAWAL:
                continue
            reports[batch.batch_id] = self.validate_batch(batch)
        return reports

    def validate_batch(self, batch: Batch) -> BatchReport:
        report = BatchReport(batch.batch_id, batch.platform_id, batch.period)
        self._check_structure(batch, report)
        self._check_mapping(batch, report)
        self._check_coverage(batch, report)
        self._check_timeliness(batch, report)
        self._check_calibre_stability(batch, report)
        self._check_history_anomaly(batch, report)
        self._check_cross_platform(batch, report)
        return report

    # ------------------------------------------------------------ 1 结构

    def _check_structure(self, batch: Batch, report: BatchReport) -> None:
        m = batch.methodology
        for key in METHODOLOGY_FIELDS:
            if not str(getattr(m, key, "")).strip():
                report.findings.append(Finding(
                    code="METHODOLOGY_MISSING",
                    severity=Severity.ERROR,
                    message=f"批次缺少随附口径说明：{CALIBRE_LABELS.get(key, key)}",
                    blocking=True,
                ))
        if not str(getattr(m, QUALITY_STATEMENT_FIELD, "")).strip():
            report.findings.append(Finding(
                code="QUALITY_STATEMENT_MISSING",
                severity=Severity.ERROR,
                message="批次缺少报送质量声明",
                blocking=True,
            ))
        if not m.category_map:
            report.findings.append(Finding(
                code="METHODOLOGY_MISSING",
                severity=Severity.ERROR,
                message="批次缺少类目字典映射表（category_map 为空）",
                blocking=True,
            ))
        if len(batch.observations) < MIN_OBSERVATIONS:
            report.findings.append(Finding(
                code="TOO_FEW_OBSERVATIONS",
                severity=Severity.WARNING,
                message=f"有效观测类目仅 {len(batch.observations)} 个，少于建议下限 {MIN_OBSERVATIONS}",
            ))

    # ------------------------------------------------------------ 2 映射

    def _check_mapping(self, batch: Batch, report: BatchReport) -> None:
        cat_map = batch.methodology.category_map
        seen_platform_cats: set[str] = set()
        for obs in batch.observations:
            if obs.platform_category not in cat_map:
                report.findings.append(Finding(
                    code="CATEGORY_UNMAPPED",
                    severity=Severity.ERROR,
                    message=(
                        f"平台原始类目“{obs.platform_category}”未出现在类目字典映射中，"
                        f"报送方却声明归入 {obs.category}"
                    ),
                    obs_id=obs.obs_id,
                    category=obs.category,
                    blocking=True,
                ))
                report.quarantined_obs.add(obs.obs_id)
            elif cat_map[obs.platform_category] != obs.category:
                report.findings.append(Finding(
                    code="CATEGORY_MAP_MISMATCH",
                    severity=Severity.ERROR,
                    message=(
                        f"平台原始类目“{obs.platform_category}”字典映射至 "
                        f"{cat_map[obs.platform_category]}，观测却声明为 {obs.category}"
                    ),
                    obs_id=obs.obs_id,
                    category=obs.category,
                    blocking=True,
                ))
                report.quarantined_obs.add(obs.obs_id)
            seen_platform_cats.add(obs.platform_category)

            if obs.sales_index <= 0:
                report.findings.append(Finding(
                    code="IMPLAUSIBLE_INDEX",
                    severity=Severity.BLOCKER,
                    message=f"销售指数 {obs.sales_index} 非正，数据不可信",
                    obs_id=obs.obs_id,
                    category=obs.category,
                    blocking=True,
                ))
                report.quarantined_obs.add(obs.obs_id)

        # 字典目标必须都存在于统一分类表
        for raw, target in cat_map.items():
            if target not in self.ds.categories:
                report.findings.append(Finding(
                    code="DICTIONARY_TARGET_UNKNOWN",
                    severity=Severity.ERROR,
                    message=f"类目字典把“{raw}”映射到不存在的统一类目 {target}",
                    category=target,
                    blocking=True,
                ))

        # 同一统一类目被多个平台类目映射时提示（可能造成重复计量）
        targets = list(cat_map.values())
        dup = {t for t in targets if targets.count(t) > 1}
        for t in sorted(dup):
            raws = sorted(k for k, v in cat_map.items() if v == t)
            report.findings.append(Finding(
                code="CATEGORY_MANY_TO_ONE",
                severity=Severity.WARNING,
                message=f"统一类目 {t} 同时映射自 {raws}，需核实去重口径是否覆盖跨类目重复",
                category=t,
            ))

    # ------------------------------------------------------------ 3 覆盖

    def _check_coverage(self, batch: Batch, report: BatchReport) -> None:
        share = batch.methodology.expected_share
        if share <= 0:
            report.findings.append(Finding(
                code="COVERAGE_SHARE_UNDECLARED",
                severity=Severity.WARNING,
                message="未声明样本市场覆盖份额，无法评估覆盖充分性",
            ))
        elif share < MIN_SHARE_FOR_AGGREGATE:
            report.findings.append(Finding(
                code="COVERAGE_TOO_LOW",
                severity=Severity.ERROR,
                message=f"声明覆盖份额 {share:.0%} 低于进入汇总趋势所需 {MIN_SHARE_FOR_AGGREGATE:.0%}",
                blocking=True,
            ))
        elif share < MIN_SHARE_FOR_PUBLIC:
            report.findings.append(Finding(
                code="COVERAGE_LIMITED",
                severity=Severity.WARNING,
                message=f"声明覆盖份额 {share:.0%} 低于公开发布建议 {MIN_SHARE_FOR_PUBLIC:.0%}",
            ))

        reported = {o.category for o in batch.observations
                    if o.obs_id not in report.quarantined_obs}
        rate = len(reported) / len(self.ds.categories) if self.ds.categories else 0
        if rate < MIN_COVERED_CATEGORY_RATE:
            report.findings.append(Finding(
                code="CATEGORY_COVERAGE_LOW",
                severity=Severity.WARNING,
                message=(
                    f"覆盖统一类目 {len(reported)}/{len(self.ds.categories)} "
                    f"（{rate:.0%}），低于 {MIN_COVERED_CATEGORY_RATE:.0%}"
                ),
            ))

    # ------------------------------------------------------------ 4 及时性

    def _check_timeliness(self, batch: Batch, report: BatchReport) -> None:
        # 改版重报/历史回补按自身事件时间入账，不判定为迟报
        if batch.kind not in TIMELY_KINDS:
            return
        if batch.received_at > batch.due_at:
            report.findings.append(Finding(
                code="LATE_SUBMISSION",
                severity=Severity.WARNING,
                message=f"迟报：应报 {batch.due_at}，实收 {batch.received_at}",
            ))

    # ------------------------------------------------------------ 5 口径稳定

    def _check_calibre_stability(self, batch: Batch, report: BatchReport) -> None:
        prior = self._prior_effective(batch)
        if prior is None:
            return
        old, new = prior.methodology, batch.methodology
        for key in CALIBRE_SIGNATURE_FIELDS:
            old_v = getattr(old, key)
            new_v = getattr(new, key)
            if old_v != new_v:
                report.findings.append(Finding(
                    code="CALIBRE_CHANGED",
                    severity=Severity.WARNING,
                    message=(
                        f"{CALIBRE_LABELS[key]}相对上一有效批次 {prior.batch_id} 发生变化"
                        f"（{old_v!r} → {new_v!r}），跨期汇总可能失去可比性"
                    ),
                ))
        # 映射表条目变化给出更具体的提示
        old_map, new_map = old.category_map, new.category_map
        added = sorted(set(new_map) - set(old_map))
        removed = sorted(set(old_map) - set(new_map))
        changed = sorted(k for k in set(old_map) & set(new_map) if old_map[k] != new_map[k])
        for raw in added:
            report.findings.append(Finding(
                code="CATEGORY_MAP_CHANGED",
                severity=Severity.WARNING,
                message=f"类目字典新增映射“{raw}”→{new_map[raw]}，需确认历史数据是否回刷",
            ))
        for raw in removed:
            report.findings.append(Finding(
                code="CATEGORY_MAP_CHANGED",
                severity=Severity.WARNING,
                message=f"类目字典移除映射“{raw}”（原→{old_map[raw]}），口径范围缩小",
            ))
        for raw in changed:
            report.findings.append(Finding(
                code="CATEGORY_MAP_CHANGED",
                severity=Severity.WARNING,
                message=f"类目“{raw}”映射由 {old_map[raw]} 改为 {new_map[raw]}，同比不可直接比较",
            ))

    # ------------------------------------------------------------ 6a 时间序列异常

    def _check_history_anomaly(self, batch: Batch, report: BatchReport) -> None:
        prior = self._prior_effective(batch)
        if prior is None:
            report.findings.append(Finding(
                code="NO_HISTORY_BASELINE",
                severity=Severity.INFO,
                message="该平台该期为首次有效报送，缺少历史基线，异常检测不适用",
            ))
            return
        history = self._history_values(batch.platform_id, batch.period)
        for obs in batch.observations:
            series = history.get(obs.category, [])
            if len(series) < HISTORY_MIN_POINTS:
                continue
            mean = statistics.fmean(series)
            stdev = max(statistics.pstdev(series), Z_STDEV_FLOOR)
            delta = abs(obs.yoy_growth - mean)
            z = delta / stdev
            if z >= Z_ANOMALY or abs(obs.yoy_growth - series[-1]) >= DELTA_ANOMALY:
                report.findings.append(Finding(
                    code="ANOMALY_HISTORICAL",
                    severity=Severity.WARNING,
                    message=(
                        f"类目 {obs.category} 同比 {obs.yoy_growth:.1%} 异常偏离历史"
                        f"（近 {len(series)} 期均值 {mean:.1%}，z={z:.1f}）；"
                        f"先进入核查，暂不写入公开统计"
                    ),
                    obs_id=obs.obs_id,
                    category=obs.category,
                    blocking=True,
                ))
                report.quarantined_obs.add(obs.obs_id)

    # ------------------------------------------------------------ 6b 跨平台异常

    def _check_cross_platform(self, batch: Batch, report: BatchReport) -> None:
        peers: dict[str, list[float]] = {}
        for (pid, period), other in self.ds.effective_batches().items():
            if period != batch.period:
                continue
            for o in other.observations:
                peers.setdefault(o.category, []).append(o.yoy_growth)
        for obs in batch.observations:
            values = peers.get(obs.category, [])
            if len(values) < 3:
                # 至少三家平台才有稳健中位数（含本平台）
                continue
            median = statistics.median(values)
            if abs(obs.yoy_growth - median) >= CROSS_PLATFORM_DELTA:
                report.findings.append(Finding(
                    code="ANOMALY_CROSS_SOURCE",
                    severity=Severity.WARNING,
                    message=(
                        f"类目 {obs.category} 同比 {obs.yoy_growth:.1%} 与同期其他平台中位数 "
                        f"{median:.1%} 偏离 {abs(obs.yoy_growth - median):.1%}，需核查来源差异"
                    ),
                    obs_id=obs.obs_id,
                    category=obs.category,
                    blocking=True,
                ))
                report.quarantined_obs.add(obs.obs_id)

    # ------------------------------------------------------------ 辅助

    def _prior_effective(self, batch: Batch) -> Batch | None:
        """取该平台紧邻上一报告期的有效批次。"""
        priors = self.ds.previous_periods(batch.period)
        for period in reversed(priors):
            b = self.ds.effective_batch(batch.platform_id, period)
            if b is not None:
                return b
        return None

    def _history_values(self, platform_id: str, period: str) -> dict[str, list[float]]:
        """该平台历史各有效批次、各类目的同比序列（不含被隔离判定所需的当期）。"""
        out: dict[str, list[float]] = {}
        for p in self.ds.previous_periods(period):
            b = self.ds.effective_batch(platform_id, p)
            if b is None:
                continue
            for o in b.observations:
                out.setdefault(o.category, []).append(o.yoy_growth)
        return out


def quarantined_obs_map(reports: dict[str, BatchReport]) -> dict[str, set[str]]:
    return {bid: set(r.quarantined_obs) for bid, r in reports.items()}
