"""多轮会话记忆图（Step 3）：消息累积 + checkpointer + thread_id。

本步最小闭环：只做"消息记录与多轮可达"，不接入 agentic 工具循环。
- ``messages`` 必须 ``Annotated[list[BaseMessage], add_messages]``：第二次写入追加而非覆盖。
- ``answer_node`` 为确定性占位回答（自报已收到第 N 条消息），真实 LLM 在 Step 7 接入。
- ``build_chat_app(*, checkpointer=None)``：无 checkpointer 时是"每问全新会话"；
  传入 MemorySaver/SqliteSaver 后按 thread_id 提供持久会话隔离。
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


class ChatState(TypedDict):
    """多轮会话共享状态。messages 必须用 add_messages 累积历史。"""

    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    answer: str
    citations: list[dict[str, Any]]
    status: str


def answer_node(state: ChatState) -> dict:
    """占位回答节点：报告当前已收到的消息数，证明 messages 在跨轮累积。

    Step 7 将把这里替换为真实 LLM 生成；本步只做记忆闭环，不引入模型。
    """
    count = len(state.get("messages", []))
    return {
        "answer": f"已收到第 {count} 条消息",
        "status": "succeeded",
    }


def build_chat_app(*, checkpointer=None):
    """构建最小多轮会话应用。

    ``checkpointer``：None（每问全新）；MemorySaver（测试/进程内）；
    SqliteSaver（跨进程持久化）。thread_id 经 invoke config 注入。
    """
    graph = StateGraph(ChatState)
    graph.add_node("answer", answer_node)
    graph.add_edge(START, "answer")
    graph.add_edge("answer", END)
    return graph.compile(checkpointer=checkpointer)