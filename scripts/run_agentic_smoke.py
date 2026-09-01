"""Step 6 冒烟：假模型驱动的 Agentic RAG 外层循环端到端。

预置两轮：第一轮 ``tool_calls=[retrieve_clauses]``，第二轮 final answer。
断言消息轨迹：HumanMessage → AIMessage(工具调用) → ToolMessage(检索结果) → 最终回答，
且最终 status=succeeded。全程离线，不依赖任何 Key。
"""
from __future__ import annotations

import sys
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
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
        ]
    )

    with MemorySaver() as saver:
        app = build_agentic_app(model=model, tools=tools, checkpointer=saver)
        out = app.invoke(
            {"messages": [HumanMessage(content="给水管道的设计秒流量如何确定？")]},
            config={"configurable": {"thread_id": "smoke-step6"}},
        )

    messages = out.get("messages", [])
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
    assert out.get("status") == "succeeded", f"status={out.get('status')}, errors={out.get('errors')}"

    print(f"\n最终状态: {out.get('status')}")
    print(f"最终回答: {final_ai}")
    print("OK: agentic RAG 外层循环冒烟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
