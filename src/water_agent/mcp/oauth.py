"""MCP OAuth 2.1 服务级授权（P7-S8，``docs/design/16-p7-plugin-mcp.md`` §3.10，MCP 规范 2025-11-25 authorization）。

**服务级**：运维在覆盖层声明 ``oauth``，管理员在 Web 扩展面板点"授权"走一次授权码 + PKCE 流程，拿到的令牌属于这台
server 的部署、所有用户的 run 共用；不做按终端用户的委托授权。客户端注册只支持预注册与动态注册（RFC 7591），
不做 Client ID Metadata Documents。

客户端侧的 MUST 项逐条落地（每条一个测试，见 ``tests/test_mcp_oauth.py``）：

1. 401 时优先用 ``WWW-Authenticate`` 的 ``resource_metadata``，没有才依次试路径插入与根路径两个 well-known；
2. 授权服务器元数据按规范顺序试 RFC 8414 与 OIDC（有 path 的 issuer 三个、无 path 的两个）；
3. ``code_challenge_methods_supported`` 缺失或不含 ``S256`` → 拒绝继续；
4. 授权请求与令牌请求都带 ``resource``（规范 URI），授权服务器不支持时也照发；
5. ``state`` 每次新生成、一次性、10 分钟过期，不匹配即丢弃；回调带 ``iss`` 时必须等于记录的 issuer（RFC 9207）；
6. 令牌只经 ``Authorization: Bearer`` 头、永不进查询串；只发给签发它的 issuer 对应的那一个 MCP endpoint；
7. 授权服务器端点必须 ``https://``；回调地址是 ``https://`` 或 localhost；
8. 动态注册信息按 issuer 存，issuer 变化即作废；
9. refresh token 被轮换时替换旧值；
10. 403 ``insufficient_scope`` 如实报出所需 scope，不自动升级。

**远端不可信**：``authorization_servers`` 取**第一个**且必须在运维配置的 ``allowed_issuers`` 里（没配只接受第一个并记录）；
受保护资源元数据里的 ``resource`` 必须等于本 server 的规范 URI（防把别的资源的令牌请求引过来）；所有端点请求都经
``http_transport.open_exchange`` 的同一套 SSRF 校验与不跟随重定向，并且每个请求都经 ``ProviderGateway``。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from water_agent.mcp.config import MCPOAuthConfig, MCPServerConfig, ServerTransport
from water_agent.mcp.credentials import read_slot
from water_agent.mcp.errors import (
    MCP_CONFIG_INVALID,
    MCP_CONNECT_FAILED,
    MCP_CREDENTIAL_MISSING,
    MCP_OAUTH_REQUIRED,
    MCP_OAUTH_UNAVAILABLE,
    MCP_TIMEOUT,
    MCPError,
)
from water_agent.mcp.http_client import MCP_HTTP_PROTOCOL_VERSION, run_governed, service_context
from water_agent.mcp.http_transport import (
    ParsedUrl,
    Resolver,
    TransportFailure,
    TransportHttpStatus,
    open_exchange,
    parse_server_url,
    parse_www_authenticate,
)
from water_agent.mcp.oauth_store import OAuthStore, StoredClient, StoredToken, store_lock
from water_agent.runtime.governance.contracts import RequestContext
from water_agent.runtime.governance.gateway import ProviderGateway

#: 授权会话（state）的有效期与同时挂起的上限。
PENDING_TTL_SECONDS = 600.0
MAX_PENDING = 64
#: 授权相关响应体上限（元数据 / 令牌响应都很小）。
MAX_RESPONSE_BYTES = 65536
#: 单次授权端点请求的时间预算。
REQUEST_TIMEOUT_SECONDS = 10.0
#: 令牌临近过期的刷新提前量上限（秒）；实际 ``min(60, 寿命的一半)``，短命令牌不会每个请求都触发刷新。
REFRESH_SKEW_SECONDS = 60.0
PKCE_METHOD = "S256"

#: 时间源（测试替换）。
_now: Callable[[], float] = time.time


# ---------------------------------------------------------------------------
# 事件（只给出"刷新失败"这类没有管理员请求在场的事实；授权 / 撤销由 Web 路由自己记审计）
# ---------------------------------------------------------------------------

EventSink = Callable[[str, str, dict[str, str]], None]
_EVENT_SINK: EventSink | None = None


def set_event_sink(sink: EventSink | None) -> None:
    """登记事件接收者（Web 应用启动时把它接到结构化审计）。事件只含 server_id、issuer 与结果码——
    永不含令牌、授权码、verifier、state。"""
    global _EVENT_SINK
    _EVENT_SINK = sink


def _emit(event: str, server_id: str, **fields: str) -> None:
    sink = _EVENT_SINK
    if sink is None:
        return
    try:
        sink(event, server_id, dict(fields))
    except Exception:  # noqa: BLE001 — 观测永不影响授权流程
        return


# ---------------------------------------------------------------------------
# 一次受治理的授权端点请求
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Response:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header_all(self, name: str) -> list[str]:
        lowered = name.lower()
        return [value for key, value in self.headers if key.lower() == lowered]

    def json_object(self) -> dict[str, Any] | None:
        try:
            parsed = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


@dataclass
class _Env:
    """一次授权操作的共享环境：哪台 server、用谁的上下文、哪个 gateway。"""

    server: MCPServerConfig
    oauth: MCPOAuthConfig
    context: RequestContext
    gateway: ProviderGateway | None = None
    resolver: Resolver | None = None
    ssl_context: Any = None
    store: OAuthStore = field(init=False)

    def __post_init__(self) -> None:
        self.store = OAuthStore(self.server.server_id)


def _env(
    server: MCPServerConfig,
    *,
    context: RequestContext | None = None,
    gateway: ProviderGateway | None = None,
    resolver: Resolver | None = None,
    ssl_context: Any = None,
) -> _Env:
    if server.transport is not ServerTransport.STREAMABLE_HTTP or server.oauth is None:
        raise MCPError(MCP_CONFIG_INVALID, f"server {server.server_id!r} 没有配置 OAuth（需要 streamable_http + oauth）")
    return _Env(
        server=server,
        oauth=server.oauth,
        context=context if context is not None else service_context(server.server_id, purpose="oauth"),
        gateway=gateway,
        resolver=resolver,
        ssl_context=ssl_context,
    )


def _https_url(raw: str, *, what: str) -> ParsedUrl:
    """授权服务器端点必须 https（规范 MUST）。连接目标的网段限制在建连时按 ``allow_private_network`` 判定。"""
    try:
        return parse_server_url(raw, allow_private_network=False)
    except ValueError as exc:
        raise MCPError(MCP_OAUTH_UNAVAILABLE, f"{what} 必须是合法的 https URL：{exc}") from exc


def _fetch(
    env: _Env,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    what: str,
) -> _Response:
    """一次出站请求：端点校验 → ``ProviderGateway`` → 钉死 IP 的连接。4xx / 5xx 作为 ``_Response`` 返回
    （由调用方解释）；5xx / 429 同时让 gateway 的熔断记一次失败。"""
    target = _https_url(url, what=what)
    merged = {"Accept": "application/json", **(headers or {})}

    def executor() -> _Response:
        exchange = open_exchange(
            target,
            method,
            headers=merged,
            body=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_private_network=env.server.allow_private_network,
            resolver=env.resolver,
            ssl_context=env.ssl_context,
        )
        try:
            data = exchange.read_body(MAX_RESPONSE_BYTES)
            if exchange.status >= 400:
                raise TransportHttpStatus(exchange.status, exchange.headers, data)
            return _Response(exchange.status, tuple(exchange.headers), data)
        finally:
            exchange.close()

    try:
        return run_governed(
            env.server.server_id, executor, context=env.context,
            deadline_seconds=REQUEST_TIMEOUT_SECONDS, gateway=env.gateway,
        )
    except TransportHttpStatus as error:
        return _Response(error.status_code or 0, tuple(error.headers), error.body)
    except TransportFailure as error:
        raise MCPError(MCP_CONNECT_FAILED, f"{what} 请求失败：{error.message}") from error


def _form(env: _Env, url: str, fields: dict[str, str], *, headers: dict[str, str] | None = None, what: str) -> _Response:
    return _fetch(
        env,
        url,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
        body=urlencode(fields).encode("utf-8"),
        what=what,
    )


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------


def _normalize_issuer(issuer: str) -> str:
    """issuer 比较用的归一化：与配置里 ``allowed_issuers`` 同一套（小写 scheme / host、省略默认端口、去尾斜杠）。
    解析不了就退回去首尾空白与尾斜杠的原文——它之后过不了 https 校验，也不会等于任何白名单项。"""
    try:
        return parse_server_url(issuer, allow_private_network=True).geturl().rstrip("/")
    except ValueError:
        return issuer.strip().rstrip("/")


@dataclass(frozen=True)
class ServerMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    revocation_endpoint: str | None
    token_auth_methods: tuple[str, ...]
    iss_parameter_supported: bool
    cimd_supported: bool


@dataclass(frozen=True)
class Discovery:
    resource: str
    issuer: str
    metadata: ServerMetadata
    scope: str | None


def canonical_resource(server: MCPServerConfig) -> str:
    """规范 URI（RFC 8707）：小写 scheme 与 host、无 fragment、无尾斜杠。"""
    assert server.url is not None
    return parse_server_url(server.url, allow_private_network=server.allow_private_network).canonical_resource_uri()


def probe_challenge(env: _Env) -> tuple[str | None, str | None]:
    """不带令牌请求 MCP endpoint，取 401 的 ``WWW-Authenticate``：返回 ``(resource_metadata, scope)``。"""
    from water_agent.mcp.protocol import CLIENT_INFO, make_request

    assert env.server.url is not None
    response = _fetch(
        env,
        env.server.url,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        body=json.dumps(
            make_request(
                1, "initialize",
                {"protocolVersion": MCP_HTTP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            )
        ).encode("utf-8"),
        what="MCP endpoint 探测",
    )
    if response.status != 401:
        raise MCPError(
            MCP_OAUTH_UNAVAILABLE,
            f"server {env.server.server_id!r} 对无令牌请求返回 HTTP {response.status}（期望 401）：它没有要求 OAuth 授权",
        )
    for challenge in parse_www_authenticate(response.header_all("WWW-Authenticate")):
        if challenge.scheme == "bearer":
            return challenge.params.get("resource_metadata"), challenge.params.get("scope")
    return None, None


def resource_metadata_candidates(mcp_url: ParsedUrl) -> list[str]:
    """受保护资源元数据的回落地址（规范顺序）：路径插入 → 根路径。MCP endpoint 在根路径时只有一个。"""
    base = f"{mcp_url.origin}/.well-known/oauth-protected-resource"
    path = mcp_url.path.rstrip("/")
    return [base + path, base] if path else [base]


def authorization_server_candidates(issuer: str) -> list[str]:
    """授权服务器元数据的发现地址（规范顺序）：RFC 8414 → OIDC（有 path 的 issuer 三个，没有的两个）。"""
    parts = urlsplit(issuer)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    if path:
        return [
            f"{origin}/.well-known/oauth-authorization-server{path}",
            f"{origin}/.well-known/openid-configuration{path}",
            f"{origin}{path}/.well-known/openid-configuration",
        ]
    return [f"{origin}/.well-known/oauth-authorization-server", f"{origin}/.well-known/openid-configuration"]


def _protected_resource(env: _Env, hint: str | None) -> dict[str, Any]:
    assert env.server.url is not None
    mcp_url = parse_server_url(env.server.url, allow_private_network=env.server.allow_private_network)
    candidates = [hint] if hint else resource_metadata_candidates(mcp_url)
    last = "没有可用的元数据地址"
    for candidate in candidates:
        response = _fetch(env, candidate, what="受保护资源元数据")
        document = response.json_object() if response.status == 200 else None
        if document is not None:
            return document
        last = f"{candidate} 返回 HTTP {response.status}"
    raise MCPError(MCP_OAUTH_UNAVAILABLE, f"无法取得受保护资源元数据（{last}）")


def _select_issuer(env: _Env, document: dict[str, Any]) -> str:
    resource = document.get("resource")
    if isinstance(resource, str):
        try:
            declared = parse_server_url(resource, allow_private_network=env.server.allow_private_network)
        except ValueError:
            declared = None
        if declared is None or declared.canonical_resource_uri() != canonical_resource(env.server):
            raise MCPError(
                MCP_OAUTH_UNAVAILABLE,
                "受保护资源元数据声明的 resource 与本 server 的规范 URI 不一致（拒绝：可能被指向别的资源）",
            )
    servers = document.get("authorization_servers")
    if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
        raise MCPError(MCP_OAUTH_UNAVAILABLE, "受保护资源元数据缺少 authorization_servers")
    issuer = _normalize_issuer(servers[0])
    allowed = env.oauth.allowed_issuers
    if allowed and issuer not in allowed:
        raise MCPError(
            MCP_OAUTH_UNAVAILABLE,
            f"server 指向的授权服务器 {issuer!r} 不在 oauth.allowed_issuers 里：拒绝向它发任何请求",
        )
    _https_url(issuer, what="授权服务器 issuer")
    return issuer


def _authorization_server_metadata(env: _Env, issuer: str) -> ServerMetadata:
    for candidate in authorization_server_candidates(issuer):
        response = _fetch(env, candidate, what="授权服务器元数据")
        document = response.json_object() if response.status == 200 else None
        if document is None:
            continue
        declared = document.get("issuer")
        if not isinstance(declared, str) or _normalize_issuer(declared) != issuer:
            continue  # RFC 8414 §3.3：issuer 必须与用来构造 URL 的 issuer 一致
        return _validated_metadata(issuer, document)
    raise MCPError(MCP_OAUTH_UNAVAILABLE, f"无法取得授权服务器 {issuer!r} 的元数据（RFC 8414 / OIDC 发现地址都不可用）")


def _validated_metadata(issuer: str, document: dict[str, Any]) -> ServerMetadata:
    methods = document.get("code_challenge_methods_supported")
    if not isinstance(methods, list) or PKCE_METHOD not in methods:
        raise MCPError(
            MCP_OAUTH_UNAVAILABLE,
            f"授权服务器元数据缺少 code_challenge_methods_supported 或不含 {PKCE_METHOD}：拒绝继续（OAuth 2.1 要求 PKCE）",
        )

    def endpoint(name: str, *, required: bool) -> str | None:
        value = document.get(name)
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise MCPError(MCP_OAUTH_UNAVAILABLE, f"授权服务器元数据缺少 {name}")
        _https_url(value, what=name)
        return value

    auth_methods = document.get("token_endpoint_auth_methods_supported")
    return ServerMetadata(
        issuer=issuer,
        authorization_endpoint=str(endpoint("authorization_endpoint", required=True)),
        token_endpoint=str(endpoint("token_endpoint", required=True)),
        registration_endpoint=endpoint("registration_endpoint", required=False),
        revocation_endpoint=endpoint("revocation_endpoint", required=False),
        token_auth_methods=tuple(str(item) for item in auth_methods) if isinstance(auth_methods, list) else (),
        iss_parameter_supported=document.get("authorization_response_iss_parameter_supported") is True,
        cimd_supported=document.get("client_id_metadata_document_supported") is True,
    )


def discover(env: _Env) -> Discovery:
    hint, challenge_scope = probe_challenge(env)
    document = _protected_resource(env, hint)
    issuer = _select_issuer(env, document)
    metadata = _authorization_server_metadata(env, issuer)
    scope = challenge_scope
    if not scope:
        supported = document.get("scopes_supported")
        if isinstance(supported, list) and supported and all(isinstance(item, str) for item in supported):
            scope = " ".join(supported)
    if not scope and env.oauth.scopes:
        scope = " ".join(env.oauth.scopes)
    return Discovery(resource=canonical_resource(env.server), issuer=issuer, metadata=metadata, scope=scope or None)


# ---------------------------------------------------------------------------
# 客户端身份与认证方式
# ---------------------------------------------------------------------------


def _client_secret(env: _Env) -> str | None:
    slot = env.oauth.client_secret_credential
    if slot is None:
        return None
    value = read_slot(env.server.server_id, slot)
    if value is None:
        raise MCPError(
            MCP_CREDENTIAL_MISSING,
            f"server {env.server.server_id!r} 的凭据槽位 {slot} 还没填值（由管理员在 Web 扩展面板填写）",
        )
    return value


def client_authentication(
    env: _Env, client_id: str, method: str
) -> tuple[dict[str, str], dict[str, str]]:
    """令牌端点的客户端认证：返回 ``(请求头, 额外表单字段)``。密钥只在这里读、只进请求头 / 表单。"""
    if method == "none":
        return {}, {"client_id": client_id}
    secret = _client_secret(env)
    assert secret is not None
    if method == "client_secret_basic":
        raw = f"{quote_plus(client_id)}:{quote_plus(secret)}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}, {}
    return {}, {"client_id": client_id, "client_secret": secret}


def _resolve_client(env: _Env, metadata: ServerMetadata, redirect_uri: str) -> tuple[str, str]:
    """返回 ``(client_id, 认证方式)``：预注册优先；否则动态注册（按 issuer 缓存）。"""
    oauth = env.oauth
    if oauth.client_id is not None:
        method = oauth.token_endpoint_auth_method
        if metadata.token_auth_methods and method not in metadata.token_auth_methods:
            raise MCPError(
                MCP_OAUTH_UNAVAILABLE,
                f"授权服务器不支持配置的客户端认证方式 {method}（它支持 {', '.join(metadata.token_auth_methods)}）",
            )
        return oauth.client_id, method
    if not oauth.dynamic_registration:
        raise MCPError(MCP_OAUTH_UNAVAILABLE, "没有预注册的 client_id，也没有开启 dynamic_registration")
    if metadata.registration_endpoint is None:
        hint = "（它只声明了 client_id_metadata_document_supported，本网关不支持 Client ID Metadata Documents）" \
            if metadata.cimd_supported else ""
        raise MCPError(MCP_OAUTH_UNAVAILABLE, f"授权服务器元数据没有 registration_endpoint{hint}：无法动态注册")
    cached = env.store.load_client(metadata.issuer, redirect_uri)
    if cached is not None:
        return cached.client_id, "none"
    response = _fetch(
        env,
        metadata.registration_endpoint,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            {
                "client_name": "water-agent",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
        ).encode("utf-8"),
        what="动态客户端注册",
    )
    document = response.json_object() if response.status in (200, 201) else None
    client_id = document.get("client_id") if document else None
    if not isinstance(client_id, str) or not client_id:
        raise MCPError(MCP_OAUTH_UNAVAILABLE, f"动态客户端注册失败（HTTP {response.status}）")
    env.store.save_client(StoredClient(metadata.issuer, client_id, redirect_uri))
    return client_id, "none"


# ---------------------------------------------------------------------------
# 授权流程
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationStart:
    """``authorization_url`` 交给管理员的浏览器；``issuer`` 与 ``expires_at`` 给界面显示。"""

    authorization_url: str
    issuer: str
    expires_at: float


@dataclass(frozen=True)
class _Pending:
    server: MCPServerConfig
    state: str
    code_verifier: str = field(repr=False)
    discovery: Discovery
    client_id: str
    client_auth: str
    redirect_uri: str
    created_at: float


class PendingAuthorizations:
    """挂起的授权会话：一次性、10 分钟过期、总量有上限（满了淘汰最旧的）。进程内——多 worker 部署需要换成共享存储。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, _Pending] = {}

    def add(self, pending: _Pending) -> None:
        with self._lock:
            self._purge(pending.created_at)
            while len(self._items) >= MAX_PENDING:
                oldest = min(self._items.values(), key=lambda item: item.created_at)
                del self._items[oldest.state]
            self._items[pending.state] = pending

    def take(self, state: str) -> _Pending | None:
        """取走并作废（无论后面成败，state 都只能用一次）。过期的视为不存在。"""
        with self._lock:
            now = _now()
            self._purge(now)
            return self._items.pop(state, None)

    def _purge(self, now: float) -> None:
        for key in [k for k, v in self._items.items() if now - v.created_at > PENDING_TTL_SECONDS]:
            del self._items[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


PENDING = PendingAuthorizations()


def validate_redirect_uri(redirect_uri: str) -> str:
    """回调地址必须是 ``https://`` 或 localhost 回环（规范允许 localhost 走 http）。"""
    parts = urlsplit(redirect_uri)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme == "https" or (parts.scheme == "http" and local):
        if not parts.hostname or parts.fragment or parts.username or parts.password:
            raise MCPError(MCP_OAUTH_UNAVAILABLE, "回调地址不得含 fragment 或 userinfo")
        return redirect_uri
    raise MCPError(MCP_OAUTH_UNAVAILABLE, "回调地址必须是 https://（本机调试允许 http://localhost / 127.0.0.1）")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)  # 86 个字符，落在 RFC 7636 的 43–128 内
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _append_query(endpoint: str, params: dict[str, str]) -> str:
    parts = urlsplit(endpoint)
    query = [*parse_qsl(parts.query, keep_blank_values=True), *params.items()]
    return urlunsplit(parts._replace(query=urlencode(query)))


