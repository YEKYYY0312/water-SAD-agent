"""从 JSONL 加载规范条款到 RetrievedClause 列表。"""
from __future__ import annotations

import json
from pathlib import Path

from water_agent.domain.models import RetrievedClause


def load_clauses_jsonl(path: str | Path) -> list[RetrievedClause]:
    """加载 JSONL 条款文件，每行一个 RetrievedClause 对象。"""
    with open(path, encoding="utf-8") as f:
        return [RetrievedClause(**json.loads(line)) for line in f if line.strip()]