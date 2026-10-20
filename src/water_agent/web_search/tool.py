"""``web_search`` 工具与装配入口（P7-S12，规格 §5.1 / §5.2）。

工具契约（SDD17-I09）：``web_search(query: str, count: int = 3)``——**只有这两个参数**，没有域名 / 时间过滤，没有任何
URL 类参数。目的地由运维写死的基址决定，模型只控制查询字符串。``ToolSpec.result_source = WEB_CONTENT``（图据此置回合
污点）、``timeout_seconds = 15``。

执行路径（全部在 ``execute`` 里，护栏在它**之前**由 ``ToolRegistry.invoke`` 完成）：

``QueryEgressGuard`` → 适配器 ``build_request`` → ``governed_get``（SSRF 分类、钉死 IP、不跟随重定向、字节与时间上限、
``ProviderGateway`` 限流 / 熔断）→ 适配器 ``parse_response`` → 结果流水线 → 审计。任何一步失败都返回固定模板的错误
observation（业务失败走返回值，不走 ``TOOL_ERROR``）；**不回退**到别的 provider、缓存或本地。

``ToolSpec`` 的定义在本文件而不在 ``tools/``：S12 的范围块禁止改 ``tools/**``，治理清单对它的登记由
``inventory.PLUGIN_GRANTED_TOOL_CAPABILITIES`` + 静态扫描 ``scan_tool_spec_names`` 的扩展范围（含 ``web_search/``）承担。

本模块**没有原始网络原语**：出站只经 ``external/http_get.py``。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from water_agent.external.http_get import ExternalGetError, governed_get
from water_agent.runtime.governance.contracts import (
    CallCategory,
    Capability,
    ProviderCall,
    ProviderKind,
    RequestContext,
    SourceTag,
)
from water_agent.tools.base import ToolSpec
from water_agent.web_search import events
from water_agent.web_search.adapters import (
    JSON_MEDIA_TYPE,
    MAX_RESPONSE_BYTES,
    AdapterParseError,
    ProviderAdapter,
    get_adapter,
)
from water_agent.web_search.config import WebSearchConfig
from water_agent.web_search.errors import (
    WEB_SEARCH_BAD_RESPONSE,
    WEB_SEARCH_QUERY_REJECTED,
    WEB_SEARCH_QUOTA,
    code_from_external,
    error_result,
)
from water_agent.web_search.guard import QueryEgressGuard
from water_agent.web_search.results import MAX_COUNT, MIN_COUNT, process_hits
from water_agent.web_search.runtime import WebSearchRuntime

TOOL_NAME = "web_search"
#: 协作式 deadline（``ToolRegistry.invoke`` 执行后校验）；与 ``ProviderKind.WEB_SEARCH`` 画像的 15 s 一致。
TIMEOUT_SECONDS = 15.0
#: 联网请求（含一次重试）的**总**预算：比工具 deadline 少 1 s 余量。网络慢时由网关按预算停止重试、返回稳定码
#: ``WEB_SEARCH_UNAVAILABLE``，而不是撞上工具层的 ``TOOL_TIMEOUT``（后者照样不含上游内容，只是码不稳定）。
NETWORK_BUDGET_SECONDS = TIMEOUT_SECONDS - 1.0
#: 参数模式（含工具定义的完整序列化）的字符预算：Step 0 实测后写入决策记录。
MAX_SCHEMA_CHARS = 500


class WebSearchArgs(BaseModel):
    # 不写类 docstring：pydantic 会把它放进发给模型的 JSON schema（实测 +114 字符），而本工具的预算是 ≤ 500。
    # 只有 query 与 count；extra="forbid" 让多余字段（尤其是 url 一类）直接 INVALID_ARGUMENTS（SDD17-I09）。
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=128, description="搜索词，单行，不含网址")
    count: int = Field(default=3, ge=MIN_COUNT, le=MAX_COUNT, description="返回条数 1–5")


DESCRIPTION = (
    "联网搜索公开网页，返回标题、摘要与网址（均为未经核验的外部摘录）。"
    "Use when 用户已勾选联网，且问题需要规范库之外的公开信息（产品资料、新闻、非规范类做法）。"
    "Do not use for 规范条文或数值（走 retrieve_clauses / calc_velocity）；不要把项目资料、个人信息放进搜索词。"
)


def provider_call_for(config: WebSearchConfig) -> ProviderCall:
    """网关调用意图：熔断按 provider 主机分桶（``model`` = ``web_search:<host>``）。"""

    return ProviderCall(
        provider=ProviderKind.WEB_SEARCH,
        category=CallCategory.WEB_SEARCH,
        model=f"web_search:{config.host or 'unknown'}",
        required_capability=Capability.WEB_SEARCH,
    )


def _record_trace(runtime: WebSearchRuntime, data: dict[str, Any], *, query: str) -> None:
    """trace 的 ``web_search`` 事件：``query`` 与用户问题同级处理（存，经 ``_redact`` 脱敏）；不记网址 / 密钥。"""

    recorder = runtime.recorder() if runtime.recorder is not None else None
    if recorder is None or not getattr(recorder, "started", False):
        return
    try:
        recorder.record(node="web_search", event="web_search", query=query, data=data)
    except Exception:  # noqa: BLE001, S110 — 观测失败不影响搜索
        pass


def build_web_search_tool(
    config: WebSearchConfig,
    runtime: WebSearchRuntime,
    *,
    resolver: Any | None = None,
    ssl_context: Any | None = None,
) -> ToolSpec:
    """用一份**已就绪**的配置构建工具。``resolver`` / ``ssl_context`` 只供评测与测试注入（DNS 重绑定、自签证书）。"""

    if not config.ready:
        raise ValueError("web_search 只能用 ready 的配置构建（缺项应由插件 skipped，而不是构建一个必然失败的工具）")
    adapter: ProviderAdapter = get_adapter(config.provider)
    guard = QueryEgressGuard(private_context_source=runtime.private_chunks, user_text_source=runtime.user_text)
    call = provider_call_for(config)
    host = config.host

    def finish(result: dict[str, Any], *, query: str, code: str) -> dict[str, Any]:
        """统一收口：审计（白名单字段）+ trace（脱敏查询）。``code`` 为 ``ok`` 或稳定错误码。"""

        context = _context(runtime)
        events.emit_performed(
            principal_hash=context.principal.principal_hash if context is not None else "",
            query_hash=events.query_hash(query),
            query_length=len(query),
            provider=config.provider,
            host=host,
            count=int(result.get("count", 0) or 0),
            result=code,
        )
        _record_trace(
            runtime,
            {
                "provider": config.provider,
                "host": host,
                "result": code,
                "count": int(result.get("count", 0) or 0),
                "dropped_suspicious": int(result.get("dropped_suspicious", 0) or 0),
            },
            query=query,
        )
        return result

    def execute(args: WebSearchArgs) -> dict[str, Any]:
        query = args.query
        verdict = guard.check(query, searches_this_turn=runtime.searches_so_far())
        if not verdict.allowed:
            code = WEB_SEARCH_QUOTA if verdict.quota else WEB_SEARCH_QUERY_REJECTED
            # 被拒的查询不发请求；次数用尽以外的拒绝不消耗本回合的次数（模型改写后可以再试）
            return finish(error_result(code), query=query, code=code)
        runtime.private.count_attempt()
        context = _context(runtime)
        if context is None:
            # 没有请求上下文就没有主体可对账：fail closed，不发请求
            return finish(error_result(code_from_external("")), query=query, code=code_from_external(""))
        spec = adapter.build_request(query, args.count, config.api_key)
        url = config.base_url.rstrip("/") + spec.path + "?" + urlencode(spec.params)
        gateway = runtime.gateway() if runtime.gateway is not None else None
        # 单次交换的超时取网关画像（``ProviderKind.WEB_SEARCH``，单一来源，改它须递进 GOVERNANCE_CONFIG_VERSION），
        # 再被总预算封顶；总预算经 RequestContext.deadline_seconds 交给网关，重试不会越过它
        profile_timeout = float(gateway.timeout_for(ProviderKind.WEB_SEARCH)) if gateway is not None else TIMEOUT_SECONDS
        timeout = min(profile_timeout, NETWORK_BUDGET_SECONDS)
        bounded = context.model_copy(update={"deadline_seconds": NETWORK_BUDGET_SECONDS})
        try:
            response = governed_get(
                url,
                headers=dict(spec.headers),
                max_bytes=MAX_RESPONSE_BYTES,
                timeout=timeout,
                allow_private_network=config.allow_private,
                ctx=bounded,
                call=call,
                require_media_type=JSON_MEDIA_TYPE,
                resolver=resolver,
                ssl_context=ssl_context,
                gateway=gateway,
            )
        except ExternalGetError as exc:
            code = code_from_external(exc.code)
            return finish(error_result(code), query=query, code=code)
        try:
            hits = adapter.parse_response(response.body)
        except AdapterParseError:
            return finish(error_result(WEB_SEARCH_BAD_RESPONSE), query=query, code=WEB_SEARCH_BAD_RESPONSE)
        result = process_hits(hits, args.count)
        return finish(result, query=query, code="ok")

    return ToolSpec(
        name="web_search",
        description=DESCRIPTION,
        args_model=WebSearchArgs,
        execute=execute,
        timeout_seconds=TIMEOUT_SECONDS,
        result_source=SourceTag.WEB_CONTENT,
    )


def _context(runtime: WebSearchRuntime) -> RequestContext | None:
    if runtime.request_context is None:
        return None
    value = runtime.request_context()
    return value if isinstance(value, RequestContext) else None


__all__ = [
    "DESCRIPTION",
    "MAX_SCHEMA_CHARS",
    "TIMEOUT_SECONDS",
    "TOOL_NAME",
    "WebSearchArgs",
    "build_web_search_tool",
    "provider_call_for",
]
