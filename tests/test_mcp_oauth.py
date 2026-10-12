"""P7-S8 Step 7：OAuth 2.1 服务级授权（``docs/design/16-p7-plugin-mcp.md`` §7 P7-S8-A06 / A07）。

规范 2025-11-25 authorization 的客户端 MUST 项逐条一个断言（Prompt §6 "规范逐条"）：

1. 401 优先 ``resource_metadata``，否则路径插入与根路径两个 well-known（A06）；
2. 授权服务器元数据按 RFC 8414 → OIDC 的顺序（A06）；
3. ``code_challenge_methods_supported`` 缺失 / 不含 S256 → 拒绝（A06）；
4. 授权请求与令牌请求都带规范 URI 的 ``resource``（A07）；
5. state 一次性、``iss`` 校验（A07）；
6. 令牌只经 ``Authorization`` 头、只发给签发它的 issuer 对应的那一个 MCP endpoint（A07 / A08）；
7. 授权服务器端点必须 https，回调地址 https 或 localhost（A07）；
8. 注册信息按 issuer 存（A07）；
9. refresh token 轮换时替换旧值（A07）；
10. ``insufficient_scope`` 如实报出、不自动升级（A07）。
"""
from __future__ import annotations

import json
import os
import ssl
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from water_agent.mcp import oauth
from water_agent.mcp.config import MCPServerConfig
from water_agent.mcp.credentials import write_slot
from water_agent.mcp.errors import (
    MCP_CREDENTIAL_MISSING,
    MCP_OAUTH_INSUFFICIENT_SCOPE,
    MCP_OAUTH_REQUIRED,
    MCP_OAUTH_UNAVAILABLE,
    MCPError,
)
from water_agent.mcp.http_client import MCPHttpClient, mcp_request_scope
from water_agent.mcp.oauth_store import OAuthStore
from water_agent.runtime.governance.contracts import (
    DEFAULT_LOCAL_CAPABILITIES,
    Capability,
    Principal,
    RequestContext,
)

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_http_server.py"
REDIRECT = "http://127.0.0.1:18080/api/admin/mcp/oauth/callback"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cert_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("oauth-tls") / "tls"
    subprocess.run([sys.executable, str(FAKE), "--make-cert", str(directory)], check=True, capture_output=True)
    return directory


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cert_dir: Path) -> Iterator[None]:
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_dir / "cert.pem"))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.delenv(key, raising=False)
    oauth.PENDING.clear()
    oauth.set_event_sink(None)
    yield
    oauth.PENDING.clear()
    oauth.set_event_sink(None)


class Fake:
    def __init__(self, mode: str, tmp: Path, cert_dir: Path, *flags: str) -> None:
        self.tmp = tmp
        tmp.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, str(FAKE), "--mode", mode, "--cert-dir", str(cert_dir), "--log", str(tmp / "log.jsonl"),
             "--secrets", str(tmp / "secrets.json"), *flags],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        ports = json.loads(self.proc.stdout.readline())  # type: ignore[union-attr]
        self.port, self.auth_port = ports["mcp_port"], ports["auth_port"]
        self.context = ssl.create_default_context(cafile=str(cert_dir / "cert.pem"))

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}/mcp"

    @property
    def issuer(self) -> str:
        return f"https://127.0.0.1:{self.auth_port}"

    def log(self) -> list[dict[str, Any]]:
        path = self.tmp / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=3)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()


@pytest.fixture
def fake(tmp_path: Path, cert_dir: Path) -> Iterator[Callable[..., Fake]]:
    started: list[Fake] = []

    def start(mode: str = "oauth", *flags: str) -> Fake:
        server = Fake(mode, tmp_path / f"fake{len(started)}", cert_dir, *flags)
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


def config(url: str, **oauth_overrides: Any) -> MCPServerConfig:
    payload: dict[str, Any] = {
        "server_id": "oauthfake",
        "transport": "streamable_http",
        "url": url,
        "allow_private_network": True,
        "allow": ["echo_query"],
        "call_timeout_seconds": 3.0,
        "startup_timeout_seconds": 3.0,
        "oauth": {"client_id": "wa-preregistered", **oauth_overrides},
    }
    extra = payload["oauth"].pop("_server", {})
    payload.update(extra)
    return MCPServerConfig.model_validate(payload)


def caller() -> RequestContext:
    return RequestContext(
        principal=Principal.local("oauth-tester", capabilities=DEFAULT_LOCAL_CAPABILITIES | {Capability.MCP_INVOKE}),
        request_id="req-oauth",
    )


def browse(url: str, cert_dir: Path | None = None) -> dict[str, list[str]]:
    """代浏览器访问授权端点（不跟随重定向），返回回调参数。"""
    return parse_qs(urlparse(oauth_browser(url)).query)


