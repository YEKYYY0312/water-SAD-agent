"""P7-S12 审计与 trace：``web_search.performed`` 字段白名单；trace 不记网址 / 密钥；服务商密钥只出现在发给服务商的请求头里。"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from tests.fixtures.fake_search_server import FakeSearchServer
from water_agent.harness.doubles import ScriptedChatModel
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.trivium import TriviumRetriever
from water_agent.runtime.agentic import AgenticSession
from water_agent.runtime.governance import binding
from water_agent.runtime.governance.gateway import ProviderGateway
from water_agent.session import default_checkpoint_serde
from water_agent.web_search import events
from water_agent.webapp.auth import audit

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "rag_fixture.jsonl"
SECRET = "brave-secret-key-0123456789abcdef"
QUERY = "给水管道经济流速"
SEARCH = {"tool_calls": [{"name": "web_search", "args": {"query": QUERY}}]}
ANSWER = {"content": "可参考 https://example.com/water/velocity 的资料，具体取值以设计院内部规定为准。"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    for name in ("WATER_AGENT_WEB_SEARCH", "WATER_AGENT_WEB_SEARCH_PROVIDER", "WATER_AGENT_WEB_SEARCH_BASE_URL",
                 "WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE", "WATER_AGENT_WEB_SEARCH_API_KEY", "WATER_AGENT_SKILLS",
                 "WATER_AGENT_TOOL_EXPOSURE"):
        monkeypatch.delenv(name, raising=False)
    previous = binding._default_gateway
    binding.set_default_gateway(ProviderGateway())
    audit._clear_events()
    try:
        yield
    finally:
        binding.set_default_gateway(previous)
        audit._clear_events()
        events.set_audit_sink(audit.audit_web_search_performed)


def _deploy(monkeypatch: pytest.MonkeyPatch, server: FakeSearchServer, *, provider: str = "searxng", key: str | None = None) -> None:
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH", "on")
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH_PROVIDER", provider)
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH_BASE_URL", server.base_url)
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE", "1")
    if key is not None:
        monkeypatch.setenv("WATER_AGENT_WEB_SEARCH_API_KEY", key)


def _ask(tmp_path: Path, steps: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], ScriptedChatModel]:
    args = argparse.Namespace(
        db_path=None, dimensions=None, rerank="local", rerank_model=None, rerank_base_url=None, rerank_api_key=None,
        top_k=5, recall_top_k=20, hybrid_alpha=0.7, model="deepseek-chat", base_url=None, api_key_env="DEEPSEEK_API_KEY",
        api_key=None, mock_generator=True, session_dir=str(tmp_path / "s"), trace_dir=str(tmp_path / "t"),
        stream=False, drawing_root="",
    )
    model = ScriptedChatModel(steps=steps, id_prefix="audit")
    with ExitStack() as stack:
        session = AgenticSession(
            args, stack=stack, thread_id="audit", checkpointer=InMemorySaver(serde=default_checkpoint_serde()),
            model=model, retriever=TriviumRetriever(db_path=None, fallback_clauses=load_clauses_jsonl(FIXTURE)),
            mcp_enabled=False, web_search=True,
        )
        payload = asyncio.run(session.aask("查一下给水管道经济流速", run_id="r1"))
        trace = session.recorder.snapshot() or {}
    return payload, trace, model


def test_audit_event_has_exactly_the_whitelisted_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A10 ``web_search.performed`` 恰为白名单字段：主体哈希、查询哈希 + 长度、provider、主机名、条数、结果码；
    不含查询原文、不含网址。"""

    with FakeSearchServer("ok") as server:
        _deploy(monkeypatch, server)
        _ask(tmp_path, [SEARCH, ANSWER])
    performed = [event for event in audit._get_events() if event["event"] == "web_search.performed"]
    assert len(performed) == 1
    assert set(performed[0]) == {"event", "ts", "principal_hash", "query_hash", "query_length", "provider", "host", "count", "result"}
    assert performed[0]["query_hash"] == events.query_hash(QUERY)
    assert performed[0]["query_length"] == len(QUERY)
    assert performed[0]["host"] == "127.0.0.1" and performed[0]["provider"] == "searxng"
    assert performed[0]["result"] == "ok" and performed[0]["count"] == 3
    text = json.dumps(performed, ensure_ascii=False)
    assert QUERY not in text and "://" not in text


