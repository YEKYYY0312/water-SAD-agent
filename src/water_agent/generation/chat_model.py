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

import json
from collections.abc import Callable
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from water_agent.config.credentials import resolve_secret
from water_agent.generation.endpoints import (
    _MODEL_NAME_RE,
    DEFAULT_CHAT_API_KEY,
    DEFAULT_CHAT_BASE_URL,
    DEFAULT_CHAT_MODEL,
    MAX_MODEL_NAME_LENGTH,
    PANEL_HTTP_HOSTS,
    EndpointConfigError,
    resolve_chat_endpoint,
    validate_panel_base_url,
)
from water_agent.generation.governed_chat import GovernedChatModel
from water_agent.mcp.errors import MCPError
from water_agent.mcp.http_transport import (
    Resolver,
    TransportFailure,
    open_exchange,
    parse_server_url,
)
from water_agent.runtime.governance.binding import default_gateway
from water_agent.runtime.governance.contracts import (
    CallCategory,
    Principal,
    ProviderCall,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import GovernanceError, ProviderGateway, ProviderUsage

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
    extra_body: dict[str, Any] | None = None,
) -> BaseChatModel:
    """构造 OpenAI 兼容 chat model（缺省 DeepSeek），供 agentic 循环使用。

    ``model`` / ``base_url`` / ``api_key_env`` 为 ``None`` 时按 ``resolve_chat_endpoint`` 解析；给了的项
    作为显式覆盖交给同一个解析函数（Key 名的回落规则也在那里：端点改了就不回落 ``DEEPSEEK_API_KEY``）。

    规范问答是事实问答（铁律②：回答可追溯、不编造）：``temperature`` 默认 0，
    调用方可显式覆盖。缺 Key 抛 ``ValueError`` 并指明环境变量名——"失败要响亮"，
    不允许静默降级为本地/离线模型。

    ``extra_body``（P7-S15）：附加进每个请求体的字段（如关闭思考 ``{"thinking": {"type": "disabled"}}``）。
    **仅给出时才传给 ``ChatOpenAI``**，缺省路径的构造参数与此前逐字相同；装配与兼容性探针必须带同一份
    （``model_catalog.extra_body_for``），否则探针验证的不是上线要用的配置。

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
    chat_kwargs: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": SecretStr(resolved_api_key),
        "temperature": temperature,
        "timeout": resolved_gateway.timeout_for(ProviderKind.DEEPSEEK_CHAT),
        "max_retries": 0,  # 重试统一由 ProviderGateway 承担，避免叠乘
        # 流式时请求 stream_options.include_usage，让最后一个 chunk 带 usage（DeepSeek 支持）。
        # 必须显式开：langchain-openai 1.6.0 只在默认 OpenAI base_url 下自动开启，自定义
        # base_url 时 stream_usage 留空 → 不发 include_usage → 流式 usage 恒为 USAGE_UNKNOWN、
        # 预算按 0 结清（实测）。非流式请求不受影响（该参数只作用于 _stream）。
        "stream_usage": True,
    }
    if extra_body:
        chat_kwargs["extra_body"] = extra_body
    inner = ChatOpenAI(**chat_kwargs)
    return GovernedChatModel(
        inner=inner,
        category=category,
        model_name_hint=model,
        gateway=gateway,
        request_context=request_context,
        request_context_factory=request_context_factory,
    )


# ---------------------------------------------------------------------------
# 获取模型列表（设置面板）
# ---------------------------------------------------------------------------

MODEL_LIST_TIMEOUT_SECONDS = 10.0
MODEL_LIST_MAX_BYTES = 256 * 1024
MODEL_LIST_MAX_ITEMS = 200

MODEL_LIST_UNAUTHORIZED = "WEB_LLM_MODELS_UNAUTHORIZED"
MODEL_LIST_UNSUPPORTED = "WEB_LLM_MODELS_UNSUPPORTED"
MODEL_LIST_UNREACHABLE = "WEB_LLM_MODELS_UNREACHABLE"
MODEL_LIST_FORBIDDEN = "WEB_LLM_MODELS_DESTINATION_FORBIDDEN"
MODEL_LIST_INVALID = "WEB_LLM_ENDPOINT_INVALID"


class ModelListError(Exception):
    """获取模型列表失败。``code`` 是稳定错误码，``message`` 不含 Key、完整 URL 与上游响应原文。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _model_list_context() -> RequestContext:
    principal = Principal.local("settings-model-list")
    return RequestContext(principal=principal, request_id="settings-model-list", thread_id="settings")