def oauth_browser(authorization_url: str) -> str:
    from water_agent.harness.mcp_http_eval import browser_authorize

    return browser_authorize(authorization_url)


def authorize(server: MCPServerConfig, **overrides: Any) -> oauth.OAuthStatus:
    start = oauth.start_authorization(server, redirect_uri=REDIRECT)
    query = parse_qs(urlparse(oauth_browser(start.authorization_url)).query)
    return oauth.complete_authorization(
        state=overrides.pop("state", query["state"][0]),
        code=overrides.pop("code", query["code"][0]),
        iss=overrides.pop("iss", query["iss"][0] if "iss" in query else None),
        redirect_uri=REDIRECT,
        **overrides,
    )


class TlsRoutes:
    """按路径脚本化的 https 服务（同一个服务既是 MCP 资源服务器又是授权服务器），用来检查请求的顺序与内容。"""

    def __init__(self, cert_dir: Path) -> None:
        outer = self
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[tuple[str, str], Callable[[dict[str, Any]], tuple[int, Any, dict[str, str]]]] = {}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: object) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                parsed = urlparse(self.path)
                record = {"method": self.command, "path": parsed.path, "query": parsed.query, "body": raw.decode(),
                          "headers": dict(self.headers)}
                outer.requests.append(record)
                handler = outer.routes.get((self.command, parsed.path)) or outer.routes.get(("*", parsed.path))
                status, body, headers = handler(record) if handler else (404, None, {})
                payload = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

            do_GET = _serve
            do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_dir / "cert.pem"), str(cert_dir / "key.pem"))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"https://127.0.0.1:{self.server.server_address[1]}"

    def route(self, method: str, path: str, status: int = 200, body: Any = None,
              headers: dict[str, str] | None = None) -> None:
        self.routes[(method, path)] = lambda _record: (status, body, headers or {})

    def paths(self) -> list[str]:
        return [item["path"] for item in self.requests]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def tls_routes(cert_dir: Path) -> Iterator[Callable[[], TlsRoutes]]:
    started: list[TlsRoutes] = []

    def start() -> TlsRoutes:
        server = TlsRoutes(cert_dir)
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


def as_metadata(origin: str, issuer: str | None = None, **overrides: Any) -> dict[str, Any]:
    document = {
        "issuer": issuer or origin,
        "authorization_endpoint": f"{origin}/authorize",
        "token_endpoint": f"{origin}/token",
        "code_challenge_methods_supported": ["S256"],
        "response_types_supported": ["code"],
    }
    document.update(overrides)
    return document


def unauthorized(origin: str, *, hint: str | None, scope: str | None = None) -> Callable[[dict[str, Any]], tuple[int, Any, dict[str, str]]]:
    parts = ["Bearer"]
    items = []
    if hint:
        items.append(f'resource_metadata="{hint}"')
    if scope:
        items.append(f'scope="{scope}"')
    header = "Bearer " + ", ".join(items) if items else "Bearer"
    del parts
    return lambda _record: (401, None, {"WWW-Authenticate": header})


def routes_server(routes: TlsRoutes, path: str = "/mcp", **oauth_overrides: Any) -> MCPServerConfig:
    return config(f"{routes.origin}{path}", **oauth_overrides)


def env_for(server: MCPServerConfig) -> oauth._Env:
    return oauth._env(server)


# ---------------------------------------------------------------------------
# A06 发现
# ---------------------------------------------------------------------------


