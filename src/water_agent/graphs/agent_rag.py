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
  内独立）、``recursion_limit``≤20、引用校验回流上限 2 次；
- 工具输出一律视为不可信输入：校验器只读 ToolMessage 里的结构化数据；
- 预算/校验超限统一返回结构化错误，绝不挂死或抛裸异常。

模型包装说明：``_BudgetChatModel`` 给任意模型套轮数预算，并为每次 tool-call 响应
分配新消息 id（``FakeMessagesListChatModel`` 复用同一对象，若 id 相同会被
``add_messages`` 去重，导致"一直调用工具"的模型循环意外早停而无法触发预算）。
"""
from __future__ import annotations

import itertools
import json
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
from water_agent.runtime.governance.binding import local_request_context
from water_agent.runtime.governance.cache_service import AnswerCacheService
from water_agent.runtime.governance.contracts import (
    CallCategory,
    RequestContext,
)
from water_agent.runtime.governance.pipeline import (
    GuardrailPipeline,
    default_guardrail_pipeline,
)

# 外层图安全网：create_react_agent 内部也有递归上限，这里统一压到 20
RECURSION_LIMIT = 20
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
    remaining_steps: NotRequired[int]
    empty_citation_reflows: NotRequired[int]  # 空引用回流计数（与 invalid 引用 retry 分开）
    #: Web Step 42 注入的长期记忆结果；PromptAssembler 只把它作为 SESSION_MEMORY 数据段渲染。
    memory_context: NotRequired[Any]
    #: P5-S48 注入的上传资料召回结果；PromptAssembler 只把它作为 RETRIEVED_FILE 数据段渲染。
    upload_context: NotRequired[Any]


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
        started = time.perf_counter()
        result = self.inner._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
        self.holder.setdefault("llm_calls", []).append(
            _llm_call_record(message, _model_name_of(self.inner), (time.perf_counter() - started) * 1000)
        )
        if isinstance(message, AIMessage) and message.tool_calls:
            self.holder["turns"] += 1
            if self.holder["turns"] > self.max_tool_turns:
                raise ToolBudgetExceeded(f"工具调用轮数超过预算（{self.max_tool_turns}）")
            message = message.model_copy(update={"id": uuid4().hex})
            result = ChatResult(generations=[ChatGeneration(message=message)])
        return result

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """流式路径同样执行轮数预算（每个新 tool-call 起始计一轮），并透传 token。

        不支持的底层流式的模型（如 ``FakeMessagesListChatModel._stream`` 抛
        ``NotImplementedError``）回退到 ``_generate`` 单块，保证离线 fake 路径可用。
        """
        try:
            inner = self.inner._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            first_chunk = next(inner)
        except StopIteration:
            return
        except NotImplementedError:
            result = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            yield _to_generation_chunk(result.generations[0].message)
            return
        seen: set[int] = set()
        started = time.perf_counter()
        merged: Any = None
        for chunk in itertools.chain((first_chunk,), inner):
            if isinstance(chunk, AIMessageChunk):
                merged = chunk if merged is None else merged + chunk
                for tool_chunk in chunk.tool_call_chunks or []:
                    idx = getattr(tool_chunk, "index", None)
                    if idx is not None and idx not in seen:
                        seen.add(idx)
                        self.holder["turns"] += 1
                        if self.holder["turns"] > self.max_tool_turns:
                            raise ToolBudgetExceeded(
                                f"工具调用轮数超过预算（{self.max_tool_turns}）"
                            )
            yield chunk
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


def _extract_validation_signal(state: AgenticRAGState) -> tuple[list[str], list[str]]:
    """从消息历史提取 (context_source_ids, 本轮 cited_ids)。

    - context：所有 ``retrieve_clauses`` 结果里的 source_id（跨轮累积——检索结果
      在会话内持续有效）；
    - cited：仅统计最后一次校验器注入（``_VALIDATOR_CALL_ID``）之后的
      ``cite_clauses`` 声明，避免上一轮的无效引用残留在本轮导致永远失败。
    """
    messages = state.get("messages", [])
    context_ids: list[str] = []
    boundary = -1
    for i, m in enumerate(messages):
        if not isinstance(m, ToolMessage):
            continue
        if m.name == "retrieve_clauses":
            for hit in _payload_of(m).get("hits", []):
                sid = hit.get("source_id")
                if sid:
                    context_ids.append(sid)
        elif m.name == "citation_validator":
            boundary = i
    cited_ids: list[str] = []
    for m in messages[boundary + 1 :]:
        if isinstance(m, ToolMessage) and m.name == "cite_clauses":
            for cid in _payload_of(m).get("cited", []) or []:
                if cid not in cited_ids:
                    cited_ids.append(cid)
    return context_ids, cited_ids


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
):
    """agent_loop 节点：重置预算计数后运行 react agent 子图；超预算转结构化错误。

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

        try:
            result = react_agent.invoke(state, config=config)
            return dict(result)
        except ToolBudgetExceeded as exc:
            return {
                "status": STATUS_FAILED,
                "errors": list(state.get("errors", [])) + [str(exc)],
            }
        finally:
            _flush_llm_calls(holder, trace, query)

    return agent_loop


def _first_human_text(state: AgenticRAGState) -> str:
    for m in state.get("messages", []):
        if isinstance(m, HumanMessage) and isinstance(m.content, str) and not m.content.startswith("[运行时上下文]"):
            return m.content
    return ""


def _last_ai_answer_text(messages: Sequence[BaseMessage]) -> str:
    """取最近一条带文本的 AIMessage 内容（供引用闭环判断"模型已作答"）。"""
    for m in reversed(messages):
        if isinstance(m, AIMessage) and isinstance(m.content, str) and m.content.strip():
            return m.content
    return ""


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
)


def _declares_no_basis(answer: str) -> bool:
    """模型是否明确说明未检索到依据（无检索结果时的零引用收口条件之一）。"""
    return any(phrase in answer for phrase in _NO_BASIS_PHRASES)


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

    零引用闭环（P1/Step 10 评审强制"引用可追溯"落地）：
    - 已检索到非空结果 + 模型已回答 + 无 cite_clauses 声明 → 注入要求声明引用的
      ToolMessage 回流一次（``EMPTY_CITATION_MAX_REFLOWS``）；
    - 回流后再空引用 → 结构化失败（错误文本进 ``errors``，对报告/用户可见）；
    - 无任何检索结果且模型明确说明"未检索到依据" → 允许无引用收口，不伪造引用；
    - 空引用回流与无效引用重试（≤``MAX_CITATION_RETRIES`` 次）分开计数互不挤占。
    """

    def citation_validator(state: AgenticRAGState) -> dict[str, Any]:
        messages = state.get("messages", [])
        context_ids, cited_ids = _extract_validation_signal(state)
        check = validate_citations(cited_ids, context_ids, version_registry=version_registry)
        errors = list(state.get("errors", []))
        retry_count = int(state.get("retry_count", 0) or 0)

        if cited_ids:
            citations = enrich_claims(
                [CitationClaim(source_id=cid, declared_by="model") for cid in cited_ids],
                check,
            )
            if check.ok:
                # 保留历史 errors（例如先前无效引用轮次的 did-you-mean 文本），与旧行为一致
                return {"status": STATUS_SUCCEEDED, "citations": citations, "errors": errors}

            # 无效引用：保持既有"最多重试 2 次"规则不变；did-you-mean 错误文本累积进 errors
            errors = errors + list(check.errors)
            if retry_count < MAX_CITATION_RETRIES:
                feedback = "\n".join(check.errors) or "引用校验未通过"
                return {
                    "messages": _validator_feedback_messages(feedback),
                    "retry_count": retry_count + 1,
                    "errors": errors,
                }
            errors.append(f"达到引用校验重试上限（{MAX_CITATION_RETRIES}），仍有无效引用")
            return {"status": STATUS_FAILED, "errors": errors}

        # ── 零引用路径 ──────────────────────────────────────────────
        answer_text = _last_ai_answer_text(messages)
        empty_reflows = int(state.get("empty_citation_reflows", 0) or 0)

        if not context_ids and answer_text and _declares_no_basis(answer_text):
            # 无检索结果且模型明确说明"未检索到依据"：允许无引用收口，不伪造引用
            return {"status": STATUS_SUCCEEDED, "citations": [], "errors": errors}

        if not answer_text:
            errors.append(
                "模型未产出文本回答且未调用 cite_clauses 声明引用；"
                "空回答/零引用不得收口为成功"
            )
            return {"status": STATUS_FAILED, "errors": errors}

        if empty_reflows < EMPTY_CITATION_MAX_REFLOWS:
            if context_ids:
                feedback = (
                    "本轮 retrieve_clauses 已返回相关条款，但你的回答没有调用 cite_clauses "
                    "声明引用。请基于检索结果调用 cite_clauses 声明回答依据的条款 id"
                    "（必须取自本次检索结果），随后给出引用后的完整回答。若你认为检索结果"
                    "不足以支撑正式引用，请在回答中明确说明依据不足，不得编造引用。"
                )
            else:
                feedback = (
                    "你的回答没有调用 cite_clauses 声明引用，且本轮没有可用的检索结果。"
                    "请先调用 retrieve_clauses 检索规范条款；若确认无相关依据，请在回答中"
                    "明确说明未检索到相关内容，不得编造引用。"
                )
            return {
                "messages": _validator_feedback_messages(feedback),
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

    def finalize(state: AgenticRAGState) -> dict[str, Any]:
        answer: str = ""
        for m in reversed(state.get("messages", [])):
            if isinstance(m, AIMessage) and isinstance(m.content, str):
                answer = m.content
                break
        status = state.get("status", STATUS_FAILED)
        errors = list(state.get("errors", []))

        # 输入护栏已阻断：直接收口 blocked，不再跑输出护栏。
        if status == STATUS_BLOCKED:
            if trace is not None and trace.started:
                trace.finish(
                    status=STATUS_BLOCKED,
                    error=errors[-1] if errors else None,
                    citations=list(state.get("citations", [])),
                    retry_count=int(state.get("retry_count", 0)),
                )
            return {"answer": "", "status": STATUS_BLOCKED}

        # 输出护栏：仅在引用校验通过（succeeded）且有答案时执行；与引用校验组合。
        if status == STATUS_SUCCEEDED and answer:
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
            if not outcome.allowed:
                errors.append(
                    f"输出未通过护栏校验（{outcome.decision.rule_id}）：{outcome.decision.detail}"
                )
                status = STATUS_BLOCKED
                answer = ""
            else:
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

        if trace is not None and trace.started:
            trace.finish(
                status=status,
                error=errors[-1] if (status in (STATUS_FAILED, STATUS_BLOCKED) and errors) else None,
                citations=list(state.get("citations", [])),
                retry_count=int(state.get("retry_count", 0)),
            )
        return {"answer": answer, "status": status, "errors": errors}

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
    react_agent = create_react_agent(
        model=budget_model,
        tools=tools,
        state_schema=AgenticRAGState,
        checkpointer=None,
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
    "retry_count": 0,
    "empty_citation_reflows": 0,
}


async def astream_agentic(
    app,
    query: str,
    config: dict,
    *,
    on_token: Callable[[str], None] | None = None,
    messages: Sequence[BaseMessage] | None = None,
    on_event: Callable[[str, Any], None] | None = None,
    memory_context: Any | None = None,
    upload_context: Any | None = None,
) -> dict[str, Any]:
    """以 messages+values 双模式流式运行 agentic 图，返回最终状态。

    - ``stream_mode=["messages", "values"]`` 的事件形态为 ``(mode, payload)``：
      ``("values", dict)`` 为每步完整状态（最后一个为 finalize 后的终态）；
      ``("messages", (message_chunk, metadata))`` 为模型输出（含 tool call 与文本）；
    - ``on_token``：收到模型文本时回调（CLI 借此实时打印）。tool-call 轮经由
      ``_BudgetChatModel._stream`` 同步计数，超预算抛 ``ToolBudgetExceeded``；
    - ``messages``：可选的初始消息列表（Step 8 用于注入运行时上下文）；
      默认 ``[HumanMessage(content=query)]``；
    - ``on_event``：可选的只读 stream 观察回调。回调收到 ``messages`` 或 ``values``
      原始事件，调用方必须自行决定安全摘要，不能把完整 state 直接发送给浏览器；
    - ``memory_context``：可选的已授权长期记忆召回结果，只进入独立 data 段，不能改变
      registry、capability、引用校验或程序化计算路径；
    - ``upload_context``：可选的已授权上传资料召回结果，同上——只是一个 RETRIEVED_FILE
      数据段，不承载指令、不参与数值推导、不改变引用校验。
    """
    final_state: dict[str, Any] = {}
    if messages is None:
        messages = [HumanMessage(content=query)]
    # 两个 *_context 是**本轮**的召回结果，必须每轮无条件写入（没有就写 None）。
    # 它们是无 reducer 的 state 键，会随 checkpoint 持久化：只在"有值"时才写，
    # 下一轮不传时图里留着的是上一轮的值——而且经生产 serde（allowed_msgpack_modules
    # 只放行 CitationClaim）读回来已不是 MemoryRecallResult/UploadRecallResult，
    # 只是个 dict，PromptAssembler 当场 TypeError（第二问与上传无关、或记忆降级即崩，
    # 两轮实测复现）。即便类型能读回，把上一问的检索资料塞进这一问也是错的上下文。
    input_state: dict[str, Any] = {
        "messages": messages,
        "query": query,
        "memory_context": memory_context,
        "upload_context": upload_context,
        **PER_TURN_RESET,
    }
    async for event in app.astream(
        input_state,
        config=config,
        stream_mode=["messages", "values"],
    ):
        if not isinstance(event, tuple) or len(event) != 2:
            continue
        mode, payload = event
        if on_event is not None:
            on_event(mode, payload)
        if mode == "values" and isinstance(payload, dict):
            final_state = payload
        elif mode == "messages" and isinstance(payload, tuple) and payload:
            chunk = payload[0]
            content = getattr(chunk, "content", "")
            if isinstance(content, str) and content and on_token is not None:
                on_token(content)
    return final_state
