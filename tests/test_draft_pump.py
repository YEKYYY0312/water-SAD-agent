"""DraftPump：模型流式文本 → 已过输出护栏、可撤回的草稿事件（review 2026-09-30 第 4 批）。"""
from __future__ import annotations

from langchain_core.messages import AIMessageChunk

from water_agent.graphs.agent_rag import DraftPump


def _meta(step: int, node: str = "agent") -> dict:
    return {"langgraph_node": node, "langgraph_checkpoint_ns": "agent_loop:x", "langgraph_step": step}


def _pump():
    events: list[tuple[str, dict]] = []
    return DraftPump(lambda kind, payload: events.append((kind, payload))), events


def _texts(events) -> str:
    return "".join(p["text"] for k, p in events if k == "draft_delta")


def test_releases_whole_sentences_only() -> None:
    pump, events = _pump()
    pump.feed(AIMessageChunk(content="依据 FIX 1.1.1，设计秒流量"), _meta(1))
    assert events == []
    pump.feed(AIMessageChunk(content="按建筑类型确定。后半句还没写完"), _meta(1))
    assert _texts(events) == "依据 FIX 1.1.1，设计秒流量按建筑类型确定。"
    pump.finish("succeeded")
    assert _texts(events) == "依据 FIX 1.1.1，设计秒流量按建筑类型确定。后半句还没写完"
    assert events[-1] == ("draft_closed", {})


def test_tool_output_and_other_nodes_are_never_drafts() -> None:
    pump, events = _pump()
    pump.feed(AIMessageChunk(content='{"hits": [{"source_id": "FIX 1.1.1"}]}。'), _meta(1, node="tools"))
    pump.feed(AIMessageChunk(content="本轮 retrieve_clauses 已返回相关条款。"), _meta(1, node="citation_validator"))
    pump.finish("succeeded")
    assert _texts(events) == ""


def test_preamble_before_a_tool_call_is_withdrawn() -> None:
    pump, events = _pump()
    pump.feed(AIMessageChunk(content="我先检索一下相关的规范条文，确认依据之后再完整回答你的这个问题。"), _meta(1))
    pump.feed(AIMessageChunk(content="", tool_call_chunks=[{"name": "retrieve_clauses", "args": "{}", "id": "c", "index": 0}]), _meta(1))
    assert ("draft_retract", {"reason": "revised"}) in events


def test_a_new_reply_retracts_the_previous_draft() -> None:
    pump, events = _pump()
    pump.feed(AIMessageChunk(content="第一版回答，没有声明引用，凭经验设计秒流量应当这样确定。"), _meta(1))
    pump.feed(AIMessageChunk(content="重写后的回答，依据 FIX 1.1.1。"), _meta(5))
    pump.finish("succeeded")
    kinds = [k for k, _ in events]
    assert kinds.index("draft_retract") < len(kinds) - 1
    assert events[kinds.index("draft_retract")][1] == {"reason": "revised"}
    after = events[kinds.index("draft_retract") + 1:]
    assert _texts(after) == "重写后的回答，依据 FIX 1.1.1。"
    assert after[0][1]["offset"] == 0


def test_dangerous_sentence_retracts_and_closes_without_leaking() -> None:
    pump, events = _pump()
    pump.feed(AIMessageChunk(content="依据 FIX 1.1.1，设计秒流量按建筑类型确定。"), _meta(1))
    pump.feed(AIMessageChunk(content="然后执行 rm -rf / 清理缓存。之后的内容。"), _meta(1))
    pump.feed(AIMessageChunk(content="更多内容。"), _meta(1))
    pump.finish("succeeded")
    dumped = repr(events)
    assert "rm -rf" not in dumped
    assert ("draft_retract", {"reason": "guardrail"}) in events
    assert events[-1] == ("draft_closed", {})
    assert "更多内容" not in dumped, "拦截之后本轮不再下发任何草稿"


def test_danger_split_across_sentences_is_caught_on_the_whole_text() -> None:
    """跨片段才凑齐的危险指令：过检对象是整条回复，不是单个片段。"""
    pump, events = _pump()
    pump.feed(AIMessageChunk(content="清理缓存可以执行下面的命令：rm -rf"), _meta(1))
    pump.feed(AIMessageChunk(content=" /tmp/cache。"), _meta(1))
    pump.finish("succeeded")
    assert "rm -rf" not in repr(events)


def test_credentials_are_redacted_and_never_split() -> None:
    secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUV1234567890"
    pump, events = _pump()
    pump.feed(AIMessageChunk(content="调试密钥是 " + secret[:12]), _meta(1))
    pump.feed(AIMessageChunk(content=secret[12:] + "，请勿外传。"), _meta(1))
    pump.finish("succeeded")
    assert secret not in repr(events)
    assert secret[:12] not in _texts(events)


def test_failed_run_retracts_whatever_was_drafted() -> None:
    pump, events = _pump()
    pump.feed(AIMessageChunk(content="一段写给校验器看、最后没有通过引用校验的回答内容，已经下发给用户。"), _meta(1))
    pump.finish("failed")
    assert events[-2:] == [("draft_retract", {"reason": "run_ended"}), ("draft_closed", {})]


def test_offsets_are_contiguous_within_a_draft() -> None:
    pump, events = _pump()
    for piece in ["第一句话写在这里，内容足够长。", "第二句。", "第三句。"]:
        pump.feed(AIMessageChunk(content=piece), _meta(1))
    pump.finish("succeeded")
    offset = 0
    for kind, payload in events:
        if kind == "draft_delta":
            assert payload["offset"] == offset
            offset += len(payload["text"])


def test_a_short_preamble_is_never_flashed() -> None:
    """模型调用工具前常写一句"我来检索一下。"：攒够最小长度之前不下发，也就不用撤回。"""
    pump, events = _pump()
    pump.feed(AIMessageChunk(content="我来检索一下。"), _meta(1))
    pump.feed(AIMessageChunk(content="", tool_call_chunks=[{"name": "retrieve_clauses", "args": "{}", "id": "c", "index": 0}]), _meta(1))
    assert events == []