def test_protected_resource_metadata_discovery_order(tls_routes: Callable[[], TlsRoutes]) -> None:
    """规范 MUST（1）：401 的 resource_metadata 优先——有就只用它，不再试 well-known；没有才按顺序试
    路径插入 → 根路径。"""

    # (a) 头里有 resource_metadata：只请求它
    routes = tls_routes()
    routes.routes[("POST", "/mcp")] = unauthorized(routes.origin, hint=f"{routes.origin}/custom/prm")
    routes.route("GET", "/custom/prm", body={"resource": f"{routes.origin}/mcp", "authorization_servers": [routes.origin]})
    routes.route("GET", "/.well-known/oauth-authorization-server", body=as_metadata(routes.origin))
    oauth.discover(env_for(routes_server(routes)))
    assert [p for p in routes.paths() if "protected-resource" in p or "custom" in p] == ["/custom/prm"]

    # (b) 没有头：路径插入失败（404）→ 根路径
    routes = tls_routes()
    routes.routes[("POST", "/mcp")] = unauthorized(routes.origin, hint=None)
    routes.route("GET", "/.well-known/oauth-protected-resource",
                 body={"resource": f"{routes.origin}/mcp", "authorization_servers": [routes.origin]})
    routes.route("GET", "/.well-known/oauth-authorization-server", body=as_metadata(routes.origin))
    oauth.discover(env_for(routes_server(routes)))
    assert [p for p in routes.paths() if "protected-resource" in p] == [
        "/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource",
    ]

    # (c) MCP endpoint 在根路径：只有根路径一个候选
    routes = tls_routes()
    routes.routes[("POST", "/")] = unauthorized(routes.origin, hint=None)
    routes.route("GET", "/.well-known/oauth-protected-resource",
                 body={"resource": routes.origin, "authorization_servers": [routes.origin]})
    routes.route("GET", "/.well-known/oauth-authorization-server", body=as_metadata(routes.origin))
    oauth.discover(env_for(routes_server(routes, path="")))
    assert [p for p in routes.paths() if "protected-resource" in p] == ["/.well-known/oauth-protected-resource"]
    assert oauth.resource_metadata_candidates(
        __import__("water_agent.mcp.http_transport", fromlist=["x"]).parse_server_url("https://a.example/x/y/")
    ) == ["https://a.example/.well-known/oauth-protected-resource/x/y", "https://a.example/.well-known/oauth-protected-resource"]

    # 头里的地址取不到：不回落（规范：有头就用头）
    routes = tls_routes()
    routes.routes[("POST", "/mcp")] = unauthorized(routes.origin, hint=f"{routes.origin}/missing")
    with pytest.raises(MCPError) as caught:
        oauth.discover(env_for(routes_server(routes)))
    assert caught.value.code == MCP_OAUTH_UNAVAILABLE
    assert not any("well-known" in p for p in routes.paths())


def test_authorization_server_metadata_discovery_order(tls_routes: Callable[[], TlsRoutes]) -> None:
    """规范 MUST（2）：有 path 的 issuer 依次试 RFC 8414 路径插入 → OIDC 路径插入 → OIDC 路径追加；
    没有 path 的试 RFC 8414 → OIDC。元数据里的 issuer 必须等于用来构造 URL 的 issuer。"""

    assert oauth.authorization_server_candidates("https://auth.example.com/tenant1") == [
        "https://auth.example.com/.well-known/oauth-authorization-server/tenant1",
        "https://auth.example.com/.well-known/openid-configuration/tenant1",
        "https://auth.example.com/tenant1/.well-known/openid-configuration",
    ]
    assert oauth.authorization_server_candidates("https://auth.example.com") == [
        "https://auth.example.com/.well-known/oauth-authorization-server",
        "https://auth.example.com/.well-known/openid-configuration",
    ]

    routes = tls_routes()
    issuer = f"{routes.origin}/tenant1"
    routes.routes[("POST", "/mcp")] = unauthorized(routes.origin, hint=None)
    routes.route("GET", "/.well-known/oauth-protected-resource/mcp",
                 body={"resource": f"{routes.origin}/mcp", "authorization_servers": [issuer]})
    # 前两个 404；第三个（OIDC 路径追加）可用
    routes.route("GET", "/tenant1/.well-known/openid-configuration", body=as_metadata(routes.origin, issuer))
    found = oauth.discover(env_for(routes_server(routes)))
    assert found.issuer == issuer
    as_paths = [p for p in routes.paths() if "well-known" in p and "protected" not in p]
    assert as_paths == [
        "/.well-known/oauth-authorization-server/tenant1",
        "/.well-known/openid-configuration/tenant1",
        "/tenant1/.well-known/openid-configuration",
    ]

    # issuer 对不上的元数据被跳过，继续试下一个地址
    routes = tls_routes()
    routes.routes[("POST", "/mcp")] = unauthorized(routes.origin, hint=None)
    routes.route("GET", "/.well-known/oauth-protected-resource/mcp",
                 body={"resource": f"{routes.origin}/mcp", "authorization_servers": [routes.origin]})
    routes.route("GET", "/.well-known/oauth-authorization-server",
                 body=as_metadata(routes.origin, "https://evil.example"))
    routes.route("GET", "/.well-known/openid-configuration", body=as_metadata(routes.origin))
    assert oauth.discover(env_for(routes_server(routes))).issuer == routes.origin


