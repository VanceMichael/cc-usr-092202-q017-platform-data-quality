"""数据集载入与批次解析：分类表、平台、报送批次、观测值。

批次规则（核心）：
同一 (platform_id, period) 下可以有多个批次，按 received_at 排序：
- scheduled/late 为常规报送；
- api_revision 是接口改版后的重报，supersede 它之前的最新有效批次；
- backfill 是对历史期的回补，同样只作为该期的新批次；
- withdrawal 撤回该期当前有效批次（批次不删除，标记被撤回）。
任何情况下旧批次都保留可追溯，只有“当前有效批次”参与评分、审批与公开。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import BatchKind


@dataclass(frozen=True)
class Category:
    code: str          # 统一类目编码
    name: str          # 统一类目名称
    domain: str        # goods / service
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Platform:
    platform_id: str
    name: str


@dataclass(frozen=True)
class Methodology:
    """一次报送随附的口径快照（不可变，随批次永久保存）。"""

    coverage_note: str
    dictionary_version: str
    dedup_method: str
    refund_handling: str
    price_basis: str
    promo_basis: str
    quality_statement: str
    expected_share: float = 0.0  # 平台声明的样本市场覆盖份额，0~1
    category_map: dict[str, str] = field(default_factory=dict)
    # 平台原始类目 -> 统一类目编码；未列出的平台类目视为未映射

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage_note": self.coverage_note,
            "dictionary_version": self.dictionary_version,
            "dedup_method": self.dedup_method,
            "refund_handling": self.refund_handling,
            "price_basis": self.price_basis,
            "promo_basis": self.promo_basis,
            "quality_statement": self.quality_statement,
            "expected_share": self.expected_share,
            "category_map": dict(self.category_map),
        }


@dataclass(frozen=True)
class Batch:
    batch_id: str
    platform_id: str
    period: str        # 报告期，如 2026-08
    kind: BatchKind
    due_at: str        # 应报时间
    received_at: str   # 实际收到时间（同时作为批次排序依据）
    methodology: Methodology | None = None
    observations: tuple["Observation", ...] = ()
    supersedes: str | None = None  # 被本批次替代的旧批次 id
    note: str = ""


@dataclass(frozen=True)
class Observation:
    obs_id: str
    platform_category: str   # 平台原始类目名（用于映射校验）
    category: str            # 报送方声明映射到的统一类目编码
    sales_index: float       # 销售额指数
    yoy_growth: float        # 同比增速（小数，0.12 = 12%）
    promo_share: float       # 促销口径相关字段，供“促销口径”变更时解释
    period: str = ""
    platform_id: str = ""


@dataclass
class Dataset:
    categories: dict[str, Category]
    platforms: dict[str, Platform]
    batches: list[Batch]

    # ---- 基本查询 ----

    def batches_for(self, platform_id: str, period: str) -> list[Batch]:
        rows = [
            b for b in self.batches
            if b.platform_id == platform_id and b.period == period
        ]
        return sorted(rows, key=lambda b: b.received_at)

    def periods(self) -> list[str]:
        return sorted({b.period for b in self.batches})

    def previous_periods(self, period: str, limit: int = 12) -> list[str]:
        prior = sorted(p for p in self.periods() if p < period)
        return prior[-limit:]

    # ---- 有效批次解析 ----

    def effective_batches(self) -> dict[tuple[str, str], Batch]:
        """返回每个 (platform, period) 当前有效的批次；被撤回/替代的不出现。"""
        result: dict[tuple[str, str], Batch] = {}
        for period in self.periods():
            for platform_id in {b.platform_id for b in self.batches if b.period == period}:
                eff = self.effective_batch(platform_id, period)
                if eff is not None:
                    result[(platform_id, period)] = eff
        return result

    def effective_batch(self, platform_id: str, period: str) -> Batch | None:
        """按时间顺序重放该期批次，得到当前有效批次。"""
        current: Batch | None = None
        for batch in self.batches_for(platform_id, period):
            if batch.kind is BatchKind.WITHDRAWAL:
                current = None
            else:
                # scheduled/late/api_revision/backfill 均成为新的当前批次
                current = batch
        return current

    def superseded_batch_ids(self) -> set[str]:
        return {b.supersedes for b in self.batches if b.supersedes}

    def withdrawn_batch_ids(self) -> set[str]:
        out: set[str] = set()
        for platform_id in {b.platform_id for b in self.batches}:
            for period in {b.period for b in self.batches if b.platform_id == platform_id}:
                current = None
                for batch in self.batches_for(platform_id, period):
                    if batch.kind is BatchKind.WITHDRAWAL and current is not None:
                        out.add(current.batch_id)
                    if batch.kind is not BatchKind.WITHDRAWAL:
                        current = batch
        return out

    # ---- 载入 ----

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Dataset":
        categories = {
            c["code"]: Category(
                code=c["code"],
                name=c["name"],
                domain=c["domain"],
                synonyms=tuple(c.get("synonyms", [])),
            )
            for c in data.get("categories", [])
        }
        platforms = {
            p["platform_id"]: Platform(platform_id=p["platform_id"], name=p["name"])
            for p in data.get("platforms", [])
        }
        batches: list[Batch] = []
        for b in data.get("batches", []):
            is_withdrawal = BatchKind(b["kind"]) is BatchKind.WITHDRAWAL
            m = b.get("methodology")
            if is_withdrawal:
                methodology = None
                if m is not None or b.get("observations"):
                    errors_like = [f"撤回批次 {b['batch_id']} 不应携带口径或观测数据"]
                    raise ValueError("；".join(errors_like))
            else:
                if m is None:
                    raise ValueError(f"非撤回批次 {b['batch_id']} 必须随附口径 methodology")
                methodology = Methodology(
                    coverage_note=m["coverage_note"],
                    dictionary_version=m["dictionary_version"],
                    dedup_method=m["dedup_method"],
                    refund_handling=m["refund_handling"],
                    price_basis=m["price_basis"],
                    promo_basis=m["promo_basis"],
                    quality_statement=m.get("quality_statement", ""),
                    expected_share=float(m.get("expected_share", 0.0)),
                    category_map=dict(m.get("category_map", {})),
                )
            observations = tuple(
                Observation(
                    obs_id=o["obs_id"],
                    platform_category=o["platform_category"],
                    category=o["category"],
                    sales_index=float(o["sales_index"]),
                    yoy_growth=float(o["yoy_growth"]),
                    promo_share=float(o.get("promo_share", 0.0)),
                    period=b["period"],
                    platform_id=b["platform_id"],
                )
                for o in b.get("observations", [])
            )
            batches.append(
                Batch(
                    batch_id=b["batch_id"],
                    platform_id=b["platform_id"],
                    period=b["period"],
                    kind=BatchKind(b["kind"]),
                    due_at=b["due_at"],
                    received_at=b["received_at"],
                    methodology=methodology,
                    observations=observations,
                    supersedes=b.get("supersedes"),
                    note=b.get("note", ""),
                )
            )
        ds = Dataset(categories=categories, platforms=platforms, batches=batches)
        ds._validate_referential()
        return ds

    def _validate_referential(self) -> None:
        """载入期的硬约束：引用必须完整，否则数据不可用。"""
        errors = []
        batch_ids = [b.batch_id for b in self.batches]
        if len(set(batch_ids)) != len(batch_ids):
            errors.append("批次标识重复")
        for b in self.batches:
            if b.platform_id not in self.platforms:
                errors.append(f"批次 {b.batch_id} 引用未知平台 {b.platform_id}")
            if b.received_at < b.due_at and b.kind in (BatchKind.SCHEDULED,):
                # scheduled 允许提前送达？按业务约定到期报送，提前也可接受；此处不报错
                pass
            for o in b.observations:
                if o.category not in self.categories:
                    errors.append(
                        f"批次 {b.batch_id} 观测 {o.obs_id} 引用未知统一类目 {o.category}"
                    )
            if b.supersedes and b.supersedes not in batch_ids:
                errors.append(f"批次 {b.batch_id} 声明替代的批次 {b.supersedes} 不存在")
            if b.kind is BatchKind.WITHDRAWAL and b.observations:
                errors.append(f"撤回批次 {b.batch_id} 不应携带观测值")
        if errors:
            raise ValueError("；".join(errors))


def load_dataset(path: str | Path) -> Dataset:
    """从 JSON 文件载入数据集并完成引用完整性校验。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Dataset.from_dict(data)
