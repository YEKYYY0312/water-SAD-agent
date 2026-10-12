"""MCP Streamable HTTP 客户端（P7-S8，``docs/design/16-p7-plugin-mcp.md`` §3.10）。

协议以 **2025-11-25**（会话制）为主：每条 JSON-RPC 消息是一次新的 HTTP POST；响应是单个 JSON 或 SSE 流
（两种都支持）；``MCP-Session-Id`` 回发、``MCP-Protocol-Version`` 头、404 重建会话、关闭时 DELETE。接受 server
回的 ``2025-06-18`` / ``2025-03-26``（对客户端语义兼容）；只讲 2026-07-28 无状态协议的 server 只**识别**
（``MCP_PROTOCOL_UNSUPPORTED``），不回落到已废弃的 HTTP+SSE。

与 :class:`water_agent.mcp.client.MCPStdioClient` 同一个外部接口（``connect`` / ``list_tools`` / ``call_tool`` /
``close`` / ``alive`` / ``server_info`` / ``notifications``），桥接层其余逻辑一行不改。

**治理**：这个客户端发出的每一个 HTTP 请求（含 initialize、通知、DELETE、对 server 请求的回绝、授权端点）都经
``ProviderGateway.call``（限流 / 熔断 / deadline / 账本）；画像 ``max_attempts=1``，**工具调用零重试**。网络层与
策略层的失败在这里映射回稳定的 MCP 错误码。**不自动重发**：流在收到响应前断开、超时、5xx 都直接报错——工具可能有
副作用，重发即重复执行。唯一的例外是 404（会话已被 server 终止，请求被拒绝在处理之前）与一次 401 之后的令牌刷新：
两者都是"server 明确表示没处理"，重发一次是安全的。

**不可信的远端**：响应里出现的任何地址（重定向、``WWW-Authenticate``、授权服务器元数据）都不是指令；出口只由
运维配置的 URL 决定，所有请求走 :func:`water_agent.mcp.http_transport.open_exchange` 的同一套 SSRF 校验。
"""
from __future__ import annotations

import json
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, Self