def test_discovery_trusts_only_the_first_allowed_issuer_and_our_own_resource(
    tls_routes: Callable[[], TlsRoutes],
) -> None:
    routes = tls_routes()
    routes.routes[("POST", "/mcp")] = unauthorized(routes.origin, hint=None)
    routes.route("GET", "/.well-known/oauth-protected-resource/mcp",
                 body={"resource": f"{routes.origin}/mcp",
                       "authorization_servers": ["https://foreign.example.com", routes.origin]})
    server = routes_server(routes, allowed_issuers=[routes.origin])
    with pytest.raises(MCPError) as caught:
        oauth.discover(env_for(server))
    assert caught.value.code == MCP_OAUTH_UNAVAILABLE and "allowed_issuers" in caught.value.message
    assert not any("foreign" in json.dumps(item) for item in routes.requests), "不向白名单外的授权服务器发请求"

    routes = tls_routes()
    routes.routes[("POST", "/mcp")] = unauthorized(routes.origin, hint=None)
    routes.route("GET", "/.well-known/oauth-protected-resource/mcp",
                 body={"resource": "https://other-resource.example/mcp", "authorization_servers": [routes.origin]})
    with pytest.raises(MCPError) as caught:
        oauth.discover(env_for(routes_server(routes)))
    assert caught.value.code == MCP_OAUTH_UNAVAILABLE and "resource" in caught.value.message


def test_pkce_support_is_required(tls_routes: Callable[[], TlsRoutes]) -> None:
    """规范 MUST（3）：缺 code_challenge_methods_supported、或不含 S256（只有 plain）都拒绝继续。"""

    for methods in (None, [], ["plain"]):
        routes = tls_routes()
        routes.routes[("POST", "/mcp")] = unauthorized(routes.origin, hint=None)
        routes.route("GET", "/.well-known/oauth-protected-resource/mcp",
                     body={"resource": f"{routes.origin}/mcp", "authorization_servers": [routes.origin]})
        document = as_metadata(routes.origin)
        if methods is None:
            del document["code_challenge_methods_supported"]
        else:
            document["code_challenge_methods_supported"] = methods
        routes.route("GET", "/.well-known/oauth-authorization-server", body=document)
        with pytest.raises(MCPError) as caught:
            oauth.start_authorization(routes_server(routes), redirect_uri=REDIRECT)
        assert caught.value.code == MCP_OAUTH_UNAVAILABLE and "S256" in caught.value.message
        assert len(oauth.PENDING) == 0


def test_authorization_server_endpoints_must_be_https(tls_routes: Callable[[], TlsRoutes]) -> None:
    """规范 MUST（7）：授权服务器端点必须 https；回调地址是 https 或 localhost。"""

    routes = tls_routes()
    routes.routes[("POST", "/mcp")] = unauthorized(routes.origin, hint=None)
    routes.route("GET", "/.well-known/oauth-protected-resource/mcp",
                 body={"resource": f"{routes.origin}/mcp", "authorization_servers": [routes.origin]})
    routes.route("GET", "/.well-known/oauth-authorization-server",
                 body=as_metadata(routes.origin, token_endpoint="http://127.0.0.1:1/token"))
    with pytest.raises(MCPError) as caught:
        oauth.start_authorization(routes_server(routes), redirect_uri=REDIRECT)
    assert caught.value.code == MCP_OAUTH_UNAVAILABLE and "token_endpoint" in caught.value.message

    assert oauth.validate_redirect_uri("https://agent.example.com/cb")
    assert oauth.validate_redirect_uri("http://localhost:8000/cb")
    assert oauth.validate_redirect_uri("http://127.0.0.1:8000/cb")
    for bad in ("http://agent.example.com/cb", "ftp://x/cb", "https://agent.example.com/cb#frag", "javascript:alert(1)"):
        with pytest.raises(MCPError):
            oauth.validate_redirect_uri(bad)


# ---------------------------------------------------------------------------
# A07 流程
# ---------------------------------------------------------------------------


def test_authorization_request_parameters(fake: Callable[..., Fake], cert_dir: Path) -> None:
    """规范 MUST（4）：授权请求与令牌请求都带规范 URI 的 resource；PKCE S256；state 足够随机；scope 优先级。"""

    server = fake("oauth")
    cfg = config(server.url)
    start = oauth.start_authorization(cfg, redirect_uri=REDIRECT)
    query = parse_qs(urlparse(start.authorization_url).query)
    assert urlparse(start.authorization_url).geturl().startswith(f"{server.issuer}/authorize?")
    assert query["response_type"] == ["code"] and query["client_id"] == ["wa-preregistered"]
    assert query["redirect_uri"] == [REDIRECT]
    assert query["code_challenge_method"] == ["S256"] and len(query["code_challenge"][0]) == 43
    assert query["resource"] == [server.url], "规范 URI：小写 scheme / host、无 fragment、无尾斜杠"
    assert query["scope"] == ["mcp:read"], "WWW-Authenticate 的 scope 优先"
    assert len(query["state"][0]) >= 32
    callback = parse_qs(urlparse(oauth_browser(start.authorization_url)).query)
    oauth.complete_authorization(state=callback["state"][0], code=callback["code"][0], iss=callback["iss"][0],
                                 redirect_uri=REDIRECT)
    tokens = [e for e in server.log() if e.get("event") == "token"]
    assert len(tokens) == 1 and tokens[0]["has_resource"] and tokens[0]["has_code_verifier"]
    assert tokens[0]["resource"] == server.url and tokens[0]["client_auth"] == "none"
    # verifier 与 challenge 对得上：服务端用 S256 校验通过才会签发（见 fake 的 /token）
    assert OAuthStore("oauthfake").load() is not None

    # state 每次新生成
    first = oauth.start_authorization(cfg, redirect_uri=REDIRECT)
    second = oauth.start_authorization(cfg, redirect_uri=REDIRECT)
    assert parse_qs(urlparse(first.authorization_url).query)["state"] != parse_qs(urlparse(second.authorization_url).query)["state"]


