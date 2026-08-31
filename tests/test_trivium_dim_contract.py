"""回归测试：ingest_to_trivium 必须以 provider 的 dimensions 打开 TriviumDB。

防止再次出现「默认 1536 维库 + 512 维向量 → 写入全失败 → Recall 全 0」。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from water_agent.ingestion.pipeline import ingest_to_trivium

MINI = [
    {"spec_id": "GB50013-2018", "article": "9.0.1", "title": "设计流量", "text": "给水管道设计秒流量应根据用水定额确定。", "fragment_id": "tiny-1", "source_line": 1},
]


class StubProvider:
    """返回 (dimensions,) 维确定性向量，不调云端。"""

    def __init__(self, dimensions: int) -> None:
        self.config = type("Cfg", (), {
            "dimensions": dimensions,
            "model": "stub",
            "index_version": f"stub__{dimensions}__curated-v1",
            "fallback_enabled": False,
        })()

    def embed(self, text: str) -> list[float]:
        return [0.1] * self.config.dimensions


def _run(dim: int):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        clause = root / "clauses"
        clause.mkdir()
        (clause / "GB50013-2018.jsonl").write_text(
            json.dumps(MINI[0], ensure_ascii=False) + "\n", encoding="utf-8"
        )
        db = root / "index.db"
        provider = StubProvider(dim)
        stats = ingest_to_trivium(
            clause / "GB50013-2018.jsonl", db,
            embedding_provider=provider, curated=True, dry_run=False,
        )
        return db, stats


def test_insert_succeeds_with_512_dim():
    _db, stats = _run(512)
    assert stats["inserted"] == 1, f"inserted=0 → 维度不匹配回归，stats={stats}"
    assert stats["failed"] == 0


def test_insert_succeeds_with_256_dim():
    _db, stats = _run(256)
    assert stats["inserted"] == 1, f"inserted=0 → 维度不匹配回归，stats={stats}"
    assert stats["failed"] == 0
