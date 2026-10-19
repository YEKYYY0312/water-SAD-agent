"""回合污点、缓存拒绝、记忆拦截、披露行与终答网址追溯（P7-S11-A06 / A07）。

S11 没有任何生产工具声明 ``result_source=WEB_CONTENT``：这里用夹具工具经**生产图**（``build_agentic_app``）验证
污点机制本身；``web_search`` 是 S12 的。引用 17 号 SDD 的不变量写 ``SDD17-I14``（不带 ``P7-`` 前缀）。
"""
from __future__ import annotations

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field

from tests.test_agent_rag import FIXTURE_PATH
from water_agent.external.taint import (
    DISCLOSURE_LINE,
    WEB_URL_UNVERIFIED_FEEDBACK,
    extract_urls,
    normalize_url,
    unverified_urls,
    web_tool_names,
)
from water_agent.graphs.agent_rag import PER_TURN_RESET, build_agentic_app
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.runtime.governance.cache import REASON_WEB_CONTENT_USED, evaluate_store_eligibility
from water_agent.runtime.governance.contracts import CallCategory, Capability, SourceTag
from water_agent.runtime.governance.pipeline import GuardrailPipeline
from water_agent.tools.base import ToolSpec
from water_agent.tools.cite import build_cite_clauses_tool
from water_agent.tools.ds_adapters import tool_spec_to_base_tool
from water_agent.tools.registry import ToolRegistry
from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool

WEB_URL = "https://example.com/pipe-velocity"
OTHER_URL = "https://evil.example/fabricated"


class _WebArgs(BaseModel):
    query: str = Field(min_length=1, max_length=64, description="夹具查询词")


def _web_spec(result_source: SourceTag | None = SourceTag.WEB_CONTENT) -> ToolSpec:
    return ToolSpec(
        name="fixture_web_lookup",
        description="Use when a test needs a stand-in for a web lookup tool. Do not use for anything outside tests.",
        args_model=_WebArgs,
        execute=lambda args: {"results": [{"title": "t", "snippet": "管径选择见规范", "url": WEB_URL}], "urls": [WEB_URL]},
        result_source=result_source,
    )


def _app(responses, *, web_result_source: SourceTag | None = SourceTag.WEB_CONTENT, cache_service=None):
    clauses = load_clauses_jsonl(FIXTURE_PATH)
    retriever = InMemoryRetriever(clauses)
    # 夹具工具不在治理清单里：按既有惯例由装配期声明能力（fail closed 对未声明的工具照常生效）
    pipeline = GuardrailPipeline(declared_capabilities={"fixture_web_lookup": Capability.CALCULATE})
    registry = ToolRegistry(pipeline=pipeline)
    registry.register(build_retrieve_clauses_tool(retriever))
    registry.register(build_cite_clauses_tool(lambda: set()))
    registry.register(_web_spec(web_result_source))
    tools = [tool_spec_to_base_tool(registry, name) for name in registry.list_all_names()]
    return build_agentic_app(
        model=FakeMessagesListChatModel(responses=list(responses)),
        tools=tools,
        checkpointer=MemorySaver(),
        cache_service=cache_service,
    )


def _web_call(call_id: str = "c-web") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "fixture_web_lookup", "args": {"query": "管径"}, "id": call_id}])


def _config(thread: str = "t1") -> dict:
    return {"configurable": {"thread_id": thread}}


# -- ToolSpec 字段 --------------------------------------------------------------------------------------------


def test_result_source_defaults_to_none_and_existing_tools_are_unchanged() -> None:
    """P7-S11-A06 ``ToolSpec.result_source`` 缺省 ``None``：现有工具一律不声明，不产生污点。"""

    assert ToolSpec.model_fields["result_source"].default is None
    clauses = load_clauses_jsonl(FIXTURE_PATH)
    assert build_retrieve_clauses_tool(InMemoryRetriever(clauses)).result_source is None
    registry = ToolRegistry()
    registry.register(_web_spec())
    registry.register(build_retrieve_clauses_tool(InMemoryRetriever(clauses)))
    assert web_tool_names(registry) == frozenset({"fixture_web_lookup"})


# -- 污点置位与复位 -------------------------------------------------------------------------------------------


def test_a_web_tool_sets_the_taint_and_the_program_appends_the_disclosure_line() -> None:
    """P7-S11-A06 工具声明了 ``WEB_CONTENT`` → state ``web_tainted=True``；finalize 由**程序**在终答末尾追加披露行，
    模型自己写的文本里没有它。"""

    app = _app([_web_call(), AIMessage(content=f"建议参考 {WEB_URL} 上的经验值。")])
    out = app.invoke({"messages": [HumanMessage(content="管径怎么选")]}, config=_config())
    assert out["web_tainted"] is True
    assert out["answer"].endswith(DISCLOSURE_LINE)
    assert DISCLOSURE_LINE not in out["messages"][-1].content, "披露行不写进会话里的模型消息"
    assert out["status"] in ("succeeded", "failed")