def test_canonical_resource_uri_and_scope_selection(tls_routes: Callable[[], TlsRoutes]) -> None:
    routes = tls_routes()
    routes.routes[("POST", "/mcp/")] = unauthorized(routes.origin, hint=None)
    routes.route("GET", "/.well-known/oauth-protected-resource/mcp",
                 body={"resource": f"{routes.origin}/mcp", "authorization_servers": [routes.origin],
                       "scopes_supported": ["a", "b"]})
    routes.route("GET", "/.well-known/oauth-authorization-server", body=as_metadata(routes.origin))
    # 尾斜杠与大写 host 都归一
    server = config(f"https://127.0.0.1:{routes.origin.rsplit(':', 1)[1]}/mcp/")
    assert oauth.canonical_resource(server) == f"{routes.origin}/mcp"
    # 挑战里没有 scope：用 scopes_supported 全部
    found = oauth.discover(env_for(server))
    assert found.scope == "a b"
    # 都没有：用运维配置的 scopes；再没有就不带
    routes.route("GET", "/.well-known/oauth-protected-resource/mcp",
                 body={"resource": f"{routes.origin}/mcp", "authorization_servers": [routes.origin]})
    assert oauth.discover(env_for(config(server.url, scopes=["x", "y"]))).scope == "x y"
    assert oauth.discover(env_for(server)).scope is None


def test_callback_rejects_bad_state_and_issuer(fake: Callable[..., Fake]) -> None:
    """规范 MUST（5）：state 一次性、不匹配即丢弃；回调带 iss 时必须等于记录的 issuer；任何失败都不留令牌。"""

    server = fake("oauth")
    cfg = config(server.url)

    def pending() -> dict[str, list[str]]:
        start = oauth.start_authorization(cfg, redirect_uri=REDIRECT)
        return parse_qs(urlparse(oauth_browser(start.authorization_url)).query)

    good = pending()
    with pytest.raises(MCPError) as caught:  # 伪造的 state
        oauth.complete_authorization(state="forged", code=good["code"][0], iss=good["iss"][0], redirect_uri=REDIRECT)
    assert caught.value.code == MCP_OAUTH_REQUIRED
    # 真 state 还在挂起：伪造的那次没有消耗它；用对了成功，再用一次就失败（一次性）
    assert oauth.complete_authorization(state=good["state"][0], code=good["code"][0], iss=good["iss"][0],
                                        redirect_uri=REDIRECT).authorized
    with pytest.raises(MCPError):
        oauth.complete_authorization(state=good["state"][0], code=good["code"][0], iss=good["iss"][0],
                                     redirect_uri=REDIRECT)
    OAuthStore("oauthfake").clear()

    for label, kwargs in (
        ("iss 不一致", {"iss": "https://evil.example"}),
        ("缺 iss 而授权服务器声明会带", {"iss": None}),
        ("授权被拒", {"error": "access_denied"}),
        ("回调地址不一致", {"redirect_uri": "http://127.0.0.1:1/other"}),
        ("缺授权码", {"code": None}),
    ):
        query = pending()
        arguments: dict[str, Any] = {"state": query["state"][0], "code": query["code"][0], "iss": query["iss"][0],
                                     "redirect_uri": REDIRECT}
        arguments.update(kwargs)
        with pytest.raises(MCPError) as bad:
            oauth.complete_authorization(**arguments)
        assert bad.value.code == MCP_OAUTH_REQUIRED, label
        assert OAuthStore("oauthfake").load() is None, label
        assert len(oauth.PENDING) == 0, f"{label}：失败后 state 也已作废"
    assert len([e for e in server.log() if e.get("event") == "token"]) == 1, "只有最前面那次合法兑换走到了令牌端点"


