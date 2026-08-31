"""端到端：真实 triviumdb 写入 → 真库混合检索，验证维度一致与 search_hybrid。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from water_agent.domain.models import RetrievedClause
from water_agent.ingestion.pipeline import ingest_to_trivium
from water_agent.retrieval.trivium import TriviumRetriever

MINI = [
    {"spec_id": "GB50013-2018", "article": "9.0.1", "title": "设计流量",
     "text": "给水管道设计秒流量应根据用水定额确定。", "fragment_id": "tiny-1", "source_line": 1},
    {"spec_id": "GB50014-2021", "article": "9.0.2", "title": "排水体制",
     "text": "排水体制分流制或合流制的选择应因地制宜。", "fragment_id": "tiny-2", "source_line": 2},
    {"spec_id": "GB50318-2017", "article": "9.0.3", "title": "规划",
     "text": "城市排水工程规划期限宜与城市总体规划一致。", "fragment_id": "tiny-3", "source_line": 3},
]


class StubProvider:
    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.config = type("Cfg", (), {
            "dimensions": dim, "model": "stub",
            "index_version": f"stub__{dim}__curated-v1", "fallback_enabled": False,
        })()

    def embed(self, text: str) -> list[float]:
        # 确定性伪向量，含文本哈希信号以便区分
        import hashlib
        d = hashlib.sha256(text.encode()).digest()
        return [((d[i % len(d)] / 255.0) - 0.5) for i in range(self.dim)]


def _build_and_retrieve(dim: int, query: str):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        clause = root / "clauses"
        clause.mkdir()
        by_spec = {}
        for r in MINI:
            by_spec.setdefault(r["spec_id"], []).append(r)
        for spec, rows in by_spec.items():
            (clause / f"{spec}.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
            )
        db = root / "index.db"
        provider = StubProvider(dim)
        for f in sorted(clause.glob("*.jsonl")):
            ingest_to_trivium(f, db, embedding_provider=provider, curated=True, dry_run=False)
        retriever = TriviumRetriever(db_path=db, embedding_fn=provider.embed, dim=dim)
        return retriever, retriever.retrieve(query, top_k=1)


_VALID = {"GB50013-2018 9.0.1", "GB50014-2021 9.0.2", "GB50318-2017 9.0.3"}


def test_hybrid_returns_from_real_db_512():
    retriever, hits = _build_and_retrieve(512, "给水管道设计秒流量")
    assert retriever.is_fallback is False, "应命中文档库而非 fallback"
    assert retriever.db_error is None, f"db_error={retriever.db_error}"
    assert len(hits) >= 1, "应该返回命中"
    assert hits[0].source_id in _VALID, hits[0].source_id


def test_hybrid_returns_from_real_db_256():
    retriever, hits = _build_and_retrieve(256, "排水体制分流制合流制的选择")
    assert retriever.is_fallback is False, "应命中文档库而非 fallback"
    assert retriever.db_error is None, f"db_error={retriever.db_error}"
    assert len(hits) >= 1
    assert hits[0].source_id in _VALID, hits[0].source_id
