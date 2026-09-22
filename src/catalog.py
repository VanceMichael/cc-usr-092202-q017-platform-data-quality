"""标准分类目录与平台类目映射。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

UNMAPPED_CODE = "000000"


@dataclass(frozen=True)
class Category:
    code: str
    name: str
    path: str
    platform_paths: dict[str, frozenset[str]] = field(default_factory=dict)
    note: str = ""

    def mapped_paths(self, platform_id: str) -> frozenset[str]:
        return self.platform_paths.get(platform_id, frozenset())


@dataclass
class Catalog:
    version: str
    effective_from: str
    unit: str
    categories: dict[str, Category]

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        raw = json.loads(path.read_text(encoding="utf-8"))
        categories = {}
        for item in raw["categories"]:
            platform_paths = {
                pid: frozenset(paths)
                for pid, paths in item.get("platform_codes", {}).items()
                if pid != raw.get("unmapped_code", UNMAPPED_CODE)
            }
            categories[item["code"]] = Category(
                code=item["code"],
                name=item["name"],
                path=item["path"],
                platform_paths=platform_paths,
                note=item.get("note", ""),
            )
        return cls(
            version=raw["catalog_version"],
            effective_from=raw["effective_from"],
            unit=raw.get("unit_default", "万元"),
            categories=categories,
        )

    def get(self, code: str) -> Category:
        if code not in self.categories:
            raise KeyError(f"未知标准类目编码: {code}")
        return self.categories[code]

    def covers(self, platform_id: str, code: str) -> bool:
        return bool(self.categories.get(code) and self.categories[code].mapped_paths(platform_id))
