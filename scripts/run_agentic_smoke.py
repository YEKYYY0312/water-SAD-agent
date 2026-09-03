"""Step 6+8 冒烟：假模型驱动的 Agentic RAG 外层循环端到端。

预置两轮：第一轮 ``tool_calls=[retrieve_clauses]``，第二轮 final answer。
Step 8 增强：
- ``prompt=PromptAssembler(tool_registry=registry)``（可调用装配器，动态系统提示）；
- 运行时上下文投影：上下文变化才注入一条用户消息，未变化不重复注入；
  断言同 thread 两轮消息轨迹里「运行时上下文」段只出现一次。

全程离线，不依赖任何 Key。
"""
from __future__ import annotations

from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from water_agent.graphs.agent_rag import build_agentic_app
from water_agent.prompting import PromptAssembler
from water_agent.prompting.runtime_context import (
    RuntimeContextProjection,
    default_context_builder,
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

CONTEXT_MARKER = "[运行时上下文]"


def main() -> int:
    clauses = load_clauses_jsonl(FIXTURE_PATH)
    retriever = InMemoryRetriever(clauses)

    registry = ToolRegistry()
    registry.register(build_retrieve_clauses_tool(retriever))
    registry.register(build_cite_clauses_tool())
    registry.register(calc_velocity_spec)
    tools = [tool_spec_to_base_tool(registry, name) for name in registry.list_all_names()]

    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve_clauses",
                        "args": {"query": "给水管道的设计秒流量", "top_k": 5},
                        "id": "c-retrieve",
                    }
                ],
            ),
            AIMessage(content="设计秒流量应按用水定额与用水人数计算。"),
            AIMessage(content="追问的最终回答。"),
        ]
    )

    # Step 8：可调用装配器作为系统提示 + 运行时上下文投影（变化才注入）
    assembler = PromptAssembler(tool_registry=registry)
    projection = RuntimeContextProjection()
    context = default_context_builder(session_refs=["smoke-step8"])

    with MemorySaver() as saver:
        app = build_agentic_app(model=model, tools=tools, checkpointer=saver, prompt=assembler)

        # 第一轮：上下文首次出现 → 注入一次
        ctx_text = projection.project(context)
        assert ctx_text is not None, "首次投影必须注入运行时上下文"
        first_messages = [
            HumanMessage(content=f"{CONTEXT_MARKER}\n{ctx_text}"),
            HumanMessage(content="给水管道的设计秒流量如何确定？"),
        ]
        out1 = app.invoke(
            {"messages": first_messages},
            config={"configurable": {"thread_id": "smoke-step8"}},
        )
        assert out1.get("status") == "succeeded", f"第一轮 status={out1.get('status')}"

        # 第二轮：上下文未变化 → project 返回 None，不重复注入
        ctx_text2 = projection.project(context)
        assert ctx_text2 is None, "上下文未变化时不得重复注入"
        out2 = app.invoke(
            {"messages": [HumanMessage(content="请再补充说明一下。")]},
            config={"configurable": {"thread_id": "smoke-step8"}},
        )

    messages = out2.get("messages", [])
    trajectory = [f"{type(m).__name__}: {str(m.content)[:60]!r}" for m in messages]
    print("消息轨迹：")
    for line in trajectory:
        print(f"  {line}")

    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    has_tool_call = any(
        isinstance(m, AIMessage) and m.tool_calls for m in messages
    )
    final_ai = next(
        (m.content for m in reversed(messages) if isinstance(m, AIMessage) and m.content),
        "",
    )
    assert has_tool_call, "缺少模型发起的工具调用 AIMessage"
    assert tool_msgs, "缺少 ToolNode 回填的 ToolMessage"
    assert final_ai, "缺少最终回答"
    assert out2.get("status") == "succeeded", f"status={out2.get('status')}, errors={out2.get('errors')}"

    # Step 8 验收：动态上下文段在消息轨迹中只出现一次（变化才注入）
    context_occurrences = [
        m for m in messages if isinstance(m, HumanMessage) and CONTEXT_MARKER in str(m.content)
    ]
    assert len(context_occurrences) == 1, (
        f"运行时上下文段必须只注入一次，实际 {len(context_occurrences)} 次"
    )

    # 装配器生成的系统提示经由 prompt runnable 注入模型（不持久化进 state），此处不再统计
    print(f"最终状态: {out2.get('status')}")
    print(f"最终回答: {final_ai}")
    print("OK: agentic RAG 外层循环 + 动态提示装配/上下文投影冒烟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
