"""受治理的 chat model 包装（P2.5 Step 12）。

三个 DeepSeek chat 出口（agentic 循环、docgen 润色、judge）都经 ``ChatOpenAI``。
本模块提供 :class:`GovernedChatModel`，把每次 ``invoke``/``_generate`` 包进
``ProviderGateway``：限流、预算预留、有界重试、429 退避、熔断与 usage 对账。

**为什么放在 `generation/` 而不是 `runtime/governance/`**：治理层不得依赖
langchain（`tests/test_governance_contracts.py` 锁死这条），而本包装必须实现
``BaseChatModel`` 接口才能被 ``create_react_agent`` 消费。治理层保持纯契约，
框架适配留在业务侧 —— 与 `tools/ds_adapters.py` 的分层思路一致。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
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


def usage_from_langchain(response: Any) -> ProviderUsage:
    """从 langchain 响应提取 usage。

    支持 ``usage_metadata``（dict 或对象）两种形态；缺失/形态不符 → ``unknown``，
    **不估算**（规格 §4：usage 不可得必须标记，估算不得当账单）。
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
        return ProviderUsage.unknown()

    def _get(name: str) -> Any:
        if isinstance(meta, dict):
            return meta.get(name)
        return getattr(meta, name, None)

    input_tokens = _get("input_tokens")
    output_tokens = _get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return ProviderUsage.unknown()

    cached = 0
    details = _get("input_token_details")
    if isinstance(details, dict) and isinstance(details.get("cache_read"), int):
        cached = int(details["cache_read"])
    return ProviderUsage.of(
        input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached
    )


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

    @property
    def _llm_type(self) -> str:
        return f"governed:{self.inner._llm_type}"

    def _resolve_gateway(self) -> ProviderGateway:
        return self.gateway if self.gateway is not None else default_gateway()

    def _resolve_context(self) -> RequestContext:
        if self.request_context is not None:
            return self.request_context
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

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        call = ProviderCall(
            provider=ProviderKind.DEEPSEEK_CHAT,
            category=self.category,
            model=self._resolve_model_name(),
            temperature=float(getattr(self.inner, "temperature", 0.0) or 0.0),
            estimated_input_tokens=self._estimate_input_tokens(messages),
            required_capability=Capability.LLM_INFERENCE,
        )

        def executor() -> tuple[ChatResult, ProviderUsage]:
            result = self.inner._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            usage = ProviderUsage.unknown()
            if result.generations:
                usage = usage_from_langchain(result.generations[0].message)
            return result, usage

        return self._resolve_gateway().call(self._resolve_context(), call, executor).value

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        """把 tools 绑到内层模型，并保持治理包装（否则绑定后即绕过 gateway）。"""
        bound = self.inner.bind_tools(tools, **kwargs)
        return GovernedChatModel(
            inner=bound,  # type: ignore[arg-type]
            category=self.category,
            model_name_hint=self._resolve_model_name(),
            gateway=self.gateway,
            request_context=self.request_context,
        )

    def __getattr__(self, name: str) -> Any:
        """未定义属性透传给内层模型（保持与原模型的鸭子类型兼容）。"""
        # pydantic BaseModel 的 __getattr__ 只在常规查找失败后被调用
        try:
            inner = object.__getattribute__(self, "__dict__")["inner"]
        except KeyError:  # pragma: no cover — 构造未完成时
            raise AttributeError(name) from None
        return getattr(inner, name)


__all__ = ["GovernedChatModel", "usage_from_langchain"]
