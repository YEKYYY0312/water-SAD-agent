"""真实 chat 模型装配（Step 7）：DeepSeek OpenAI 兼容 function calling。

OpenAI 兼容路径（``ChatOpenAI`` + 自定义 ``base_url``），不引入多 provider 抽象。缺 Key 必须响亮
失败（与 ``generation/llm.py`` 的 ``LLMGenerator`` 风格一致），无 Key 时的离线闭环由
``--mock-generator`` / fake 承担，禁止静默降级。

P7-S10：端点 / 模型 / Key 名没显式给出时取 ``generation/endpoints.py::resolve_chat_endpoint()``
（缺省仍是 DeepSeek），本函数只是构造器，不再自带一份缺省值。

P2.5（Step 12）：返回的模型被 ``GovernedChatModel`` 包裹，每次生成都经
``ProviderGateway``（限流/预算/有界重试/429 退避/熔断/usage 对账）；流式生成经
``ProviderGateway.stream_call``（重试只在首个 chunk 之前）。
"""
from __future__ import annotations

from collections.abc import Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from water_agent.config.credentials import resolve_secret
from water_agent.generation.endpoints import (
    DEFAULT_CHAT_API_KEY,
    DEFAULT_CHAT_BASE_URL,
    DEFAULT_CHAT_MODEL,
    resolve_chat_endpoint,
)
from water_agent.generation.governed_chat import GovernedChatModel
from water_agent.runtime.governance.binding import default_gateway
from water_agent.runtime.governance.contracts import (
    CallCategory,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import ProviderGateway

# 历史名字保留（调用方与测试按 chat_model.DEFAULT_* 引用）；值的唯一来源是 endpoints。
DEFAULT_MODEL = DEFAULT_CHAT_MODEL
DEFAULT_BASE_URL = DEFAULT_CHAT_BASE_URL
DEFAULT_API_KEY_ENV = DEFAULT_CHAT_API_KEY


def build_chat_model(
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.0,
    category: CallCategory = CallCategory.AGENTIC_LOOP,
    gateway: ProviderGateway | None = None,
    request_context: RequestContext | None = None,
    request_context_factory: Callable[[], RequestContext] | None = None,
) -> BaseChatModel:
    """构造 OpenAI 兼容 chat model（缺省 DeepSeek），供 agentic 循环使用。

    ``model`` / ``base_url`` / ``api_key_env`` 为 ``None`` 时按 ``resolve_chat_endpoint`` 解析；给了的项
    作为显式覆盖交给同一个解析函数（Key 名的回落规则也在那里：端点改了就不回落 ``DEEPSEEK_API_KEY``）。

    规范问答是事实问答（铁律②：回答可追溯、不编造）：``temperature`` 默认 0，
    调用方可显式覆盖。缺 Key 抛 ``ValueError`` 并指明环境变量名——"失败要响亮"，
    不允许静默降级为本地/离线模型。

    P2.5 Step 12：返回 ``GovernedChatModel``，每次生成经 ``ProviderGateway``。
    显式 ``timeout`` 取自 gateway 的 provider 画像（补上原先缺失的超时）；
    ``ChatOpenAI`` 自带重试必须关掉（``max_retries=0``），否则会与 gateway
    的重试**叠乘**，实际请求数变成两者之积。
    """
    endpoint = resolve_chat_endpoint(base_url=base_url, model=model, api_key_env=api_key_env)
    model = endpoint.model
    base_url = endpoint.base_url
    api_key_env = endpoint.api_key_name or DEFAULT_CHAT_API_KEY
    resolved_api_key = (api_key or endpoint.api_key_literal or resolve_secret(api_key_env) or "").strip()
    if not resolved_api_key:
        raise ValueError(
            f"{api_key_env} must not be empty"
            f"（可设同名环境变量、{api_key_env}_FILE，或在 Web 设置面板填写）"
        )
    resolved_gateway = gateway if gateway is not None else default_gateway()
    inner = ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=SecretStr(resolved_api_key),
        temperature=temperature,
        timeout=resolved_gateway.timeout_for(ProviderKind.DEEPSEEK_CHAT),
        max_retries=0,  # 重试统一由 ProviderGateway 承担，避免叠乘
        # 流式时请求 stream_options.include_usage，让最后一个 chunk 带 usage（DeepSeek 支持）。
        # 必须显式开：langchain-openai 1.6.0 只在默认 OpenAI base_url 下自动开启，自定义
        # base_url 时 stream_usage 留空 → 不发 include_usage → 流式 usage 恒为 USAGE_UNKNOWN、
        # 预算按 0 结清（实测）。非流式请求不受影响（该参数只作用于 _stream）。
        stream_usage=True,
    )
    return GovernedChatModel(
        inner=inner,
        category=category,
        model_name_hint=model,
        gateway=gateway,
        request_context=request_context,
        request_context_factory=request_context_factory,
    )
