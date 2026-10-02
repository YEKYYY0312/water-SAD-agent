"""发给模型的会话历史窗口（review 2026-09-30 第 4 批）。

checkpoint 里存的是整条 thread：每轮的检索结果（单条最多 10 KB）、工具调用、校验器回流消息
都原样保留。此前每次模型调用都把它们全部发出去，prompt 随轮数线性增长——长会话迟早超出
上下文窗口，之后这条 thread 的每一轮都失败，而且历史只增不减、无法自愈。

窗口只改"发给模型的视图"，**不改 checkpoint**（P5 任务书：摘要不替代原始 checkpoint）：

- 本轮（最后一个问题及其之后的全部消息）原样保留——工具循环、校验器回流都在这里；
- 之前的轮次只保留"问题 + 该轮最终回答"，工具调用与结果、校验器回流消息都不再发送。
  引用可追溯检查与引用校验读的是 checkpoint 里的完整消息，不受影响：模型仍可引用前几轮
  检索到的条款，只是看不到原文（需要原文就重新检索）；
- 之前的轮次按"最近优先"装入，最多 ``MAX_PROMPT_HISTORY_TURNS`` 轮、
  ``MAX_PROMPT_HISTORY_CHARS`` 字符；
- 运行时上下文前言只保留最近一份。

保留下来的回答重建为纯文本消息：checkpoint 里的旧回答可能挂着畸形工具调用
（``invalid_tool_calls``），原样发出去就是一个没有应答的 tool_call，OpenAI 兼容接口会拒绝
整个请求，这条 thread 从此每轮都失败。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

RUNTIME_CONTEXT_PREFIX = "[运行时上下文]"
#: 之前的轮次最多保留几轮（问题 + 最终回答）。
MAX_PROMPT_HISTORY_TURNS = 8
#: 之前的轮次总字符上限（粗估 1 字符 ≈ 1 token；本轮不计入，也不截断）。
MAX_PROMPT_HISTORY_CHARS = 16_000


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _is_preamble(message: BaseMessage) -> bool:
    return isinstance(message, HumanMessage) and _text(message).startswith(RUNTIME_CONTEXT_PREFIX)


def _final_answer(turn: Sequence[BaseMessage]) -> AIMessage | None:
    for message in reversed(turn):
        if isinstance(message, AIMessage) and not message.tool_calls and _text(message).strip():
            return AIMessage(content=_text(message), id=message.id)
    return None


def window_messages(
    messages: Sequence[Any],
    *,
    max_turns: int = MAX_PROMPT_HISTORY_TURNS,
    max_chars: int = MAX_PROMPT_HISTORY_CHARS,
) -> list[Any]:
    """返回发给模型的消息视图（见模块说明）；没有可识别的问题时原样返回。"""

    preamble: BaseMessage | None = None
    turns: list[list[BaseMessage]] = []
    for message in messages:
        if _is_preamble(message):
            preamble = message
        elif isinstance(message, HumanMessage):
            turns.append([message])
        elif turns:
            turns[-1].append(message)
    if not turns:
        return list(messages)

    kept: list[list[BaseMessage]] = []
    used = 0
    for turn in reversed(turns[:-1]):
        if len(kept) >= max_turns:
            break
        answer = _final_answer(turn)
        entry: list[BaseMessage] = [turn[0]] + ([answer] if answer is not None else [])
        size = sum(len(_text(item)) for item in entry)
        if used + size > max_chars:
            break
        kept.append(entry)
        used += size

    window: list[Any] = [preamble] if preamble is not None else []
    for entry in reversed(kept):
        window.extend(entry)
    window.extend(turns[-1])
    return window


__all__ = [
    "MAX_PROMPT_HISTORY_CHARS",
    "MAX_PROMPT_HISTORY_TURNS",
    "RUNTIME_CONTEXT_PREFIX",
    "window_messages",
]
