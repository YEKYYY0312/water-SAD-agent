"""P7-S12 回答纪律 R1–R5（17 号 SDD §5.6，SDD17-I11）：经生产 ``AgenticSession`` + 生产 ``web_search`` 工具 + 进程内假搜索服务。

R1 网络内容不能当引用；R2 终答网址可追溯；R3 只有网络证据的规范性回答不能 succeeded；R4 披露行由程序追加；R5 污点效应。
另：勾选了联网却没开放（部署没就绪 / 最紧组合让位）时必须告警 ``WEB_SEARCH_NOT_EXPOSED``。
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from tests.fixtures.fake_search_server import FakeSearchServer
from water_agent.external.taint import DISCLOSURE_LINE
from water_agent.harness.doubles import ScriptedChatModel
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.trivium import TriviumRetriever
from water_agent.runtime.agentic import AgenticSession
from water_agent.runtime.governance import binding
from water_agent.runtime.governance.gateway import ProviderGateway
from water_agent.session import default_checkpoint_serde

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
SEARCH = {"tool_calls": [{"name": "web_search", "args": {"query": "给水管道经济流速"}}]}
GENUINE = "可参考 https://example.com/water/velocity 的资料，具体取值以设计院内部规定为准。"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    for name in ("WATER_AGENT_WEB_SEARCH", "WATER_AGENT_WEB_SEARCH_PROVIDER", "WATER_AGENT_WEB_SEARCH_BASE_URL",
                 "WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE", "WATER_AGENT_WEB_SEARCH_API_KEY", "WATER_AGENT_SKILLS",
                 "WATER_AGENT_TOOL_EXPOSURE", "DRAWING_ROOT"):
        monkeypatch.delenv(name, raising=False)
    previous = binding._default_gateway
    binding.set_default_gateway(ProviderGateway())  # 熔断 / 限流是进程级共享的：每个用例一份干净的
    try:
        yield
    finally:
        binding.set_default_gateway(previous)


def _deploy(monkeypatch: pytest.MonkeyPatch, server: FakeSearchServer) -> None:
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH", "on")
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH_BASE_URL", server.base_url)
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE", "1")


def _ask(tmp_path: Path, steps: list[dict[str, Any]], query: str, *, drawing_root: str = "") -> tuple[dict[str, Any], Any, ScriptedChatModel]:
    args = argparse.Namespace(
        db_path=None, dimensions=None, rerank="local", rerank_model=None, rerank_base_url=None, rerank_api_key=None,
        top_k=5, recall_top_k=20, hybrid_alpha=0.7, model="deepseek-chat", base_url=None, api_key_env="DEEPSEEK_API_KEY",
        api_key=None, mock_generator=True, session_dir=str(tmp_path / "s"), trace_dir=str(tmp_path / "t"),
        stream=False, drawing_root=drawing_root,
    )
    model = ScriptedChatModel(steps=steps, id_prefix="disc")
    with ExitStack() as stack:
        session = AgenticSession(
            args, stack=stack, thread_id="disc", checkpointer=InMemorySaver(serde=default_checkpoint_serde()),
            model=model, retriever=TriviumRetriever(db_path=None, fallback_clauses=load_clauses_jsonl(FIXTURE)),
            mcp_enabled=False, web_search=True,
        )
        payload = asyncio.run(session.aask(query, run_id="r1"))
    assert model.state["calls"] == len(steps), "脚本必须恰好用完"
    return payload, session, model


def _state(session: Any) -> dict[str, Any]:
    return dict(getattr(session.app.get_state(session.config), "values", {}) or {})


def test_r1_web_urls_never_become_citations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A07 / SDD17-I11 R1：联网网址当 ``cite_clauses`` 的条款号——工具层先被参数护栏拒绝，引用校验也不成立；
    ``web_refs`` 永不进本 thread 的可引用集合。"""

    with FakeSearchServer("ok") as server:
        _deploy(monkeypatch, server)
        payload, session, _ = _ask(
            tmp_path,
            [SEARCH, {"tool_calls": [{"name": "cite_clauses", "args": {"article_ids": ["https://example.com/water/velocity x"]}}]},
             {"content": "没有可引用的规范条文，我只给出参考资料。"}],
            "给水管道流速依据是什么",
        )
    assert payload["citations"] == []
    sources = session._thread_retrieved_source_ids()
    assert not any(str(item).startswith(("http://", "https://")) for item in sources)
    assert "https://example.com/water/velocity" in (_state(session).get("web_refs") or [])


def test_r2_fabricated_url_reflows_once_then_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A07 / SDD17-I11 R2：终答里出现不在 ``web_refs`` 的网址 → 回流一次，再出现即 ``failed``。"""

    with FakeSearchServer("ok") as server:
        _deploy(monkeypatch, server)
        payload, _, _ = _ask(
            tmp_path,
            [SEARCH, {"content": "参见 https://made-up.example/spec 。"}, {"content": "仍然参见 https://made-up.example/spec 。"}],
            "查一下给水管道经济流速",
        )
    assert payload["status"] == "failed"


def test_r2_genuine_url_passes_without_a_reflow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A07 / SDD17-I11 R2：终答只引用工具返回过的网址 → 一次通过（脚本两步恰好用完即证明没有回流）。"""

    with FakeSearchServer("ok") as server:
        _deploy(monkeypatch, server)
        payload, _, _ = _ask(tmp_path, [SEARCH, {"content": GENUINE}], "查一下给水管道经济流速")
    assert payload["status"] == "succeeded"