def start_authorization(
    server: MCPServerConfig,
    *,
    redirect_uri: str,
    context: RequestContext | None = None,
    gateway: ProviderGateway | None = None,
    resolver: Resolver | None = None,
    ssl_context: Any = None,
) -> AuthorizationStart:
    """发现 → 校验 → 生成 PKCE 与 state → 返回要打开的授权 URL。失败抛 ``MCPError``（不挂起任何会话）。"""
    env = _env(server, context=context, gateway=gateway, resolver=resolver, ssl_context=ssl_context)
    validate_redirect_uri(redirect_uri)
    discovery = discover(env)
    client_id, client_auth = _resolve_client(env, discovery.metadata, redirect_uri)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": PKCE_METHOD,
        "state": state,
        "resource": discovery.resource,
    }
    if discovery.scope:
        params["scope"] = discovery.scope
    created = _now()
    PENDING.add(
        _Pending(server=server, state=state, code_verifier=verifier, discovery=discovery, client_id=client_id,
                 client_auth=client_auth, redirect_uri=redirect_uri, created_at=created)
    )
    return AuthorizationStart(
        authorization_url=_append_query(discovery.metadata.authorization_endpoint, params),
        issuer=discovery.issuer,
        expires_at=created + PENDING_TTL_SECONDS,
    )