def test_pending_state_expires(fake: Callable[..., Fake], monkeypatch: pytest.MonkeyPatch) -> None:
    server = fake("oauth")
    cfg = config(server.url)
    start = oauth.start_authorization(cfg, redirect_uri=REDIRECT)
    query = parse_qs(urlparse(oauth_browser(start.authorization_url)).query)
    real_now = oauth._now
    monkeypatch.setattr(oauth, "_now", lambda: real_now() + oauth.PENDING_TTL_SECONDS + 1)
    with pytest.raises(MCPError) as caught:
        oauth.complete_authorization(state=query["state"][0], code=query["code"][0], iss=query["iss"][0],
                                     redirect_uri=REDIRECT)
    assert caught.value.code == MCP_OAUTH_REQUIRED and "已过期" in caught.value.message


def test_pending_sessions_are_bounded(fake: Callable[..., Fake]) -> None:
    server = fake("oauth")
    cfg = config(server.url)
    for _ in range(oauth.MAX_PENDING + 5):
        oauth.start_authorization(cfg, redirect_uri=REDIRECT)
    assert len(oauth.PENDING) == oauth.MAX_PENDING


def test_refresh_rotation_and_expiry(fake: Callable[..., Fake], monkeypatch: pytest.MonkeyPatch) -> None:
    """规范 MUST（9）：refresh token 被轮换时替换旧值（第二次刷新仍成功）；即将过期才刷新；
    刷新被拒 → 清除令牌、MCP_OAUTH_REQUIRED、记事件；授权服务器暂时不可用 → 令牌保留。"""

    server = fake("oauth_expire")
    cfg = config(server.url)
    authorize(cfg)
    store = OAuthStore("oauthfake")
    first = store.load()
    assert first is not None and first.expires_at is not None
    auth = oauth.OAuthAuth(cfg)
    assert auth.authorization() == f"Bearer {first.access_token}", "新鲜令牌：不刷新"
    assert [e for e in server.log() if e.get("event") == "token"][-1]["grant_type"] == "authorization_code"

    real_now = oauth._now
    offset = {"value": 2.0}  # 寿命 3 秒，提前量 min(60, 1.5)：1.5 秒之后就该刷新
    monkeypatch.setattr(oauth, "_now", lambda: real_now() + offset["value"])
    second_header = auth.authorization()
    second = store.load()
    assert second is not None and second.access_token != first.access_token
    assert second_header == f"Bearer {second.access_token}"
    assert second.refresh_token != first.refresh_token, "轮换后的 refresh token 替换了旧值"
    assert second.authorization_id == first.authorization_id, "刷新不是重新授权：授权身份不变"
    assert second.authorization_digest() == first.authorization_digest()

    offset["value"] += 2.0
    third_header = auth.authorization()  # 用的是轮换后的 refresh token：旧值已被服务端作废，成功说明确实换了
    third = store.load()
    assert third is not None and third_header == f"Bearer {third.access_token}" and third.access_token != second.access_token
    refreshes = [e for e in server.log() if e.get("event") == "token" and e["grant_type"] == "refresh_token"]
    assert len(refreshes) == 2 and all(e["has_resource"] for e in refreshes)


def test_refresh_failures_clear_or_keep_the_token(fake: Callable[..., Fake], monkeypatch: pytest.MonkeyPatch,
                                                  tls_routes: Callable[[], TlsRoutes]) -> None:
    events: list[tuple[str, str, dict[str, str]]] = []
    oauth.set_event_sink(lambda name, server_id, fields: events.append((name, server_id, fields)))
    server = fake("oauth_refresh_fail")
    cfg = config(server.url)
    authorize(cfg)
    real_now = oauth._now
    monkeypatch.setattr(oauth, "_now", lambda: real_now() + 10.0)  # 令牌已过期
    with pytest.raises(MCPError) as caught:
        oauth.OAuthAuth(cfg).authorization()
    assert caught.value.code == MCP_OAUTH_REQUIRED
    assert OAuthStore("oauthfake").load() is None, "刷新被拒：本地令牌清除"
    assert not oauth.authorization_status(cfg).authorized
    assert events == [("refresh_failed", "oauthfake", {"issuer": server.issuer, "reason": "invalid_grant"})]
    assert "AT-" not in json.dumps(events) and "RT-" not in json.dumps(events)

    # 授权服务器暂时不可用（5xx）：令牌保留，下次再试；旧令牌还没真过期时继续用它
    routes = tls_routes()
    routes.routes[("POST", "/mcp")] = unauthorized(routes.origin, hint=None)
    routes.route("POST", "/token", status=503)
    cfg2 = routes_server(routes)
    store = OAuthStore("oauthfake")
    import time as _time

    now = _time.time()
    from water_agent.mcp.oauth_store import StoredToken

    store.save(StoredToken(
        authorization_id="a1", issuer=routes.origin, resource=oauth.canonical_resource(cfg2), client_id="wa-preregistered",
        client_auth="none", token_endpoint=f"{routes.origin}/token", revocation_endpoint=None,
        access_token="still-valid-token-123", refresh_token="refresh-token-123", issued_at=now - 90, expires_at=now + 30,
        scope=None))
    monkeypatch.setattr(oauth, "_now", real_now)
    assert oauth.OAuthAuth(cfg2).authorization() == "Bearer still-valid-token-123"
    assert store.load() is not None
    monkeypatch.setattr(oauth, "_now", lambda: real_now() + 60.0)  # 现在真过期了
    with pytest.raises(MCPError) as down:
        oauth.OAuthAuth(cfg2).authorization()
    assert down.value.code != MCP_OAUTH_REQUIRED, "暂时不可用不是需要重新授权"
    assert store.load() is not None, "5xx 不清除令牌"