def test_no_taint_without_a_web_tool_and_the_answer_is_unchanged() -> None:
    """P7-S11-A06 / SDD17-I14 的反面：没调联网工具的回合，``web_tainted`` 为假、答案不加披露行（缺省路径逐字不变）。"""

    app = _app([AIMessage(content="你好，我可以帮你查规范。")])
    out = app.invoke({"messages": [HumanMessage(content="你好")]}, config=_config())
    assert not out.get("web_tainted")
    assert DISCLOSURE_LINE not in out["answer"]


def test_taint_is_per_turn_but_web_refs_accumulate_across_turns() -> None:
    """P7-S11-A06 ``web_tainted`` 逐轮复位（``PER_TURN_RESET``）；``web_refs`` 跨轮累积（不在复位表里）。"""

    assert "web_tainted" in PER_TURN_RESET and PER_TURN_RESET["web_tainted"] is False
    assert "web_refs" not in PER_TURN_RESET

    app = _app([_web_call(), AIMessage(content=f"见 {WEB_URL}。"), AIMessage(content="第二问：你好，能力说明。")])
    cfg = _config("two-turns")
    first = app.invoke({"messages": [HumanMessage(content="管径怎么选")], **PER_TURN_RESET}, config=cfg)
    assert first["web_tainted"] is True
    second = app.invoke({"messages": [HumanMessage(content="你好")], **PER_TURN_RESET}, config=cfg)
    assert not second.get("web_tainted"), "下一回合污点复位"
    assert DISCLOSURE_LINE not in second["answer"]
    assert normalize_url(WEB_URL) in second["web_refs"], "web_refs 跨轮保留"


# -- 缓存拒绝（SDD17-I14） -------------------------------------------------------------------------------------


def test_web_tainted_turns_are_never_eligible_for_the_answer_cache() -> None:
    """P7-S11-A06 / SDD17-I14 ``web_tainted`` 的回合缓存写入资格恒为 false，稳定拒绝原因 ``web_content_used``；
    其余条件全满足时没有污点才可存。"""

    common = {
        "category": CallCategory.RAG_GENERATION,
        "temperature": 0.0,
        "citation_verified": True,
        "output_guardrails_passed": True,
        "spec_versions_known": True,
        "answer": "回答",
    }
    assert evaluate_store_eligibility(**common).eligible is True
    tainted = evaluate_store_eligibility(**common, web_tainted=True)
    assert tainted.eligible is False and tainted.reason == REASON_WEB_CONTENT_USED == "web_content_used"
    assert evaluate_store_eligibility(**common, web_tainted=False).eligible is True


# -- 终答网址追溯 ---------------------------------------------------------------------------------------------


def test_url_helpers_normalize_and_compare() -> None:
    """P7-S11-A07 网址抽取与规范化：去 fragment、小写 scheme/host、去尾部标点；比较按规范化形态。"""

    assert normalize_url("HTTPS://Example.COM/a#frag") == "https://example.com/a"
    assert extract_urls("见 https://example.com/a。另见（http://b.example/x）。") == [
        "https://example.com/a",
        "http://b.example/x",
    ]
    refs = {normalize_url(WEB_URL)}
    assert unverified_urls(f"参考 {WEB_URL}", refs, user_text="") == []
    assert unverified_urls(f"参考 {OTHER_URL}", refs, user_text="") == [normalize_url(OTHER_URL)]
    # 用户自己消息里的网址可以复述
    assert unverified_urls(f"你提到的 {OTHER_URL}", refs, user_text=f"看看 {OTHER_URL}") == []


def test_a_fabricated_url_is_reflowed_once_then_fails() -> None:
    """P7-S11-A07 终答里出现不在 ``web_refs`` ∪ 用户消息的网址 → 回流一次；再次出现 → ``failed``。"""

    app = _app([_web_call(), AIMessage(content=f"依据 {OTHER_URL}。"), AIMessage(content=f"仍然依据 {OTHER_URL}。")])
    out = app.invoke({"messages": [HumanMessage(content="管径怎么选")]}, config=_config("fab"))
    assert out["status"] == "failed"
    assert any(WEB_URL_UNVERIFIED_FEEDBACK in str(m.content) for m in out["messages"] if isinstance(m, ToolMessage))
    assert any("联网" in error or "网址" in error for error in out["errors"])


