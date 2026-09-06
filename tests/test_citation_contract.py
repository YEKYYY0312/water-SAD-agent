"""引用契约测试：任何使用 cite_clauses / 产生引用的图必须挂确定性引用校验器。

契约（2026-09-04 新增，对应 code review 问题 6）：
- agentic 图（graphs/agent_rag.py）注册了 cite_clauses 工具 → 必须带 citation_validator 节点，
  模型声明不在检索上下文的引用 → 必须回流并最终结构化失败（不得静默放行）；
- legacy 图（graphs/rag.py）由生成器产出引用 → 必须带 citation_check 节点，
  编造引用 → 必须失败（不得成功收口）；
- 有效引用必须正常通过（校验器不得误伤）。

静态断言（节点存在）+ 功能断言（编造引用被拦截）双重锁定，防止未来新入口
只接 cite_clauses 工具却漏挂校验器。
"""
from __future__ import annotations

from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from water_agent.generation.base import Generator
from water_agent.graphs.agent_rag import build_agentic_app
from water_agent.graphs.rag import build_rag_graph
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.tools.cite import build_cite_clauses_tool
from water_agent.tools.ds_adapters import tool_spec_to_base_tool
from water_agent.tools.registry import ToolRegistry
from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


def _graph_node_names(app) -> set[str]:
    """解开 RunnableBinding 包装，取底层编译图的节点名集合。"""
    bound = app
    while hasattr(bound, "bound"):
        bound = bound.bound
    return set(getattr(bound, "nodes", {}).keys())


def _retrieve_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "retrieve_clauses",
                "args": {"query": "给水管道的设计秒流量", "top_k": 5},
                "id": "c-retrieve-contract",
            }
        ],
    )


def _cite_call(article_ids: list[str], *, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "cite_clauses", "args": {"article_ids": article_ids}, "id": call_id}],
    )


class _FabricatingGenerator(Generator):
    """永远编造不在检索上下文中的引用（legacy 图契约用）。"""

    def generate(self, query: str, context):
        from water_agent.domain.models import Citation, GeneratedAnswer

        return GeneratedAnswer(
            answer="编造引用的回答。",
            citations=[Citation(spec_id="FAKE", article="0.0.0", title="不存在", text_snippet="编造")],
            intent="rag",
        )


class TestAgenticCiteRequiresValidator:
    """agentic 图：注册 cite_clauses 必须同时挂 citation_validator。"""

    def test_static_graph_has_citation_validator_node(self) -> None:
        """静态契约：图节点必须包含 citation_validator。"""
        clauses = load_clauses_jsonl(FIXTURE_PATH)
        retriever = InMemoryRetriever(clauses)
        registry = ToolRegistry()
        registry.register(build_retrieve_clauses_tool(retriever))
        registry.register(build_cite_clauses_tool())
        tools = [tool_spec_to_base_tool(registry, name) for name in registry.list_all_names()]
        model = FakeMessagesListChatModel(responses=[AIMessage(content="结束")])
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=tools, checkpointer=saver)
        nodes = _graph_node_names(app)
        assert "citation_validator" in nodes, (
            f"agentic 图注册了 cite_clauses 却缺少 citation_validator 节点，实际节点：{sorted(nodes)}"
        )
        assert "agent_loop" in nodes and "finalize" in nodes

    def test_fabricated_citation_is_blocked(self) -> None:
        """功能契约：模型声明不在检索结果的引用 → 回流耗尽后结构化失败。"""
        clauses = load_clauses_jsonl(FIXTURE_PATH)
        retriever = InMemoryRetriever(clauses)
        registry = ToolRegistry()
        registry.register(build_retrieve_clauses_tool(retriever))
        registry.register(build_cite_clauses_tool())
        tools = [tool_spec_to_base_tool(registry, name) for name in registry.list_all_names()]
        responses = [
            _retrieve_call(),
            _cite_call(["FAKE 0.0.0"], call_id="c-bad-1"),
            AIMessage(content="轮 1"),
            _cite_call(["FAKE 9.9.9"], call_id="c-bad-2"),
            AIMessage(content="轮 2"),
            _cite_call(["FAKE 8.8.8"], call_id="c-bad-3"),
            AIMessage(content="轮 3"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=tools, checkpointer=saver)
            out = app.invoke(
                {"messages": [HumanMessage(content="设计秒流量如何确定")]},
                config={"configurable": {"thread_id": "contract-t1"}},
            )
        assert out["status"] == "failed", (
            f"编造引用必须被 citation_validator 拦截（否则 cite_clauses 可绕过校验），errors: {out['errors']}"
        )
        assert any("不在本次检索结果" in e for e in out["errors"])

    def test_valid_citation_passes_contract(self) -> None:
        """功能契约反向：有效引用正常通过，校验器不得误伤。"""
        clauses = load_clauses_jsonl(FIXTURE_PATH)
        retriever = InMemoryRetriever(clauses)
        registry = ToolRegistry()
        registry.register(build_retrieve_clauses_tool(retriever))
        registry.register(build_cite_clauses_tool())
        tools = [tool_spec_to_base_tool(registry, name) for name in registry.list_all_names()]
        responses = [
            _retrieve_call(),
            _cite_call(["FIX 1.1.1"], call_id="c-good-contract"),
            AIMessage(content="根据检索结果回答。"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=tools, checkpointer=saver)
            out = app.invoke(
                {"messages": [HumanMessage(content="设计秒流量如何确定")]},
                config={"configurable": {"thread_id": "contract-t2"}},
            )
        assert out["status"] == "succeeded"
        assert [c.source_id for c in out["citations"]] == ["FIX 1.1.1"]


class TestLegacyGraphRequiresCitationCheck:
    """legacy 图：生成器产出引用必须经 citation_check 校验。"""

    def test_static_graph_has_citation_check_node(self) -> None:
        clauses = load_clauses_jsonl(FIXTURE_PATH)
        app = build_rag_graph(InMemoryRetriever(clauses), _FabricatingGenerator())
        nodes = _graph_node_names(app)
        assert "citation_check" in nodes, f"legacy 图缺少 citation_check 节点，实际节点：{sorted(nodes)}"
        assert "retrieve" in nodes and "generate" in nodes and "finalize" in nodes

    def test_fabricated_citation_fails(self) -> None:
        clauses = load_clauses_jsonl(FIXTURE_PATH)
        app = build_rag_graph(InMemoryRetriever(clauses), _FabricatingGenerator())
        out = app.invoke({"query": "给水管道的设计秒流量"})
        assert out["status"] == "failed"
        assert any("FAKE 0.0.0" in e for e in out["errors"])