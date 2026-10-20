"""P7-S12 暴露：``request`` 模式（入口标志决定，不看 query）、两道门、最紧组合让位告警、schema 预算。"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from water_agent.plugins.exposure import (
    AMBIENT,
    CORE,
    REQUEST,
    ExposureRule,
    ToolExposurePolicy,
    ToolGroup,
    schema_chars,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "water_agent"


def _definition(name: str, size: int) -> dict[str, Any]:
    # 体积放进参数模式而不是描述：指引只取描述首句，描述撑大会先撞指引预算（800），测不到 schema 预算
    return {
        "type": "function",
        "function": {"name": name, "description": f"{name} 工具。", "parameters": {"padding": "x" * size}},
    }


def _policy(**sizes: int) -> ToolExposurePolicy:
    groups = [
        ToolGroup("core", ("core_tool",), CORE),
        ToolGroup("intent", ("intent_tool",), ExposureRule(mode="intent", keywords=("图纸",))),
        ToolGroup("web_search", ("web_search",), REQUEST),
        ToolGroup("mcp:x", ("mcp_tool",), AMBIENT),
    ]
    definitions = {name: _definition(name, size) for name, size in sizes.items()}
    return ToolExposurePolicy(
        strategy="intent",
        groups=groups,
        definitions=definitions,
        order=["core_tool", "intent_tool", "web_search", "mcp_tool"],
        guide_budget=800,
    )


def test_request_group_opens_only_when_the_entry_enables_it() -> None:
    """P7-S12-A08 ``request`` 组由入口标志决定：不看 query；不传 ``request_enabled`` 时永不开放（与此前逐字相同）。"""

    policy = _policy(core_tool=100, intent_tool=100, web_search=100, mcp_tool=100)
    plain = policy.decide("联网查一下 web_search 给水管道")  # 关键词骗不开 request 组
    assert "web_search" not in plain.exposed
    assert all(group != "web_search" for group, _ in plain.dropped), "没勾选不算「被让位」"
    ticked = policy.decide("给水管道", request_enabled=["web_search"])
    assert "web_search" in ticked.exposed and ("web_search", "request") in ticked.reasons


def test_request_group_is_never_sticky() -> None:
    """P7-S12-A08 勾选只对那一条消息有效：上一轮调用过 ``web_search``，这一轮没勾选就不开放（sticky 不延续 request 组）。"""

    policy = _policy(core_tool=100, intent_tool=100, web_search=100, mcp_tool=100)
    decision = policy.decide("谢谢", previous_turn_tools=["web_search", "intent_tool"])
    assert "web_search" not in decision.exposed
    assert "intent_tool" in decision.exposed, "其他组照常 sticky"


def test_request_group_yields_after_intent_under_the_tightest_budget() -> None:
    """P7-S12-A08 入选顺序 core → intent → request → sticky → ambient：预算只够 core + intent 时 request 组整组让位，
    记进 ``dropped``（由外层图据此发 ``WEB_SEARCH_NOT_EXPOSED``）；本地工具不因勾选联网被挤掉。"""

    # core + intent ≈ 4000 放得下；再加 web_search ≈ 4900 放不下。若 request 排在 intent 之前，被挤掉的会是 intent_tool——
    # 这条用例同时区分了入选顺序
    policy = _policy(core_tool=1800, intent_tool=2000, web_search=800, mcp_tool=10)
    alone = policy.decide("你好", request_enabled=["web_search"])
    assert "web_search" in alone.exposed, "没有 intent 竞争时 web_search 放得下"
    decision = policy.decide("看一下图纸", request_enabled=["web_search"])
    assert "intent_tool" in decision.exposed
    assert "web_search" not in decision.exposed
    assert ("web_search", "schema_budget") in decision.dropped
    assert decision.schema_chars <= 4500


def test_request_rule_rejects_keywords() -> None:
    """P7-S12-A08 ``request`` 规则不能带关键词 / 正则（否则又回到了"猜意图"）。"""

    with pytest.raises(ValueError):
        ExposureRule(mode="request", keywords=("联网",))


def test_web_search_tool_schema_fits_and_common_combinations_fit() -> None:
    """P7-S12-A08 ``web_search`` 的参数模式 ≤ 500 字符；"core + web_search"与"core + 读图 + web_search"都放得下 4500；
    最紧组合"读图 + 材料"（4497）放不下它——是预期，由让位告警兜住，不上调 4500。"""

    from water_agent.web_search.tool import DESCRIPTION, WebSearchArgs

    params = WebSearchArgs.model_json_schema()
    assert len(json.dumps(params, ensure_ascii=False)) <= 500
    web = {"type": "function", "function": {"name": "web_search", "description": DESCRIPTION, "parameters": params}}
    baseline = json.loads((Path(__file__).resolve().parents[1] / "data" / "eval" / "tool_assembly_main.json").read_text(encoding="utf-8"))
    tools = baseline["profiles"]["session_drawing_arg"]["surface"]["tools"]
    core = ["retrieve_clauses", "cite_clauses", "calc_velocity"]

    def total(names: list[str]) -> int:
        return sum(tools[name]["schema_chars"] for name in names) + len(json.dumps(web, ensure_ascii=False)) + 2 * len(names)

    assert total(core) < 4500
    assert total([*core, "read_drawing"]) < 4500
    assert total([*core, "read_drawing", "material_stats"]) > 4500, "最紧组合放不下 web_search（需要让位告警）"
    assert schema_chars([web]) < 1000


def test_session_off_is_byte_identical_and_unticked_registers_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S12-A01 / P7-S12-A08 两道门：部署 off，或部署 on 但这条消息没勾选——都不注册 ``web_search``、主体没有
    ``WEB_SEARCH``；只有两道都过才注册并授予。"""

    import argparse
    from contextlib import ExitStack

    from langgraph.checkpoint.memory import InMemorySaver

    from water_agent.retrieval.loader import load_clauses_jsonl
    from water_agent.retrieval.trivium import TriviumRetriever
    from water_agent.runtime.agentic import AgenticSession
    from water_agent.runtime.governance.contracts import Capability

    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    for name in ("WATER_AGENT_WEB_SEARCH", "WATER_AGENT_WEB_SEARCH_PROVIDER", "WATER_AGENT_WEB_SEARCH_BASE_URL",
                 "WATER_AGENT_SKILLS", "WATER_AGENT_TOOL_EXPOSURE"):
        monkeypatch.delenv(name, raising=False)
    args = argparse.Namespace(
        db_path=None, dimensions=None, rerank="local", rerank_model=None, rerank_base_url=None, rerank_api_key=None,
        top_k=5, recall_top_k=20, hybrid_alpha=0.7, model="deepseek-chat", base_url=None, api_key_env="DEEPSEEK_API_KEY",
        api_key=None, mock_generator=True, session_dir=str(tmp_path / "s"), trace_dir=str(tmp_path / "t"),
        stream=False, drawing_root="",
    )
    clauses = load_clauses_jsonl(Path(__file__).resolve().parents[1] / "data" / "fixtures" / "rag_fixture.jsonl")

    def session(*, web_search: bool) -> Any:
        with ExitStack() as stack:
            return AgenticSession(
                args, stack=stack, thread_id="exp", checkpointer=InMemorySaver(),
                retriever=TriviumRetriever(db_path=None, fallback_clauses=clauses), mcp_enabled=False,
                web_search=web_search,
            )

    off = session(web_search=True)
    assert "web_search" not in off.registry.list_all_names()
    assert Capability.WEB_SEARCH not in off.request_context.principal.capabilities
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH", "on")
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH_BASE_URL", "https://search.example.invalid/")
    unticked = session(web_search=False)
    assert "web_search" not in unticked.registry.list_all_names()
    assert Capability.WEB_SEARCH not in unticked.request_context.principal.capabilities
    assert unticked.request_groups == ()
    ticked = session(web_search=True)
    assert "web_search" in ticked.registry.list_all_names()
    assert Capability.WEB_SEARCH in ticked.request_context.principal.capabilities
    assert ticked.request_groups == ("web_search",)


def test_exposure_module_stays_deterministic() -> None:
    """P7-S12-A08 加了 request 模式之后，暴露决定仍是确定性纯函数（AST：不 import 模型 / HTTP / random / time）。"""

    tree = ast.parse((SRC / "plugins" / "exposure.py").read_text(encoding="utf-8"))
    imported = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])  # type: ignore[list-item]
    }
    for banned in ("random", "time", "httpx", "requests", "socket", "langchain_openai"):
        assert not any(name == banned or name.startswith(f"{banned}.") for name in imported), banned