def test_a_fabricated_url_then_a_genuine_one_succeeds() -> None:
    """P7-S11-A07 回流后改用真实检索到的网址 → 收口成功（回流机制可纠正）。"""

    app = _app([_web_call(), AIMessage(content=f"依据 {OTHER_URL}。"), AIMessage(content=f"改为依据 {WEB_URL}。")])
    out = app.invoke({"messages": [HumanMessage(content="管径怎么选")]}, config=_config("fix"))
    assert out["status"] == "succeeded"
    assert WEB_URL in out["answer"]


def test_a_genuine_url_passes_without_a_reflow() -> None:
    """P7-S11-A07 网址来自本 thread 的 ``web_refs`` → 直接放行，不回流。"""

    app = _app([_web_call(), AIMessage(content=f"建议参考 {WEB_URL}。")])
    out = app.invoke({"messages": [HumanMessage(content="管径怎么选")]}, config=_config("ok"))
    assert out["status"] == "succeeded"
    assert out.get("retry_count", 0) == 0


def test_urls_are_not_checked_when_nothing_web_was_used() -> None:
    """P7-S11-A07 无污点且无 ``web_refs`` 的回合不校验网址：缺省路径行为逐字不变（回答里有网址照样 succeeded）。"""

    app = _app([AIMessage(content="规范可在 https://example.org/spec 查阅。")])
    out = app.invoke({"messages": [HumanMessage(content="规范在哪看")]}, config=_config("plain"))
    assert out["status"] == "succeeded"
    assert out["answer"] == "规范可在 https://example.org/spec 查阅。"


def test_web_urls_never_enter_the_citable_source_set() -> None:
    """P7-S11-A07 R1：``web_refs`` 永不进"本 thread 检索到的 source_id 集合"，``cite_clauses`` 不接受网址。"""

    app = _app([
        _web_call(),
        AIMessage(content="", tool_calls=[{"name": "cite_clauses", "args": {"article_ids": [WEB_URL]}, "id": "c-cite"}]),
        AIMessage(content="我引用了网页。"),
        AIMessage(content="我改为不引用。"),
    ])
    out = app.invoke({"messages": [HumanMessage(content="管径怎么选")]}, config=_config("r1"))
    cite_results = [m for m in out["messages"] if isinstance(m, ToolMessage) and m.name == "cite_clauses"]
    assert cite_results, "cite_clauses 被调用了"
    assert WEB_URL not in (out.get("citations") or [])
    assert all(getattr(c, "source_id", "") != WEB_URL for c in out.get("citations") or [])


# -- 记忆写入拦截 ---------------------------------------------------------------------------------------------


def test_web_content_can_never_be_written_to_long_term_memory() -> None:
    """P7-S11-A06 联网内容进不了长期记忆：管线入口 ``check_memory_write`` 对 ``WEB_CONTENT`` 直接 BLOCK
    （规则 ``memory.web_content``），``MemoryWriteGuard`` 也只接受 ``USER_MESSAGE``。

    **如实说明**：当前代码里没有任何"模型 / 系统发起的记忆写入"路径——记忆只有面板里用户显式确认一条，来源是
    ``USER_MESSAGE``，不受影响（最后一行断言）。这条拦截是 fail closed 的入口约束：将来有人把记忆写入接到模型发起
    的路径上，联网内容也进不去。它不是对某个现有写入路径的修复。"""

    import pytest

    from water_agent.memory.write_guard import MemoryInjectionBlockedError, MemoryWriteGuard
    from water_agent.runtime.governance.contracts import ContentEnvelope
    from water_agent.runtime.governance.pipeline import GuardrailPipeline

    benign = "管径选择以设计院内部规定为准。"
    outcome = GuardrailPipeline().check_memory_write(ContentEnvelope(source_tag=SourceTag.WEB_CONTENT, text=benign))
    assert outcome.allowed is False and outcome.decision.rule_id == "memory.web_content"
    # 对照：同样的文本换成用户来源，管线放行
    assert GuardrailPipeline().check_memory_write(ContentEnvelope(source_tag=SourceTag.USER_MESSAGE, text=benign)).allowed

    guard = MemoryWriteGuard()
    for other in (SourceTag.WEB_CONTENT, SourceTag.TOOL_OUTPUT, SourceTag.RETRIEVED_FILE, SourceTag.GENERATED_OUTPUT):
        with pytest.raises(MemoryInjectionBlockedError):
            guard.check(ContentEnvelope(source_tag=other, text=benign))
    assert guard.check(ContentEnvelope(source_tag=SourceTag.USER_MESSAGE, text=benign)).quarantined is False
