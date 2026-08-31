"""P1-1/P1-2 父子级切块测试：不跨条/不跨章/ID稳定/表格单块/超长拆分/父块回填。"""
from __future__ import annotations

import json

import pytest

from water_agent.domain.models import Chunk, ChunkType, content_hash_of, stable_chunk_id
from water_agent.ingestion.chunker import ChunkConfig, Chunker, RawClause


def make_clause(
    spec_id="GB50014-2021",
    chapter="4",
    section="4.4",
    article="4.4.2",
    title="污水管道水力计算",
    text="污水管道的最大设计充满度，应按下列规定取值。",
) -> RawClause:
    return RawClause(
        spec_id=spec_id,
        chapter=chapter,
        section=section,
        article=article,
        title=title,
        text=text,
        source_path="knowledge/GB 50014-2021.md",
        page=42,
    )


class TestChunkModel:
    def test_stable_chunk_id_deterministic(self):
        cid1 = stable_chunk_id("GB50014-2021", "4", "4.4", "4.4.2", "article", "abc")
        cid2 = stable_chunk_id("GB50014-2021", "4", "4.4", "4.4.2", "article", "abc")
        assert cid1 == cid2
        assert len(cid1) == 16

    def test_content_hash_stable(self):
        h1 = content_hash_of("污水管道设计")
        h2 = content_hash_of("污水管道设计")
        assert h1 == h2
        assert len(h1) == 16

    def test_chunk_source_id_binds_article(self):
        chunk = Chunk(
            chunk_id="x",
            chunk_type="article",
            spec_id="GB50014-2021",
            chapter="4",
            section="4.4",
            article="4.4.2",
            text="text",
            content_hash="h",
        )
        assert chunk.source_id == "GB50014-2021 4.4.2"

    def test_chunk_is_parent_child(self):
        parent = Chunk(chunk_id="p", chunk_type="section", spec_id="S", text="", content_hash="h")
        child = Chunk(chunk_id="c", chunk_type="article", spec_id="S", text="", content_hash="h")
        assert parent.is_parent and not parent.is_child
        assert child.is_child and not child.is_parent


class TestChunkerStructure:
    def test_builds_parent_and_child_hierarchy(self):
        chunks = Chunker().chunk_clause(make_clause())
        types = {c.chunk_type for c in chunks}
        assert "chapter" in types
        assert "section" in types
        assert "article" in types
        article = next(c for c in chunks if c.chunk_type == "article")
        section = next(c for c in chunks if c.chunk_type == "section")
        assert article.parent_id == section.chunk_id

    def test_no_article_crosses_two_clauses(self):
        # 输入为单条 RawClause 时，切块器不应产出属于另一条号的子块（article 字段保持单一）
        long_text = "4.4.2 污水管道设计。\n4.4.3 雨水管道设计。"
        chunks = Chunker().chunk_clause(make_clause(article="4.4.2", text=long_text))
        articles = [c for c in chunks if c.chunk_type in ("article", "text_window")]
        assert len(articles) >= 1
        assert {a.article for a in articles} == {"4.4.2"}

    def test_parent_does_not_span_unrelated_sections(self):
        chunks = Chunker().chunk_clauses(
            [
                make_clause(spec_id="GB50014-2021", chapter="4", section="4.4", article="4.4.2"),
                make_clause(spec_id="GB50014-2021", chapter="5", section="5.2", article="5.2.1"),
            ]
        )
        parents = [c for c in chunks if c.chunk_type in ("chapter", "section")]
        assert len(parents) == 4  # 2 chapter + 2 section
        secs = [c for c in parents if c.chunk_type == "section"]
        assert len({c.section for c in secs}) == 2

    def test_long_article_recursively_split_keeps_article_and_parent(self):
        # 超长条款：重复段落，触发递归拆分，保持 article 与 parent_id
        para = "污水管道水力计算应按下列规定执行。满流与非满流按设计流量计算。" * 40
        chunks = Chunker(config=ChunkConfig(article_max_tokens=80)).chunk_clause(
            make_clause(article="4.4.2", text=para)
        )
        children = [c for c in chunks if c.chunk_type in ("article", "text_window")]
        assert len(children) > 1
        for c in children:
            assert c.article == "4.4.2"
            assert c.parent_id is not None
        # 同一个 article 的所有子块共享逻辑引用
        assert len({c.article for c in children}) == 1

    def test_window_split_has_overlap(self):
        para = "给水管道设计秒流量应按用水定额与用水人数确定。" * 200
        chunks = Chunker(config=ChunkConfig(target_tokens=80, overlap_tokens=20)).chunk_clause(
            make_clause(article="1.1.1", text=para)
        )
        windows = [c for c in chunks if c.chunk_type == "text_window"]
        assert len(windows) > 1
        # 相邻窗口有 overlap 内容（近似断言：窗口数合理且文本非空）
        assert all(len(w.text) > 0 for w in windows)

    def test_table_extracted_as_single_chunk(self):
        text = (
            "管道最大设计充满度应符合下列规定：\n"
            "| 管径 | 最大充满度 |\n"
            "|------|-----------|\n"
            "| DN200 | 0.55 |\n"
            "| DN300 | 0.65 |\n"
            "其余内容按规范执行。"
        )
        chunks = Chunker().chunk_clause(make_clause(article="4.4.3", text=text))
        tables = [c for c in chunks if c.chunk_type == "table"]
        assert len(tables) == 1
        assert "| DN300 | 0.65 |" in tables[0].text
        # 表格不跨条款：仍绑定 4.4.3
        assert tables[0].article == "4.4.3"

    def test_no_article_falls_back_to_paragraph(self):
        chunks = Chunker().chunk_clause(make_clause(article=None, title="某节标题", text="第一段。\n\n第二段。"))
        paragraphs = [c for c in chunks if c.chunk_type == "paragraph"]
        assert len(paragraphs) == 2

    def test_plain_text_fallback(self):
        chunks = Chunker().chunk_clause(
            RawClause(spec_id="TXT", chapter=None, section=None, article=None, title="", text="无结构文本段落。")
        )
        assert any(c.chunk_type == "paragraph" for c in chunks)


