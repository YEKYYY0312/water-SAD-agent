"""Code Review 修复的针对性回归测试。

覆盖：
1. 真实索引失败必须抛出结构化错误（含 source_file / chunk_id / operation / 原始异常）
2. 二次入库幂等，检索无重复
3. 父块持久化 <db_path>.parents.json，新 Retriever 跨进程自动加载并回填
4. 真实库检索失败必须抛 TriviumRetrievalError，不得静默回退 fixture
5. 标题型无条号文本必须有稳定父块，子块挂 parent_id，且不跨无关标题
6. 纯表格条款只生成 table chunk，无空 article，引用仍指向原 article
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from water_agent.ingestion.chunker import Chunker, RawClause
from water_agent.ingestion.pipeline import (
    ingest_jsonl_to_chunks,
    ingest_to_trivium,
    load_parent_store,
)
from water_agent.retrieval.trivium import TriviumRetrievalError, TriviumRetriever, mock_embed

FIXTURE = Path("data/fixtures/rag_fixture.jsonl")


class TestIngestFailureReporting:
    def test_insert_failure_raises_structured_error(self, monkeypatch):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fail.db"

            def bad_embed(text: str) -> list[float]:
                raise RuntimeError("embed boom")

            with pytest.raises(RuntimeError) as excinfo:
                ingest_to_trivium(FIXTURE, db_path, embedding_fn=bad_embed)
            message = str(excinfo.value)
            payload = json.loads(message)
            assert payload["source_file"].endswith("rag_fixture.jsonl")
            assert payload["issues"], "must contain per-chunk issues"
            issue = payload["issues"][0]
            assert issue["operation"] in {"insert", "embed"}
            assert issue["chunk_id"]
            assert "embed boom" in issue["error"]
            assert payload["stats"]["inserted"] == 0
            assert payload["stats"]["failed"] > 0
            assert payload["stats"]["failed"] == len(payload["issues"])

    def test_index_text_failure_is_reported(self, monkeypatch):
        import triviumdb

        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "idx.db"

            real_open = triviumdb.TriviumDB

            class FlakyIndexDB:
                def __init__(self, path: str, dim: int = 1536, load_text_index: bool = False):
                    self._inner = real_open(path, dim=dim, load_text_index=load_text_index)
                    self._indexed: set[int] = set()

                def insert_with_id(self, node_id, vector, payload):
                    self._inner.insert_with_id(node_id, vector, payload)

                def upsert_with_id(self, node_id, vector, payload):
                    self._inner.upsert_with_id(node_id, vector, payload)

                def index_text(self, node_id, text):
                    if node_id not in self._indexed:
                        self._indexed.add(node_id)
                        raise RuntimeError("text index boom")
                    self._inner.index_text(node_id, text)

                def all_node_ids(self):
                    return self._inner.all_node_ids()

                def build_text_index(self):
                    self._inner.build_text_index()

                def flush(self):
                    self._inner.flush()

                def close(self):
                    self._inner.close()

            monkeypatch.setattr(triviumdb, "TriviumDB", FlakyIndexDB)
            with pytest.raises(RuntimeError) as excinfo:
                ingest_to_trivium(FIXTURE, db_path)
            payload = json.loads(str(excinfo.value))
            assert payload["stats"]["failed"] == len(payload["issues"])
            assert any(issue["operation"] in {"index_text", "upsert", "insert"} for issue in payload["issues"])
            assert any("text index boom" in issue["error"] for issue in payload["issues"])
            # 失败条目不得计入 inserted
            assert payload["stats"]["inserted"] == 0


class TestIngestIdempotent:
    def test_double_ingest_no_duplicate_hits(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "idem.db"
            first = ingest_to_trivium(FIXTURE, db_path)
            second = ingest_to_trivium(FIXTURE, db_path)
            assert first["inserted"] > 0
            assert second["inserted"] == 0
            assert second["skipped_existing"] == first["inserted"]
            assert second["updated"] == 0
            retriever = TriviumRetriever(db_path=db_path)
            hits = retriever.retrieve("污水管道最大充满度", top_k=5)
            source_ids = [h.source_id for h in hits]
            assert len(source_ids) == len(set(source_ids)), "search results must not duplicate"
            retriever.close()

    def test_update_path_counts_updated(self):
        # upsert 分支：手动预置同 chunk_id 节点后，再次入库应走 update
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "upd.db"
            chunks, _ = ingest_jsonl_to_chunks(FIXTURE)
            child = next(c for c in chunks if not c.is_parent)
            import triviumdb

            db = triviumdb.TriviumDB(str(db_path))
            db.insert_with_id(
                int(child.chunk_id, 16),
                mock_embed(child.text),
                payload=child.model_dump(mode="json"),
            )
            db.close()
            stats = ingest_to_trivium(FIXTURE, db_path)
            assert stats["updated"] == 1
            assert stats["failed"] == 0


class TestParentPersistence:
    def test_parents_file_created_and_loadable(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pp.db"
            ingest_to_trivium(FIXTURE, db_path)
            parents_path = Path(str(db_path) + ".parents.json")
            assert parents_path.exists()
            data = json.loads(parents_path.read_text(encoding="utf-8"))
            assert data
            loaded = load_parent_store(db_path)
            assert len(loaded) == len(data)
            assert all(c.chunk_type in ("chapter", "section", "subsection") for c in loaded.values())

    def test_new_retriever_cross_process_enriches_context(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cross.db"
            ingest_to_trivium(FIXTURE, db_path)
            # 不传 parent_store，模拟新进程
            retriever = TriviumRetriever(db_path=db_path)
            assert retriever.parent_store
            hits = retriever.retrieve("污水管道最大充满度", top_k=3)
            assert hits
            assert any(h.text.startswith("【") for h in hits)
            # citation 仍绑定 child article
            assert all(h.source_id.startswith("FIX ") and h.article for h in hits)
            retriever.close()

    def test_missing_parents_file_still_retrieves_and_observable(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "orphan.db"
            ingest_to_trivium(FIXTURE, db_path)
            Path(str(db_path) + ".parents.json").unlink()
            retriever = TriviumRetriever(db_path=db_path)
            assert retriever.parent_store == {}
            hits = retriever.retrieve("给水管道设计秒流量", top_k=3)
            assert hits  # 检索仍可用
            assert not any(h.text.startswith("【") for h in hits)
            retriever.close()


class TestRealDbFailureNoSilentFallback:
    def test_search_hybrid_failure_raises_not_fixture(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "boom.db"
            ingest_to_trivium(FIXTURE, db_path)
            retriever = TriviumRetriever(db_path=db_path)
            assert not retriever.is_fallback

            class ExplodingDB:
                def search_hybrid(self, *args, **kwargs):
                    raise RuntimeError("hybrid exploded")

                def close(self):
                    pass

            retriever._db = ExplodingDB()  # type: ignore[assignment]
            with pytest.raises(TriviumRetrievalError) as excinfo:
                retriever.retrieve("给水管道设计秒流量")
            assert "hybrid exploded" in str(excinfo.value)
            # 不得返回 fixture 结果
            assert not excinfo.value.args[0].startswith("fixture")
            retriever.close()

    def test_payload_parse_failure_raises(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "badpay.db"
            ingest_to_trivium(FIXTURE, db_path)
            retriever = TriviumRetriever(db_path=db_path)

            class BadPayloadDB:
                def search_hybrid(self, *args, **kwargs):
                    class Hit:
                        def __init__(self):
                            self.score = 0.5
                            self.payload = {"chunk_type": "article", "bogus_field_only": True}

                    return [Hit()]

                def close(self):
                    pass

            retriever._db = BadPayloadDB()  # type: ignore[assignment]
            with pytest.raises(TriviumRetrievalError):
                retriever.retrieve("给水管道设计秒流量")
            retriever.close()

    def test_empty_hits_returns_empty_not_fixture(self):
        """真实库 0 命中是合法结果：返回空列表，不回落 fixture、也不当链路故障抛错。"""
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "empty.db"
            ingest_to_trivium(FIXTURE, db_path)
            retriever = TriviumRetriever(db_path=db_path)

            class EmptyDB:
                def search_hybrid(self, *args, **kwargs):
                    return []

                def close(self):
                    pass

            retriever._db = EmptyDB()  # type: ignore[assignment]
            assert retriever.retrieve("不存在的内容") == []
            assert retriever.is_fallback is False
            retriever.close()

    def test_text_index_not_ready_raises(self):
        """文本索引重建失败必须可观测：retrieve 拒绝服务而不是退化成纯向量。"""
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "noidx.db"
            ingest_to_trivium(FIXTURE, db_path)
            retriever = TriviumRetriever(db_path=db_path)
            assert retriever.text_index_ready is True
            retriever._text_index_ready = False  # type: ignore[attr-defined]
            retriever._db_error = "simulated"  # type: ignore[attr-defined]
            with pytest.raises(TriviumRetrievalError, match="text index not ready"):
                retriever.retrieve("给水管道设计秒流量")
            retriever.close()


class TestTitleParentWithoutArticle:
    def test_title_parent_created_and_children_linked(self):
        clause = RawClause(
            spec_id="TXT-1",
            chapter=None,
            section=None,
            article=None,
            title="第三章 施工组织设计",
            text="第一段内容。\n\n第二段内容。",
        )
        chunks = Chunker().chunk_clause(clause)
        parents = [c for c in chunks if c.chunk_type == "section"]
        assert len(parents) == 1
        parent = parents[0]
        children = [c for c in chunks if c.chunk_type in ("paragraph", "text_window")]
        assert children
        for child in children:
            assert child.parent_id == parent.chunk_id

    def test_title_parent_display_is_human_readable(self):
        clause = RawClause(
            spec_id="TXT-1",
            chapter=None,
            section=None,
            article=None,
            title="第三章 施工组织设计",
            text="第一段内容。",
        )
        chunks = Chunker().chunk_clause(clause)
        parent = next(c for c in chunks if c.chunk_type == "section")
        from water_agent.retrieval.trivium import TriviumRetriever

        retriever = TriviumRetriever(db_path=None, fallback_clauses=[], parent_store={parent.chunk_id: parent})
        clause_out = retriever._enrich_with_parent(
            type("X", (), {"text": "正文", "chapter": None, "section": None, "score": None})(),
            {"parent_id": parent.chunk_id},
        )
        label = clause_out.text
        assert label.startswith("【标题块 / 施工组织设计 /")
        assert "f6a9ce82" in label

    def test_title_parent_stable_and_does_not_cross_unrelated_titles(self):
        c1 = RawClause(spec_id="TXT-1", chapter=None, section=None, article=None, title="标题A", text="内容A")
        c2 = RawClause(spec_id="TXT-1", chapter=None, section=None, article=None, title="标题B", text="内容B")
        chunks = Chunker().chunk_clauses([c1, c2])
        parents = [c for c in chunks if c.chunk_type == "section"]
        assert len(parents) == 2
        # 相同输入 chunk_id 稳定
        again = Chunker().chunk_clauses([c1, c2])
        assert [c.chunk_id for c in again] == [c.chunk_id for c in chunks]

    def test_same_title_different_body_gets_distinct_parents(self):
        c1 = RawClause(spec_id="TXT-1", chapter=None, section=None, article=None, title="同一标题", text="正文一")
        c2 = RawClause(spec_id="TXT-1", chapter=None, section=None, article=None, title="同一标题", text="完全不同的正文二")
        chunks = Chunker().chunk_clauses([c1, c2])
        parents = [c for c in chunks if c.chunk_type == "section"]
        assert len(parents) == 2
        for p in parents:
            children = [c for c in chunks if c.parent_id == p.chunk_id and not c.is_parent]
            assert children, "each parent must own its own children"
            texts = " ".join(c.text for c in children)
            # 不得跨标题混入另一条正文
            if p.title == "同一标题":
                assert ("正文一" in texts) != ("正文二" in texts)


class TestPureTableClause:
    def test_table_only_clause_has_no_empty_article(self):
        table_text = (
            "| 管径 | 最大设计充满度 |\n"
            "|------|----------------|\n"
            "| DN150 | 0.55 |\n"
            "| DN200 | 0.65 |\n"
        )
        clause = RawClause(
            spec_id="GB50014-2021",
            chapter="4",
            section="4.4",
            article="4.4.4",
            title="表4.4.4 最大设计充满度",
            text=table_text,
        )
        chunks = Chunker().chunk_clause(clause)
        types = [c.chunk_type for c in chunks]
        assert "table" in types
        assert "article" not in types
        tables = [c for c in chunks if c.chunk_type == "table"]
        assert len(tables) == 1
        # 表格保留原始 Markdown 结构
        assert "| DN150 | 0.55 |" in tables[0].text
        assert tables[0].text.startswith("|")
        # 引用仍指向原 article
        assert tables[0].article == "4.4.4"
        assert tables[0].source_id == "GB50014-2021 4.4.4"

    def test_payload_conversion_skips_empty_child_text(self):
        from water_agent.ingestion.pipeline import chunks_to_trivium_payloads

        chunks, _ = ingest_jsonl_to_chunks(FIXTURE)
        payloads = chunks_to_trivium_payloads(chunks)
        assert all(p["text"].strip() for p in payloads)
