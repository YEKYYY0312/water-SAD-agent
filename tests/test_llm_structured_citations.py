"""LLMGenerator 结构化引用契约（2026-09-04 新增，对应 code review 问题 1）。

覆盖：LLM 必须输出结构化 JSON（answer + citations）→ 生成器校验每条引用 ∈ 检索上下文
且 article 与 source_id 一致 → 产出可追溯引用；编造/JSON 损坏/条号不一致 → 响亮失败
（不得静默用上下文前 N 条代填）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from water_agent.generation.llm import (
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    LLMConfig,
    LLMGenerator,
)
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


def _generator(transport) -> LLMGenerator:
    return LLMGenerator(
        LLMConfig(
            api_key="test-key",
            model=DEFAULT_MODEL,
            system_prompt=DEFAULT_SYSTEM_PROMPT,
        ),
        transport=transport,
    )


def _context():
    return InMemoryRetriever(load_clauses_jsonl(FIXTURE_PATH)).retrieve("给水管道的设计秒流量", top_k=3)


def _transport_returning(payload: dict):
    def transport(request_payload: dict) -> dict:
        return {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    return transport


class TestStructuredCitationParsing:
    def test_valid_json_citations_from_context(self) -> None:
        context = _context()
        first = context[0]
        gen = _generator(
            _transport_returning(
                {
                    "answer": "根据检索结果回答。",
                    "citations": [
                        {
                            "source_id": first.source_id,
                            "article": first.article,
                            "title": first.title,
                            "text_snippet": first.text[:40],
                        }
                    ],
                }
            )
        )
        result = gen.generate("给水管道的设计秒流量", context)
        assert result.answer == "根据检索结果回答。"
        assert len(result.citations) == 1
        assert result.citations[0].source_id == first.source_id
        assert result.citations[0].article == first.article
        assert result.citations[0].title == first.title

    def test_invalid_citation_not_in_context_raises(self) -> None:
        context = _context()
        gen = _generator(
            _transport_returning(
                {
                    "answer": "回答。",
                    "citations": [
                        {
                            "source_id": "FAKE 0.0.0",
                            "article": "0.0.0",
                            "title": "",
                            "text_snippet": "",
                        }
                    ],
                }
            )
        )
        with pytest.raises(ValueError, match="not in context"):
            gen.generate("给水管道的设计秒流量", context)

    def test_article_mismatch_raises(self) -> None:
        context = _context()
        first = context[0]
        gen = _generator(
            _transport_returning(
                {
                    "answer": "回答。",
                    "citations": [
                        {
                            "source_id": first.source_id,
                            "article": "9.9.9",
                            "title": "",
                            "text_snippet": "",
                        }
                    ],
                }
            )
        )
        with pytest.raises(ValueError, match="article mismatch"):
            gen.generate("给水管道的设计秒流量", context)

    def test_malformed_json_raises(self) -> None:
        def transport(request_payload: dict) -> dict:
            return {"choices": [{"message": {"content": "不是 JSON"}}]}

        gen = _generator(transport)
        with pytest.raises(ValueError, match="not valid JSON"):
            gen.generate("给水管道的设计秒流量", _context())

    def test_empty_context_returns_no_citations(self) -> None:
        gen = _generator(lambda payload: {"choices": []})
        result = gen.generate("xxx", [])
        assert result.citations == []
        assert "未检索到" in result.answer


class TestStructuredCitationRequest:
    def test_request_requires_json_object(self) -> None:
        gen = _generator(lambda payload: {"choices": [{"message": {"content": "{}"}}]})
        payload = gen._chat_request("q", _context())
        assert payload is not None
        assert payload["response_format"] == {"type": "json_object"}
        assert any("JSON" in m["content"] for m in payload["messages"] if m["role"] == "system")
        assert any("source_id" in m["content"] for m in payload["messages"] if m["role"] == "system")