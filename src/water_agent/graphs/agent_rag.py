"""Agentic RAG 外层循环（Step 6）。

``create_react_agent`` 的标准行为是"模型 → 工具 → 模型……直到模型不再调用工具"，
它**没有**"最终回答后再插入引用校验并回流"的阶段。因此引用校验由外层图承担：

```text
START → agent_loop(create_react_agent 子图)
             ↓
        citation_validator
          ├─ 通过 → finalize（成功）
          ├─ 失败且有预算 → 注入合成 tool_call + 带错误文本的 ToolMessage → 回 agent_loop
          └─ 失败且无预算 → finalize（失败，errors 可见）
END
```

护栏（skill-agent-loop）：
- 主停止条件：模型不再调用工具；安全带：``max_tool_turns`` 轮数预算（agent_loop
  内独立，内层递归上限按预算推出，保证预算先触发）、外层 ``recursion_limit``≤20、
  引用校验回流上限（见 citation_validator）；
- 工具输出一律视为不可信输入：校验器只读 ToolMessage 里的结构化数据；
- 预算/校验超限、治理层拒绝统一返回结构化错误（带稳定错误码），绝不挂死或抛裸异常。

模型包装说明：``_BudgetChatModel`` 给任意模型套轮数预算，并为每次 tool-call 响应
分配新消息 id（``FakeMessagesListChatModel`` 复用同一对象，若 id 相同会被
``add_messages`` 去重，导致"一直调用工具"的模型循环意外早停而无法触发预算）。
"""
from __future__ import annotations

import itertools
import json
import re
import time
from collections.abc import Callable, Sequence
from typing import Annotated, Any, NotRequired, TypedDict, cast
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    BaseMessageChunk,
    HumanMessage,
    ToolCallChunk,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableBinding
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import create_react_agent
from pydantic import ConfigDict

from water_agent.domain.citation import CitationClaim, enrich_claims, validate_citations
from water_agent.domain.state import (
    MAX_CITATION_RETRIES,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
)
from water_agent.generation.cost import estimate_deepseek_cost
from water_agent.generation.governed_chat import GovernedChatModel
from water_agent.ingestion.version_registry import get_version_registry
from water_agent.observability import otel
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.prompting.assembler import ROLE_AND_PRINCIPLES
from water_agent.runtime.cancellation import raise_if_cancelled
from water_agent.runtime.governance.binding import local_request_context
from water_agent.runtime.governance.cache_service import AnswerCacheService
from water_agent.runtime.governance.contracts import (
    CODE_BUDGET_EXCEEDED,
    CODE_CAPABILITY_DENIED,
    CODE_GUARDRAIL_BLOCKED,
    CODE_INPUT_TOO_LARGE,
    CODE_RATE_LIMITED,
    CallCategory,
    GovernanceError,
    RequestContext,
)
from water_agent.runtime.governance.pipeline import (
    GuardrailPipeline,
    default_guardrail_pipeline,
)

# 外层图安全网：agent_loop ↔ citation_validator 最多 4 轮 + finalize，远小于 20
RECURSION_LIMIT = 20


def inner_recursion_limit(max_tool_turns: int) -> int:
    """内层 react 子图的递归上限：必须让工具预算先于它触发。

    每个工具轮占 2 个 superstep（模型 + 工具），第 ``max_tool_turns + 1`` 次模型调用（预算
    在这里抛 ``ToolBudgetExceeded``）落在第 ``2 * max_tool_turns + 1`` 步。此前内层沿用外层
    的 20：默认 ``max_tool_turns=10`` 时第 21 步先撞上 ``GraphRecursionError``，预算这条安全带
    根本触发不到（review 2026-09-30）。
    """
    return 2 * max_tool_turns + 3


#: 治理层的策略性拒绝（本地限流/预算/能力/护栏）收口为 blocked；其余（上游不可用、超时、
#: 认证失败、熔断……）是运行失败。两者都带稳定错误码进 errors。
_POLICY_REFUSAL_CODES = frozenset(
    {
        CODE_RATE_LIMITED,
        CODE_BUDGET_EXCEEDED,
        CODE_CAPABILITY_DENIED,
        CODE_GUARDRAIL_BLOCKED,
        CODE_INPUT_TOO_LARGE,
    }
)
# 校验器注入回流消息时使用的合成 tool_call_id 前缀（每次注入追加唯一后缀，避免
# OpenAI 兼容 API 对历史重复 tool_call id 的歧义）
_VALIDATOR_CALL_ID = "citation-validator"
# 空引用（有检索/有回答但没调用 cite_clauses）最多回流一次，再次空引用即结构化失败
EMPTY_CITATION_MAX_REFLOWS = 1

# 默认系统提示（prompt=None 时使用）。规则正文与 PromptAssembler 的
# ROLE_AND_PRINCIPLES 同源（P1/Step 10 评审：消除两条提示路径的重复规则来源，
# 避免漂移），此处只追加工具环境的差异说明。
DEFAULT_AGENTIC_PROMPT = ROLE_AND_PRINCIPLES + (
    "\n\n当前环境可用工具：retrieve_clauses（检索规范条款）、cite_clauses（声明引用）、"
    "calc_*（水力计算工具族）。"
)


class ToolBudgetExceeded(RuntimeError):
    """agent_loop 内模型工具调用轮数超预算。"""


#: 模型给出的工具调用参数不是合法 JSON（provider 解析失败，落在 ``invalid_tool_calls``）时，
#: 改写成一次合法的"错误回填"调用：工具名换成这个保留名，ToolNode 找不到同名工具会返回
#: 带可用工具清单的错误 observation——模型看得到错误、可以重试，计入工具轮数预算。
INVALID_TOOL_CALL_NAME = "invalid_tool_call"


def _repair_invalid_tool_calls(message: AIMessage) -> AIMessage:
    """把 ``invalid_tool_calls`` 改写成可被应答的工具调用；没有就原样返回。

    此前两条路径都不处理它：create_react_agent 只看 ``tool_calls``，带畸形调用的回复被当成
    最终回答（"我来检索一下。"判 succeeded）；同步路径还把它原样存进 thread，下一轮请求里
    出现一个没有应答的 tool_call id，OpenAI 兼容接口会拒绝整个请求、这条 thread 从此每轮失败
    （review 2026-09-30）。改写后：模型收到错误 observation 重试一次，thread 里只有合法消息。
    """
    invalid = list(message.invalid_tool_calls or [])
    if not invalid:
        return message
    repaired = list(message.tool_calls or [])
    for index, call in enumerate(invalid):
        repaired.append(
            {
                "name": INVALID_TOOL_CALL_NAME,
                "args": {
                    "intended_tool": str(call.get("name") or ""),
                    "error": str(call.get("error") or "工具调用参数不是合法 JSON")[:300],
                },
                "id": str(call.get("id") or f"invalid-{uuid4().hex[:8]}-{index}"),
                "type": "tool_call",
            }
        )
    return message.model_copy(update={"tool_calls": repaired, "invalid_tool_calls": []})


def _to_generation_chunk(message: BaseMessage) -> ChatGenerationChunk:
    """把完整 ``AIMessage`` 转为 ``ChatGenerationChunk``（流式回退路径用）。

    ``ChatGenerationChunk.message`` 要求 ``BaseMessageChunk`` 子类；完整消息需手工
    转 ``AIMessageChunk``（tool_calls → tool_call_chunks 才能正确聚合回 tool_calls）。
    """
    if isinstance(message, AIMessage):
        chunk: BaseMessageChunk = AIMessageChunk(
            content=message.content,
            additional_kwargs=message.additional_kwargs,
            response_metadata=message.response_metadata,
            id=message.id,
            tool_call_chunks=[
                ToolCallChunk(
                    name=tc["name"],
                    args=json.dumps(tc["args"], ensure_ascii=False),
                    id=tc["id"],
                    index=i,
                )
                for i, tc in enumerate(message.tool_calls or [])
            ],
        )
    else:
        chunk = cast(BaseMessageChunk, message)
    return ChatGenerationChunk(message=chunk)