@dataclass(frozen=True)
class OAuthStatus:
    """一台 server 的授权状态（给面板与评测）。**不含任何令牌值。**"""

    configured: bool
    authorized: bool
    state: str  # none | authorized | expired | resource_changed | issuer_not_allowed
    expires_at: float | None = None
    scope: str | None = None
    issuer: str | None = None


def _token_problem(server: MCPServerConfig, token: StoredToken) -> str | None:
    """令牌不能用于这台 server 的原因（resource 变了 / issuer 不在白名单里）；可用返回 ``None``。"""
    if token.resource != canonical_resource(server):
        return "resource_changed"
    allowed = server.oauth.allowed_issuers if server.oauth is not None else ()
    if allowed and token.issuer not in allowed:
        return "issuer_not_allowed"
    return None


def authorization_status(server: MCPServerConfig) -> OAuthStatus:
    if server.oauth is None or server.transport is not ServerTransport.STREAMABLE_HTTP:
        return OAuthStatus(False, False, "none")
    token = OAuthStore(server.server_id).load()
    if token is None:
        return OAuthStatus(True, False, "none")
    problem = _token_problem(server, token)
    if problem is not None:
        return OAuthStatus(True, False, problem, token.expires_at, token.scope, token.issuer)
    left = token.seconds_left(_now())
    if left is not None and left <= 0 and not token.refresh_token:
        return OAuthStatus(True, False, "expired", token.expires_at, token.scope, token.issuer)
    return OAuthStatus(True, True, "authorized", token.expires_at, token.scope, token.issuer)


