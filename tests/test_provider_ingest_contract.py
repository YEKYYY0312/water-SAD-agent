"""真实 curated 索引必须显式声明 Embedding Provider 和 index version。"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from water_agent.embedding import BailianEmbeddingConfig, BailianEmbeddingProvider
from water_agent.ingestion.pipeline import ingest_to_trivium

VALID = {
    "spec_id": "GB50318-2017",
    "chapter": "1",
    "section": "1.1",
    "article": "1.1.1",
    "title": "测试条款",
    "text": "这是一个用于测试真实 provider 入库契约的规范条款正文。",
}


def _write(path: Path) -> None:
    path.write_text(json.dumps(VALID, ensure_ascii=False) + "\n", encoding="utf-8")


def test_curated_ingest_requires_explicit_provider(monkeypatch):
    with TemporaryDirectory() as tmp:
        source = Path(tmp) / "clauses.jsonl"
        _write(source)
        with pytest.raises(ValueError, match="explicit embedding provider"):
            ingest_to_trivium(source, Path(tmp) / "index.db", curated=True)


def test_curated_ingest_records_provider_metadata(monkeypatch):
    with TemporaryDirectory() as tmp:
        source = Path(tmp) / "clauses.jsonl"
        _write(source)
        config = BailianEmbeddingConfig(api_key="secret")
        provider = BailianEmbeddingProvider(config, transport=lambda _: {"data": [{"embedding": [0.1] * 512}]})
        monkeypatch.setattr("triviumdb.TriviumDB", lambda _: None)
        # 由实现提供 dry_run，验证 provider 元数据，不创建 DB 或发起真实网络请求。
        result = ingest_to_trivium(source, Path(tmp) / "index.db", embedding_provider=provider, dry_run=True, curated=True)
        assert result["embedding"]["model"] == "qwen3.7-text-embedding"
        assert result["embedding"]["dimensions"] == 512
        assert result["embedding"]["index_version"] == "qwen3.7-text-embedding__512__curated-v1"
        assert not (Path(tmp) / "index.db").exists()
