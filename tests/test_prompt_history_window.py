"""发给模型的会话历史窗口（review 2026-09-30 第 4 批）。

此前每次模型调用都把整条 thread（每轮检索原文、工具调用、校验器回流）全部发出去，prompt
随轮数线性增长；窗口只改发给模型的视图，checkpoint 原样不动。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from tests.test_agentic_runtime_robustness import _cite, _retrieve, _session
from water_agent.harness.doubles import ScriptedChatModel
from water_agent.prompting import PromptAssembler
from water_agent.prompting.history import MAX_PROMPT_HISTORY_TURNS, window_messages
from water_agent.session import default_checkpoint_serde


def _turn(n: int, *, answer: str | None = None) -> list:
    call = {"name": "retrieve_clauses", "args": {"query": f"q{n}"}, "id": f"c{n}", "type": "tool_call"}
    turn = [
        HumanMessage(content=f"问题 {n}", id=f"h{n}"),
        AIMessage(content="", tool_calls=[call], id=f"a{n}"),
        ToolMessage(content=json.dumps({"hits": [{"source_id": f"X {n}", "text": "条文" * 200}]}), tool_call_id=f"c{n}",
                    name="retrieve_clauses", id=f"t{n}"),
    ]
    if answer is not None:
        turn.append(AIMessage(content=answer, id=f"r{n}"))
    return turn


def test_earlier_turns_keep_only_question_and_final_answer() -> None:
    messages = _turn(1, answer="答 1") + _turn(2, answer="答 2") + _turn(3)
    window = window_messages(messages)
    assert [m.content for m in window[:4]] == ["问题 1", "答 1", "问题 2", "答 2"]
    assert window[4:] == messages[-3:], "本轮原样保留（工具循环在这里）"
    assert not any(isinstance(m, ToolMessage) for m in window[:4])


def test_window_caps_the_number_of_earlier_turns() -> None:
    messages: list = []
    for n in range(MAX_PROMPT_HISTORY_TURNS + 5):
        messages += _turn(n, answer=f"答 {n}")
    messages += _turn(999)
    window = window_messages(messages)
    questions = [m.content for m in window if isinstance(m, HumanMessage)]
    assert len(questions) == MAX_PROMPT_HISTORY_TURNS + 1
    assert questions[-1] == "问题 999"
    assert questions[0] == f"问题 {5}", "保留的是最近的几轮"


def test_window_caps_characters_of_earlier_turns() -> None:
    messages = _turn(1, answer="长" * 9000) + _turn(2, answer="长" * 9000) + _turn(3)
    window = window_messages(messages, max_chars=10_000)
    assert [m.content for m in window if isinstance(m, HumanMessage)] == ["问题 2", "问题 3"]


def test_only_the_latest_runtime_preamble_is_kept() -> None:
    messages = (
        [HumanMessage(content="[运行时上下文]\n旧", id="p1")] + _turn(1, answer="答 1")
        + [HumanMessage(content="[运行时上下文]\n新", id="p2")] + _turn(2)
    )
    window = window_messages(messages)
    preambles = [m.content for m in window if isinstance(m, HumanMessage) and m.content.startswith("[运行时上下文]")]
    assert preambles == ["[运行时上下文]\n新"]


def test_earlier_answers_are_resent_as_plain_text_without_dangling_tool_calls() -> None:
    """checkpoint 里的旧回答若挂着畸形工具调用，原样发出就是没有应答的 tool_call（provider 400）。"""
    broken = AIMessage(content="我来检索一下。", id="b1", invalid_tool_calls=[
        {"name": "retrieve_clauses", "args": '{"query": ', "id": "call_bad", "error": "bad json", "type": "invalid_tool_call"}
    ])
    messages = [HumanMessage(content="问题 1", id="h1"), broken] + _turn(2)
    window = window_messages(messages)
    resent = window[1]
    assert isinstance(resent, AIMessage) and resent.content == "我来检索一下。"
    assert not resent.tool_calls and not resent.invalid_tool_calls


def test_assembler_sends_the_window_not_the_whole_thread() -> None:
    messages: list = []
    for n in range(20):
        messages += _turn(n, answer=f"答 {n}")
    messages += _turn(99)
    prompt = PromptAssembler()({"messages": messages})
    assert len(prompt) < len(messages) / 2
    assert sum(1 for m in prompt if isinstance(m, ToolMessage)) == 1, "只有本轮的工具结果"


def test_prompt_size_stops_growing_with_conversation_length(tmp_path: Path) -> None:
    """真实装配：每轮新 session（同 Web），第 N 轮发给模型的消息数有上界。"""
    saver = InMemorySaver(serde=default_checkpoint_serde())
    sizes: list[int] = []
    with ExitStack() as stack:
        for n in range(MAX_PROMPT_HISTORY_TURNS + 4):
            model = ScriptedChatModel(steps=[_retrieve("设计秒流量"), _cite("FIX 1.1.1"), {"content": f"依据 FIX 1.1.1 回答第 {n} 问。"}])
            session = _session(tmp_path, stack, model, saver)
            result = asyncio.run(session.aask(f"第 {n} 问：设计秒流量如何确定", run_id=f"r{n}"))
            assert result["status"] == "succeeded"
            sizes.append(len(model.state["received"][0]))
    assert sizes[-1] == sizes[-2] == sizes[-3], sizes
    assert sizes[-1] <= 2 * MAX_PROMPT_HISTORY_TURNS + 3


def test_cross_turn_citation_still_works_through_the_window(tmp_path: Path) -> None:
    """窗口不发旧检索原文，但引用可追溯与校验读的是完整 checkpoint：仍可引用前几轮的条款。"""
    saver = InMemorySaver(serde=default_checkpoint_serde())
    with ExitStack() as stack:
        first = ScriptedChatModel(steps=[_retrieve("设计秒流量"), _cite("FIX 1.1.1"), {"content": "依据 FIX 1.1.1。"}])
        asyncio.run(_session(tmp_path, stack, first, saver).aask("设计秒流量如何确定", run_id="r1"))
        second = ScriptedChatModel(steps=[_cite("FIX 1.1.1"), {"content": "依据 FIX 1.1.1 再说明一次。"}])
        result = asyncio.run(_session(tmp_path, stack, second, saver).aask("那条规定再说一下", run_id="r2"))
    assert result["status"] == "succeeded"
    assert [c["source_id"] for c in result["citations"]] == ["FIX 1.1.1"]
    assert not any(isinstance(m, ToolMessage) and m.name == "retrieve_clauses" for m in second.state["received"][0])