def _sanitized_error(response: _Response) -> str:
    """授权服务器的 ``error`` 字段（OAuth 错误码形状），其余内容一律不回显。"""
    document = response.json_object() or {}
    error = document.get("error")
    if isinstance(error, str) and 0 < len(error) <= 40 and all(ch.isalnum() or ch in "_-" for ch in error):
        return error
    return f"HTTP {response.status}"


def _parse_token_response(response: _Response, *, requested_scope: str | None) -> dict[str, Any]:
    document = response.json_object() if response.status == 200 else None
    access = document.get("access_token") if document else None
    if not isinstance(access, str) or not access:
        raise MCPError(MCP_OAUTH_REQUIRED, f"授权服务器没有签发令牌（{_sanitized_error(response)}）")
    assert document is not None
    if str(document.get("token_type", "")).lower() != "bearer":
        raise MCPError(MCP_OAUTH_REQUIRED, "授权服务器签发的令牌类型不是 Bearer")
    expires_in = document.get("expires_in")
    refresh = document.get("refresh_token")
    scope = document.get("scope")
    return {
        "access_token": access,
        "refresh_token": refresh if isinstance(refresh, str) and refresh else None,
        "expires_in": float(expires_in) if isinstance(expires_in, (int, float)) and expires_in > 0 else None,
        "scope": scope if isinstance(scope, str) and scope else requested_scope,
    }