def test_rejected_queries_are_audited_by_code_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A10 被守卫拒绝的查询同样留审计（结果码 ``WEB_SEARCH_QUERY_REJECTED``），且不含查询原文。"""

    leaky = "联系 13800138000 的施工单位"
    with FakeSearchServer("ok") as server:
        _deploy(monkeypatch, server)
        _ask(tmp_path, [{"tool_calls": [{"name": "web_search", "args": {"query": leaky}}]}, {"content": "该查询没有被执行。"}])
        assert server.requests == []
    performed = [event for event in audit._get_events() if event["event"] == "web_search.performed"]
    assert [event["result"] for event in performed] == ["WEB_SEARCH_QUERY_REJECTED"]
    assert "13800138000" not in json.dumps(performed, ensure_ascii=False)


def test_extra_fields_never_reach_the_audit_and_a_failing_sink_is_harmless() -> None:
    """P7-S12-A10 ``emit_performed`` 丢弃白名单之外的键；sink 抛错不影响调用方（审计失败不改变运行结果）。"""

    seen: list[dict[str, Any]] = []
    events.set_audit_sink(lambda **fields: seen.append(fields))
    events.emit_performed(query_hash="h", query_length=3, provider="searxng", host="h", count=1, result="ok",
                          principal_hash="p", query="原文", url="https://x", api_key=SECRET)
    assert seen == [{"query_hash": "h", "query_length": 3, "provider": "searxng", "host": "h", "count": 1,
                     "result": "ok", "principal_hash": "p"}]

    def boom(**_: Any) -> None:
        raise RuntimeError("sink down")

    events.set_audit_sink(boom)
    events.emit_performed(result="ok")  # 不抛


def test_trace_event_has_no_urls_and_only_whitelisted_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A10 trace 的 ``web_search`` 事件：data 只有 provider / host / result / count / dropped_suspicious；不记网址。"""

    with FakeSearchServer("ok") as server:
        _deploy(monkeypatch, server)
        _, trace, _ = _ask(tmp_path, [SEARCH, ANSWER])
    found = [event for event in trace.get("events", []) if event.get("event_type") == "web_search"]
    assert len(found) == 1
    assert set(found[0]["data"]) == {"provider", "host", "result", "count", "dropped_suspicious"}
    assert "://" not in json.dumps(found[0]["data"], ensure_ascii=False)


@pytest.mark.parametrize("mode", ["ok", "status_401"])
def test_provider_key_is_only_in_the_request_header(
    mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """P7-S12-A10 brave 的订阅令牌只出现在发给服务商的 ``X-Subscription-Token`` 头里：工具结果、模型输入、查询串、审计、
    trace、日志、标准输出 / 错误、异常文本里一律零出现（成功与 401 两条路径都查）。"""

    caplog.set_level(logging.DEBUG)
    with FakeSearchServer(mode, require_token=SECRET) as server:
        _deploy(monkeypatch, server, provider="brave", key=SECRET)
        payload, trace, model = _ask(tmp_path, [SEARCH, ANSWER] if mode == "ok" else [SEARCH, {"content": "没有查到可用的结果。"}])
        requests = list(server.requests)
    assert requests and requests[0]["has_token"] is True and requests[0]["token_ok"] is True
    surfaces = {
        "payload": json.dumps(payload, ensure_ascii=False, default=str),
        "model_input": json.dumps([[str(m.content) for m in messages] for messages in model.state["received"]], ensure_ascii=False),
        "query_string": json.dumps([item["raw_query"] for item in requests], ensure_ascii=False),
        "audit": json.dumps(audit._get_events(), ensure_ascii=False),
        "trace": json.dumps(trace, ensure_ascii=False, default=str),
        "logs": caplog.text,
        "stdio": "".join(capsys.readouterr()),
    }
    leaked = sorted(name for name, text in surfaces.items() if SECRET in text)
    assert leaked == []
    if mode == "status_401":
        assert "WEB_SEARCH_AUTH" in surfaces["model_input"]


def test_config_repr_never_shows_the_key() -> None:
    """P7-S12-A10 部署配置的 ``repr`` 不含密钥与基址（误打日志也不泄漏）。"""

    from water_agent.web_search.config import resolve_web_search_config

    config = resolve_web_search_config(
        {"WATER_AGENT_WEB_SEARCH": "on", "WATER_AGENT_WEB_SEARCH_PROVIDER": "brave",
         "WATER_AGENT_WEB_SEARCH_BASE_URL": "https://api.search.brave.com/"},
        getenv=lambda name: SECRET if name == "WATER_AGENT_WEB_SEARCH_API_KEY" else None,
    )
    assert config.ready and config.api_key == SECRET
    assert SECRET not in repr(config) and "api.search.brave.com" not in repr(config)
