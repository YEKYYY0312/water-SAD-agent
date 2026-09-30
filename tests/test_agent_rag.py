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

from water_agent.graphs.agent_rag import (
    PER_TURN_RESET,
    WARNING_NON_NORMATIVE_NO_CITATION,
    build_agentic_app,
    current_turn_messages,
)
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


def _assert_tool_message_protocol(messages: list[BaseMessage]) -> None:
    """回归护栏（真实链路 rag-p1 暴露）：OpenAI 兼容 API 要求 role='tool' 消息必须
    紧跟在带 tool_calls 的 assistant 消息后——逐条检查历史中 ToolMessage 的前置合法性。"""
    for i, m in enumerate(messages):
        if isinstance(m, ToolMessage):
            assert i > 0, f"ToolMessage({m.name}) 前必须有 AIMessage，实际是历史首条"
            prev = messages[i - 1]
            assert isinstance(prev, AIMessage), (
                f"ToolMessage({m.name}) 前一条必须是 AIMessage，实际是 {type(prev).__name__}"
            )
            ids = [tc.get("id") for tc in (prev.tool_calls or [])]
            assert m.tool_call_id in ids, (
                f"ToolMessage({m.name}) 的 tool_call_id={m.tool_call_id!r} 必须在"
                f"前一条 AIMessage 的 tool_calls 中（{ids}）"
            )


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
        """假模型触发 ToolNode 执行，工具结果以 ToolMessage 回填；引用闭环成立。"""
        responses = [
            _retrieve_call(retriever),
            _cite_call(["FIX 1.1.1"]),
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
        assert [c.source_id for c in out["citations"]] == ["FIX 1.1.1"]
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
        _assert_tool_message_protocol(messages)

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

    # ── P1/Step 10 评审：零引用强制闭环 ──────────────────────────────

    def test_zero_citation_triggers_reflow_then_correct_cite_succeeds(self, retriever) -> None:
        """有检索、有回答、无引用 → 注入要求 cite_clauses 的 ToolMessage 回流一次；
        回流后正确引用 → 收口成功。"""
        responses = [
            _retrieve_call(retriever),
            _assistant("第一轮给出了回答但没有声明引用。"),
            _cite_call(["FIX 1.1.1"], call_id="c-cite-after-reflow"),
            _assistant("根据检索结果，设计秒流量按规范条文确定。"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            out = app.invoke(
                {"messages": [HumanMessage(content="设计秒流量如何确定")]},
                config={"configurable": {"thread_id": "t1"}},
            )
        messages: list[BaseMessage] = out["messages"]
        reflow = [
            m for m in messages
            if isinstance(m, ToolMessage) and "cite_clauses" in str(m.content) and "声明引用" in str(m.content)
        ]
        assert reflow, "空引用必须以明确要求调用 cite_clauses 的 ToolMessage 回流一次"
        assert out["status"] == "succeeded"
        assert out["retry_count"] == 1
        assert out["empty_citation_reflows"] == 1
        assert [c.source_id for c in out["citations"]] == ["FIX 1.1.1"]
        _assert_tool_message_protocol(messages)

    def test_consecutive_zero_citations_fail_visibly(self, retriever) -> None:
        """有检索、已回流一次后仍零引用 → 结构化失败，错误文本必须可见。"""
        responses = [
            _retrieve_call(retriever),
            _assistant("第一轮：给出回答但未声明引用。"),
            _assistant("第二轮：仍然没有调用 cite_clauses。"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            out = app.invoke(
                {"messages": [HumanMessage(content="设计秒流量如何确定")]},
                config={"configurable": {"thread_id": "t1"}},
            )
        messages: list[BaseMessage] = out["messages"]
        assert out["status"] == "failed"
        assert out["empty_citation_reflows"] == 1
        assert any("cite_clauses" in e for e in out["errors"]), f"失败错误必须可见: {out['errors']}"
        assert any("零引用" in e or "空引用" in e for e in out["errors"])
        _assert_tool_message_protocol(messages)

    def test_zero_citation_without_retrieval_and_explicit_no_basis_succeeds(self, retriever) -> None:
        """无检索结果且模型明确说明未检索到依据 → 允许无引用收口，不强制伪造引用。"""
        responses = [
            _assistant("未能在规范条款库中检索到相关内容，无法提供条款依据。"),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            out = app.invoke(
                {"messages": [HumanMessage(content="某新型材料是否合规")]},
                config={"configurable": {"thread_id": "t1"}},
            )
        assert out["status"] == "succeeded", f"errors: {out['errors']}"
        assert out["citations"] == []
        assert out.get("retry_count", 0) == 0
        assert out["errors"] == []

    def test_zero_citation_with_empty_answer_fails(self, retriever) -> None:
        """空回答 + 零引用：无内容可收口 → 结构化失败。"""
        responses = [
            _retrieve_call(retriever),
            AIMessage(content="", tool_calls=[]),
        ]
        model = FakeMessagesListChatModel(responses=responses)
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            out = app.invoke(
                {"messages": [HumanMessage(content="设计秒流量如何确定")]},
                config={"configurable": {"thread_id": "t1"}},
            )
        assert out["status"] == "failed"
        assert out["errors"], "空回答/零引用失败原因必须可见"

    def test_astream_agentic_offline_yields_tokens_and_final_state(self, retriever) -> None:
        """流式接线离线验证（Step 7）：messages+values 双模式，token 回调可见、最终 state 完整。"""
        from water_agent.graphs.agent_rag import astream_agentic

        responses = [
            _retrieve_call(retriever),
            _cite_call(["FIX 1.1.1"]),
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


class TestTurnScopedCitationValidator:
    """P5-S49：零引用校验按轮次取信号，非规范性回答确定性豁免，回流要求面向用户作答。

    复现截图：第 1 轮检索过，第 2 轮问"能读 PDF 吗"——旧实现扫描整条 thread，把上一轮
    的检索结果当成本轮的，强制回流后只剩"本轮我未引用任何规范条款…结论重申"。
    """

    @staticmethod
    def _invoke(app, text: str, thread: str = "t-s49"):
        return app.invoke(
            {"messages": [HumanMessage(content=text)], **PER_TURN_RESET},
            config={"configurable": {"thread_id": thread}},
        )

    def test_capability_question_without_retrieval_is_exempt_with_visible_warning(self, retriever) -> None:
        model = FakeMessagesListChatModel(responses=[
            _assistant("可以。上传 Word、PDF 或 TXT 后，我能读取其中的文字内容；DXF 图纸目前在网页端还不能直接读取。"),
        ])
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            out = self._invoke(app, "你能读取 PDF 文件吗")
        assert out["status"] == "succeeded", out["errors"]
        assert out["citations"] == []
        assert out.get("empty_citation_reflows", 0) == 0
        assert out["warnings"] == [WARNING_NON_NORMATIVE_NO_CITATION]

    @pytest.mark.parametrize(
        "answer",
        [
            "生活给水管道流速一般不宜大于 2 m/s。",
            "按 GB50015 的要求确定。",
            "依据 3.6.1 条执行。",
            "干管可选 DN100。",
            "坡度不小于 0.3%。",
        ],
    )
    def test_normative_looking_answer_without_citation_still_reflows(self, retriever, answer: str) -> None:
        """凭记忆给出规范号/条款号/带单位数值的回答不得借豁免绕过引用（铁律 2）。"""
        model = FakeMessagesListChatModel(responses=[_assistant(answer), _assistant(answer)])
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            out = self._invoke(app, "给水管道流速多少合适")
        assert out["status"] == "failed"
        assert out["empty_citation_reflows"] == 1
        assert WARNING_NON_NORMATIVE_NO_CITATION not in out.get("warnings", [])

    def test_previous_turn_retrieval_does_not_force_reflow_on_capability_question(self, retriever) -> None:
        model = FakeMessagesListChatModel(responses=[
            _retrieve_call(retriever),
            _cite_call(["FIX 1.1.1"]),
            _assistant("设计秒流量按规范条文确定。"),
            _assistant("可以读取有文字层的 PDF；CAD 导出的矢量图纸大多没有文字层，暂时读不到内容。"),
        ])
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            first = self._invoke(app, "设计秒流量如何确定")
            second = self._invoke(app, "那我给你 CAD 设计图的 PDF，你能读取吗")
        assert first["status"] == "succeeded"
        assert second["status"] == "succeeded", second["errors"]
        assert second.get("empty_citation_reflows", 0) == 0
        assert second["citations"] == []
        # 本轮消息里不得出现校验器回流
        turn = current_turn_messages(second["messages"])
        assert not any(isinstance(m, ToolMessage) and m.name == "citation_validator" for m in turn)

    def test_previous_turn_citation_does_not_count_for_this_turn(self, retriever) -> None:
        """第 2 轮有检索但零引用：上一轮的 cite_clauses 不得让本轮通过。"""
        model = FakeMessagesListChatModel(responses=[
            _retrieve_call(retriever),
            _cite_call(["FIX 1.1.1"]),
            _assistant("第一轮回答。"),
            _assistant("", tool_calls=[{"name": "retrieve_clauses", "args": {"query": "排水", "top_k": 5}, "id": "c-r2"}]),
            _assistant("第二轮回答，未声明引用。"),
            _assistant("第二轮回流后仍未声明引用。"),
        ])
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            self._invoke(app, "设计秒流量如何确定")
            second = self._invoke(app, "排水管道怎么设计")
        assert second["status"] == "failed"
        assert second["empty_citation_reflows"] == 1

    def test_reflow_feedback_asks_for_a_user_facing_answer(self, retriever) -> None:
        model = FakeMessagesListChatModel(responses=[
            _retrieve_call(retriever),
            _assistant("给出回答但没有声明引用。"),
            _cite_call(["FIX 1.1.1"], call_id="c-cite-2"),
            _assistant("设计秒流量按规范条文确定。"),
        ])
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            out = self._invoke(app, "设计秒流量如何确定")
        feedback = [
            str(m.content) for m in out["messages"]
            if isinstance(m, ToolMessage) and m.name == "citation_validator"
        ]
        assert feedback and all("面向用户" in f and "结论重申" in f for f in feedback)
        # 上一轮有检索、本轮也检索过 → 选"本轮已返回相关条款"的文案
        assert "本轮 retrieve_clauses 已返回相关条款" in feedback[0]

    def test_warnings_reset_between_turns(self, retriever) -> None:
        model = FakeMessagesListChatModel(responses=[
            _assistant("你好，我是给排水设计助手，可以帮你查规范、做水力计算。"),
            _retrieve_call(retriever),
            _cite_call(["FIX 1.1.1"]),
            _assistant("设计秒流量按规范条文确定。"),
        ])
        with MemorySaver() as saver:
            app = build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver)
            first = self._invoke(app, "你好")
            second = self._invoke(app, "设计秒流量如何确定")
        assert first["warnings"] == [WARNING_NON_NORMATIVE_NO_CITATION]
        assert second["warnings"] == []


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


class TestMentionedClauseReflow:
    """P5-S50 附件问答实测：模型正文写"GB 50014-2021 第 7.4.3 条"却不调 cite_clauses。"""

    def test_mentioned_ids_are_matched_against_the_retrieval_context_only(self) -> None:
        from water_agent.graphs.agent_rag import _mentioned_source_ids

        answer = (
            "GB 50014-2021 第 7.4.3 条规定停留时间宜大于 5 min；GB50014—2021 第7.1.1条；"
            "GB 50014-2021 表 9.9.9（检索里没有）。"
        )
        assert _mentioned_source_ids(answer, ["GB50014-2021 7.4.3", "GB50014-2021 7.1.1"]) == [
            "GB50014-2021 7.4.3",
            "GB50014-2021 7.1.1",
        ]

    def test_reflow_lists_the_mentioned_clauses_but_never_declares_them(self) -> None:
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        from water_agent.graphs.agent_rag import make_citation_validator

        state = {
            "messages": [
                HumanMessage(content="曝气沉砂池停留时间取 2.5 min 合适吗"),
                AIMessage(content="", tool_calls=[{"id": "r1", "name": "retrieve_clauses", "args": {}}]),
                ToolMessage(
                    name="retrieve_clauses",
                    tool_call_id="r1",
                    content='{"hits": [{"source_id": "GB50014-2021 7.4.3"}, {"source_id": "GB50014-2021 7.4.4"}]}',
                ),
                AIMessage(content="GB 50014-2021 第 7.4.3 条规定停留时间宜大于 5 min，取值偏小。"),
            ]
        }
        out = make_citation_validator()(state)
        feedback = out["messages"][-1].content
        assert "GB50014-2021 7.4.3" in feedback and "7.4.4" not in feedback
        assert "调用 cite_clauses" in feedback
        assert "citations" not in out, "校验器只提示，绝不代为声明引用"


class TestFinalAnswerIsThisTurnsLastTextReply:
    """review 2026-09-30：收口只认"本轮最后一条不带 tool_calls 的 AIMessage 的非空文本"。

    此前有引用分支不查回答是否为空（cite 后空回答 → succeeded、answer=''）；零引用分支与
    finalize 取答案时扫描整条 thread，上一轮的回答会被拿来判定、被当成本轮答案返回。
    """

    @staticmethod
    def _invoke(app, text: str, thread: str = "t-final"):
        return app.invoke(
            {"messages": [HumanMessage(content=text)], **PER_TURN_RESET},
            config={"configurable": {"thread_id": thread}},
        )

    def _app(self, retriever, responses, saver, **kwargs):
        model = FakeMessagesListChatModel(responses=responses)
        return build_agentic_app(model=model, tools=_build_tools(retriever), checkpointer=saver, **kwargs)

    def test_valid_citation_with_an_empty_reply_fails(self, retriever) -> None:
        with MemorySaver() as saver:
            app = self._app(retriever, [_retrieve_call(retriever), _cite_call(["FIX 1.1.1"]), _assistant("")], saver)
            out = self._invoke(app, "设计秒流量如何确定")
        assert out["status"] == "failed"
        assert out["answer"] == ""
        assert any("文本回答" in error for error in out["errors"]), out["errors"]

    def test_whitespace_reply_is_not_an_answer(self, retriever) -> None:
        with MemorySaver() as saver:
            app = self._app(retriever, [_retrieve_call(retriever), _cite_call(["FIX 1.1.1"]), _assistant("  \n ")], saver)
            out = self._invoke(app, "设计秒流量如何确定")
        assert out["status"] == "failed"

    def test_text_attached_to_a_tool_call_is_not_the_answer(self, retriever) -> None:
        """"我来检索一下"这类附在 tool_call 上的前置文本不是回答。"""
        cite_with_preamble = _assistant(
            "我来声明引用。",
            tool_calls=[{"name": "cite_clauses", "args": {"article_ids": ["FIX 1.1.1"]}, "id": "c-cite"}],
        )
        with MemorySaver() as saver:
            app = self._app(retriever, [_retrieve_call(retriever), cite_with_preamble, _assistant("")], saver)
            out = self._invoke(app, "设计秒流量如何确定")
        assert out["status"] == "failed"
        assert out["answer"] == ""

    def test_previous_turns_answer_is_never_this_turns_answer(self, retriever) -> None:
        with MemorySaver() as saver:
            app = self._app(
                retriever,
                [
                    _assistant("你好，我是给排水设计助手。"),
                    _retrieve_call(retriever),
                    _cite_call(["FIX 1.1.1"], call_id="c-cite-2"),
                    _assistant(""),
                ],
                saver,
            )
            first = self._invoke(app, "你好")
            second = self._invoke(app, "设计秒流量如何确定")
        assert first["status"] == "succeeded"
        assert second["status"] == "failed"
        assert "你好，我是给排水设计助手" not in second["answer"]

    def test_a_turn_that_fails_inside_the_loop_returns_no_stale_answer(self, retriever) -> None:
        """第 2 轮在循环里超预算失败：返回的 answer 不得是第 1 轮的回答。"""
        runaway = _assistant(
            "",
            tool_calls=[{"name": "retrieve_clauses", "args": {"query": "排水", "top_k": 5}, "id": "c-loop"}],
        )
        with MemorySaver() as saver:
            app = self._app(
                retriever,
                [_assistant("你好，我是给排水设计助手。"), *([runaway] * 10)],
                saver,
                max_tool_turns=2,
            )
            self._invoke(app, "你好")
            second = self._invoke(app, "排水管道怎么设计")
        assert second["status"] == "failed"
        assert second["answer"] == ""