def complete_authorization(
    *,
    state: str | None,
    code: str | None,
    error: str | None = None,
    iss: str | None = None,
    redirect_uri: str | None = None,
    context: RequestContext | None = None,
    gateway: ProviderGateway | None = None,
    resolver: Resolver | None = None,
    ssl_context: Any = None,
) -> OAuthStatus:
    """回调：只认服务端记下的 state（一次性）；``iss`` 对不上、授权被拒、换码失败都丢弃，不留令牌。"""
    pending = PENDING.take(state) if state else None
    if pending is None:
        raise MCPError(MCP_OAUTH_REQUIRED, "授权会话不存在或已过期（state 对不上）：请重新发起授权")
    server = pending.server
    env = _env(server, context=context, gateway=gateway, resolver=resolver, ssl_context=ssl_context)
    if error:
        safe = error if len(error) <= 40 and all(ch.isalnum() or ch in "_-" for ch in error) else "（已省略）"
        raise MCPError(MCP_OAUTH_REQUIRED, f"授权服务器拒绝了授权：{safe}")
    if redirect_uri is not None and redirect_uri != pending.redirect_uri:
        raise MCPError(MCP_OAUTH_REQUIRED, "回调地址与发起授权时不一致")
    metadata = pending.discovery.metadata
    if iss is not None:
        if _normalize_issuer(iss) != pending.discovery.issuer:
            raise MCPError(MCP_OAUTH_REQUIRED, "回调的 iss 与发起授权时记录的授权服务器不一致（RFC 9207）：已丢弃")
    elif metadata.iss_parameter_supported:
        raise MCPError(MCP_OAUTH_REQUIRED, "授权服务器声明会在回调里带 iss，但回调没有：已丢弃（RFC 9207）")
    if not code:
        raise MCPError(MCP_OAUTH_REQUIRED, "回调没有授权码")

    headers, extra = client_authentication(env, pending.client_id, pending.client_auth)
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "code_verifier": pending.code_verifier,
        "resource": pending.discovery.resource,
        **extra,
    }
    response = _form(env, metadata.token_endpoint, fields, headers=headers, what="令牌端点")
    parsed = _parse_token_response(response, requested_scope=pending.discovery.scope)
    now = _now()
    token = StoredToken(
        authorization_id=uuid.uuid4().hex,
        issuer=pending.discovery.issuer,
        resource=pending.discovery.resource,
        client_id=pending.client_id,
        client_auth=pending.client_auth,
        token_endpoint=metadata.token_endpoint,
        revocation_endpoint=metadata.revocation_endpoint,
        access_token=parsed["access_token"],
        refresh_token=parsed["refresh_token"],
        issued_at=now,
        expires_at=None if parsed["expires_in"] is None else now + parsed["expires_in"],
        scope=parsed["scope"],
    )
    env.store.save(token)
    return authorization_status(server)