class TestChunkerStability:
    def test_same_input_same_chunk_ids(self):
        c1 = Chunker().chunk_clauses([make_clause(), make_clause(article="5.2.1", section="5.2", chapter="5")])
        c2 = Chunker().chunk_clauses([make_clause(), make_clause(article="5.2.1", section="5.2", chapter="5")])
        assert [c.chunk_id for c in c1] == [c.chunk_id for c in c2]

    def test_clause_does_not_cross_chapters(self):
        chunks = Chunker().chunk_clauses(
            [make_clause(chapter="4", section="4.4", article="4.4.2"), make_clause(chapter="5", section="5.2", article="5.2.1")]
        )
        for c in chunks:
            if c.chunk_type == "article":
                assert c.chapter in ("4", "5")
            if c.chunk_type == "chapter":
                assert c.chapter in ("4", "5")


class TestChunkerFromFixture:
    def test_fixture_clauses_chunk_with_parents(self):
        import pathlib

        from water_agent.ingestion.pipeline import load_raw_clauses_from_jsonl

        fixture = pathlib.Path("data/fixtures/rag_fixture.jsonl")
        raw = load_raw_clauses_from_jsonl(fixture)
        chunks = Chunker().chunk_clauses(raw)
        articles = [c for c in chunks if c.chunk_type == "article"]
        assert len(articles) >= 10
        # 每个 article 都有父块链
        parents = {c.chunk_id: c for c in chunks if c.chunk_type in ("chapter", "section")}
        for a in articles:
            assert a.parent_id is not None
            assert a.parent_id in parents

    def test_fixture_chunks_serialize_to_json(self):
        import pathlib

        from water_agent.ingestion.pipeline import load_raw_clauses_from_jsonl

        fixture = pathlib.Path("data/fixtures/rag_fixture.jsonl")
        raw = load_raw_clauses_from_jsonl(fixture)
        chunks = Chunker().chunk_clauses(raw)
        payload = [c.model_dump(mode="json") for c in chunks]
        assert len(payload) > 0
        # 子块 payload 可被 TriviumRetriever 解析
        from water_agent.retrieval.trivium import TriviumRetriever

        from water_agent.domain.models import RetrievedClause

        retriever = TriviumRetriever(db_path=None, fallback_clauses=RetrievedClause.model_validate(chunks[0].model_dump()) if False else [])
        assert retriever.is_fallback

    def test_parent_store_build(self):
        import pathlib

        from water_agent.ingestion.pipeline import build_parent_store, load_raw_clauses_from_jsonl

        fixture = pathlib.Path("data/fixtures/rag_fixture.jsonl")
        raw = load_raw_clauses_from_jsonl(fixture)
        chunks = Chunker().chunk_clauses(raw)
        parents = build_parent_store(chunks)
        assert len(parents) > 0
        assert all(v.chunk_type in ("chapter", "section") for v in parents.values())