class AgenticRAGState(TypedDict):
    """agentic 外层图状态：ChatState + 引用校验/预算字段。

    ``create_react_agent`` 要求 state_schema 必须含 ``messages`` 与
    ``remaining_steps``；除 ``messages`` 外全部 NotRequired，便于只传消息即可调用。
    """

    messages: Annotated[list[BaseMessage], add_messages]
    query: NotRequired[str]
    answer: NotRequired[str]
    citations: NotRequired[list[CitationClaim]]
    status: NotRequired[str]
    retry_count: NotRequired[int]
    errors: NotRequired[list[str]]
    #: 不影响 status 的可见标记（如非规范性回答零引用豁免），逐轮重置。
    warnings: NotRequired[list[str]]
    remaining_steps: NotRequired[int]
    empty_citation_reflows: NotRequired[int]  # 空引用回流计数（与 invalid 引用 retry 分开）
    #: Web Step 42 注入的长期记忆结果；PromptAssembler 只把它作为 SESSION_MEMORY 数据段渲染。
    memory_context: NotRequired[Any]
    #: P5-S48 注入的上传资料召回结果；PromptAssembler 只把它作为 RETRIEVED_FILE 数据段渲染。
    upload_context: NotRequired[Any]
    #: P5-S49 Web 文件上下文（能力说明 + 清单 + 点名附件）；只作为 RETRIEVED_FILE 数据段渲染。
    attachment_context: NotRequired[Any]


def _llm_call_record(message: Any, model_name: str, latency_ms: float) -> dict[str, Any]:
    """把一次模型响应压成 llm_call 观测记录（token 来自 usage_metadata，缺失记 0）。"""
    meta = getattr(message, "usage_metadata", None) or {}
    in_tok = int(meta.get("input_tokens", 0) or 0) if isinstance(meta, dict) else int(getattr(meta, "input_tokens", 0) or 0)
    out_tok = int(meta.get("output_tokens", 0) or 0) if isinstance(meta, dict) else int(getattr(meta, "output_tokens", 0) or 0)
    content = getattr(message, "content", "")
    tool_calls = getattr(message, "tool_calls", None) or []
    preview = content if isinstance(content, str) and content else (
        "tool_calls: " + ", ".join(tc.get("name", "?") for tc in tool_calls) if tool_calls else ""
    )
    return {
        "model": model_name,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "cost_usd": estimate_deepseek_cost(in_tok, out_tok),
        "latency_ms": latency_ms,
        "response_preview": preview,
    }


