"""P1 ingestion / TriviumDB 回归测试。"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from water_agent.ingestion.pipeline import ingest_jsonl_to_chunks, ingest_to_trivium, load_parent_store
from water_agent.retrieval.trivium import TriviumRetriever, mock_embed


FIXTURE = Path("data/fixtures/rag_fixture.jsonl")


def test_resume_skips_embed_for_manifest_chunks():
    calls = {"n": 0}

    def counting_embed(text: str) -> list[float]:
        calls["n"] += 1
        return mock_embed(text)

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p1.db"
        first = ingest_to_trivium(FIXTURE, db_path, embedding_fn=counting_embed)
        assert first["inserted"] == 10
        first_calls = calls["n"]
        assert first_calls == 10
        second = ingest_to_trivium(FIXTURE, db_path, embedding_fn=counting_embed)
        assert second["skipped_existing"] == 10
        assert second["inserted"] == 0
        assert calls["n"] == first_calls


def test_ingest_is_idempotent_and_persists_parents():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p1.db"
        # first ingest
        stats1 = ingest_to_trivium(FIXTURE, db_path)
        assert stats1["inserted"] == 10
        assert stats1["failed"] == 0
        parents_path = Path(str(db_path) + ".parents.json")
        assert parents_path.exists()
        parent_store = load_parent_store(db_path)
        assert parent_store
        # second ingest should not duplicate
        stats2 = ingest_to_trivium(FIXTURE, db_path)
        assert stats2["inserted"] == 0
        assert stats2["skipped_existing"] == 10
        assert stats2["failed"] == 0
        # new retriever should load parents from disk automatically
        retriever = TriviumRetriever(db_path=db_path)
        hits = retriever.retrieve("污水管道最大充满度", top_k=3)
        assert hits
        assert any(h.text.startswith("【") for h in hits)
        assert all(h.source_id.startswith("FIX ") for h in hits)
        retriever.close()


def test_retrieve_real_db_failure_does_not_fallback_to_fixture(monkeypatch):
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p1.db"
        stats = ingest_to_trivium(FIXTURE, db_path)
        assert stats["inserted"] == 10
        retriever = TriviumRetriever(db_path=db_path)
        assert not retriever.is_fallback
        # monkeypatch search_hybrid to explode
        class DummyDB:
            def search_hybrid(self, *args, **kwargs):
                raise RuntimeError("boom")

            def close(self):
                pass

        retriever._db = DummyDB()  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError):
            retriever.retrieve("污水管道最大充满度")
        retriever.close()


def test_plain_fixture_mode_still_allowed_when_db_missing():
    retriever = TriviumRetriever(db_path=None)
    assert retriever.is_fallback
    hits = retriever.retrieve("给水管道设计问题")
    assert hits