def extract_model_ids(payload: object) -> list[str]:
    """从 ``/models`` 响应里取模型 id：OpenAI 形（``data[].id``）、Ollama 形（``models[].name``/``model``）、纯数组。

    只留合法模型名（字符集与长度同 ``validate_model_name``），去重、保序、最多 ``MODEL_LIST_MAX_ITEMS`` 条。
    上游的其余字段一概不透传。
    """

    if isinstance(payload, dict):
        items = payload.get("data")
        if not isinstance(items, list):
            items = payload.get("models")
    else:
        items = payload
    if not isinstance(items, list):
        return []
    seen: dict[str, None] = {}
    for item in items:
        if isinstance(item, str):
            candidate = item
        elif isinstance(item, dict):
            candidate = next((item[key] for key in ("id", "name", "model") if isinstance(item.get(key), str)), "")
        else:
            continue
        candidate = candidate.strip()
        if candidate and len(candidate) <= MAX_MODEL_NAME_LENGTH and _MODEL_NAME_RE.fullmatch(candidate):
            seen.setdefault(candidate, None)
        if len(seen) >= MODEL_LIST_MAX_ITEMS:
            break
    return list(seen)


def list_models(
    base_url: str,
    api_key: str,
    *,
    gateway: ProviderGateway | None = None,
    resolver: Resolver | None = None,
    ssl_context: object | None = None,
) -> list[str]:
    """对用户填写的对话端点发一次 ``GET {base_url}/models``，返回模型 id 列表。

    这是读取原语（响应内容会回到浏览器），所以走 MCP 网关的出站内核 ``open_exchange``：解析后整体分类目的地址
    （私有 / 回环 / 链路本地 / 云元数据一律拒绝，``PANEL_HTTP_HOSTS`` 里的本机主机例外）、连接钉死校验过的 IP、
    不跟随重定向、响应有字节上限。经 ``ProviderGateway.call`` 限流 / 熔断 / 落账；HTTP 错误不带状态属性，
    所以不会被当成瞬时错误重试。``api_key`` 只进 ``Authorization`` 头，不写进任何异常、日志或返回值。
    """

    try:
        cleaned = validate_panel_base_url(base_url)
        local = (parse_server_url(cleaned, allow_private_network=True).host).lower() in PANEL_HTTP_HOSTS
        target = parse_server_url(f"{cleaned}/models", allow_private_network=local)
    except (EndpointConfigError, ValueError) as exc:
        raise ModelListError(MODEL_LIST_INVALID, str(exc)) from exc
    key = (api_key or "").strip()
    if not key:
        raise ModelListError(MODEL_LIST_UNAUTHORIZED, "没有可用的 API Key：请先填写密钥")

    def executor() -> tuple[list[str], ProviderUsage]:
        exchange = open_exchange(
            target,
            "GET",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            body=None,
            timeout=MODEL_LIST_TIMEOUT_SECONDS,
            allow_private_network=local,
            resolver=resolver,
            ssl_context=ssl_context,  # type: ignore[arg-type]
        )
        try:
            status = exchange.status
            if status in (401, 403):
                raise ModelListError(MODEL_LIST_UNAUTHORIZED, f"端点拒绝了这把 Key（HTTP {status}）")
            if status != 200:
                raise ModelListError(
                    MODEL_LIST_UNSUPPORTED,
                    f"端点没有提供模型列表（HTTP {status}）：请手动输入模型名",
                )
            body = exchange.read_body(MODEL_LIST_MAX_BYTES)
        finally:
            exchange.close()
        try:
            models = extract_model_ids(json.loads(body.decode("utf-8")))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ModelListError(MODEL_LIST_UNSUPPORTED, "端点返回的不是模型列表格式：请手动输入模型名") from exc
        if not models:
            raise ModelListError(MODEL_LIST_UNSUPPORTED, "端点返回的模型列表为空：请手动输入模型名")
        return models, ProviderUsage.unknown()

    context = _model_list_context().model_copy(update={"deadline_seconds": MODEL_LIST_TIMEOUT_SECONDS + 5})
    call = ProviderCall(
        provider=ProviderKind.DEEPSEEK_CHAT,
        category=CallCategory.MODEL_LIST,
        model=f"model-list:{target.host}",
    )
    try:
        return (gateway if gateway is not None else default_gateway()).call(context, call, executor).value
    except GovernanceError as exc:
        cause = exc.__cause__
        if isinstance(cause, ModelListError):
            raise cause from None
        if isinstance(cause, MCPError):
            forbidden = cause.code == "MCP_DESTINATION_FORBIDDEN"
            raise ModelListError(
                MODEL_LIST_FORBIDDEN if forbidden else MODEL_LIST_UNREACHABLE,
                "该地址解析到内网、回环或云元数据地址，已拒绝连接" if forbidden else "无法连接该端点",
            ) from None
        if isinstance(cause, TransportFailure):
            raise ModelListError(MODEL_LIST_UNREACHABLE, "无法连接该端点（连接失败或超时）") from None
        raise ModelListError(MODEL_LIST_UNREACHABLE, f"请求被治理层拒绝或失败（{exc.code}）") from None