def test_a_401_triggers_one_refresh_and_a_retry(fake: Callable[..., Fake]) -> None:
    """令牌被 server 拒绝（401）：用 refresh token 换一次新令牌并重发被拒绝的那个请求；换不到就 MCP_OAUTH_REQUIRED。"""

    server = fake("oauth")
    cfg = config(server.url)
    authorize(cfg)
    store = OAuthStore("oauthfake")
    token = store.load()
    assert token is not None
    from dataclasses import replace

    store.save(replace(token, access_token="revoked-on-the-server-side"))
    client = MCPHttpClient(cfg)
    client.connect()  # initialize 先被 401 → 刷新 → 重发成功
    with mcp_request_scope(caller()):
        assert client.call_tool("echo_query", {"query": "x"})["ok"] is True
    client.close()
    grants = [e["grant_type"] for e in server.log() if e.get("event") == "token"]
    assert grants == ["authorization_code", "refresh_token"], "只刷新一次"
    refreshed = store.load()
    assert refreshed is not None and refreshed.access_token not in ("revoked-on-the-server-side", token.access_token)

    # 没有 refresh token：换不到，直接 MCP_OAUTH_REQUIRED
    store.save(replace(refreshed, access_token="bad", refresh_token=None))
    with pytest.raises(MCPError) as caught:
        MCPHttpClient(cfg).connect()
    assert caught.value.code == MCP_OAUTH_REQUIRED


def test_registration_is_bound_to_the_issuer(fake: Callable[..., Fake]) -> None:
    """规范 MUST（8）：动态注册的 client_id 按 issuer（与回调地址）存；对不上就作废、重新注册，不复用。"""

    server = fake("oauth", "--dynamic-registration")
    cfg = config(server.url)
    cfg = MCPServerConfig.model_validate({**cfg.model_dump(mode="json"), "oauth": {"dynamic_registration": True}})
    authorize(cfg)
    authorize(cfg)
    registrations = [e for e in server.log() if e.get("event") == "register"]
    assert len(registrations) == 1, "同一 issuer、同一回调地址：复用，不重复注册"
    store = OAuthStore("oauthfake")
    cached = store.load_client(server.issuer, REDIRECT)
    assert cached is not None and cached.client_id.startswith("dyn-")
    assert store.load_client("https://another-issuer.example", REDIRECT) is None, "issuer 变了：作废"
    assert store.load_client(server.issuer, "http://127.0.0.1:1/other") is None, "回调地址变了：作废"
    oauth.start_authorization(cfg, redirect_uri="http://127.0.0.1:18081/other")
    assert len([e for e in server.log() if e.get("event") == "register"]) == 2
    # 没有 registration_endpoint 又没有预注册：MCP_OAUTH_UNAVAILABLE
    plain = fake("oauth")
    with pytest.raises(MCPError) as caught:
        oauth.start_authorization(
            MCPServerConfig.model_validate({**config(plain.url).model_dump(mode="json"), "oauth": {"dynamic_registration": True}}),
            redirect_uri=REDIRECT,
        )
    assert caught.value.code == MCP_OAUTH_UNAVAILABLE and "registration_endpoint" in caught.value.message


