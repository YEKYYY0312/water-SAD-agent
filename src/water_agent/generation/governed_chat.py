"""受治理的 chat model 包装（P2.5 Step 12）。

三个 DeepSeek chat 出口（agentic 循环、docgen 润色、judge）都经 ``ChatOpenAI``。
本模块提供 :class:`GovernedChatModel`，把每次 ``invoke``/``_generate`` 包进
``ProviderGateway``：限流、预算预留、有界重试、429 退避、熔断与 usage 对账。

流式：``_stream`` 经 ``ProviderGateway.stream_call``，发送前检查与 ``_generate`` 相同，
重试只在首个 chunk 之前，usage 取最后一个带 ``usage_metadata`` 的 chunk（内层须开启
``stream_usage``，见 ``chat_model.py``）。是否走流式仍由 LangChain 判定，``invoke`` 的默认
行为不变：只有挂了流式回调或显式 ``stream=True`` 才流式；内层不支持流式时一律走
``_generate``（见 :meth:`GovernedChatModel._should_stream`）。

**为什么放在 `generation/` 而不是 `runtime/governance/`**：治理层不得依赖
langchain（`tests/test_governance_contracts.py` 锁死这条），而本包装必须实现
``BaseChatModel`` 接口才能被 ``create_react_agent`` 消费。治理层保持纯契约，
框架适配留在业务侧 —— 与 `tools/ds_adapters.py` 的分层思路一致。
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableBinding
from pydantic import ConfigDict

from water_agent.runtime.governance.binding import default_gateway, local_request_context
from water_agent.runtime.governance.contracts import (
    CallCategory,
    Capability,
    ProviderCall,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import ProviderGateway, ProviderUsage


def usage_from_langchain_or_none(response: Any) -> ProviderUsage | None:
    """与 :func:`usage_from_langchain` 同一口径，但 usage 缺失 / 形态不符时返回 ``None``。

    流式逐块取 usage 用：绝大多数 chunk 不带 usage，``None`` 表示"这一块没有"，由网关
    取流里最后一个非空值。不能拿 ``ProviderUsage.unknown()`` 占位——那会把先前真正的
    usage 覆盖掉。
    """
    meta = getattr(response, "usage_metadata", None)
    if not meta:
        # 回退到 response_metadata.token_usage（部分 provider 只填这里）
        rmeta = getattr(response, "response_metadata", None)
        if isinstance(rmeta, dict):
            token_usage = rmeta.get("token_usage")
            if isinstance(token_usage, dict):
                prompt = token_usage.get("prompt_tokens")
                completion = token_usage.get("completion_tokens")
                if isinstance(prompt, int) and isinstance(completion, int):
                    return ProviderUsage.of(input_tokens=prompt, output_tokens=completion)
        return None

    def _get(name: str) -> Any:
        if isinstance(meta, dict):
            return meta.get(name)
        return getattr(meta, name, None)

    input_tokens = _get("input_tokens")
    output_tokens = _get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None

    cached = 0
    details = _get("input_token_details")
    if isinstance(details, dict) and isinstance(details.get("cache_read"), int):
        cached = int(details["cache_read"])
    return ProviderUsage.of(
        input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached
    )


def usage_from_langchain(response: Any) -> ProviderUsage:
    """从 langchain 响应提取 usage。

    支持 ``usage_metadata``（dict 或对象）两种形态；缺失/形态不符 → ``unknown``，
    **不估算**（规格 §4：usage 不可得必须标记，估算不得当账单）。
    """
    usage = usage_from_langchain_or_none(response)
    return usage if usage is not None else ProviderUsage.unknown()


def _chunk_usage(chunk: ChatGenerationChunk) -> ProviderUsage | None:
    """流式 chunk 的 usage（OpenAI 兼容接口开 include_usage 后只在最后一个 chunk 上）。"""
    return usage_from_langchain_or_none(chunk.message)


def _supports_streaming(model: Any) -> bool:
    """内层是否真的实现了 ``_stream``；``RunnableBinding`` 取其 ``bound`` 判断。

    与 LangChain 自己的判定同一口径：``_stream`` 未被覆写即不支持流式
    （``BaseChatModel._stream`` 的默认实现只会抛 ``NotImplementedError``）。
    """
    while isinstance(model, RunnableBinding):
        model = model.bound
    return isinstance(model, BaseChatModel) and type(model)._stream is not BaseChatModel._stream


class GovernedChatModel(BaseChatModel):
    """把内层 chat model 的每次生成包进 ``ProviderGateway``。

    透传 ``bind_tools`` 等能力所需的属性访问（``__getattr__``），
    因此 ``create_react_agent`` 可以像用原模型一样用它。

    Attributes:
        inner: 被包装的真实 chat model。
        category: 调用分类（限流/预算/缓存资格按分类区分）。
        model_name_hint: 模型名（账本与价格查询用）；缺省从 inner 推断。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    inner: BaseChatModel
    category: CallCategory = CallCategory.AGENTIC_LOOP
    model_name_hint: str = ""
    gateway: ProviderGateway | None = None
    request_context: RequestContext | None = None
    #: 会话级上下文工厂：由 CLI/runner 提供（如 ``AgenticSession`` 在创建 thread_id 后
    #: 提供 ``lambda: local_request_context(thread_id=self.thread_id)``）。与固定
    #: ``request_context`` 二选一；两者都未提供时回落**进程级默认主体**（仍经
    #: default_gateway，不绕过治理，但主体未按会话隔离——仅测试/向后兼容路径）。
    request_context_factory: Callable[[], RequestContext] | None = None

    @property
    def _llm_type(self) -> str:
        return f"governed:{self.inner._llm_type}"

    def _resolve_gateway(self) -> ProviderGateway:
        return self.gateway if self.gateway is not None else default_gateway()

    def _resolve_context(self) -> RequestContext:
        if self.request_context is not None:
            return self.request_context
        if self.request_context_factory is not None:
            return self.request_context_factory()
        return local_request_context(thread_id=f"chat-{self.category.value}")

    def _resolve_model_name(self) -> str:
        if self.model_name_hint:
            return self.model_name_hint
        for attr in ("model_name", "model"):
            value = getattr(self.inner, attr, None)
            if isinstance(value, str) and value:
                return value
        return "unknown"

    def _estimate_input_tokens(self, messages: Sequence[BaseMessage]) -> int:
        """粗略估算（仅用于限流/预算预留，绝非账单依据）。"""
        total = 0
        for message in messages:
            content = getattr(message, "content", "")
            total += len(content) if isinstance(content, str) else len(str(content))
        return max(1, total)

    def _provider_call(self, messages: Sequence[BaseMessage]) -> ProviderCall:
        """本次生成的调用意图（``_generate`` 与 ``_stream`` 共用，保证两条路径同一口径）。"""
        return ProviderCall(
            provider=ProviderKind.DEEPSEEK_CHAT,
            category=self.category,
            model=self._resolve_model_name(),
            temperature=float(getattr(self.inner, "temperature", 0.0) or 0.0),
            estimated_input_tokens=self._estimate_input_tokens(messages),
            required_capability=Capability.LLM_INFERENCE,
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        call = self._provider_call(messages)

        def executor() -> tuple[ChatResult, ProviderUsage]:
            result = self.inner._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            usage = ProviderUsage.unknown()
            if result.generations:
                usage = usage_from_langchain(result.generations[0].message)
            return result, usage

        return self._resolve_gateway().call(self._resolve_context(), call, executor).value

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """逐块透传内层的流式输出，每次流式生成经 ``ProviderGateway.stream_call``。

        内层不支持流式时**不进网关**（不限流、不预留、不计数、不落账），直接抛
        ``NotImplementedError``：上层 ``_BudgetChatModel._stream`` 据此回退到 ``_generate``。
        ``tools`` 等 kwargs 原样透传给内层。
        """
        if not _supports_streaming(self.inner):
            raise NotImplementedError(
                f"内层模型 {type(self.inner).__name__} 未实现流式（_stream）"
            )
        call = self._provider_call(messages)

        def opener() -> Iterator[ChatGenerationChunk]:
            return self.inner._stream(messages, stop=stop, run_manager=run_manager, **kwargs)

        yield from self._resolve_gateway().stream_call(
            self._resolve_context(), call, opener, usage_of=_chunk_usage
        )

    def _should_stream(self, **kwargs: Any) -> bool:
        """内层不支持流式时一律不走流式；其余照 LangChain 原判定（流式回调 / ``stream=True``）。

        LangChain 只看 ``type(self)._stream`` 有没有被覆写，而本包装总是覆写：不在这里拦下，
        包着非流式模型（如测试 fake）时一挂流式回调就会调到 ``_stream`` 并抛
        ``NotImplementedError``——覆写 ``_stream`` 之前这条路径回落的是 ``_generate``。
        """
        return _supports_streaming(self.inner) and super()._should_stream(**kwargs)

    def _should_use_protocol_streaming(self, **kwargs: Any) -> bool:
        """v2 流式回调路径同 :meth:`_should_stream`：内层不能流式就走 ``_generate``。"""
        return _supports_streaming(self.inner) and super()._should_use_protocol_streaming(**kwargs)

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        """把 tools 绑在**本包装**上，保证绑定之后的每次调用仍经 gateway。

        工具格式化交给内层（OpenAI function schema、``tool_choice`` 等与 provider 相关），
        取出它产出的绑定参数再改绑到 ``self``：调用时这些参数作为 ``_generate`` /
        ``_stream`` 的 kwargs 原样透传给内层。

        此前写成 ``GovernedChatModel(inner=<绑定产物>)``：langchain-core 1.6 的
        ``bind_tools`` 返回 ``RunnableBinding`` 子类，``inner: BaseChatModel`` 的 pydantic
        校验必然 ValidationError——任何 ``bind_tools`` 调用都会抛错（实测）。
        """
        bound = self.inner.bind_tools(tools, **kwargs)
        if not isinstance(bound, RunnableBinding) or bound.bound is not self.inner:
            # 还原不成"内层 + 绑定参数"：拒绝，而不是返回一个绕过 gateway 的 runnable
            raise TypeError(
                f"{type(self.inner).__name__}.bind_tools 返回了 {type(bound).__name__}，"
                "无法在保持治理包装的前提下绑定工具"
            )
        rebound = self.bind(**bound.kwargs)
        return rebound.with_config(bound.config) if bound.config else rebound

    def __getattr__(self, name: str) -> Any:
        """未定义属性透传给内层模型（保持与原模型的鸭子类型兼容）。"""
        # pydantic BaseModel 的 __getattr__ 只在常规查找失败后被调用
        try:
            inner = object.__getattribute__(self, "__dict__")["inner"]
        except KeyError:  # pragma: no cover — 构造未完成时
            raise AttributeError(name) from None
        return getattr(inner, name)


__all__ = ["GovernedChatModel", "usage_from_langchain", "usage_from_langchain_or_none"]