def _model_name_of(model: Any) -> str:
    for attr in ("model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


class _BudgetChatModel(BaseChatModel):
    """给任意模型套工具调用轮数预算，并防止同 id 消息被 add_messages 去重。

    同时把每次模型响应压成 llm_call 记录进 ``holder["llm_calls"]``（Step 10 观测），
    由 agent_loop 节点统一刷进 trace / OTel。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    inner: BaseChatModel
    max_tool_turns: int
    holder: dict

    @property
    def _llm_type(self) -> str:
        return "budget-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        # 取消在每次模型调用之前生效：此前取消后内层循环照样跑完剩下的模型调用（继续计费）
        raise_if_cancelled()
        started = time.perf_counter()
        result = self.inner._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
        self.holder.setdefault("llm_calls", []).append(
            _llm_call_record(message, _model_name_of(self.inner), (time.perf_counter() - started) * 1000)
        )
        if isinstance(message, AIMessage) and message.invalid_tool_calls:
            message = _repair_invalid_tool_calls(message)
            result = ChatResult(generations=[ChatGeneration(message=message)])
        if isinstance(message, AIMessage) and message.tool_calls:
            self.holder["turns"] += 1
            if self.holder["turns"] > self.max_tool_turns:
                raise ToolBudgetExceeded(f"工具调用轮数超过预算（{self.max_tool_turns}）")
            message = message.model_copy(update={"id": uuid4().hex})
            result = ChatResult(generations=[ChatGeneration(message=message)])
        return result

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """流式路径同样执行轮数预算，并逐块透传。

        计轮口径与 ``_generate`` 一致：**一次带工具调用的模型响应计一轮**（不论并行调用了几个
        工具）。此前按 tool-call index 计数，一次并行调用两个工具就吃掉两轮预算，与非流式路径
        对不上，也与 :func:`inner_recursion_limit` 的推导前提（一轮一次模型调用）不符。

        不支持底层流式的模型（如 ``FakeMessagesListChatModel._stream`` 抛
        ``NotImplementedError``）回退到 ``_generate`` 单块，保证离线 fake 路径可用。
        预算超限、取消或任何异常中断时显式关闭上游生成器：受治理模型据此关闭连接、结清预算预留。
        """
        raise_if_cancelled()
        try:
            inner = self.inner._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            first_chunk = next(inner)
        except StopIteration:
            return
        except NotImplementedError:
            result = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            yield _to_generation_chunk(result.generations[0].message)
            return
        counted = False
        started = time.perf_counter()
        merged: Any = None
        # 工具调用分块先攒着、流结束时整理成合法调用一次交出（_settle_streamed_calls）：参数 JSON
        # 是逐块拼出来的，只有收齐才知道合不合法；畸形调用原样交出会被当成"没有工具调用"的最终
        # 回答，前置文本"我来检索一下。"就判 succeeded（review 2026-09-30）。文本分块照常即时交出。
        pending_calls: dict[int, dict[str, str]] = {}
        try:
            for chunk in itertools.chain((first_chunk,), inner):
                # 流式分块之间同样检查取消：已取消就不再继续收（也不再继续计费）
                raise_if_cancelled()
                message = getattr(chunk, "message", chunk)
                if isinstance(message, AIMessageChunk):
                    merged = message if merged is None else merged + message
                    if message.tool_call_chunks:
                        if not counted:
                            counted = True
                            self.holder["turns"] += 1
                            if self.holder["turns"] > self.max_tool_turns:
                                raise ToolBudgetExceeded(
                                    f"工具调用轮数超过预算（{self.max_tool_turns}）"
                                )
                        _accumulate_call_chunks(pending_calls, message.tool_call_chunks)
                        chunk = ChatGenerationChunk(
                            message=message.model_copy(
                                update={"tool_call_chunks": [], "tool_calls": [], "invalid_tool_calls": []}
                            ),
                            generation_info=getattr(chunk, "generation_info", None),
                        )
                yield chunk
        finally:
            close = getattr(inner, "close", None)
            if callable(close):
                close()
        if pending_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=_settle_streamed_calls(pending_calls),
                    id=getattr(merged, "id", None),
                )
            )
        if merged is not None:
            self.holder.setdefault("llm_calls", []).append(
                _llm_call_record(merged, _model_name_of(self.inner), (time.perf_counter() - started) * 1000)
            )

    def bind_tools(self, tools, **kwargs):
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.args_schema.model_json_schema() if t.args_schema else {},
                },
            }
            for t in tools
        ]
        return RunnableBinding(bound=self, kwargs={"tools": tool_defs, **kwargs})


def _accumulate_call_chunks(pending: dict[int, dict[str, str]], chunks: Sequence[Any]) -> None:
    """按 index 拼接流式工具调用分块（name/args 逐块追加，id 取首个非空值）。"""
    for position, item in enumerate(chunks):
        index = item.get("index") if isinstance(item, dict) else getattr(item, "index", None)
        key = int(index) if isinstance(index, int) else position
        entry = pending.setdefault(key, {"name": "", "args": "", "id": ""})
        for field in ("name", "args"):
            value = item.get(field) if isinstance(item, dict) else getattr(item, field, None)
            if isinstance(value, str):
                entry[field] += value
        call_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        if isinstance(call_id, str) and call_id and not entry["id"]:
            entry["id"] = call_id


def _settle_streamed_calls(pending: dict[int, dict[str, str]]) -> list[ToolCallChunk]:
    """收齐的流式工具调用：参数是合法 JSON 对象的原样交出，其余改写成"错误回填"调用。"""
    settled: list[ToolCallChunk] = []
    for index in sorted(pending):
        entry = pending[index]
        raw = entry["args"].strip() or "{}"
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        call_id = entry["id"] or f"invalid-{uuid4().hex[:8]}-{index}"
        if entry["name"] and isinstance(parsed, dict):
            settled.append(ToolCallChunk(name=entry["name"], args=raw, id=call_id, index=index))
            continue
        settled.append(
            ToolCallChunk(
                name=INVALID_TOOL_CALL_NAME,
                args=json.dumps(
                    {"intended_tool": entry["name"], "error": "工具调用参数不是合法 JSON 对象"},
                    ensure_ascii=False,
                ),
                id=call_id,
                index=index,
            )
        )
    return settled


def _payload_of(message: ToolMessage) -> dict[str, Any]:
    """解析工具结果 ToolMessage 的 content（dict 输出会被序列化为 JSON 字符串）。"""
    content = message.content
    if isinstance(content, dict):
        return dict(content)
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def current_turn_messages(messages: Sequence[Any]) -> list[Any]:
    """本轮产生的消息：最后一条 human 消息之后的全部。

    checkpointer 会把整条 thread 还原进 ``state["messages"]``；凡是"本轮做了什么"
    的判断都必须先按这里切片，否则上一轮的检索/引用会被当成本轮的（P5-S49：上一轮
    检索过、本轮问"能读 PDF 吗"被判成"已检索却零引用"而强制回流）。运行时上下文前言
    也是 HumanMessage，但总插在问题之前，取最后一条即是问题本身。
    """
    for index in range(len(messages) - 1, -1, -1):
        if getattr(messages[index], "type", None) == "human":
            return list(messages[index + 1 :])
    return list(messages)


def _extract_validation_signal(
    state: AgenticRAGState,
) -> tuple[list[str], list[str], bool]:
    """从消息历史提取 (context_source_ids, 本轮 cited_ids, 本轮是否检索)。

    - context：所有 ``retrieve_clauses`` 结果里的 source_id（跨轮累积——检索结果
      在会话内持续有效，引用上一轮检索到的条款是合法的）；
    - cited：只看本轮，且仅统计本轮最后一次校验器注入（``_VALIDATOR_CALL_ID``）之后
      的 ``cite_clauses`` 声明，避免无效引用残留导致永远失败，也避免上一轮的引用
      被算进本轮；
    - retrieved_this_turn：本轮是否调用过 ``retrieve_clauses``（决定零引用豁免与
      回流文案，不能用跨轮的 context 判断）。
    """
    messages = state.get("messages", [])
    context_ids: list[str] = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == "retrieve_clauses":
            for hit in _payload_of(m).get("hits", []):
                sid = hit.get("source_id")
                if sid:
                    context_ids.append(sid)
    turn = current_turn_messages(messages)
    retrieved_this_turn = any(
        isinstance(m, ToolMessage) and m.name == "retrieve_clauses" for m in turn
    )
    boundary = -1
    for i, m in enumerate(turn):
        if _is_validator_feedback(m):
            boundary = i
    cited_ids: list[str] = []
    for m in turn[boundary + 1 :]:
        if isinstance(m, ToolMessage) and m.name == "cite_clauses":
            for cid in _payload_of(m).get("cited", []) or []:
                if cid not in cited_ids:
                    cited_ids.append(cid)
    return context_ids, cited_ids, retrieved_this_turn


def _is_validator_feedback(message: Any) -> bool:
    """校验器自己注入的回流消息（按保留的 tool_call_id 前缀识别，不按名字）。

    模型在历史里见过 ``citation_validator`` 调用，可能模仿着调用它：ToolNode 回填一条同名的
    "工具不存在"错误。按名字识别边界时，这条伪造的消息会把本轮有效的 cite 声明切到边界之前、
    整轮判成零引用失败（review 2026-09-30 实测）。只有校验器注入的调用才带这个前缀。
    """
    return (
        isinstance(message, ToolMessage)
        and message.name == "citation_validator"
        and str(message.tool_call_id or "").startswith(f"{_VALIDATOR_CALL_ID}-")
    )


def _flush_llm_calls(holder: dict, trace: JsonTraceRecorder | None, query: str) -> None:
    """把 holder 里攒的 llm_call 记录写进 trace 与 OTel（无 SDK 时 OTel 为 no-op）。"""
    calls = holder.pop("llm_calls", None) or []
    for call in calls:
        if trace is not None and trace.started:
            trace.record_llm_call(
                node="agent_loop",
                model=call["model"],
                provider="deepseek",
                prompt_preview=query,
                response_preview=call["response_preview"],
                input_tokens=call["input_tokens"],
                output_tokens=call["output_tokens"],
                total_tokens=call["total_tokens"],
                cost_usd=call["cost_usd"],
                latency_ms=call["latency_ms"],
                query=query,
            )
        otel.record_llm_call(
            model=call["model"],
            **{
                "gen_ai.usage.input_tokens": call["input_tokens"],
                "gen_ai.usage.output_tokens": call["output_tokens"],
                "water_agent.latency_ms": call["latency_ms"],
            },
        )


def make_agent_loop(
    react_agent,
    holder: dict,
    trace: JsonTraceRecorder | None = None,
    *,
    pipeline: GuardrailPipeline | None = None,
    request_context: RequestContext | None = None,
    cache_service: AnswerCacheService | None = None,
    model_name: str = "",
    temperature: float = 0.0,
    recursion_limit: int | None = None,
):
    """agent_loop 节点：重置预算计数后运行 react agent 子图；超预算转结构化错误。

    ``recursion_limit``：内层子图的递归上限（见 :func:`inner_recursion_limit`）；不给则沿用
    外层 config。超预算、撞上递归上限、治理层拒绝都收口为结构化结果，不以裸异常冒出图。

    ``trace``：首次进入时 ``start``，每轮结束把 llm_call 记录刷进去（Step 10 观测）。

    P2.5 接线（review 修复）：运行 react agent 前先对用户输入做护栏（注入/超长→blocked），
    再在真正 provider 调用前做缓存 lookup（agentic 分类不可缓存，故只会 MISS/INELIGIBLE）。
    """
    pipeline = pipeline if pipeline is not None else default_guardrail_pipeline()
    if request_context is None:
        request_context = local_request_context(thread_id="chat-agentic_loop")

    def _record_guardrail(stage: str, outcome) -> None:
        if trace is None:
            return
        try:
            payload = outcome.trace_payload()
            trace.record_guardrail_event(
                stage=stage,
                action=payload.get("action", ""),
                rule_id=payload.get("rule_id", ""),
                ruleset_version=payload.get("ruleset_version", ""),
                code=payload.get("code"),
                hits=payload.get("hits", []),
                input_hash=trace.hash_input(outcome.content) if outcome.content else "",
            )
        except Exception:  # noqa: BLE001, S110
            pass

    def _record_cache(disposition) -> None:
        if trace is None:
            return
        try:
            trace.record_cache_event(
                node="cache",
                cache_status=disposition.status.value,
                cache_key_hash=disposition.cache_key_hash,
                reason=disposition.reason,
                entry_age_seconds=disposition.entry_age_seconds,
                eligible_for_store=disposition.eligible_for_store,
            )
        except Exception:  # noqa: BLE001, S110
            pass

    def agent_loop(state: AgenticRAGState, config) -> dict[str, Any]:
        holder["turns"] = 0
        query = state.get("query", "") or _first_human_text(state)
        if trace is not None and not trace.started:
            trace.start(run_id=uuid4().hex, trace_id=uuid4().hex, query=query)

        # 输入护栏（在 provider 调用与工具执行之前）
        outcome = pipeline.check_user_input(query, request_context)
        _record_guardrail("input", outcome)
        if not outcome.allowed:
            block_msg = f"输入未通过护栏校验（{outcome.decision.rule_id}）：{outcome.decision.detail}"
            return {
                "status": STATUS_BLOCKED,
                "errors": list(state.get("errors", [])) + [block_msg],
                "answer": "",
                "messages": _rewrite(
                    _last_human_message(state.get("messages", [])), BLOCKED_INPUT_PLACEHOLDER
                ),
            }

        # 缓存 lookup（真正 provider 调用之前；agentic 分类不可缓存 → 只会 MISS/INELIGIBLE）
        if cache_service is not None and model_name:
            try:
                disposition, _cached = cache_service.lookup(
                    query=query,
                    ctx=request_context,
                    model=model_name,
                    temperature=temperature,
                    category=CallCategory.AGENTIC_LOOP,
                )
                _record_cache(disposition)
            except Exception:  # noqa: BLE001, S110 — 缓存失败不影响主流程
                pass

        inner_config = config if recursion_limit is None else {**config, "recursion_limit": recursion_limit}
        try:
            result = react_agent.invoke(state, config=inner_config)
            return dict(result)
        except ToolBudgetExceeded as exc:
            return {
                "status": STATUS_FAILED,
                "errors": list(state.get("errors", [])) + [str(exc)],
            }
        except GraphRecursionError:
            # 预算应先触发；走到这里说明预算与递归上限配置不一致，仍收口为可见失败
            return {
                "status": STATUS_FAILED,
                "errors": list(state.get("errors", []))
                + [f"内层循环达到递归上限（{recursion_limit} 步）仍未停止"],
            }
        except GovernanceError as exc:
            status = STATUS_BLOCKED if exc.code in _POLICY_REFUSAL_CODES else STATUS_FAILED
            return {
                "status": status,
                "errors": list(state.get("errors", [])) + [f"{exc.code}：{exc.message}"],
            }
        finally:
            _flush_llm_calls(holder, trace, query)

    return agent_loop


def _first_human_text(state: AgenticRAGState) -> str:
    for m in state.get("messages", []):
        if isinstance(m, HumanMessage) and isinstance(m.content, str) and not m.content.startswith("[运行时上下文]"):
            return m.content
    return ""


def _message_text(message: BaseMessage) -> str:
    """消息的文本内容：字符串原样返回；内容块列表只取文本块拼接。"""
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


def _final_answer_message(messages: Sequence[BaseMessage]) -> AIMessage | None:
    """本轮最后一条不带 tool_calls 的 AIMessage（:func:`final_answer_text` 的消息本体）。"""
    for message in reversed(current_turn_messages(messages)):
        if isinstance(message, AIMessage) and not message.tool_calls:
            return message
    return None


def final_answer_text(messages: Sequence[BaseMessage]) -> str:
    """本轮最终回答：本轮最后一条**不带 tool_calls** 的 AIMessage 的文本，空白即无回答。

    收口判定（校验器两条分支）与 finalize 取答案共用这一个定义：
    - 只看本轮（``current_turn_messages``）——上一轮的回答不得拿来判定"已作答"，也不得在
      本轮失败时被当成本轮答案返回；
    - 带 tool_calls 的 AIMessage 是"要去调工具"，附在上面的前置文本（"我来检索一下"）
      以及校验器注入的合成调用都不是回答；
    - 最后那条回复为空就是没有回答，不回头找更早的草稿——那些已被校验器驳回。
    """
    message = _final_answer_message(messages)
    if message is None:
        return ""
    text = _message_text(message)
    return text if text.strip() else ""


#: 被输入护栏拦下的问题在会话里只留占位：原文若留在 checkpoint，下一轮会原样发给模型
#: （追问一句"请继续"即绕过输入护栏，review 2026-09-30 实测）。
BLOCKED_INPUT_PLACEHOLDER = "[该条输入未通过护栏校验，已拦截]"
#: 被输出护栏拦截的回答同理：会话里只留占位，不让模型在后续轮次看到它。
BLOCKED_OUTPUT_PLACEHOLDER = "[该条回答未通过输出护栏，已撤回]"


def _rewrite(message: BaseMessage | None, text: str) -> list[BaseMessage]:
    """同 id 改写一条消息的正文（``add_messages`` 按 id 替换），没有目标就什么都不做。"""
    if message is None or message.id is None:
        return []
    return [message.model_copy(update={"content": text})]


#: 本轮收口状态写在最终回答消息的 ``additional_kwargs`` 里（不发给 provider：OpenAI 兼容接口的
#: 消息转换只取 tool_calls/function_call/audio 等已知键）。历史投影据此给失败的回答打标——此前
#: 刷新后每条回答都显示成 succeeded，"未通过校验"的横幅消失（review 2026-09-30）。
TURN_STATUS_KEY = "water_agent_turn_status"


def _stamp_status(message: AIMessage | None, status: str, rewrites: list[BaseMessage]) -> list[BaseMessage]:
    """把本轮收口状态盖到最终回答消息上（与正文改写合并为同一条同 id 替换）。"""
    if message is None or message.id is None:
        return rewrites
    base = next((m for m in rewrites if m.id == message.id), message)
    stamped = base.model_copy(
        update={"additional_kwargs": {**(base.additional_kwargs or {}), TURN_STATUS_KEY: status}}
    )
    return [m for m in rewrites if m.id != message.id] + [stamped]


def _last_human_message(messages: Sequence[BaseMessage]) -> HumanMessage | None:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message
    return None


# 明确"未检索到依据"的拒答表述（程序化关键词匹配，LLM 不参与判断）。
# 只匹配"未检索到/未找到…"类的显式否定表述，避免把一般性"未找到参数"等误判为拒答。
_NO_BASIS_PHRASES = (
    "未检索到",
    "没有检索到",
    "未能检索到",
    "检索不到",
    "未找到相关",
    "没有找到相关",
    "未获得相关",
    "未找到依据",
    "没有找到依据",
    "无法提供条款依据",
    # 回流文案本身要求"依据不足时明确说明"，照做的回答必须能被识别
    "依据不足",
)


def _declares_no_basis(answer: str) -> bool:
    """模型是否明确说明未检索到依据（无检索结果时的零引用收口条件之一）。"""
    return any(phrase in answer for phrase in _NO_BASIS_PHRASES)


# 非规范性回答判定（P5-S49）：能力询问、寒暄、操作说明不需要条款依据，强制回流只会
# 逼出"本轮我未引用任何规范条款…结论重申"这种写给校验器的话。判定是确定性规则、
# 刻意保守：只要回答里出现规范号/条款号形态，或"数字 + 工程单位"，就视为规范性结论，
# 仍须引用——防止模型凭记忆说"流速不大于 2 m/s"绕过铁律 2。
WARNING_NON_NORMATIVE_NO_CITATION = "no_citation_non_normative"
#: 诚实拒答：本轮确实检索过、零命中（或回流后仍判定依据不足），回答明确说明无依据且不含
#: 规范性结论。与非规范性豁免分开计，便于统计"系统说不知道"的比例。
WARNING_NO_BASIS_REFUSAL = "no_citation_no_basis"
#: trace 写盘失败：答案照常返回，只少了诊断现场（见 make_finalize._finish_trace）。
WARNING_TRACE_WRITE_FAILED = "trace_write_failed"
_NORMATIVE_MARKERS = (
    re.compile(r"(?:GB|CJJ|CECS|JGJ|DBJ?|T/)\s*/?\s*T?\s*\d{3,5}", re.IGNORECASE),
    re.compile(r"(?<![\d.])\d{1,2}\.\d{1,2}\.\d{1,3}(?![\d.])"),
    re.compile(r"第\s*\d+(?:\.\d+)*\s*条"),
    re.compile(r"\b(?:DN|De|dn)\s*\d+"),
    re.compile(
        r"\d(?:[\d.,]*)\s*(?:m/s|m³/h|m3/h|L/s|l/s|L/min|MPa|kPa|Pa\b|mm\b|cm\b|m\b|"
        r"米|毫米|厘米|%|‰|℃|°C|kW|m³|m3|L\b|升|吨|t/d|m³/d|m3/d)",
        re.IGNORECASE,
    ),
    # 不含数字的定性强制结论（"严禁……必须……"）同样是规范性结论。只收规范用语里的强约束
    # 词形；"应该""可以"这类日常用语不在内，避免把能力说明误判为规范性回答。
    re.compile(
        r"严禁|必须|不得|禁止|应当|不应|不宜|"
        r"应(?:符合|满足|按|设|采用|根据|大于|小于|不小于|不大于)|宜(?:采用|设|按)"
    ),
)


def _is_non_normative_answer(answer: str) -> bool:
    """回答是否不含任何规范性结论形态（规范号、条款号、带工程单位的数值、定性强制用语）。"""
    return not any(pattern.search(answer) for pattern in _NORMATIVE_MARKERS)


def _retrieval_hits_this_turn(messages: Sequence[BaseMessage]) -> list[str]:
    """本轮 retrieve_clauses 实际返回的 source_id（跨轮累积的检索上下文不算）。"""
    hits: list[str] = []
    for message in current_turn_messages(messages):
        if isinstance(message, ToolMessage) and message.name == "retrieve_clauses":
            for hit in _payload_of(message).get("hits", []) or []:
                sid = hit.get("source_id")
                if sid and sid not in hits:
                    hits.append(sid)
    return hits


# 正文里写出的条款号（P5-S50 附件问答实测：模型在正文写"GB 50014-2021 第 7.4.3 条"，
# 却两次都不调 cite_clauses，于是整轮失败）。只做**提示**：把正文提到、且在检索上下文
# 里真实存在的 source_id 列给模型，让它补一次声明；是否引用仍由 cite_clauses + 校验器
# 裁定，这里绝不代为声明（代填就回到了 legacy 的"系统代填、citation_ok 恒真"）。
_MENTIONED_CLAUSE_RE = re.compile(
    r"(GB|CJJ|CECS|JGJ)\s*/?\s*T?\s*(\d{4,5})\s*[-—–]\s*(\d{4})"
    r"[^\d\n]{0,24}?(\d{1,2}(?:\.\d{1,3}){1,3})"
)


def _mentioned_source_ids(answer: str, context_ids: Sequence[str]) -> list[str]:
    """正文里提到、且在检索上下文中存在的 source_id（保持出现顺序）。"""

    available = set(context_ids)
    found: list[str] = []
    for match in _MENTIONED_CLAUSE_RE.finditer(answer):
        prefix, number, year, article = match.groups()
        candidate = f"{prefix.upper()}{number}-{year} {article}"
        if candidate in available and candidate not in found:
            found.append(candidate)
    return found


# 回流后模型是在回复校验器的 ToolMessage，不提醒就会写"本轮我未引用…结论重申"。
_USER_FACING_SUFFIX = (
    "\n\n请直接给出面向用户的完整回答：不要提及本次校验、引用检查、"
    "\"本轮\"或\"结论重申\"等字样。"
)


def _new_validator_call_id() -> str:
    """每次回流注入使用唯一 tool_call_id，避免 OpenAI 兼容 API 对重复 id 的歧义。"""
    return f"{_VALIDATOR_CALL_ID}-{uuid4().hex[:8]}"


def _validator_feedback_messages(feedback: str) -> list[BaseMessage]:
    """构造校验器回流消息对：合成 assistant tool_call + 带错误文本的 ToolMessage。

    OpenAI 兼容 API（DeepSeek 实测）要求 role='tool' 消息必须紧跟在带 tool_calls 的
    assistant 消息后，裸注入 ToolMessage 会被 400 拒绝（真实链路评测 rag-p1 暴露）。
    """
    call_id = _new_validator_call_id()
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "citation_validator",
                    "args": {},
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content=feedback, name="citation_validator", tool_call_id=call_id),
    ]


def make_citation_validator(version_registry: Any | None = None):
    """citation_validator 节点：引用校验 → 通过收口 / 失败回流 / 失败收口。

    ``version_registry``（Step 8）：为每条引用解析版本状态写回 ``CitationClaim.version_status``；
    未知只标记（``warnings``），不回流、不判失败。

    零引用闭环（P1/Step 10 评审强制"引用可追溯"落地；豁免条件 2026-09-30 修正）：
    - 已检索到非空结果 + 模型已回答 + 无 cite_clauses 声明 → 注入要求声明引用的
      ToolMessage 回流一次（``EMPTY_CITATION_MAX_REFLOWS``）；
    - 回流后再空引用 → 结构化失败（错误文本进 ``errors``，对报告/用户可见）；
    - 豁免只看回答是否含规范性结论（规范号、条款号、带单位数值、"严禁/必须"类定性
      要求），不看措辞："未检索到"不是通行证；
    - 诚实拒答：本轮确实检索过、零命中（有命中则须已回流一次），明确说明无依据且回答
      不含规范性结论 → 无引用收口（``WARNING_NO_BASIS_REFUSAL``），不伪造引用；
    - 本轮未检索且回答不含规范性结论（寒暄、能力说明）→ 无引用收口
      （``WARNING_NON_NORMATIVE_NO_CITATION``）；
    - 空引用回流单独计数（``empty_citation_reflows`` ≤ ``EMPTY_CITATION_MAX_REFLOWS``），
      同时计入 ``retry_count``；无效引用重试看 ``retry_count``（≤ ``MAX_CITATION_RETRIES``），
      所以先发生的空引用回流会占掉一次无效引用重试。一轮总回流次数
      ≤ ``MAX_CITATION_RETRIES + EMPTY_CITATION_MAX_REFLOWS``。
    """

    def citation_validator(state: AgenticRAGState) -> dict[str, Any]:
        messages = state.get("messages", [])
        context_ids, cited_ids, retrieved_this_turn = _extract_validation_signal(state)
        check = validate_citations(cited_ids, context_ids, version_registry=version_registry)
        errors = list(state.get("errors", []))
        warnings = list(state.get("warnings", []))
        retry_count = int(state.get("retry_count", 0) or 0)

        if cited_ids:
            citations = enrich_claims(
                [CitationClaim(source_id=cid, declared_by="model") for cid in cited_ids],
                check,
            )
            if check.ok:
                if not final_answer_text(messages):
                    errors.append(
                        "模型声明了引用但未给出文本回答；空回答不得收口为成功"
                    )
                    return {"status": STATUS_FAILED, "citations": citations, "errors": errors}
                # 保留历史 errors（例如先前无效引用轮次的 did-you-mean 文本），与旧行为一致
                return {"status": STATUS_SUCCEEDED, "citations": citations, "errors": errors}

            # 无效引用：保持既有"最多重试 2 次"规则不变；did-you-mean 错误文本累积进 errors
            errors = errors + list(check.errors)
            if retry_count < MAX_CITATION_RETRIES:
                feedback = "\n".join(check.errors) or "引用校验未通过"
                return {
                    "messages": _validator_feedback_messages(feedback + _USER_FACING_SUFFIX),
                    "retry_count": retry_count + 1,
                    "errors": errors,
                }
            errors.append(f"达到引用校验重试上限（{MAX_CITATION_RETRIES}），仍有无效引用")
            return {"status": STATUS_FAILED, "errors": errors}

        # ── 零引用路径 ──────────────────────────────────────────────
        answer_text = final_answer_text(messages)
        empty_reflows = int(state.get("empty_citation_reflows", 0) or 0)

        if not answer_text:
            errors.append(
                "模型未产出文本回答且未调用 cite_clauses 声明引用；"
                "空回答/零引用不得收口为成功"
            )
            return {"status": STATUS_FAILED, "errors": errors}

        # 豁免只看回答内容是否含规范性结论，不看措辞：此前"未检索到"是一句魔法短语——
        # 不检索、写一句"未检索到相关条款"再凭记忆给出限值，也判成功且不带 warning。
        normative = not _is_non_normative_answer(answer_text)
        hits_this_turn = _retrieval_hits_this_turn(messages)

        if (
            retrieved_this_turn
            and not normative
            and _declares_no_basis(answer_text)
            and (not hits_this_turn or empty_reflows >= EMPTY_CITATION_MAX_REFLOWS)
        ):
            # 诚实拒答：本轮确实检索过，零命中（有命中时须已回流一次、给过引用机会），
            # 回答明确说明无依据且不含规范性结论 → 无引用收口，不伪造引用。
            if WARNING_NO_BASIS_REFUSAL not in warnings:
                warnings.append(WARNING_NO_BASIS_REFUSAL)
            return {
                "status": STATUS_SUCCEEDED,
                "citations": [],
                "errors": errors,
                "warnings": warnings,
            }

        if not retrieved_this_turn and not normative:
            # 本轮未检索、回答不含规范性结论：非规范性回答（能力询问、寒暄、操作说明），
            # 无引用收口；warning 保证豁免可见、可统计。
            if WARNING_NON_NORMATIVE_NO_CITATION not in warnings:
                warnings.append(WARNING_NON_NORMATIVE_NO_CITATION)
            return {
                "status": STATUS_SUCCEEDED,
                "citations": [],
                "errors": errors,
                "warnings": warnings,
            }

        if empty_reflows < EMPTY_CITATION_MAX_REFLOWS:
            mentioned = _mentioned_source_ids(answer_text, context_ids)
            if mentioned:
                feedback = (
                    "你的回答正文引用了规范条款，但没有调用 cite_clauses 声明。正文提到、且在检索"
                    f"结果中存在的条款：{mentioned}。请调用 cite_clauses 声明你实际依据的条款"
                    "（只声明真正支撑结论的那几条），然后给出完整回答；正文里提到但检索结果里"
                    "没有的条款号，请删去或注明\"未检索到原文\"。"
                )
            elif hits_this_turn:
                feedback = (
                    "本轮 retrieve_clauses 已返回相关条款，但你的回答没有调用 cite_clauses "
                    "声明引用。请基于检索结果调用 cite_clauses 声明回答依据的条款 id"
                    "（必须取自本次检索结果），随后给出引用后的完整回答。若你认为检索结果"
                    "不足以支撑正式引用，请在回答中明确说明依据不足，不得编造引用。"
                )
            elif retrieved_this_turn:
                # 本轮检索过但零命中：不能沿用"已返回相关条款"的文案（前几轮的命中不算本轮的）
                feedback = (
                    "本轮检索没有返回结果，你的回答却给出了规范性结论（规范号、条款号、数值限值"
                    "或\"严禁/必须\"类要求）且没有依据。若确认无相关依据，请在回答中明确说明"
                    "未检索到相关内容，不得凭记忆给出规范性结论，也不得编造引用。"
                )
            else:
                feedback = (
                    "你的回答没有调用 cite_clauses 声明引用，且本轮没有检索。"
                    "请先调用 retrieve_clauses 检索规范条款；若确认无相关依据，请在回答中"
                    "明确说明未检索到相关内容，不得编造引用。"
                )
            return {
                "messages": _validator_feedback_messages(feedback + _USER_FACING_SUFFIX),
                "retry_count": retry_count + 1,
                "empty_citation_reflows": empty_reflows + 1,
                "errors": errors,
            }

        errors.append(
            "空引用收口失败：存在检索结果/模型已回答，但两次校验均未调用 cite_clauses "
            "声明引用（空引用已回流一次）。零引用回答不得计为成功。"
        )
        return {"status": STATUS_FAILED, "errors": errors}

    return citation_validator


def make_finalize(
    trace: JsonTraceRecorder | None = None,
    *,
    pipeline: GuardrailPipeline | None = None,
    request_context: RequestContext | None = None,
    cache_service: AnswerCacheService | None = None,
    model_name: str = "",
    temperature: float = 0.0,
):
    """finalize 节点：从最后一条 AI 消息提取 answer，保留最终 status；有 trace 则落盘。

    P2.5 接线（review 修复）：输出护栏在最终返回/写 trace 之前执行；agentic 分类
    不可缓存（``AGENTIC_LOOP``），写入资格恒 INELIGIBLE（fail closed）。
    """
    pipeline = pipeline if pipeline is not None else default_guardrail_pipeline()
    if request_context is None:
        request_context = local_request_context(thread_id="chat-agentic_loop")

    def _guard_output(answer: str) -> Any:
        outcome = pipeline.check_output(
            answer,
            request_context,
            citations_verified=True,
            structured_output_ok=True,
        )
        if trace is not None:
            try:
                payload = outcome.trace_payload()
                trace.record_guardrail_event(
                    stage="output",
                    action=payload.get("action", ""),
                    rule_id=payload.get("rule_id", ""),
                    ruleset_version=payload.get("ruleset_version", ""),
                    code=payload.get("code"),
                    hits=payload.get("hits", []),
                    input_hash=trace.hash_input(outcome.content) if outcome.content else "",
                )
            except Exception:  # noqa: BLE001, S110
                pass
        return outcome

    def _finish_trace(**kwargs: Any) -> list[str]:
        """落盘 trace；写失败只记一条 warning，绝不改变运行结果。

        此前这里没有 try：trace 目录不可写（容器只读根文件系统、卷未挂载）时，已经通过引用
        校验与输出护栏的答案在收口处随 ``OSError`` 一起丢失，checkpoint 停在 finalize 之前，
        Web 报"agent run failed"、刷新后历史里却又出现这条答案（review 2026-09-30）。
        """
        if trace is None or not trace.started:
            return []
        try:
            trace.finish(**kwargs)
        except Exception:  # noqa: BLE001 — 可观测性失败不得改变运行结果
            return [WARNING_TRACE_WRITE_FAILED]
        return []

    def finalize(state: AgenticRAGState) -> dict[str, Any]:
        answer_message = _final_answer_message(state.get("messages", []))
        answer = final_answer_text(state.get("messages", []))
        rewrites: list[BaseMessage] = []
        status = state.get("status", STATUS_FAILED)
        errors = list(state.get("errors", []))
        if status == STATUS_SUCCEEDED and not answer:
            # 双保险：校验器两条分支都已拦空回答，这里兜住任何绕过校验器到达收口的路径
            status = STATUS_FAILED
            errors.append("收口时没有本轮文本回答；空回答不得收口为成功")

        # 输入护栏已阻断：直接收口 blocked，不再跑输出护栏。
        if status == STATUS_BLOCKED:
            trace_warnings = _finish_trace(
                status=STATUS_BLOCKED,
                error=errors[-1] if errors else None,
                citations=list(state.get("citations", [])),
                retry_count=int(state.get("retry_count", 0)),
            )
            result: dict[str, Any] = {"answer": "", "status": STATUS_BLOCKED}
            if trace_warnings:
                result["warnings"] = list(state.get("warnings", [])) + trace_warnings
            return result

        # 输出护栏：succeeded 与引用校验组合；failed 的回答同样要过（见下方分支）。
        if status == STATUS_SUCCEEDED and answer:
            outcome = _guard_output(answer)
            if not outcome.allowed:
                errors.append(
                    f"输出未通过护栏校验（{outcome.decision.rule_id}）：{outcome.decision.detail}"
                )
                status = STATUS_BLOCKED
                answer = ""
                rewrites = _rewrite(answer_message, BLOCKED_OUTPUT_PLACEHOLDER)
            else:
                if outcome.content != answer:
                    # 脱敏后的文本才是用户看到的回答；会话里保留原文，下一轮就会原样发给模型
                    rewrites = _rewrite(answer_message, outcome.content)
                answer = outcome.content

            # 缓存写入资格：agentic 分类不可缓存（REASON_CATEGORY），恒 INELIGIBLE。
            if cache_service is not None and model_name:
                try:
                    source_ids = [c.source_id for c in state.get("citations", [])]
                    eligibility = cache_service.eligibility_for(
                        category=CallCategory.AGENTIC_LOOP,
                        temperature=temperature,
                        citation_verified=True,
                        output_guardrails_passed=outcome.allowed,
                        answer=answer,
                        source_ids=source_ids,
                        ctx=request_context,
                    )
                    disposition = cache_service.store(
                        query=state.get("query", "") or _first_human_text(state),
                        ctx=request_context,
                        model=model_name,
                        temperature=temperature,
                        answer=answer,
                        source_ids=source_ids,
                        eligibility=eligibility,
                        category=CallCategory.AGENTIC_LOOP,
                    )
                    if trace is not None:
                        try:
                            trace.record_cache_event(
                                node="cache",
                                cache_status=disposition.status.value,
                                cache_key_hash=disposition.cache_key_hash,
                                reason=disposition.reason,
                                eligible_for_store=disposition.eligible_for_store,
                            )
                        except Exception:  # noqa: BLE001, S110
                            pass
                except Exception:  # noqa: BLE001, S110 — 缓存失败不影响主流程
                    pass
        elif status == STATUS_FAILED and answer:
            # 失败运行的回答照样会返回并显示（带失败状态与原因），此前却完全不过输出护栏：
            # 引用重试耗尽的回答里带危险指令也原样下发（review 2026-09-30）。这里只查危险
            # 指令与 PII/凭据——与 Web 历史投影同一口径；引用未通过由 failed 状态本身表达。
            outcome = _guard_output(answer)
            if not outcome.allowed:
                errors.append(
                    f"输出未通过护栏校验（{outcome.decision.rule_id}）：{outcome.decision.detail}"
                )
                answer = ""
                rewrites = _rewrite(answer_message, BLOCKED_OUTPUT_PLACEHOLDER)
            elif outcome.content != answer:
                rewrites = _rewrite(answer_message, outcome.content)
                answer = outcome.content

        trace_warnings = _finish_trace(
            status=status,
            error=errors[-1] if (status in (STATUS_FAILED, STATUS_BLOCKED) and errors) else None,
            citations=list(state.get("citations", [])),
            retry_count=int(state.get("retry_count", 0)),
        )
        return {
            "answer": answer,
            "status": status,
            "errors": errors,
            "warnings": list(state.get("warnings", [])) + trace_warnings,
            "messages": _stamp_status(answer_message, status, rewrites),
        }

    return finalize


def build_agentic_app(
    model,
    tools,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    prompt: str | Callable[[dict], Any] | None = None,
    max_tool_turns: int = 10,
    version_registry: Any | None = None,
    trace: JsonTraceRecorder | None = None,
    request_context: RequestContext | None = None,
    pipeline: GuardrailPipeline | None = None,
    cache_service: AnswerCacheService | None = None,
):
    """构建 agentic RAG 外层图。

    - ``model``：任意 chat model（真实或 fake）；内部自动套轮数预算；
    - ``tools``：``BaseTool`` 列表（经 ``tool_spec_to_base_tool`` 转换）；
    - ``checkpointer``：必配（thread_id 会话隔离，护住会话纪律）；
    - ``prompt``：系统提示；可以是字符串，或 ``(state) -> SystemMessage``
      可调用对象（Step 8 动态装配器）。缺省用 ``DEFAULT_AGENTIC_PROMPT``；
    - ``max_tool_turns``：agent_loop 内模型工具调用轮数独立预算；
    - ``version_registry``：版本注册表（Step 8）；缺省用 ``data/versions.json`` 单例，
      引用校验时把版本状态写回 ``CitationClaim.version_status``（未知只标记）；
    - ``trace``：可选 JsonTraceRecorder（Step 10 观测）：agent_loop 首次进入 ``start``，
      每次模型响应记一条 ``llm_call``（token/cost/延迟/摘要），finalize ``finish`` 落盘；
    - ``request_context`` / ``pipeline`` / ``cache_service``：P2.5 治理接线（review 修复），
      缺省回落进程级默认实例（仍经治理层，不绕过）。
    """
    if checkpointer is None:
        raise ValueError("agentic 循环必须配置 checkpointer（thread_id 会话隔离）")
    if version_registry is None:
        version_registry = get_version_registry()

    # 缓存只对真实受治理模型有意义（agentic 分类本身不可缓存，这里只是接线）。
    cache_model_name = ""
    cache_temperature = 0.0
    if cache_service is not None and isinstance(model, GovernedChatModel):
        cache_model_name = model._resolve_model_name()
        cache_temperature = float(getattr(model, "temperature", 0.0) or 0.0)

    holder: dict[str, Any] = {"turns": 0}
    budget_model = _BudgetChatModel(inner=model, max_tool_turns=max_tool_turns, holder=holder)
    # pydantic 校验会拷贝 dict 字段：以模型内部持有的那份为准，外层节点才能读到计数与 llm_calls
    holder = budget_model.holder
    if prompt is None:
        system_prompt: str | Callable[[dict], Any] = DEFAULT_AGENTIC_PROMPT
    else:
        system_prompt = prompt
    # checkpointer=False：内层子图不持久化。``None`` 的含义是"继承父图的 checkpointer"——每轮
    # 3 次工具调用就多写 9 个子图 checkpoint、消息字节约为父图的 2.5 倍，且永远不会被读取
    # （外层 agent_loop 节点每次都以完整 state 重新调用子图；review 2026-09-30 实测）。
    react_agent = create_react_agent(
        model=budget_model,
        tools=tools,
        state_schema=AgenticRAGState,
        checkpointer=False,
        prompt=system_prompt,
    )

    graph = StateGraph(AgenticRAGState)
    graph.add_node(
        "agent_loop",
        make_agent_loop(
            react_agent,
            holder,
            trace,
            pipeline=pipeline,
            request_context=request_context,
            cache_service=cache_service,
            model_name=cache_model_name,
            temperature=cache_temperature,
            recursion_limit=inner_recursion_limit(max_tool_turns),
        ),
    )
    graph.add_node("citation_validator", make_citation_validator(version_registry))
    graph.add_node(
        "finalize",
        make_finalize(
            trace,
            pipeline=pipeline,
            request_context=request_context,
            cache_service=cache_service,
            model_name=cache_model_name,
            temperature=cache_temperature,
        ),
    )

    def route_after_agent(state: AgenticRAGState) -> str:
        if state.get("status") in (STATUS_FAILED, STATUS_BLOCKED):
            return "finalize"
        return "citation_validator"

    def route_after_citation(state: AgenticRAGState) -> str:
        if state.get("status") in (STATUS_SUCCEEDED, STATUS_FAILED):
            return "finalize"
        return "agent_loop"

    graph.add_edge(START, "agent_loop")
    graph.add_conditional_edges(
        "agent_loop",
        route_after_agent,
        {"citation_validator": "citation_validator", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "citation_validator",
        route_after_citation,
        {"agent_loop": "agent_loop", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)

    app = graph.compile(checkpointer=checkpointer)
    return app.with_config({"recursion_limit": RECURSION_LIMIT})


#: 每轮开始时必须归零的**本轮**状态。它们和两个 *_context 一样是无 reducer 的键、
#: 随 checkpoint 持久化：不归零的话，第 1 轮的引用重试/空引用回流次数会吃掉第 2 轮
#: 的重试预算，第 1 轮的 errors/citations 会出现在第 2 轮的结果里（两轮实测）。
#: ``messages`` 不在此列——它有 add_messages reducer，跨轮累积是会话记忆本身。
PER_TURN_RESET: dict[str, Any] = {
    "answer": "",
    "status": "",
    "citations": [],
    "errors": [],
    "warnings": [],
    "retry_count": 0,
    "empty_citation_reflows": 0,
}


#: 草稿事件的类型：模型正在写的回答（已过输出护栏）、草稿作废、流式通道整体关闭。
DRAFT_DELTA = "draft_delta"
DRAFT_RETRACT = "draft_retract"
DRAFT_CLOSED = "draft_closed"
#: 草稿作废原因：被引用校验驳回（回流重写）/ 输出护栏拦截 / 运行失败或被拦截。
RETRACT_REVISED = "revised"
RETRACT_GUARDRAIL = "guardrail"
RETRACT_RUN_ENDED = "run_ended"

#: 草稿按"完整句子"下发：句末标点或换行处切分。只有已过输出护栏的完整片段才发出去——
#: 危险指令、凭据、PII 的正则都需要完整的字面形态才能命中，半截 token 过检没有意义。
_DRAFT_BOUNDARY = re.compile(r"[。！？；!?;\n]")
#: 迟迟没有句末标点时的强制切分长度（防止一整段长文本憋到最后才出现）。
_DRAFT_MAX_PENDING = 160
#: 每条回复首次下发前至少攒这么多字：模型调用工具前常先写一句"我来检索一下。"，
#: 不攒一下它会先闪现、再被撤回。
_DRAFT_MIN_FIRST_RELEASE = 24
#: 凭据、手机号、证件号、邮箱、URL 都是连续的非空白 ASCII 串：强制切分绝不切在这种串中间，
#: 否则前半截按"不像凭据"放行、后半截才凑出完整形态，脱敏就晚了。
_DRAFT_TOKEN_CHAR = re.compile(r"[A-Za-z0-9_\-./:@+=%#~?&]")


class DraftPump:
    """把模型的流式文本变成"已过输出护栏的草稿"事件。

    语义（review 2026-09-30 第 4 批，"所有草稿都流式"）：

    - 只接收 ``agent`` 节点（内层 react 子图的模型调用）的文本分块；工具结果、校验器回流
      消息、带 tool_calls 的前置文本一律不是回答；
    - 文本攒到完整句子才过一次 ``check_output``（危险指令 + PII/凭据，与 finalize 失败分支、
      历史投影同一口径——引用是否成立在流式时还不知道，由草稿标注"待校验"表达）；
      放行的发 ``draft_delta``（脱敏过的就发脱敏文本）；
    - 一旦有片段被拦截：发 ``draft_retract(guardrail)`` 并**关闭流式通道**——本轮后续草稿
      一律不再下发，回答只等 finalize。被拦截的原文永远不出现在任何事件里；
    - 模型开始一条新的回复（回流重写、或前一条原来是"我来检索一下"的工具调用前置文本）：
      先发 ``draft_retract(revised)``，新回复从头开始；
    - 收口：``finish(status)``。succeeded 时把还没发出的尾巴补发出去，最终回答以 finalize 的
      ``answer`` 为准；failed/blocked/cancelled 时发 ``draft_retract(run_ended)``。
    """

    def __init__(
        self,
        emit: Callable[[str, dict[str, Any]], None],
        *,
        pipeline: GuardrailPipeline | None = None,
        request_context: RequestContext | None = None,
    ) -> None:
        self._emit = emit
        self._pipeline = pipeline if pipeline is not None else default_guardrail_pipeline()
        self._context = request_context or local_request_context(thread_id="draft-stream")
        self._message_key: tuple[Any, Any] | None = None
        self._pending = ""
        self._released_raw = ""  # 本条回复已下发部分的原文（过检时与待发部分合在一起查）
        self._sent = 0  # 本条回复已下发的字符数（脱敏后的长度）
        self._closed = False
        self._open_draft = False

    @property
    def closed(self) -> bool:
        return self._closed

    def feed(self, chunk: Any, metadata: dict[str, Any] | None) -> None:
        if self._closed or not isinstance(chunk, AIMessageChunk):
            return
        meta = metadata or {}
        if meta.get("langgraph_node") != "agent":
            return
        # 一次模型调用 = 内层子图的一个 superstep：(命名空间, step) 唯一标识"这一条回复"。
        # 不能用 chunk.id：LangChain 收尾的空块用 run id，与 provider 给的消息 id 不同。
        key = (meta.get("langgraph_checkpoint_ns"), meta.get("langgraph_step"))
        if key != self._message_key:
            self._start_message(key)
        if chunk.tool_call_chunks or chunk.tool_calls:
            # 这条回复其实是工具调用：附在上面的文字不是回答
            self._withdraw(RETRACT_REVISED)
            return
        text = _message_text(chunk)
        if text:
            self._pending += text
        # 一条回复写完（LangChain 的收尾块）：剩下的文字已是完整草稿，过检后下发
        self._release(final=getattr(chunk, "chunk_position", None) == "last")

    def finish(self, status: str) -> None:
        if self._closed:
            return
        if status == STATUS_SUCCEEDED:
            self._release(final=True)
        else:
            self._withdraw(RETRACT_RUN_ENDED)
        self._close()

    def abort(self) -> None:
        """取消/异常：作废已下发的草稿并关闭通道。"""
        if self._closed:
            return
        self._withdraw(RETRACT_RUN_ENDED)
        self._close()

    def _start_message(self, key: tuple[Any, Any]) -> None:
        self._withdraw(RETRACT_REVISED)
        self._message_key = key
        self._pending = ""

    def _withdraw(self, reason: str) -> None:
        if self._open_draft:
            self._emit(DRAFT_RETRACT, {"reason": reason})
        self._open_draft = False
        self._pending = ""
        self._released_raw = ""
        self._sent = 0

    def _close(self) -> None:
        self._closed = True
        self._pending = ""
        self._emit(DRAFT_CLOSED, {})

    def _release(self, *, final: bool) -> None:
        while self._pending and not self._closed:
            cut = self._cut_point(final=final)
            if cut <= 0:
                return
            # 过检对象是"本条回复至今收到的全部文本"（已发 + 待发），不是单个片段：跨句的危险
            # 指令一旦在后文凑齐就能命中；放行时只下发切出的这一段（按同一规则脱敏）。
            whole = self._pipeline.check_output(self._released_raw + self._pending, self._context)
            if not whole.allowed:
                self._withdraw(RETRACT_GUARDRAIL)
                self._close()
                return
            piece, self._pending = self._pending[:cut], self._pending[cut:]
            outcome = self._pipeline.check_output(piece, self._context)
            if not outcome.allowed:
                self._withdraw(RETRACT_GUARDRAIL)
                self._close()
                return
            self._released_raw += piece
            if outcome.content:
                self._emit(DRAFT_DELTA, {"text": outcome.content, "offset": self._sent})
                self._sent += len(outcome.content)
                self._open_draft = True

    def _cut_point(self, *, final: bool) -> int:
        if final:
            return len(self._pending)
        if not self._open_draft and len(self._pending) < _DRAFT_MIN_FIRST_RELEASE:
            return 0
        last = -1
        for match in _DRAFT_BOUNDARY.finditer(self._pending):
            last = match.end()
        if last > 0:
            return last
        if len(self._pending) < _DRAFT_MAX_PENDING:
            return 0
        return _token_safe_cut(self._pending, _DRAFT_MAX_PENDING)


def _token_safe_cut(text: str, limit: int) -> int:
    """不切在连续 ASCII 串中间的切分点（≤ limit）；整段都是一个串时返回 0（等更多文本）。"""
    index = min(limit, len(text))
    while index > 0 and (
        _DRAFT_TOKEN_CHAR.match(text[index - 1]) and index < len(text) and _DRAFT_TOKEN_CHAR.match(text[index])
    ):
        index -= 1
    return index


def _agentic_input(
    query: str,
    messages: Sequence[BaseMessage] | None,
    memory_context: Any | None,
    upload_context: Any | None,
    attachment_context: Any | None,
) -> dict[str, Any]:
    # 两个 *_context 是**本轮**的召回结果，必须每轮无条件写入（没有就写 None）。
    # 它们是无 reducer 的 state 键，会随 checkpoint 持久化：只在"有值"时才写，
    # 下一轮不传时图里留着的是上一轮的值——而且经生产 serde（allowed_msgpack_modules
    # 只放行 CitationClaim）读回来已不是 MemoryRecallResult/UploadRecallResult，
    # 只是个 dict，PromptAssembler 当场 TypeError（第二问与上传无关、或记忆降级即崩，
    # 两轮实测复现）。即便类型能读回，把上一问的检索资料塞进这一问也是错的上下文。
    return {
        "messages": list(messages) if messages is not None else [HumanMessage(content=query)],
        "query": query,
        "memory_context": memory_context,
        "upload_context": upload_context,
        "attachment_context": attachment_context,
        **PER_TURN_RESET,
    }


class _StreamRouter:
    """把 ``astream(..., subgraphs=True)`` 的事件分发给观察者、草稿泵与 token 回调。

    - ``on_event`` 收外层图的全部事件，外加内层的 ``messages`` 事件（模型的逐块输出与工具
      结果消息）：开 subgraphs 之后，内层已发过的消息在外层按 id 去重、不再重发，观察者只有
      从内层事件里才能看到"模型发起了哪个工具调用"。内层每步的完整 state 不给它；
    - ``on_token``：本轮已过护栏的草稿文本（CLI 实时打印用）；
    - ``on_draft``：草稿事件（delta/retract/closed，Web SSE 用）。
    """

    def __init__(self, on_token, on_event, on_draft, pipeline, request_context) -> None:
        self.final_state: dict[str, Any] = {}
        self._on_event = on_event
        self._pump: DraftPump | None = None
        if on_token is not None or on_draft is not None:

            def emit(kind: str, payload: dict[str, Any]) -> None:
                if kind == DRAFT_DELTA and on_token is not None:
                    on_token(payload["text"])
                if on_draft is not None:
                    on_draft(kind, payload)

            self._pump = DraftPump(emit, pipeline=pipeline, request_context=request_context)

    def handle(self, event: Any) -> None:
        if not isinstance(event, tuple) or len(event) != 3:
            return
        namespace, mode, payload = event
        inner = bool(namespace)
        # 外层的全部事件 + 内层的 messages 事件交给观察者（工具卡片在模型发出调用时就出现）；
        # 内层每一步的完整 state 不给——外层 values 才是"本轮做了什么"的权威快照。
        if self._on_event is not None and (not inner or mode == "messages"):
            self._on_event(mode, payload)
        if not inner:
            if mode == "values" and isinstance(payload, dict):
                self.final_state = payload
            return
        if mode == "messages" and self._pump is not None and isinstance(payload, tuple) and payload:
            metadata = payload[1] if len(payload) > 1 and isinstance(payload[1], dict) else None
            self._pump.feed(payload[0], metadata)

    def finish(self) -> None:
        if self._pump is not None:
            self._pump.finish(str(self.final_state.get("status") or STATUS_FAILED))

    def abort(self) -> None:
        if self._pump is not None:
            self._pump.abort()


async def astream_agentic(
    app,
    query: str,
    config: dict,
    *,
    on_token: Callable[[str], None] | None = None,
    messages: Sequence[BaseMessage] | None = None,
    on_event: Callable[[str, Any], None] | None = None,
    on_draft: Callable[[str, dict[str, Any]], None] | None = None,
    memory_context: Any | None = None,
    upload_context: Any | None = None,
    attachment_context: Any | None = None,
    pipeline: GuardrailPipeline | None = None,
    request_context: RequestContext | None = None,
) -> dict[str, Any]:
    """流式运行 agentic 图（``subgraphs=True``，内层模型逐块到达），返回最终状态。

    - ``on_event``：原始 ``(mode, payload)`` 事件——外层图的全部事件（``values`` 为每步完整
      状态，最后一个为 finalize 后的终态）与内层模型/工具的 ``messages`` 事件。调用方必须自行
      决定安全摘要，不能把完整 state 或消息正文直接发给浏览器；
    - ``on_token``：本轮**已过输出护栏**的回答草稿文本（完整句子为单位，见 :class:`DraftPump`）；
      工具输出、校验器回流提示、工具调用前置文本、被拦截的片段都不会出现；
    - ``on_draft``：草稿事件 ``(kind, payload)``，kind 为 ``draft_delta``/``draft_retract``/
      ``draft_closed``——草稿可撤回，最终回答只以 finalize 的 ``answer`` 为准；
    - ``messages``：可选的初始消息列表（Step 8 用于注入运行时上下文），默认只有问题本身；
    - ``memory_context`` / ``upload_context`` / ``attachment_context``：本轮召回结果，只进入
      独立 data 段，不承载指令、不参与数值推导、不改变引用校验；
    - ``pipeline`` / ``request_context``：草稿过检用的护栏与主体（缺省回落默认实例）。
    """
    router = _StreamRouter(on_token, on_event, on_draft, pipeline, request_context)
    input_state = _agentic_input(query, messages, memory_context, upload_context, attachment_context)
    try:
        async for event in app.astream(
            input_state,
            config=config,
            stream_mode=["messages", "values"],
            subgraphs=True,
        ):
            router.handle(event)
    except BaseException:
        router.abort()
        raise
    router.finish()
    return router.final_state


def stream_agentic(
    app,
    query: str,
    config: dict,
    *,
    on_token: Callable[[str], None] | None = None,
    messages: Sequence[BaseMessage] | None = None,
    on_event: Callable[[str, Any], None] | None = None,
    on_draft: Callable[[str, dict[str, Any]], None] | None = None,
    pipeline: GuardrailPipeline | None = None,
    request_context: RequestContext | None = None,
) -> dict[str, Any]:
    """:func:`astream_agentic` 的同步版本（CLI ``--stream``）。

    CLI 的会话 checkpointer 是同步 ``SqliteSaver``，LangGraph 拒绝经由它的 async 方法驱动图
    （此前 ``--mode agentic --stream`` 接真实模型必然 ``NotImplementedError``）；这里走同步
    ``app.stream``，草稿语义与 Web 完全相同。CLI 不做召回，三个 *_context 显式写 None。
    """
    router = _StreamRouter(on_token, on_event, on_draft, pipeline, request_context)
    input_state = _agentic_input(query, messages, None, None, None)
    try:
        for event in app.stream(
            input_state,
            config=config,
            stream_mode=["messages", "values"],
            subgraphs=True,
        ):
            router.handle(event)
    except BaseException:
        router.abort()
        raise
    router.finish()
    return router.final_state
