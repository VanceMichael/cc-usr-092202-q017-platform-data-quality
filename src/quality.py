"""六维质量评分与置信等级。

维度对应用户要求每次报送保存的六项方法论要素：
样本覆盖、类目字典、去重方法、退款处理、价格口径、质量声明。
评分按“平台 × 标准类目 × 批次（报告期）”给出，扣分均附原因，
使指标采用时的置信说明可以逐维回溯。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .validation import BatchCheck, Finding

DIMENSIONS = ("coverage", "dictionary", "dedup", "refund", "price", "statement")
DIMENSION_LABELS = {
    "coverage": "样本覆盖",
    "dictionary": "类目字典",
    "dedup": "去重方法",
    "refund": "退款处理",
    "price": "价格口径",
    "statement": "质量声明",
}

# 等级门槛
GRADE_A = 90
GRADE_B = 80
GRADE_C = 65

CONFIDENCE_BY_GRADE = {
    "A": "高置信：口径稳定、要素齐全，可在批准范围内采用",
    "B": "较高置信：存在轻微口径差异，采用时需附说明",
    "C": "有限置信：口径或覆盖存在明显保留，仅限批准的较低范围使用",
    "D": "不可用：存在阻断性问题，不得用于任何统计成果",
}

# 校验问题代码到评分维度的归属
FINDING_DIMENSION = {
    "MAPPING_TABLE_EMPTY": "dictionary",
    "UNMAPPED_CATEGORY": "dictionary",
    "UNKNOWN_STANDARD_CODE": "dictionary",
    "MAPPING_NOT_IN_DICTIONARY": "dictionary",
    "OBS_WITHOUT_MAPPING": "dictionary",
    "OBS_CODE_MISMATCH": "dictionary",
    "API_CALIBER_BREAK": "dictionary",
    "MAPPING_SCOPE_BREAK": "dictionary",
    "METHODOLOGY_DRIFT": "dedup",
    "DUP_CATEGORY_OBS": "dedup",
    "COVERAGE_GAP": "coverage",
    "OUT_OF_SCOPE_CATEGORY": "coverage",
    "REFUND_EXCEEDS_SALES": "refund",
    "ZERO_ORDERS": "statement",
    "PERIOD_MISMATCH": "statement",
    "LATE_SUBMISSION": "statement",
    "QUALITY_DECL_EMPTY": "statement",
    "WD_WITH_OBS": "statement",
    "WD_TARGET_INVALID": "statement",
    "BACKFILL_AFTER_BLOCKER": "statement",
}
MAPPING_BREAK_CODES = {"API_CALIBER_BREAK", "MAPPING_SCOPE_BREAK"}
METHODOLOGY_DIMENSION = {
    "sample_coverage": "coverage",
    "category_dictionary": "dictionary",
    "dedup_method": "dedup",
    "refund_treatment": "refund",
    "price_caliber": "price",
    "quality_statement": "statement",
}
SEVERITY_POINTS = {"major": 25, "minor": 8, "info": 0}


@dataclass
class QualityRating:
    platform_id: str
    category_code: str
    batch_id: str
    report_period: str
    scores: dict[str, int]
    deductions: list[str] = field(default_factory=list)
    grade: str = "D"
    confidence: str = ""
    blocked: bool = False
    quarantined: bool = False

    @property
    def total(self) -> int:
        return round(sum(self.scores.values()) / len(self.scores))

    def as_dict(self) -> dict:
        return {
            "platform_id": self.platform_id,
            "category_code": self.category_code,
            "batch_id": self.batch_id,
            "report_period": self.report_period,
            "total_score": self.total,
            "grade": self.grade,
            "confidence": self.confidence,
            "dimension_scores": {
                DIMENSION_LABELS[key]: self.scores[key] for key in DIMENSIONS
            },
            "deductions": self.deductions,
            "blocked": self.blocked,
            "quarantined": self.quarantined,
        }


def grade_for(score: int) -> str:
    if score >= GRADE_A:
        return "A"
    if score >= GRADE_B:
        return "B"
    if score >= GRADE_C:
        return "C"
    return "D"


class QualityScorer:
    def __init__(self, full_catalog_size: int, platform_covered_codes: set[str]):
        self.full_catalog_size = full_catalog_size
        self.platform_covered_codes = platform_covered_codes

    def rate(
        self,
        check: BatchCheck,
        category_code: str,
        *,
        anomaly_open: bool = False,
        historical_break: bool = False,
    ) -> QualityRating:
        batch = check.batch
        scores = {key: 100 for key in DIMENSIONS}
        deductions: list[str] = []
        blocked = False

        def deduct(dimension: str, points: int, reason: str, is_blocker: bool = False) -> None:
            nonlocal blocked
            if is_blocker:
                scores[dimension] = 0
                blocked = True
            else:
                scores[dimension] = max(0, scores[dimension] - points)
            deductions.append(f"[{DIMENSION_LABELS[dimension]}] {reason}")

        for finding in check.findings:
            if finding.category_code not in (None, category_code):
                continue
            dimension = self._dimension_of(finding)
            if finding.severity == "blocker":
                deduct(dimension, 100, f"{finding.code}: {finding.message}", is_blocker=True)
            else:
                points = SEVERITY_POINTS.get(finding.severity, 0)
                if points:
                    deduct(dimension, points, f"{finding.code}: {finding.message}")

        self._score_concepts(batch, category_code, scores, deductions, deduct,
                             historical_break)

        if anomaly_open:
            deduct("statement", 12, "ANOMALY_OPEN: 异常波动核查未关闭，观测处于隔离状态")

        total = round(sum(scores.values()) / len(scores))
        if blocked:
            grade = "D"
        else:
            grade = grade_for(total)
            if anomaly_open and grade in ("A", "B"):
                grade = "C"
                deductions.append("异常核查未关闭，等级上限调为C")

        return QualityRating(
            platform_id=batch.platform_id,
            category_code=category_code,
            batch_id=batch.batch_id,
            report_period=batch.report_period,
            scores=scores,
            deductions=deductions,
            grade=grade,
            confidence=CONFIDENCE_BY_GRADE[grade],
            blocked=blocked,
            quarantined=anomaly_open,
        )

    def _dimension_of(self, finding: Finding) -> str:
        if finding.code == "METHODOLOGY_MISSING":
            key = finding.message.rsplit(":", 1)[-1].strip()
            return METHODOLOGY_DIMENSION.get(key, "statement")
        if finding.code == "METHODOLOGY_DRIFT":
            marker = "口径要素 "
            if marker in finding.message:
                key = finding.message.split(marker, 1)[1].split(" 较", 1)[0].strip()
                return METHODOLOGY_DIMENSION.get(key, "statement")
        return FINDING_DIMENSION.get(finding.code, "statement")

    def _score_concepts(
        self, batch, category_code, scores, deductions, deduct, historical_break
    ) -> None:
        method = batch.methodology
        obs = batch.observation_for(category_code)
        if obs is None:
            return

        # 样本覆盖：覆盖广度 + 代表性比例声明
        breadth = len(self.platform_covered_codes) / self.full_catalog_size
        if breadth < 1.0:
            missing = self.full_catalog_size - len(self.platform_covered_codes)
            deduct("coverage", 15, f"平台仅覆盖 {len(self.platform_covered_codes)}/"
                                   f"{self.full_catalog_size} 个标准类目（缺 {missing} 个）")
        if not re.search(r"约?\s*\d+\s*%", method.get("sample_coverage", "")):
            deduct("coverage", 10, "样本覆盖未声明代表性比例，无法估计对总体的覆盖程度")

        # 类目字典：宽口径映射声明 + 历史口径断点
        broad_codes = self._broad_mapping_codes(batch)
        if category_code in broad_codes:
            deduct("dictionary", 30,
                   f"MAPPING_OVER_BROAD: {method.get('mapping_basis', '存在多路径并入且未拆分的宽口径')}")
        if historical_break:
            deduct("dictionary", 15, "HISTORICAL_BREAK: 历史上发生接口/口径变更且未回溯重述，"
                                     "与变更前时期不可直接比较")

        # 去重方法：须明确订单级去重键
        dedup_text = method.get("dedup_method", "")
        if "订单" not in dedup_text:
            deduct("dedup", 15, "去重方法未说明订单级去重键，重复计入风险无法评估")

        # 退款处理：跨期回溯调整属于与“当期冲减”不同的口径
        refund_text = method.get("refund_treatment", "")
        if "回溯调整上期" in refund_text or "冲减原发货月" in refund_text:
            deduct("refund", 10, "退款按原发货月跨期冲减，与当期冲减口径存在时间差")

        # 价格口径：税/运费要素完整，积分抵扣为已知差异
        price_text = method.get("price_caliber", "")
        if "税" not in price_text:
            deduct("price", 10, "价格口径未说明是否含税")
        if "运费" not in price_text:
            deduct("price", 10, "价格口径未说明运费处理")
        if "积分" in price_text:
            deduct("price", 10, "价格口径剔除平台积分抵扣部分，与其他平台口径有差异")

        # 质量声明：迟报、自我标注待核实
        if batch.event_type == "late":
            deduct("statement", 8, "迟报批次，报送及时性不足")
        decl = batch.quality_declaration.get("statement", "")
        if "待核实" in decl or "待核" in decl:
            deduct("statement", 10, f"质量声明自标注待核实: {decl}")

    @staticmethod
    def _broad_mapping_codes(batch) -> set[str]:
        method = batch.methodology
        if not method.get("mapping_basis"):
            return set()
        codes: dict[str, int] = {}
        for mapping in method.get("category_mappings", []):
            code = mapping.get("standard_code")
            if code:
                codes[code] = codes.get(code, 0) + 1
        return {code for code, count in codes.items() if count > 1}
