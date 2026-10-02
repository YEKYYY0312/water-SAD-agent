"""Harness 用测试替身。"""
from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from water_agent.domain.models import Citation, GeneratedAnswer, RetrievedClause
from water_agent.generation.base import Generator


class BadCitationGenerator(Generator):
    """故意返回不在检索上下文中的引用，用于触发引用校验失败。"""

    def generate(self, query: str, context: list[RetrievedClause]) -> GeneratedAnswer:
        citations = [
            Citation(
                spec_id="FAKE",
                article="0.0.0",
                title="伪造条款",
                text_snippet="This citation does not exist in context.",
            )
        ]
        return GeneratedAnswer(
            answer=f"这是一个包含无效引用的回答：{query}",
            citations=citations,
            intent="rag",
        )


class ScriptExhausted(RuntimeError):
    """脚本模型被调用的次数超过脚本步数：循环比用例作者预期的多转了一圈。"""


class ScriptedChatModel(BaseChatModel):
    """按 agentic 用例脚本逐次响应的离线模型（mock-agentic 评测用）。

    每次调用返回脚本的下一步——带 ``tool_calls`` 的 AIMessage 或纯文本回答；超出脚本
    即抛 :class:`ScriptExhausted`，绝不循环复用（``FakeMessagesListChatModel`` 会从头
    再来，多转的那一圈就被掩盖了）。``state`` 记录调用次数与每次实际收到的消息，
    供"脚本恰好用完""某内容不得进入模型输入"一类断言使用。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    steps: list[dict[str, Any]]
    id_prefix: str = "script"
    state: dict[str, Any] = Field(default_factory=lambda: {"calls": 0, "received": []})

    @property
    def _llm_type(self) -> str:
        return "scripted-eval"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        index = self.state["calls"]
        self.state["calls"] = index + 1
        self.state["received"].append(list(messages))
        if index >= len(self.steps):
            raise ScriptExhausted(
                f"脚本只有 {len(self.steps)} 步，模型被第 {index + 1} 次调用（循环比预期多转了一圈）"
            )
        step = self.steps[index]
        tool_calls = [
            {
                "name": call["name"],
                "args": dict(call.get("args") or {}),
                "id": f"{self.id_prefix}-{index}-{position}",
                "type": "tool_call",
            }
            for position, call in enumerate(step.get("tool_calls") or [])
        ]
        message = AIMessage(content=str(step.get("content") or ""), tool_calls=tool_calls)
        return ChatResult(generations=[ChatGeneration(message=message)])
