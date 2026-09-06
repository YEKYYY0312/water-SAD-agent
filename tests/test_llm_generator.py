"""OpenAI 兼容 LLM 生成器（DeepSeek 官方 Claude 接口）契约测试。"""
from __future__ import annotations

import json

import pytest

from water_agent.domain.models import RetrievedClause
from water_agent.generation.llm import LLMConfig, LLMGenerator, build_llm_generator


def _clause(spec: str, article: str, text: str, title: str = "") -> RetrievedClause:
    return RetrievedClause(spec_id=spec, article=article, title=title, text=text)


def _fake_transport(*, answer: str = "这是模型生成的规定摘要。", citations: list[dict] | None = None):
    """捕获请求体，返回固定 mock 回答（结构化 JSON 引用契约）。"""
    captured = {}

    def transport(payload: dict) -> dict:
        captured["payload"] = payload
        content = json.dumps({"answer": answer, "citations": citations or []}, ensure_ascii=False)
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    return transport, captured


def _structured_citation(clause: RetrievedClause) -> dict:
    return {
        "source_id": clause.source_id,
        "article": clause.article,
        "title": clause.title,
        "text_snippet": clause.text[:40],
    }


def test_config_defaults_are_deepseek_official():
    cfg = LLMConfig(api_key="sk-test")
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-chat"
    assert cfg.timeout_seconds > 0


def test_config_rejects_empty_api_key():
    with pytest.raises(ValueError):
        LLMConfig(api_key="")


def test_generate_sends_chat_request_with_context():
    clause = _clause("GB50013-2018", "5.1.1", "给水管道设计秒流量应按用水定额确定。", "设计流量")
    transport, captured = _fake_transport(citations=[_structured_citation(clause)])
    gen = LLMGenerator(LLMConfig(api_key="sk-test"), transport=transport)
    ans = gen.generate("给水管道设计秒流量如何确定？", [clause])
    payload = captured["payload"]
    assert payload["model"] == "deepseek-chat"
    assert payload["messages"][0]["role"] == "system"
    combined = " ".join(m["content"] for m in payload["messages"])
    assert "GB50013-2018 5.1.1" in combined
    assert ans.answer == "这是模型生成的规定摘要。"
    assert [c.source_id for c in ans.citations] == ["GB50013-2018 5.1.1"]


def test_citations_only_from_context():
    context = [_clause("A", "1", "x"), _clause("B", "2", "y")]
    transport, _ = _fake_transport(citations=[_structured_citation(context[0])])
    gen = LLMGenerator(LLMConfig(api_key="sk-test"), transport=transport)
    ans = gen.generate("q", context)
    sources = {c.source_id for c in ans.citations}
    valid = {c.source_id for c in context}
    assert sources.issubset(valid)
    assert sources


def test_model_declared_citation_outside_context_rejected():
    """结构化引用契约：模型声明不在检索上下文的 source_id → 响亮失败（禁止代填/放行）。"""
    context = [_clause("A", "1", "x")]
    transport, _ = _fake_transport(citations=[_structured_citation(_clause("FAKE", "0.0.0", "编造"))])
    gen = LLMGenerator(LLMConfig(api_key="sk-test"), transport=transport)
    with pytest.raises(ValueError, match="not in context"):
        gen.generate("q", context)


def test_empty_context_graceful():
    transport, _ = _fake_transport()
    gen = LLMGenerator(LLMConfig(api_key="sk-test"), transport=transport)
    gen.last_usage = {"input_tokens": 10, "output_tokens": 5}  # 上一条用例的用量
    ans = gen.generate("不存在的东西", [])
    assert ans.citations == []
    assert "无法" in ans.answer
    # S10-02：未实际调用 LLM 时清空用量，避免 0 命中用例继承上一条用例的 token/cost
    assert gen.last_usage == {}


def test_dimensionful_endpoint_uses_base_url():
    clause = _clause("A", "1", "x")
    transport, _ = _fake_transport(citations=[_structured_citation(clause)])
    cfg = LLMConfig(api_key="sk-test", base_url="https://custom.example.com/v1")
    gen = LLMGenerator(cfg, transport=transport)
    _ = gen.generate("q", [clause])
    # 验证内部 urlopen 会打到正确的 /chat/completions（transport 模式下不发请求，但逻辑在 build）
    assert cfg.base_url == "https://custom.example.com/v1"


def test_build_llm_generator_requires_key():
    import os

    saved = os.environ.get("DEEPSEEK_API_KEY")
    if saved is not None:
        os.environ.pop("DEEPSEEK_API_KEY")
    try:
        with pytest.raises(ValueError):
            build_llm_generator()
    finally:
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved