"""Agentic RAG 外层循环（Step 6）。

``create_react_agent`` 的标准行为是"模型 → 工具 → 模型……直到模型不再调用工具"，
它**没有**"最终回答后再插入引用校验并回流"的阶段。因此引用校验由外层图承担：

```text
START → agent_loop(create_react_agent 子图)
             ↓
        citation_validator
          ├─ 通过 → finalize（成功）
          ├─ 失败且有预算 → 注入带错误文本的 ToolMessage → 回 agent_loop
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
from collections.abc import Callable
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

from water_agent.domain.citation import CitationClaim, validate_citations
from water_agent.domain.state import MAX_CITATION_RETRIES, STATUS_FAILED, STATUS_SUCCEEDED

# 外层图安全网：create_react_agent 内部也有递归上限，这里统一压到 20
RECURSION_LIMIT = 20
# 校验器注入回流消息时使用的合成 tool_call_id（不与任何真实工具调用冲突）
_VALIDATOR_CALL_ID = "citation-validator"

# Step 7 起 agentic 循环带默认真实系统提示（Step 8 升级为可调用装配器）。
# 落到三条铁律：引用可追溯（只引检索结果内条款）／计算程序化（工具返回数值）／不猜测。
DEFAULT_AGENTIC_PROMPT = (
    "你是给排水专业标准问答助手，处于工具调用型 Agent 环境（可用工具："
    "retrieve_clauses 检索规范条款、cite_clauses 声明引用、calc_* 水力计算）。\n"
    "执行规则：\n"
    "1. 引用可追溯：回答只能依据 retrieve_clauses 检索结果中真实存在的条款，并调用 "
    "cite_clauses 用条款 id（如 GB50015-2019 5.4.2）声明引用；禁止编造规范号或条文。\n"
    "2. 计算程序化：涉及数值计算一律调用计算工具（如 calc_velocity），使用工具返回的"
    "数值，不得自行口算。\n"
    "3. 检索结果不足以回答时，明确说明未检索到相关内容；参数缺失时说明缺什么，不猜测"
    "补默认值。\n"
    "4. 回答专业、简洁、可追溯。"
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


class _BudgetChatModel(BaseChatModel):
    """给任意模型套工具调用轮数预算，并防止同 id 消息被 add_messages 去重。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    inner: BaseChatModel
    max_tool_turns: int
    holder: dict

    @property
    def _llm_type(self) -> str:
        return "budget-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = self.inner._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
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
        for chunk in itertools.chain((first_chunk,), inner):
            if isinstance(chunk, AIMessageChunk):
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


def make_agent_loop(react_agent, holder: dict):
    """agent_loop 节点：重置预算计数后运行 react agent 子图；超预算转结构化错误。"""

    def agent_loop(state: AgenticRAGState, config) -> dict[str, Any]:
        holder["turns"] = 0
        try:
            result = react_agent.invoke(state, config=config)
            return dict(result)
        except ToolBudgetExceeded as exc:
            return {
                "status": STATUS_FAILED,
                "errors": list(state.get("errors", [])) + [str(exc)],
            }

    return agent_loop


def make_citation_validator():
    """citation_validator 节点：引用校验 → 通过收口 / 失败回流（≤2 次）/ 失败收口。"""

    def citation_validator(state: AgenticRAGState) -> dict[str, Any]:
        context_ids, cited_ids = _extract_validation_signal(state)
        check = validate_citations(cited_ids, context_ids)
        errors = list(state.get("errors", [])) + list(check.errors)
        citations = [
            CitationClaim(source_id=cid, declared_by="model") for cid in cited_ids
        ]
        if check.ok:
            return {"status": STATUS_SUCCEEDED, "citations": citations, "errors": errors}

        retry_count = state.get("retry_count", 0)
        if retry_count < MAX_CITATION_RETRIES:
            feedback = "\n".join(check.errors) or "引用校验未通过"
            return {
                "messages": [
                    ToolMessage(
                        content=feedback,
                        name="citation_validator",
                        tool_call_id=_VALIDATOR_CALL_ID,
                    )
                ],
                "retry_count": retry_count + 1,
                "errors": errors,
            }
        errors.append(f"达到引用校验重试上限（{MAX_CITATION_RETRIES}），仍有无效引用")
        return {"status": STATUS_FAILED, "errors": errors}

    return citation_validator


def make_finalize():
    """finalize 节点：从最后一条 AI 消息提取 answer，保留最终 status。"""

    def finalize(state: AgenticRAGState) -> dict[str, Any]:
        answer: str = ""
        for m in reversed(state.get("messages", [])):
            if isinstance(m, AIMessage) and isinstance(m.content, str):
                answer = m.content
                break
        return {"answer": answer, "status": state.get("status", STATUS_FAILED)}

    return finalize


def build_agentic_app(
    model,
    tools,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    prompt: str | None = None,
    max_tool_turns: int = 10,
):
    """构建 agentic RAG 外层图。

    - ``model``：任意 chat model（真实或 fake）；内部自动套轮数预算；
    - ``tools``：``BaseTool`` 列表（经 ``tool_spec_to_base_tool`` 转换）；
    - ``checkpointer``：必配（thread_id 会话隔离，护住会话纪律）；
    - ``prompt``：系统提示；缺省用 ``DEFAULT_AGENTIC_PROMPT``（Step 7 起
      真实链路带默认真实提示，Step 8 升级为可调用装配器）；
    - ``max_tool_turns``：agent_loop 内模型工具调用轮数独立预算。
    """
    if checkpointer is None:
        raise ValueError("agentic 循环必须配置 checkpointer（thread_id 会话隔离）")

    holder: dict[str, int] = {"turns": 0}
    budget_model = _BudgetChatModel(inner=model, max_tool_turns=max_tool_turns, holder=holder)
    react_agent = create_react_agent(
        model=budget_model,
        tools=tools,
        state_schema=AgenticRAGState,
        checkpointer=None,
        prompt=prompt if prompt is not None else DEFAULT_AGENTIC_PROMPT,
    )

    graph = StateGraph(AgenticRAGState)
    graph.add_node("agent_loop", make_agent_loop(react_agent, holder))
    graph.add_node("citation_validator", make_citation_validator())
    graph.add_node("finalize", make_finalize())

    def route_after_agent(state: AgenticRAGState) -> str:
        if state.get("status") == STATUS_FAILED:
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


async def astream_agentic(
    app,
    query: str,
    config: dict,
    *,
    on_token: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """以 messages+values 双模式流式运行 agentic 图，返回最终状态。

    - ``stream_mode=["messages", "values"]`` 的事件形态为 ``(mode, payload)``：
      ``("values", dict)`` 为每步完整状态（最后一个为 finalize 后的终态）；
      ``("messages", (message_chunk, metadata))`` 为模型输出（含 tool call 与文本）；
    - ``on_token``：收到模型文本时回调（CLI 借此实时打印）。tool-call 轮经由
      ``_BudgetChatModel._stream`` 同步计数，超预算抛 ``ToolBudgetExceeded``。
    """
    final_state: dict[str, Any] = {}
    async for event in app.astream(
        {"messages": [HumanMessage(content=query)], "query": query},
        config=config,
        stream_mode=["messages", "values"],
    ):
        if not isinstance(event, tuple) or len(event) != 2:
            continue
        mode, payload = event
        if mode == "values" and isinstance(payload, dict):
            final_state = payload
        elif mode == "messages" and isinstance(payload, tuple) and payload:
            chunk = payload[0]
            content = getattr(chunk, "content", "")
            if isinstance(content, str) and content and on_token is not None:
                on_token(content)
    return final_state