def test_client_secret_comes_from_the_slot(fake: Callable[..., Fake]) -> None:
    server = fake("oauth")
    base = config(server.url)
    cfg = MCPServerConfig.model_validate({
        **base.model_dump(mode="json"),
        "credential_env": ["OAUTH_CLIENT_SECRET"],
        "oauth": {"client_id": "wa-preregistered", "client_secret_credential": "OAUTH_CLIENT_SECRET",
                  "token_endpoint_auth_method": "client_secret_basic"},
    })
    start = oauth.start_authorization(cfg, redirect_uri=REDIRECT)
    query = parse_qs(urlparse(oauth_browser(start.authorization_url)).query)
    with pytest.raises(MCPError) as caught:  # 槽位没填值
        oauth.complete_authorization(state=query["state"][0], code=query["code"][0], iss=query["iss"][0],
                                     redirect_uri=REDIRECT)
    assert caught.value.code == MCP_CREDENTIAL_MISSING
    write_slot("oauthfake", "OAUTH_CLIENT_SECRET", "wa-preregistered-secret-0001")
    assert authorize(cfg).authorized
    assert [e["client_auth"] for e in server.log() if e.get("event") == "token"] == ["basic"]
    assert "wa-preregistered-secret-0001" not in json.dumps(server.log())


def test_insufficient_scope_is_reported_and_never_upgraded(fake: Callable[..., Fake]) -> None:
    """规范 MUST（10）：403 insufficient_scope → 如实报出所需 scope，不自动发起升级授权。"""

    server = fake("insufficient_scope")
    cfg = config(server.url)
    authorize(cfg)
    client = MCPHttpClient(cfg)
    client.connect()
    with pytest.raises(MCPError) as caught, mcp_request_scope(caller()):
        client.call_tool("echo_query", {"query": "x"})
    client.close()
    assert caught.value.code == MCP_OAUTH_INSUFFICIENT_SCOPE
    assert "mcp:read mcp:write" in caught.value.message and "不自动升级" in caught.value.message
    assert [e["grant_type"] for e in server.log() if e.get("event") == "token"] == ["authorization_code"]
    assert len([e for e in server.log() if e.get("event") == "authorize"]) == 1, "没有新的授权请求"


def test_tokens_are_only_sent_to_the_configured_endpoint(fake: Callable[..., Fake]) -> None:
    """规范 MUST（6）：令牌只发给签发它的 issuer 对应的那个 MCP endpoint——URL 变了、issuer 不在白名单了就不发。"""

    server = fake("oauth")
    cfg = config(server.url)
    authorize(cfg)
    assert oauth.OAuthAuth(cfg).authorization() is not None
    moved = MCPServerConfig.model_validate({**cfg.model_dump(mode="json"), "url": f"https://127.0.0.1:{server.port}/other"})
    assert oauth.OAuthAuth(moved).authorization() is None
    assert oauth.authorization_status(moved).state == "resource_changed"
    other_issuer = config(server.url, allowed_issuers=["https://auth.other.example"])
    assert oauth.OAuthAuth(other_issuer).authorization() is None
    assert oauth.authorization_status(other_issuer).state == "issuer_not_allowed"


def test_revoke_calls_the_revocation_endpoint_once_and_clears_locally(fake: Callable[..., Fake]) -> None:
    server = fake("oauth")
    cfg = config(server.url)
    authorize(cfg)
    status = oauth.revoke_authorization(cfg)
    assert not status.authorized and OAuthStore("oauthfake").load() is None
    assert len([e for e in server.log() if e.get("event") == "revoke"]) == 1
    assert oauth.revoke_authorization(cfg).authorized is False, "幂等"


# ---------------------------------------------------------------------------
# 保管
# ---------------------------------------------------------------------------


def test_store_files_are_private_and_the_digest_ignores_token_values(tmp_path: Path) -> None:
    from water_agent.mcp.oauth_store import StoredToken

    store = OAuthStore("storetest")
    token = StoredToken(
        authorization_id="auth-1", issuer="https://a.example", resource="https://m.example/mcp",
        client_id="c", client_auth="none", token_endpoint="https://a.example/token", revocation_endpoint=None,
        access_token="access-secret-1", refresh_token="refresh-secret-1", issued_at=1.0, expires_at=100.0, scope="s",
    )
    store.save(token)
    path = tmp_path / "credentials" / "mcp" / "storetest" / "oauth.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "access-secret-1" not in repr(token) and "refresh-secret-1" not in repr(token)
    rotated = token.with_tokens(access_token="access-secret-2", refresh_token="refresh-secret-2", issued_at=2.0,
                                expires_at=200.0, scope=None)
    store.save(rotated)
    assert store.authorization_digest() == token.authorization_digest() != "none"
    assert "secret" not in store.authorization_digest()
    reauthorized = StoredToken(**{**token.__dict__, "authorization_id": "auth-2"})
    store.save(reauthorized)
    assert store.authorization_digest() != token.authorization_digest(), "重新授权换指纹"
    store.clear()
    assert store.authorization_digest() == "none"
    assert os.listdir(path.parent) == [] or "oauth.json" not in os.listdir(path.parent)
    with pytest.raises(ValueError):
        OAuthStore("../escape").save(token)
