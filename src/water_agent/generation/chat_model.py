"""真实 chat 模型装配（Step 7）：DeepSeek OpenAI 兼容 function calling。

本步只实现 DeepSeek 官方 OpenAI 兼容路径（``ChatOpenAI`` + 自定义
``base_url``），暂不引入多 provider 抽象。缺 Key 必须响亮失败（与
``generation/llm.py`` 的 ``LLMGenerator`` 风格一致），无 Key 时的离线闭环
由 ``--mock-generator`` / fake 承担，禁止静默降级。

P2.5（Step 12）：返回的模型被 ``GovernedChatModel`` 包裹，每次生成都经
``ProviderGateway``（限流/预算/有界重试/429 退避/熔断/usage 对账）。
"""
from __future__ import annotations

import os
from collections.abc import Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from water_agent.generation.governed_chat import GovernedChatModel
from water_agent.runtime.governance.binding import default_gateway
from water_agent.runtime.governance.contracts import (
    CallCategory,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import ProviderGateway

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"


def build_chat_model(
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    temperature: float = 0.0,
    category: CallCategory = CallCategory.AGENTIC_LOOP,
    gateway: ProviderGateway | None = None,
    request_context: RequestContext | None = None,
    request_context_factory: Callable[[], RequestContext] | None = None,
) -> BaseChatModel:
    """构造 DeepSeek（OpenAI 兼容）chat model，供 agentic 循环使用。

    规范问答是事实问答（铁律②：回答可追溯、不编造）：``temperature`` 默认 0，
    调用方可显式覆盖。缺 Key 抛 ``ValueError`` 并指明环境变量名——"失败要响亮"，
    不允许静默降级为本地/离线模型。

    P2.5 Step 12：返回 ``GovernedChatModel``，每次生成经 ``ProviderGateway``。
    显式 ``timeout`` 取自 gateway 的 provider 画像（补上原先缺失的超时）；
    ``ChatOpenAI`` 自带重试必须关掉（``max_retries=0``），否则会与 gateway
    的重试**叠乘**，实际请求数变成两者之积。
    """
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise ValueError(f"{api_key_env} must not be empty")
    resolved_gateway = gateway if gateway is not None else default_gateway()
    inner = ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=SecretStr(api_key),
        temperature=temperature,
        timeout=resolved_gateway.timeout_for(ProviderKind.DEEPSEEK_CHAT),
        max_retries=0,  # 重试统一由 ProviderGateway 承担，避免叠乘
    )
    return GovernedChatModel(
        inner=inner,
        category=category,
        model_name_hint=model,
        gateway=gateway,
        request_context=request_context,
        request_context_factory=request_context_factory,
    )