from water_agent.mcp.client import MAX_LIST_PAGES, RemoteTool
from water_agent.mcp.config import MCPServerConfig, ServerTransport
from water_agent.mcp.credentials import read_slot
from water_agent.mcp.errors import (
    MCP_CONFIG_INVALID,
    MCP_CONNECT_FAILED,
    MCP_CREDENTIAL_MISSING,
    MCP_NOT_INITIALIZED,
    MCP_OAUTH_INSUFFICIENT_SCOPE,
    MCP_OAUTH_REQUIRED,
    MCP_PROTOCOL_ERROR,
    MCP_PROTOCOL_UNSUPPORTED,
    MCP_TIMEOUT,
    MCP_TOOL_REMOTE_ERROR,
    MCPError,
)
from water_agent.mcp.http_transport import (
    ERROR_BODY_LIMIT,
    ParsedUrl,
    Resolver,
    TransportFailure,
    TransportHttpStatus,
    open_exchange,
    parse_server_url,
    parse_www_authenticate,
    valid_session_id,
)
from water_agent.mcp.protocol import (
    CLIENT_INFO,
    JSONRPC_METHOD_NOT_FOUND,
    RpcMessage,
    decode_message,
    make_error_response,
    make_notification,
    make_request,
    parse_tool_call_result,
)
from water_agent.runtime.cancellation import RunCancelled, raise_if_cancelled
from water_agent.runtime.governance.binding import default_gateway
from water_agent.runtime.governance.contracts import (
    DEFAULT_LOCAL_CAPABILITIES,
    CallCategory,
    Capability,
    Principal,
    ProviderCall,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import GovernanceError, ProviderGateway, ProviderUsage

#: 本客户端在 ``initialize`` 里请求的协议版本（2025-11-25 会话制）。
MCP_HTTP_PROTOCOL_VERSION = "2025-11-25"
#: 接受的 server 协商结果（对客户端语义兼容的会话制版本）。
ACCEPTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
#: 2026-07-28 无状态协议里 server 拒绝 legacy ``initialize`` 时使用的 JSON-RPC 错误码
#: （HeaderMismatch / MissingRequiredClientCapability / UnsupportedProtocolVersion）。
MODERN_PROTOCOL_ERROR_CODES = frozenset({-32020, -32021, -32022})
MODERN_PROTOCOL_VERSION = "2026-07-28"
#: 关闭时 DELETE 的时间上限（秒）：关闭路径不能因为远端慢而卡住。
CLOSE_TIMEOUT_SECONDS = 2.0
#: 对 server 请求回绝的时间上限（秒）。
REPLY_TIMEOUT_SECONDS = 5.0

_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar("water_agent_mcp_request_context", default=None)


@contextmanager
def mcp_request_scope(context: RequestContext | None) -> Iterator[None]:
    """登记"当前这次调用的请求上下文"（主体、request_id）。

    ``tools/call`` 必须在这个作用域内发出——远程调用的能力校验（``MCP_INVOKE``）、限流与预算都按**调用者
    的主体**计，不按 server 或进程计。``AgenticSession`` 在每个会话入口（``aask`` / ``astream`` / ``aresume`` /
    同步 ``ask``）里登记它，与 ``cancellation_scope`` 并列；LangGraph 在执行器线程里复制 context，工具线程读得到。
    """
    token = _REQUEST_CONTEXT.set(context)
    try:
        yield
    finally:
        _REQUEST_CONTEXT.reset(token)


def current_request_context() -> RequestContext | None:
    return _REQUEST_CONTEXT.get()


def service_context(server_id: str, *, purpose: str = "lifecycle") -> RequestContext:
    """这台 server 的服务级请求上下文：装配期的生命周期请求与授权端点用它（运维配置驱动，不是用户主体）。

    主体只额外持有 ``MCP_INVOKE``（``GRANTABLE_CAPABILITIES`` 允许），与危险能力无关。
    """
    principal = Principal.local(
        f"mcp-{server_id}", capabilities=DEFAULT_LOCAL_CAPABILITIES | {Capability.MCP_INVOKE}
    )
    return RequestContext(principal=principal, request_id=f"mcp-{server_id}-{purpose}", thread_id=f"mcp-{purpose}")


def mcp_provider_call(server_id: str) -> ProviderCall:
    return ProviderCall(
        provider=ProviderKind.MCP_REMOTE,
        category=CallCategory.MCP_TOOL_CALL,
        model=f"mcp:{server_id}",
        required_capability=Capability.MCP_INVOKE,
    )


def unwrap_governance_error(exc: GovernanceError, server_id: str) -> BaseException:
    """gateway 把 executor 抛出的异常包成 ``GovernanceError``：取回原因并映射回 MCP 错误码。"""
    cause = exc.__cause__
    if isinstance(cause, TransportHttpStatus):
        return cause  # 状态码由协议层解释（401 / 403 / 404 / 400 ……）
    if isinstance(cause, MCPError):
        return cause
    if isinstance(cause, TransportFailure):
        code = cause.mcp_code if cause.mcp_code in (MCP_TIMEOUT, MCP_CONNECT_FAILED) else MCP_CONNECT_FAILED
        return MCPError(code, f"server {server_id!r}：{cause.message}")
    return MCPError(
        MCP_CONNECT_FAILED,
        f"server {server_id!r} 的远程调用被治理层拒绝或失败（{exc.code}）：{exc}",
    )


def run_governed[T](
    server_id: str,
    executor: Callable[[], T],
    *,
    context: RequestContext,
    deadline_seconds: float,
    gateway: ProviderGateway | None = None,
) -> T:
    """把一次出站交换放进 ``ProviderGateway.call``（限流 / 熔断 / deadline / 账本，``max_attempts=1``）。

    ``executor`` 里抛的 ``MCPError`` / ``TransportFailure`` / ``TransportHttpStatus`` 在这里还原成原异常
    （HTTP 状态由调用方解释）；取消（``_Cancelled``）还原成 ``RunCancelled``。授权端点请求与 MCP 请求共用它。
    """
    bounded = context.model_copy(update={"deadline_seconds": deadline_seconds})

    def wrapped() -> tuple[T, ProviderUsage]:
        return executor(), ProviderUsage.unknown()

    try:
        return (gateway if gateway is not None else default_gateway()).call(
            bounded, mcp_provider_call(server_id), wrapped
        ).value
    except _Cancelled:
        raise RunCancelled() from None
    except GovernanceError as exc:
        raise unwrap_governance_error(exc, server_id) from exc


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------


class HttpAuth(Protocol):
    """给每个 HTTP 请求提供 ``Authorization`` 头（规范：每个请求都要带，即使属于同一个会话）。"""

    def authorization(self) -> str | None:
        """完整的头值（``Bearer <token>``）；没有令牌时 ``None``（请求会被 server 以 401 拒绝）。
        可能先刷新即将过期的令牌；刷新失败抛 ``MCPError``（``MCP_OAUTH_REQUIRED``）。"""

    def refresh_after_unauthorized(self) -> bool:
        """server 回 401 之后调用一次：能换到新令牌就返回 True（调用方重发这个被拒绝的请求一次）。"""


class StaticBearerAuth:
    """静态 Bearer：值来自凭据槽位（每次请求现读，面板改值立即生效），只进 ``Authorization`` 头。"""

    def __init__(self, server_id: str, slot: str) -> None:
        self._server_id = server_id
        self._slot = slot

    def authorization(self) -> str | None:
        value = read_slot(self._server_id, self._slot)
        if value is None:
            raise MCPError(
                MCP_CREDENTIAL_MISSING,
                f"server {self._server_id!r} 的凭据槽位 {self._slot} 还没填值（由管理员在 Web 扩展面板填写）",
            )
        return f"Bearer {value}"

    def refresh_after_unauthorized(self) -> bool:
        return False


def build_auth(
    config: MCPServerConfig,
    *,
    gateway: ProviderGateway | None = None,
    resolver: Resolver | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> HttpAuth | None:
    if config.bearer_credential is not None:
        return StaticBearerAuth(config.server_id, config.bearer_credential)
    if config.oauth is not None:
        from water_agent.mcp.oauth import OAuthAuth  # 延后导入：oauth 模块依赖本模块的类型

        return OAuthAuth(config, gateway=gateway, resolver=resolver, ssl_context=ssl_context)
    return None


# ---------------------------------------------------------------------------
# 内部类型
# ---------------------------------------------------------------------------


class _Cancelled(BaseException):
    """把 ``RunCancelled`` 穿过 gateway 的 executor（gateway 只对 ``Exception`` 分类并包装）。"""


class _SessionExpired(Exception):
    """带会话号的请求收到 404：server 已终止该会话（请求没有被处理）。"""


@dataclass(frozen=True, slots=True)
class _Reply:
    payload: dict[str, Any] | None
    session_header: str | None


def _json_bytes(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _modern_rejection(body: bytes) -> bool:
    """响应体是 2026-07-28 协议的 JSON-RPC 错误（HeaderMismatch / UnsupportedProtocolVersion ……）。"""
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict):
        return False
    if error.get("code") in MODERN_PROTOCOL_ERROR_CODES:
        return True
    data = error.get("data")
    supported = data.get("supported") if isinstance(data, dict) else None
    return isinstance(supported, list) and MODERN_PROTOCOL_VERSION in supported


def parse_tools_page(result: Any) -> tuple[list[RemoteTool], str | None]:
    """``tools/list`` 一页的 shape 校验（内容安检在桥接层）。返回 (本页工具, 下一页游标)。"""
    if not isinstance(result, dict):
        raise MCPError(MCP_PROTOCOL_ERROR, f"tools/list 结果必须是 object，得到 {type(result).__name__}")
    page = result.get("tools")
    if not isinstance(page, list):
        raise MCPError(MCP_PROTOCOL_ERROR, "tools/list 结果缺少 tools 数组")
    tools: list[RemoteTool] = []
    for item in page:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise MCPError(MCP_PROTOCOL_ERROR, "tools/list 存在非法工具项（缺 string name）")
        raw_description = item.get("description")
        raw_schema = item.get("inputSchema")
        tools.append(
            RemoteTool(
                name=item["name"],
                description=raw_description if isinstance(raw_description, str) else "",
                input_schema=raw_schema if isinstance(raw_schema, dict) else {"type": "object", "properties": {}},
            )
        )
    cursor = result.get("nextCursor")
    if not cursor:
        return tools, None
    if not isinstance(cursor, str):
        raise MCPError(MCP_PROTOCOL_ERROR, "nextCursor 必须是 string")
    return tools, cursor


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class MCPHttpClient:
    """一台 Streamable HTTP MCP server 的连接（会话）。

    用法与 :class:`MCPStdioClient` 相同（``with`` 或显式 ``connect`` / ``close``）。没有常驻 TCP 连接：每个请求
    一次新连接（每次都重新解析、分类、钉死 IP），"连接"指的是 MCP 会话（``MCP-Session-Id``）。
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        gateway: ProviderGateway | None = None,
        request_context: RequestContext | None = None,
        auth: HttpAuth | None = None,
        resolver: Resolver | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if config.transport is not ServerTransport.STREAMABLE_HTTP or config.url is None:
            raise MCPError(MCP_CONFIG_INVALID, f"server {config.server_id!r} 不是 Streamable HTTP server")
        self.config = config
        self.server_info: dict[str, Any] | None = None
        self._url: ParsedUrl = parse_server_url(config.url, allow_private_network=config.allow_private_network)
        self._gateway = gateway
        self._context = request_context
        self._auth = (
            auth if auth is not None else build_auth(config, gateway=gateway, resolver=resolver, ssl_context=ssl_context)
        )
        self._resolver = resolver
        self._ssl_context = ssl_context
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._connected = False
        self._closed = False
        self._next_id = 0
        self._notifications: list[dict[str, Any]] = []
        #: 请求锁：同一会话上的 JSON-RPC 请求串行（与 stdio 客户端同一纪律，P7-S5 F17）。
        self._request_lock = threading.Lock()
        #: 本客户端因 404 重建会话的次数（可观测，测试用）。
        self.session_rebuilds = 0

    # ------------------------------------------------------------------ 状态

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def alive(self) -> bool:
        """已握手且未关闭。没有常驻连接可探活：会话被 server 终止时下一次请求会透明重建一次。"""
        return self._connected and not self._closed

    @property
    def notifications(self) -> tuple[RpcMessage, ...]:
        return tuple(RpcMessage(m) for m in self._notifications)

    @property
    def session_id_digest(self) -> str | None:
        """会话号的短摘要（可观测，不暴露原值）。"""
        if self._session_id is None:
            return None
        import hashlib

        return hashlib.sha256(self._session_id.encode("utf-8")).hexdigest()[:10]

    # ------------------------------------------------------------------ 治理

    def _gateway_for_call(self) -> ProviderGateway:
        return self._gateway if self._gateway is not None else default_gateway()

    def _context_for(self, *, tool_call: bool) -> RequestContext:
        """请求主体：作用域登记的 > 构造时给的 > （仅生命周期请求）这台 server 的服务主体。

        ``tools/call`` **没有**服务主体兜底：没有调用者主体就拒绝（fail closed），不会借服务主体的
        ``MCP_INVOKE`` 替未授权的调用者发请求。生命周期请求（initialize / tools/list / DELETE ……）发生在装配期，
        由运维配置驱动，用 server 级服务主体。
        """
        scoped = current_request_context()
        if scoped is not None:
            return scoped
        if self._context is not None:
            return self._context
        if tool_call:
            raise MCPError(
                MCP_NOT_INITIALIZED,
                f"server {self.config.server_id!r} 的 tools/call 缺少调用者主体（未在 mcp_request_scope 内调用）",
            )
        return service_context(self.config.server_id)

    # ------------------------------------------------------------------ 一次 HTTP 交换

    def _headers(self, *, include_session: bool, delete: bool = False) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if delete:
            headers.pop("Content-Type")
        if include_session and self._protocol_version is not None:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if include_session and self._session_id is not None:
            headers["MCP-Session-Id"] = self._session_id
        if self._auth is not None:
            value = self._auth.authorization()
            if value is not None:
                headers["Authorization"] = value
        return headers

    def _exchange(
        self,
        message: dict[str, Any] | None,
        *,
        method: str,
        expect_id: int | None,
        deadline_at: float,
        include_session: bool,
        context: RequestContext,
    ) -> _Reply:
        """gateway 的 executor：一次完整的 HTTP 交换（含读完响应 / 读到匹配的 SSE 事件）。"""
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise MCPError(MCP_TIMEOUT, f"server {self.config.server_id!r} 的调用超过时间预算")
        exchange = open_exchange(
            self._url,
            method,
            headers=self._headers(include_session=include_session, delete=method == "DELETE"),
            body=_json_bytes(message) if message is not None else None,
            timeout=remaining,
            allow_private_network=self.config.allow_private_network,
            resolver=self._resolver,
            ssl_context=self._ssl_context,
        )
        try:
            if exchange.status >= 400:
                body = b""
                try:
                    body = exchange.read_body(ERROR_BODY_LIMIT)
                except (MCPError, TransportFailure):
                    body = b""
                raise TransportHttpStatus(exchange.status, exchange.headers, body)
            session_header = exchange.header("MCP-Session-Id")
            if expect_id is None:
                return _Reply(None, session_header)
            media = exchange.media_type
            if media == "application/json":
                payload = decode_message(exchange.read_body(self.config.max_response_bytes))
                return _Reply(self._match_response(payload, expect_id), session_header)
            if media == "text/event-stream":
                return _Reply(self._read_stream(exchange, expect_id, deadline_at, context), session_header)
            raise MCPError(MCP_PROTOCOL_ERROR, f"server 返回了不支持的 Content-Type {media or '（空）'!r}")
        finally:
            exchange.close()

    @staticmethod
    def _match_response(payload: dict[str, Any], expect_id: int) -> dict[str, Any]:
        message = RpcMessage(payload)
        if not message.is_response or message.message_id != expect_id:
            raise MCPError(MCP_PROTOCOL_ERROR, "响应不是对本次请求的 JSON-RPC 响应（id 对不上）")
        return payload

    def _read_stream(self, exchange: Any, expect_id: int, deadline_at: float, context: RequestContext) -> dict[str, Any]:
        def check() -> None:
            try:
                raise_if_cancelled()
            except RunCancelled as exc:
                raise _Cancelled() from exc

        limit = self.config.max_response_bytes
        for event in exchange.events(max_event_bytes=limit, max_total_bytes=limit * 4, check=check):
            if event.event != "message" or not event.data.strip():
                continue  # 预热事件（空 data）与非 message 事件
            message = RpcMessage(decode_message(event.data))
            if message.is_response and message.message_id == expect_id:
                return message.payload
            if message.is_request:
                self._refuse_server_request(message.message_id, deadline_at, context)
            elif message.is_notification:
                self._notifications.append(message.payload)
        raise TransportFailure(MCP_CONNECT_FAILED, "事件流在收到响应前结束（连接中断）")

    def _refuse_server_request(self, request_id: Any, deadline_at: float, context: RequestContext) -> None:
        """本网关不支持任何 server→client 请求（sampling / roots ……）：POST 回 MethodNotFound，
        不让 server 侧挂起。这一次回绝同样是受治理的出站请求。"""
        reply = make_error_response(request_id, JSONRPC_METHOD_NOT_FOUND, "unsupported server-to-client request")
        reply_deadline = min(deadline_at, time.monotonic() + REPLY_TIMEOUT_SECONDS)
        try:
            self._governed(reply, method="POST", expect_id=None, deadline_at=reply_deadline,
                           include_session=True, context=context)
        except (MCPError, TransportFailure, TransportHttpStatus):
            pass  # 回绝失败不影响正在等的那个响应

    def _governed(
        self,
        message: dict[str, Any] | None,
        *,
        method: str,
        expect_id: int | None,
        deadline_at: float,
        include_session: bool,
        context: RequestContext,
    ) -> _Reply:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise MCPError(MCP_TIMEOUT, f"server {self.config.server_id!r} 的调用超过时间预算")

        def executor() -> _Reply:
            return self._exchange(
                message, method=method, expect_id=expect_id, deadline_at=deadline_at,
                include_session=include_session, context=context,
            )

        return run_governed(
            self.config.server_id, executor, context=context, deadline_seconds=remaining, gateway=self._gateway
        )

    # ------------------------------------------------------------------ 请求（含状态码解释）

    def _status_error(self, error: TransportHttpStatus, *, during_initialize: bool) -> MCPError:
        status = error.status_code
        name = self.config.server_id
        if status == 401:
            if self.config.oauth is not None:
                return MCPError(
                    MCP_OAUTH_REQUIRED,
                    f"server {name!r} 返回 401：本部署还没有有效的 OAuth 令牌（或令牌已失效），"
                    "需管理员在 Web 扩展面板（重新）授权",
                )
            if self.config.bearer_credential is not None:
                return MCPError(MCP_CONNECT_FAILED, f"server {name!r} 拒绝了静态 Bearer（401）：请检查凭据槽位的值")
            return MCPError(
                MCP_CONNECT_FAILED,
                f"server {name!r} 要求认证（401），但配置里没有 bearer_credential 或 oauth",
            )
        if status == 403:
            for challenge in parse_www_authenticate(error.header_all("WWW-Authenticate")):
                if challenge.scheme == "bearer" and challenge.params.get("error") == "insufficient_scope":
                    scope = challenge.params.get("scope", "（server 没有给出）")
                    return MCPError(
                        MCP_OAUTH_INSUFFICIENT_SCOPE,
                        f"server {name!r} 要求 scope：{scope}。本网关不自动升级授权——请管理员确认后重新授权并授予该 scope",
                    )
            return MCPError(MCP_CONNECT_FAILED, f"server {name!r} 拒绝访问（403）")
        if status == 400 and during_initialize and _modern_rejection(error.body):
            return MCPError(
                MCP_PROTOCOL_UNSUPPORTED,
                f"server {name!r} 只支持无状态协议 {MODERN_PROTOCOL_VERSION}（本网关只实现 {MCP_HTTP_PROTOCOL_VERSION} "
                "会话制，未实现新版）",
            )
        if status is not None and (status == 429 or status >= 500 or status == 408):
            return MCPError(MCP_CONNECT_FAILED, f"server {name!r} 暂时不可用（HTTP {status}）")
        return MCPError(MCP_PROTOCOL_ERROR, f"server {name!r} 拒绝了请求（HTTP {status}）")

    def _send(
        self,
        message: dict[str, Any] | None,
        *,
        method: str = "POST",
        expect_id: int | None,
        deadline_at: float,
        include_session: bool = True,
        tool_call: bool = False,
        during_initialize: bool = False,
    ) -> _Reply:
        """发一个请求；401 时让认证提供者换一次令牌并重发这个被拒绝的请求（server 明确没处理）。

        带会话号的请求收到 404 抛 :class:`_SessionExpired`，由 :meth:`_request` 重建会话后重发一次。
        """
        context = self._context_for(tool_call=tool_call)
        refreshed = False
        while True:
            try:
                return self._governed(
                    message, method=method, expect_id=expect_id, deadline_at=deadline_at,
                    include_session=include_session, context=context,
                )
            except TransportHttpStatus as error:
                if error.status_code == 401 and not refreshed and self._auth is not None:
                    refreshed = True
                    if self._auth.refresh_after_unauthorized():
                        continue
                if error.status_code == 404 and include_session and self._session_id is not None:
                    raise _SessionExpired() from error
                raise self._status_error(error, during_initialize=during_initialize) from error

    def _request(self, method: str, params: dict[str, Any] | None, *, deadline: float, tool_call: bool = False) -> Any:
        """发请求并等待匹配 id 的响应。同一会话上的请求串行；等锁计入 deadline。"""
        deadline_at = time.monotonic() + deadline
        if not self._request_lock.acquire(timeout=max(deadline, 0.0)):
            raise MCPError(
                MCP_TIMEOUT,
                f"MCP server {self.config.server_id!r} 正忙：等待前一个调用超过 {deadline:.1f}s",
            )
        try:
            if self._closed:
                raise MCPError(MCP_CONNECT_FAILED, f"MCP server {self.config.server_id!r} 的连接已关闭")
            if not self._connected and method != "initialize":
                raise MCPError(MCP_NOT_INITIALIZED, f"MCP server {self.config.server_id!r} 未连接")
            try:
                return self._request_locked(method, params, deadline_at=deadline_at, tool_call=tool_call)
            except _SessionExpired:
                # 404：server 已终止会话，这个请求没有被处理。重新 initialize 一次再发它一次；
                # 再 404 就是 server 不可用，直接报错（不循环）。
                self._rebuild_session_locked(deadline_at)
                try:
                    return self._request_locked(method, params, deadline_at=deadline_at, tool_call=tool_call)
                except _SessionExpired as again:
                    raise MCPError(
                        MCP_CONNECT_FAILED, f"server {self.config.server_id!r} 重建会话后仍返回 404"
                    ) from again
        finally:
            self._request_lock.release()

    def _request_locked(self, method: str, params: dict[str, Any] | None, *, deadline_at: float, tool_call: bool) -> Any:
        self._next_id += 1
        request_id = self._next_id
        reply = self._send(
            make_request(request_id, method, params),
            expect_id=request_id,
            deadline_at=deadline_at,
            tool_call=tool_call,
            during_initialize=method == "initialize",
            include_session=method != "initialize",
        )
        assert reply.payload is not None
        message = RpcMessage(reply.payload)
        if message.error is not None:
            raise MCPError(MCP_TOOL_REMOTE_ERROR, str(message.error.get("message", "unknown error"))[:500])
        return message.result

    # ------------------------------------------------------------------ 连接

    def _initialize_locked(self, deadline_at: float) -> dict[str, Any]:
        self._session_id = None
        self._protocol_version = None
        self._next_id += 1
        request_id = self._next_id
        reply = self._send(
            make_request(
                request_id,
                "initialize",
                {"protocolVersion": MCP_HTTP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            ),
            expect_id=request_id,
            deadline_at=deadline_at,
            include_session=False,
            during_initialize=True,
        )
        assert reply.payload is not None
        message = RpcMessage(reply.payload)
        if message.error is not None:
            raise MCPError(MCP_CONNECT_FAILED, f"initialize 被拒绝：{str(message.error.get('message', ''))[:200]}")
        result = message.result
        if not isinstance(result, dict) or not isinstance(result.get("serverInfo"), dict):
            raise MCPError(MCP_CONNECT_FAILED, f"MCP server {self.config.server_id!r} initialize 响应缺少 serverInfo")
        version = result.get("protocolVersion")
        if version not in ACCEPTED_PROTOCOL_VERSIONS:
            raise MCPError(
                MCP_PROTOCOL_ERROR,
                f"server 协商出本网关不支持的协议版本 {str(version)[:40]!r}（支持 {', '.join(ACCEPTED_PROTOCOL_VERSIONS)}）",
            )
        session = reply.session_header
        if session is not None and not valid_session_id(session):
            raise MCPError(MCP_PROTOCOL_ERROR, "server 返回的 MCP-Session-Id 含非法字符（只允许 0x21–0x7E）")
        self._session_id = session
        self._protocol_version = str(version)
        self.server_info = result["serverInfo"]
        # 握手后回 initialized 通知；通知失败不算连接失败（server 仍可应答请求）
        try:
            self._send(make_notification("notifications/initialized"), expect_id=None, deadline_at=deadline_at)
        except (MCPError, _SessionExpired):
            pass
        return result

    def connect(self) -> dict[str, Any]:
        """initialize 握手（幂等：已连接则直接返回）。"""
        deadline = self.config.startup_timeout_seconds
        deadline_at = time.monotonic() + deadline
        if not self._request_lock.acquire(timeout=deadline):
            raise MCPError(MCP_TIMEOUT, f"MCP server {self.config.server_id!r} 正忙")
        try:
            if self._connected:
                assert self.server_info is not None
                return self.server_info
            try:
                self._initialize_locked(deadline_at)
            except MCPError as exc:
                self._connected = False
                self._session_id = None
                if exc.code in (MCP_TIMEOUT,):
                    raise MCPError(
                        MCP_CONNECT_FAILED,
                        f"MCP server {self.config.server_id!r} initialize 失败：{exc.message}",
                    ) from exc
                raise
            self._connected = True
            assert self.server_info is not None
            return self.server_info
        finally:
            self._request_lock.release()

    def _rebuild_session_locked(self, deadline_at: float) -> None:
        self.session_rebuilds += 1
        self._initialize_locked(deadline_at)

    # ------------------------------------------------------------------ 工具

    def list_tools(self) -> list[RemoteTool]:
        """拉取完整工具清单（自动翻页）。"""
        tools: list[RemoteTool] = []
        cursor: str | None = None
        for _page in range(MAX_LIST_PAGES):
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            result = self._request("tools/list", params, deadline=self.config.call_timeout_seconds)
            page, cursor = parse_tools_page(result)
            tools.extend(page)
            if cursor is None:
                return tools
        raise MCPError(MCP_PROTOCOL_ERROR, f"tools/list 分页超过 {MAX_LIST_PAGES} 页，视为异常清单")

    def call_tool(
        self, name: str, arguments: dict[str, Any], *, deadline_seconds: float | None = None
    ) -> dict[str, Any]:
        """调用远程工具（与 stdio 客户端同一返回形状）。

        远端业务失败（``isError=true``）→ 结构化 dict ``{"ok": False, ...}``；连接 / 协议 / 超时 / 超限抛
        :class:`MCPError`。**不自动重发**：超时或流中断之后远端状态未知。
        """
        if not self._connected:
            raise MCPError(
                MCP_NOT_INITIALIZED,
                f"MCP server {self.config.server_id!r} 未完成握手，不能调用工具",
            )
        result = self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
            deadline=deadline_seconds or self.config.call_timeout_seconds,
            tool_call=True,
        )
        parsed = parse_tool_call_result(result)
        if parsed.is_error:
            detail = "; ".join(parsed.texts)[:500] if parsed.texts else "server 未给出原因"
            return {
                "ok": False,
                "server_id": self.config.server_id,
                "tool": name,
                "code": MCP_TOOL_REMOTE_ERROR,
                "message": f"远程工具 {name!r} 执行失败：{detail}",
            }
        return {
            "ok": True,
            "server_id": self.config.server_id,
            "tool": name,
            "structured_content": parsed.structured_content,
            "content_text": "\n".join(parsed.texts),
            "unsupported_blocks": list(parsed.unsupported),
        }

    # ------------------------------------------------------------------ 关闭

    def close(self) -> None:
        """结束会话：有会话号就发一次 DELETE（405 视为正常；失败一律吞掉）。幂等，永不抛。"""
        if self._closed:
            return
        session = self._session_id
        self._closed = True
        was_connected = self._connected
        self._connected = False
        if session is None or not was_connected:
            return
        deadline_at = time.monotonic() + CLOSE_TIMEOUT_SECONDS
        try:
            # 关闭路径只借锁短暂等待：正在进行的调用不应被 DELETE 抢先
            acquired = self._request_lock.acquire(timeout=CLOSE_TIMEOUT_SECONDS)
            try:
                context = self._context_for(tool_call=False)
                self._governed(None, method="DELETE", expect_id=None, deadline_at=deadline_at,
                               include_session=True, context=context)
            finally:
                if acquired:
                    self._request_lock.release()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            self._session_id = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = [
    "ACCEPTED_PROTOCOL_VERSIONS",
    "MCP_HTTP_PROTOCOL_VERSION",
    "HttpAuth",
    "MCPHttpClient",
    "StaticBearerAuth",
    "build_auth",
    "current_request_context",
    "mcp_request_scope",
    "parse_tools_page",
]
