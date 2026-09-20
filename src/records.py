"""读取并检查共享领域资料。"""

import json
from pathlib import Path

def load_records(path: Path) -> dict:
    """返回结构完整的领域资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {"domain", "version", "sample_id", "actors", "facts", "constraints", "records"}
    if not required.issubset(value):
        raise ValueError("领域资料缺少必要字段")
    if value["version"] < 1 or len(value["actors"]) < 2 or len(value["records"]) < 2:
        raise ValueError("领域资料内容不完整")
    if len({record["id"] for record in value["records"]}) != len(value["records"]):
        raise ValueError("领域记录标识重复")
    return value