def revoke_authorization(
    server: MCPServerConfig,
    *,
    context: RequestContext | None = None,
    gateway: ProviderGateway | None = None,
    resolver: Resolver | None = None,
    ssl_context: Any = None,
) -> OAuthStatus:
    """撤销：先尽力调用一次授权服务器的 revocation endpoint（失败不影响），再删本地令牌。"""
    env = _env(server, context=context, gateway=gateway, resolver=resolver, ssl_context=ssl_context)
    token = env.store.load()
    if token is not None and token.revocation_endpoint is not None:
        try:
            headers, extra = client_authentication(env, token.client_id, token.client_auth)
            _form(
                env,
                token.revocation_endpoint,
                {"token": token.refresh_token or token.access_token,
                 "token_type_hint": "refresh_token" if token.refresh_token else "access_token", **extra},
                headers=headers,
                what="撤销端点",
            )
        except (MCPError, ValueError):
            pass  # 授权服务器不可达不应挡住本地撤销
    env.store.clear()
    return authorization_status(server)


# ---------------------------------------------------------------------------
# 令牌使用与刷新（MCPHttpClient 的认证提供者）
# ---------------------------------------------------------------------------


class OAuthAuth:
    """``HttpAuth``：给每个发往这台 server 的请求提供 ``Authorization: Bearer``。

    令牌只在这里读、只经请求头发出、只发给签发它的 issuer 对应的这一个 MCP endpoint（resource 对不上就不发）。
    即将过期时用 refresh token 刷新一次（轮换后的 refresh token 替换旧值）；刷新被拒绝 → 清除令牌、
    ``MCP_OAUTH_REQUIRED``。
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        gateway: ProviderGateway | None = None,
        resolver: Resolver | None = None,
        ssl_context: Any = None,
    ) -> None:
        self._server = config
        self._gateway = gateway
        self._resolver = resolver
        self._ssl_context = ssl_context
        self._store = OAuthStore(config.server_id)

    def _usable(self) -> StoredToken | None:
        token = self._store.load()
        if token is None or _token_problem(self._server, token) is not None:
            return None
        return token

    @staticmethod
    def _skew(token: StoredToken) -> float:
        lifetime = token.lifetime()
        return REFRESH_SKEW_SECONDS if lifetime is None else min(REFRESH_SKEW_SECONDS, lifetime * 0.5)

    def authorization(self) -> str | None:
        token = self._usable()
        if token is None:
            return None
        left = token.seconds_left(_now())
        if left is not None and left <= self._skew(token):
            if token.refresh_token:
                try:
                    token = self._refresh(token)
                except MCPError:
                    if left <= 0:
                        raise
                    # 刷新暂时失败而旧令牌还没真过期：先用旧的（definitive 失败时令牌已被清除，下面读不到）
                    token = self._usable()
                    if token is None:
                        raise
            elif left <= 0:
                raise MCPError(MCP_OAUTH_REQUIRED, "访问令牌已过期且没有 refresh token：需管理员重新授权")
        return f"Bearer {token.access_token}"

    def refresh_after_unauthorized(self) -> bool:
        token = self._usable()
        if token is None or not token.refresh_token:
            return False
        self._refresh(token)
        return True

    def _refresh(self, snapshot: StoredToken) -> StoredToken:
        env = _env(self._server, gateway=self._gateway, resolver=self._resolver, ssl_context=self._ssl_context)
        with store_lock(self._server.server_id):
            current = env.store.load()
            if current is None or _token_problem(self._server, current) is not None:
                raise MCPError(MCP_OAUTH_REQUIRED, "没有可用的授权：需管理员（重新）授权")
            if current.access_token != snapshot.access_token:
                return current  # 别的线程已经刷新过了（含 401 之后的强制刷新：不重复刷）
            if not current.refresh_token:
                raise MCPError(MCP_OAUTH_REQUIRED, "没有 refresh token：需管理员重新授权")
            headers, extra = client_authentication(env, current.client_id, current.client_auth)
            fields = {
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
                "resource": current.resource,
                **extra,
            }
            try:
                response = _form(env, current.token_endpoint, fields, headers=headers, what="令牌端点（刷新）")
            except MCPError as exc:
                if exc.code == MCP_TIMEOUT:
                    raise MCPError(MCP_CONNECT_FAILED, f"刷新令牌超时：{exc.message}") from exc
                raise
            if response.status >= 500 or response.status in (408, 429):
                raise MCPError(MCP_CONNECT_FAILED, f"授权服务器暂时不可用（HTTP {response.status}），令牌未刷新")
            try:
                parsed = _parse_token_response(response, requested_scope=current.scope)
            except MCPError as exc:
                env.store.clear()
                _emit("refresh_failed", self._server.server_id, issuer=current.issuer, reason=_sanitized_error(response))
                raise MCPError(
                    MCP_OAUTH_REQUIRED, f"刷新令牌被拒绝（{_sanitized_error(response)}）：已清除本地令牌，需管理员重新授权"
                ) from exc
            now = _now()
            renewed = current.with_tokens(
                access_token=parsed["access_token"],
                refresh_token=parsed["refresh_token"],  # 被轮换就换新值；没返回就保留旧值
                issued_at=now,
                expires_at=None if parsed["expires_in"] is None else now + parsed["expires_in"],
                scope=parsed["scope"],
            )
            env.store.save(renewed)
            return renewed


def build_oauth_auth(config: MCPServerConfig) -> OAuthAuth:
    return OAuthAuth(config)


__all__ = [
    "PENDING",
    "PENDING_TTL_SECONDS",
    "AuthorizationStart",
    "Discovery",
    "OAuthAuth",
    "OAuthStatus",
    "PendingAuthorizations",
    "ServerMetadata",
    "authorization_server_candidates",
    "authorization_status",
    "canonical_resource",
    "complete_authorization",
    "resource_metadata_candidates",
    "revoke_authorization",
    "set_event_sink",
    "start_authorization",
    "validate_redirect_uri",
]