def test_r3_a_normative_answer_backed_only_by_the_web_cannot_succeed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A07 R3：只有网络证据、含规范性结论（"不得大于 2.0 m/s"）的回答——零引用豁免不因搜索放宽，沿用空引用回流一次
    再失败；``web_refs`` 不是 ``source_id``。"""

    normative = {"content": "根据网页资料，给水管道流速不得大于 2.0 m/s。https://blog.example/spec"}
    with FakeSearchServer("spec_impersonation") as server:
        _deploy(monkeypatch, server)
        payload, _, _ = _ask(tmp_path, [SEARCH, normative, normative], "给水管道流速上限是多少")
    assert payload["status"] == "failed"
    assert payload["citations"] == []


def test_r4_disclosure_line_is_appended_by_the_program_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A07 R4：用过联网内容 → 终答末尾由程序追加披露行；会话里的模型消息不含它（下一轮模型看到的历史不变）。"""

    with FakeSearchServer("ok") as server:
        _deploy(monkeypatch, server)
        payload, session, _ = _ask(tmp_path, [SEARCH, {"content": GENUINE}], "查一下给水管道经济流速")
    assert payload["answer"].endswith(DISCLOSURE_LINE)
    ai_texts = [str(message.content) for message in _state(session)["messages"] if getattr(message, "type", "") == "ai"]
    assert not any(DISCLOSURE_LINE in text for text in ai_texts)


def test_r5_taint_refuses_the_answer_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A07 R5：联网回合 ``web_tainted`` 为真，答案缓存写入资格恒拒绝（``web_content_used``）。"""

    from water_agent.runtime.governance.cache import (
        REASON_WEB_CONTENT_USED,
        evaluate_store_eligibility,
    )
    from water_agent.runtime.governance.contracts import CallCategory

    with FakeSearchServer("ok") as server:
        _deploy(monkeypatch, server)
        payload, session, _ = _ask(tmp_path, [SEARCH, {"content": GENUINE}], "查一下给水管道经济流速")
    state = _state(session)
    assert state.get("web_tainted") is True
    eligibility = evaluate_store_eligibility(
        category=CallCategory.RAG_GENERATION, temperature=0.0, citation_verified=True, output_guardrails_passed=True,
        spec_versions_known=True, answer=payload["answer"], web_tainted=True,
    )
    assert not eligibility.eligible and eligibility.reason == REASON_WEB_CONTENT_USED


def test_ticked_but_unused_search_leaves_no_taint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A07 勾选了联网但模型没调用 ``web_search``：不置污点、不追加披露行（污点只认工具结果的 artifact）。"""

    with FakeSearchServer("ok") as server:
        _deploy(monkeypatch, server)
        payload, session, _ = _ask(tmp_path, [{"content": "我可以回答给水排水规范问题并做流速计算。"}], "你好，介绍一下你的能力")
        assert server.requests == []
    assert not payload["answer"].endswith(DISCLOSURE_LINE)
    assert not _state(session).get("web_tainted")


def test_ticked_while_the_deployment_is_off_warns_instead_of_silently_dropping(tmp_path: Path) -> None:
    """P7-S12-A07 / P7-S12-A08 勾选了联网，但部署没开：不注册工具、不授予能力，回答照常，``warnings`` 含 ``WEB_SEARCH_NOT_EXPOSED``。"""

    payload, session, model = _ask(tmp_path, [{"content": "好的。"}], "查一下给水管道经济流速")
    assert "web_search" not in session.registry.list_all_names()
    assert "WEB_SEARCH_NOT_EXPOSED" in payload["warnings"]
    assert "web_search" not in [tool["function"]["name"] for tool in model.state["tools"][0]]


def test_the_tightest_combination_yields_and_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A07 / P7-S12-A08 读图 + 材料同时命中（4497 字符）时放不下 ``web_search``：整组让位、本地工具照常开放，
    并向用户告警 ``WEB_SEARCH_NOT_EXPOSED``——不上调 4500。"""

    from water_agent.drawing.synth import build_synthetic_drawing

    drawings = tmp_path / "drawings"
    drawings.mkdir()
    build_synthetic_drawing(drawings, drawing_id="P-01")
    with FakeSearchServer("ok") as server:
        _deploy(monkeypatch, server)
        payload, _, model = _ask(tmp_path, [{"content": "好的。"}], "根据图纸 P-01 统计管材用量", drawing_root=str(drawings))
    exposed = [tool["function"]["name"] for tool in model.state["tools"][0]]
    assert {"read_drawing", "material_stats"} <= set(exposed)
    assert "web_search" not in exposed
    assert "WEB_SEARCH_NOT_EXPOSED" in payload["warnings"]
