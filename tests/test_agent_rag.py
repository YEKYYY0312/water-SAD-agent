"""Step 6：agentic RAG 外层循环 + ToolSpec→BaseTool 适配层测试。

全程离线（FakeMessagesListChatModel + InMemoryRetriever），不依赖任何 Key。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from water_agent.graphs.agent_rag import build_agentic_app
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.tools.calc_velocity import calc_velocity_spec
from water_agent.tools.cite import build_cite_clauses_tool
from water_agent.tools.ds_adapters import tool_spec_to_base_tool
from water_agent.tools.registry import ToolRegistry
from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


@pytest.fixture
def clauses():
    return load_clauses_jsonl(FIXTURE_PATH)


@pytest.fixture
def retriever(clauses):
    return InMemoryRetriever(clauses)


def _assistant(message: str, /, tool_calls: list[dict] | None = None) -> AIMessage:
    return AIMessage(content=message, tool_calls=tool_calls or [])


def _retrieve_call(retriever):
    return _assistant(
        "",
        tool_calls=[{"name": "retrieve_clauses", "args": {"query": "给水管道的设计秒流量", "top_k": 5}, "id": "c-retrieve"}],
    )


def _cite_call(article_ids: list[str], *, call_id: str = "c-cite") -> AIMessage:
    return _assistant(
        "",
        tool_calls=[{"name": "cite_clauses", "args": {"article_ids": article_ids}, "id": call_id}],
    )


def _build_tools(retriever) -> list:
    """构建注册表（retrieve_clauses 注入真实内存检索器）并转 BaseTool 列表。"""
    registry = ToolRegistry()
    registry.register(build_retrieve_clauses_tool(retriever))
    registry.register(build_cite_clauses_tool())
    registry.register(calc_velocity_spec)
    return [tool_spec_to_base_tool(registry, name) for name in registry.list_all_names()]


def _agentic_args(tmp_path, **overrides) -> argparse.Namespace:
    """构造 run_agentic_once 所需的最小 Namespace（默认走离线 mock 路径）。"""
    base = {
        "query": "",
        "db_path": None,
        "dimensions": None,
        "rerank": "local",
        "rerank_model": None,
        "rerank_base_url": None,
        "rerank_api_key": None,
        "top_k": 5,
        "recall_top_k": 20,
        "hybrid_alpha": 0.7,
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "mock_generator": True,
        "interactive": False,
        "resume": None,
        "session_dir": str(tmp_path / "sessions"),
        "mode": "agentic",
        "trace_dir": str(tmp_path / "traces"),
        "json": False,
        "api_key_env": "DEEPSEEK_API_KEY",
        "stream": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestToolSpecToBaseTool:
    def test_adapter_proxies_registry(self, retriever) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        tool = tool_spec_to_base_tool(registry, "calc_velocity")
        result = tool.invoke({"diameter_mm": 150, "flow_lps": 20})
        assert isinstance(result, dict)
        assert result["velocity_mps"] == pytest.approx(1.132, abs=1e-3)

    def test_adapter_preserves_structured_errors(self, retriever) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        tool = tool_spec_to_base_tool(registry, "calc_velocity")
        result = tool.invoke({"diameter_mm": -1, "flow_lps": 20})
        # 结构化错误不抛裸异常：error/code/suggestions 完整可见
        assert result["error"] is True
        assert result["code"] == "INVALID_ARGUMENTS"
        assert result["suggestions"]

    def test_adapter_export_matches_registry_definition(self, retriever) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        tool = tool_spec_to_base_tool(registry, "calc_velocity")
        assert tool.name == "calc_velocity"
        assert tool.args_schema is calc_velocity_spec.args_model
        assert tool.description == calc_velocity_spec.description

    def test_unknown_tool_rejected(self, retriever) -> None:
        registry = ToolRegistry()
        with pytest.raises(ValueError, match="未注册"):
            tool_spec_to_base_tool(registry, "no_such_tool")


class TestAgenticApp:
    def test_requires_checkpointer(self, retriever) -> None:
        """无 checkpointer 拒绝构建（护住会话纪律）。"""
        tools = _build_tools(retriever)
        model = FakeMessagesListChatModel(responses=[_assistant("你好")])
        with pytest.raises(ValueError, match="checkpointer"):
            build_agentic_app(model=model, tools=tools, checkpointer=None)

    def test_tool_call_executed_and_observation_returned(self, retriever) -> None:
        """假模型触发 ToolNode 执行，工具结果以 ToolMessage 回填。"""
        responses = [
            _retrieve_call(retriever),
            _assistant("根据检索结果，设计秒流量按规范条文确定。"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            out = app.invoke(
                {"messages": [HumanMessage(content="设计秒流量如何确定")]},
                config={"configurable": {"thread_id": "t1"}},
            )
        messages = out["messages"]
        assert any(isinstance(m, ToolMessage) for m in messages), "工具必须被执行"
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        assert any(m.name == "retrieve_clauses" for m in tool_msgs)
        assert out["status"] == "succeeded"
        assert messages[-1].content  # 最终模型回答非空

    def test_invalid_citation_reinjected_and_retries(self, retriever) -> None:
        """引用 ∉ context → 注入带错误文本的 ToolMessage 回流；重试后纠正 → 收口成功。"""
        responses = [
            _retrieve_call(retriever),
            _cite_call(["FAKE 0.0.0"], call_id="c-bad"),
            _assistant("这是第一轮回答。"),
            _cite_call(["FIX 1.1.1"], call_id="c-good"),
            _assistant("修正后的最终回答。"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            out = app.invoke(
                {"messages": [HumanMessage(content="设计秒流量如何确定")]},
                config={"configurable": {"thread_id": "t1"}},
            )
        messages: list[BaseMessage] = out["messages"]
        # 回流 observation 可见：错误文本作为 ToolMessage 写回消息历史
        reinject = [m for m in messages if isinstance(m, ToolMessage) and "不在本次检索结果中" in str(m.content)]
        assert reinject, "引用校验失败必须以带错误文本的 ToolMessage 回流"
        assert out["retry_count"] == 1
        assert out["status"] == "succeeded"
        assert [c.source_id for c in out["citations"]] == ["FIX 1.1.1"]

    def test_citation_claims_carry_version_status(self, retriever) -> None:
        """Step 8：校验器接版本注册表，版本状态写回 CitationClaim；未知只标记不进 errors。"""

        class FakeRegistry:
            def get_spec_status(self, spec_id: str) -> dict:
                return {"status": "current" if spec_id == "FIX" else "unknown"}

        responses = [
            _retrieve_call(retriever),
            _cite_call(["FIX 1.1.1"]),
            _assistant("最终回答。"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        with MemorySaver() as saver:
            app = build_agentic_app(
                model=model,
                tools=_build_tools(retriever),
                checkpointer=saver,
                version_registry=FakeRegistry(),
            )
            out = app.invoke(
                {"messages": [HumanMessage(content="设计秒流量如何确定")]},
                config={"configurable": {"thread_id": "t1"}},
            )
        assert out["status"] == "succeeded"
        assert out["citations"][0].version_status == "current"
        assert out["errors"] == []

    def test_citation_retry_bounded_then_fails(self, retriever) -> None:
        """最多回流 2 次；持续编造引用 → 结构化失败且 errors 可见。"""
        responses = [
            _retrieve_call(retriever),
            _cite_call(["FAKE 0.0.0"], call_id="c1"),
            _assistant("轮 1"),
            _cite_call(["FAKE 9.9.9"], call_id="c2"),
            _assistant("轮 2"),
            _cite_call(["FAKE 8.8.8"], call_id="c3"),
            _assistant("轮 3"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            out = app.invoke(
                {"messages": [HumanMessage(content="设计秒流量如何确定")]},
                config={"configurable": {"thread_id": "t1"}},
            )
        assert out["status"] == "failed"
        assert out["retry_count"] == 2  # 上限即停止
        assert any("不在本次检索结果" in e for e in out["errors"])

    def test_turn_budget_exceeded_returns_structured_error(self, retriever) -> None:
        """模型无限调用工具 → 轮数预算超限 → 结构化错误而非挂死。"""
        always_tool = AIMessage(
            content="",
            tool_calls=[{"name": "retrieve_clauses", "args": {"query": "设计秒流量", "top_k": 5}, "id": "c-loop"}],
        )
        model = FakeMessagesListChatModel(responses=[always_tool] * 30)
        with MemorySaver() as saver:
            app = build_agentic_app(
                model=model,
                tools=_build_tools(retriever),
                checkpointer=saver,
                max_tool_turns=4,
            )
            out = app.invoke(
                {"messages": [HumanMessage(content="设计秒流量如何确定")]},
                config={"configurable": {"thread_id": "t1"}},
            )
        assert out["status"] == "failed"
        assert any("预算" in e or "轮数" in e for e in out["errors"]), f"errors: {out['errors']}"

    def test_astream_agentic_offline_yields_tokens_and_final_state(self, retriever) -> None:
        """流式接线离线验证（Step 7）：messages+values 双模式，token 回调可见、最终 state 完整。"""
        from water_agent.graphs.agent_rag import astream_agentic

        responses = [
            _retrieve_call(retriever),
            _assistant("根据检索结果，设计秒流量按规范条文确定。"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        tokens: list[str] = []
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            state = asyncio.run(
                astream_agentic(
                    app,
                    "设计秒流量如何确定",
                    {"configurable": {"thread_id": "stream-t1"}},
                    on_token=tokens.append,
                )
            )
        assert "".join(tokens), "流式模式下应产出模型文本 token"
        assert state.get("status") == "succeeded"
        assert state.get("answer")


class TestCliAgenticOffline:
    def test_run_agentic_once_works_without_api_keys(self, tmp_path, monkeypatch) -> None:
        """离线闭环：--mode agentic + --mock-generator 时无 DEEPSEEK/DASHSCOPE Key 也跑通（fixture 回落）。"""
        from water_agent.cli import run_agentic_once

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        args = _agentic_args(tmp_path)
        payload = run_agentic_once("给水管道的设计秒流量如何确定", args=args)
        assert payload["intent"] == "agentic"
        assert payload["status"] == "succeeded"
        assert payload["answer"]
        assert payload["errors"] == []
        assert payload["streamed"] is False

    def test_run_agentic_once_without_key_errors_with_guidance(self, tmp_path, monkeypatch) -> None:
        """Step 7 契约：--mode agentic 无 Key 且未开 --mock-generator → 结构化错误 + 指引，不得静默降级。"""
        from water_agent.cli import run_agentic_once

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        args = _agentic_args(tmp_path, mock_generator=False)
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY must not be empty") as excinfo:
            run_agentic_once("给水管道的设计秒流量如何确定", args=args)
        assert "--mock-generator" in str(excinfo.value), "错误必须带可行动的离线验证指引"

class TestAgenticTrace:
    def test_agentic_trace_records_llm_calls_and_finishes(self, retriever, tmp_path) -> None:
        """Step 10 观测：agentic 每次模型响应一条 llm_call，finalize 时 trace 落盘（run_end）。"""
        from water_agent.observability.json_trace import JsonTraceRecorder

        responses = [_retrieve_call(retriever), _cite_call(["FIX 1.1.1"]), _assistant("最终回答。")]
        model = FakeMessagesListChatModel(responses=responses)
        recorder = JsonTraceRecorder(tmp_path)
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver, trace=recorder)
            out = app.invoke(
                {"messages": [HumanMessage(content="设计秒流量如何确定")], "query": "设计秒流量如何确定"},
                config={"configurable": {"thread_id": "trace-t1"}},
            )
        assert out["status"] == "succeeded"
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1, "finalize 必须把 trace 落盘"
        document = json.loads(files[0].read_text(encoding="utf-8"))
        llm_calls = [e for e in document["events"] if e["event_type"] == "llm_call"]
        assert len(llm_calls) == 3, [e["event_type"] for e in document["events"]]
        assert llm_calls[0]["data"]["response_preview"].startswith("tool_calls: retrieve_clauses")
        assert llm_calls[-1]["data"]["response_preview"] == "最终回答。"
        assert document["status"] == "succeeded"
        assert any(e["event_type"] == "run_end" for e in document["events"])
