"""平台与账号目录（样例中不含真实个人信息与凭据）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Account:
    username: str
    role: str
    display_name: str
    platform_id: str | None = None


@dataclass
class Directory:
    platforms: dict[str, dict]
    accounts: dict[str, Account]

    @classmethod
    def load(cls, path: Path) -> "Directory":
        raw = json.loads(path.read_text(encoding="utf-8"))
        platforms = {item["platform_id"]: item for item in raw["platforms"]}
        accounts = {
            item["username"]: Account(
                username=item["username"],
                role=item["role"],
                display_name=item["display_name"],
                platform_id=item.get("platform_id"),
            )
            for item in raw["accounts"]
        }
        return cls(platforms=platforms, accounts=accounts)

    def account(self, username: str) -> Account:
        if username not in self.accounts:
            raise KeyError(f"账号不存在: {username}")
        return self.accounts[username]

    def covered_categories(self, platform_id: str) -> list[str]:
        return self.platforms[platform_id].get("covered_categories", [])
