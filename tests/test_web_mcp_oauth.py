"""P7-S8 Step 7b：OAuth 授权的 Web 路由（``docs/design/16-p7-plugin-mcp.md`` §7 P7-S8-A08）。

SQLite 上的真实鉴权 + 假授权服务器（自签 TLS，授权端点不需要浏览器：测试代管理员的浏览器访问它，再带着回调参数
回到我们的回调路由）。验证：全部路由 ``require_admin``、写接口 CSRF、回调只认服务端 state、
``WEB_PUBLIC_BASE_URL`` 未配不提供授权、审计与响应里没有令牌 / 授权码 / verifier / state。
"""
from __future__ import annotations

import json
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from tests.test_web_mcp_admin import (  # noqa: F401 — pytest 夹具与辅助函数
    PASSWORD,
    _headers,
    _login,
    database,
    factory,
)
from water_agent.harness.mcp_http_eval import browser_authorize
from water_agent.mcp import oauth
from water_agent.mcp.config import MCP_CONFIG_VERSION
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.audit import _clear_events, _get_events
from water_agent.webapp.auth.models import UserRole

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_http_server.py"
BASE = "https://agent.example.com"


@pytest.fixture(scope="module")
def cert_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("web-oauth-tls") / "tls"
    subprocess.run([sys.executable, str(FAKE), "--make-cert", str(directory)], check=True, capture_output=True)
    return directory


class Running:
    def __init__(self, tmp: Path, cert_dir: Path, mode: str = "oauth") -> None:
        self.tmp = tmp
        self.proc = subprocess.Popen(
            [sys.executable, str(FAKE), "--mode", mode, "--cert-dir", str(cert_dir), "--log", str(tmp / "log.jsonl"),
             "--secrets", str(tmp / "secrets.json")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        ports = json.loads(self.proc.stdout.readline())  # type: ignore[union-attr]
        self.port = ports["mcp_port"]

    def secrets(self) -> list[str]:
        path = self.tmp / "secrets.json"
        return json.loads(path.read_text()) if path.exists() else []

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
def layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cert_dir: Path) -> Iterator[SimpleNamespace]:
    from water_agent.config.credentials import forget_injected_credentials
    from water_agent.runtime.governance.inventory import clear_mcp_tool_bindings

    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    fake = Running(fake_dir, cert_dir)
    base = tmp_path / "repo" / "mcp_servers.json"
    base.parent.mkdir(parents=True)
    base.write_text(json.dumps({"version": MCP_CONFIG_VERSION, "servers": []}))
    server: dict[str, Any] = {
        "server_id": "adm_oauth",
        "transport": "streamable_http",
        "url": f"https://127.0.0.1:{fake.port}/mcp",
        "allow_private_network": True,
        "allow": ["echo_query"],
        "oauth": {"client_id": "wa-preregistered"},
        "call_timeout_seconds": 3.0,
    }
    plain = {"server_id": "adm_plain", "transport": "streamable_http", "url": f"https://127.0.0.1:{fake.port}/mcp",
             "allow_private_network": True, "allow": ["echo_query"]}
    off = {**server, "server_id": "adm_off", "disabled": True}
    overlay = tmp_path / "deploy" / "overlay.json"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(json.dumps({"servers": [server, plain, off]}))
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(base))
    monkeypatch.setenv("WATER_AGENT_MCP_OVERLAY", str(overlay))
    monkeypatch.setenv("WATER_AGENT_MCP_WEB_LAYER", str(tmp_path / "mcp-admin" / "web_layer.json"))
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_dir / "cert.pem"))
    monkeypatch.setenv("WEB_PUBLIC_BASE_URL", BASE)
    for key in ("WATER_AGENT_TOOL_EXPOSURE", "DRAWING_ROOT", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY",
                "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "SSL_CERT_DIR"):
        monkeypatch.delenv(key, raising=False)
    forget_injected_credentials()
    clear_mcp_tool_bindings()
    oauth.PENDING.clear()
    _clear_events()
    yield SimpleNamespace(fake=fake, tmp=tmp_path, overlay=overlay)
    clear_mcp_tool_bindings()
    forget_injected_credentials()
    oauth.PENDING.clear()
    oauth.set_event_sink(None)
    fake.stop()


@pytest.fixture
def app(database: Any, tmp_path: Path, factory: Any, layers: SimpleNamespace) -> Any:  # noqa: F811
    return create_app(database=database, static_dir=tmp_path, session_factory=factory)


def start_url(client: TestClient, server_id: str = "adm_oauth") -> Any:
    return client.post(f"/api/admin/mcp/servers/{server_id}/oauth/start", headers=_headers(client))


def browse(url: str) -> dict[str, str]:
    query = parse_qs(urlparse(browser_authorize(url)).query)
    return {key: values[0] for key, values in query.items()}


def callback(client: TestClient, params: dict[str, str]) -> Any:
    return client.get("/api/admin/mcp/oauth/callback", params=params, follow_redirects=False)


def test_oauth_round_trip_is_admin_only(app: Any, database: Any, layers: SimpleNamespace) -> None:  # noqa: F811
    """P7-S8-A08：授权 / 回调 / 撤销全部 require_admin（匿名 401、成员 403、临时密码管理员 403），写接口缺 CSRF 403；
    管理员走完一圈：发起 → 浏览器 → 回调 303 → 已授权 → 撤销；回调只认服务端 state（一次性）；审计不含秘密。"""

    anonymous = TestClient(app)
    for method, url in (
        ("post", "/api/admin/mcp/servers/adm_oauth/oauth/start"),
        ("get", "/api/admin/mcp/oauth/callback?state=x&code=y"),
        ("delete", "/api/admin/mcp/servers/adm_oauth/oauth"),
    ):
        # 写接口的 CSRF 依赖先于鉴权求值：匿名写请求得到 403 或 401，总之不是 2xx / 3xx
        expected = {401} if method == "get" else {401, 403}
        assert getattr(anonymous, method)(url, follow_redirects=False).status_code in expected, url
    member = _login(app, database, UserRole.MEMBER)
    assert start_url(member).status_code == 403
    assert callback(member, {"state": "x", "code": "y"}).status_code == 403
    assert member.delete("/api/admin/mcp/servers/adm_oauth/oauth", headers=_headers(member)).status_code == 403
    temporary = _login(app, database, UserRole.ADMIN, must_change=True)
    assert start_url(temporary).status_code == 403

    admin = _login(app, database, UserRole.ADMIN)
    for method, url in (("post", "/api/admin/mcp/servers/adm_oauth/oauth/start"),
                        ("delete", "/api/admin/mcp/servers/adm_oauth/oauth")):
        response = getattr(admin, method)(url)
        assert response.status_code == 403 and response.json()["error"]["code"] == "AUTH_CSRF_INVALID"

    # 发起：返回授权 URL 与"所有用户共用该授权身份"的提示
    response = start_url(admin)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "所有用户都会以该授权身份调用此 server" in data["notice"]
    params = browse(data["authorization_url"])
    assert params["state"] and params["code"] and params["iss"]

    # 匿名 / 成员带着合法回调参数来：拒绝，且没有消耗服务端 state
    assert callback(anonymous, params).status_code == 401
    assert callback(member, params).status_code == 403
    assert len(oauth.PENDING) == 1

    # 管理员的回调：303 回应用首页；查询串里只有结果与服务器 id
    response = callback(admin, params)
    assert response.status_code == 303
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}" == BASE
    assert parse_qs(location.query) == {"mcp_oauth": ["authorized"], "server": ["adm_oauth"]}
    assert response.headers["cache-control"] == "no-store"
    assert len(oauth.PENDING) == 0

    # state 一次性：同一组参数再来一次 → failed，且不带任何参数回显
    again = callback(admin, params)
    assert again.status_code == 303
    assert parse_qs(urlparse(again.headers["location"]).query) == {"mcp_oauth": ["failed"], "code": ["MCP_OAUTH_REQUIRED"]}
    forged = callback(admin, {"state": "forged-state", "code": "whatever"})
    assert parse_qs(urlparse(forged.headers["location"]).query)["mcp_oauth"] == ["failed"]
    assert "forged-state" not in forged.headers["location"]

    # 撤销
    response = admin.delete("/api/admin/mcp/servers/adm_oauth/oauth", headers=_headers(admin))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["oauth"]["authorized"] is False
    assert [e for e in layers.fake.log() if e.get("event") == "revoke"]

    events = {e["event"]: e for e in _get_events() if str(e["event"]).startswith("mcp.oauth")}
    assert set(events) >= {"mcp.oauth_authorized", "mcp.oauth_revoked", "mcp.oauth_failed"}
    assert events["mcp.oauth_authorized"]["server_id"] == "adm_oauth"
    assert events["mcp.oauth_authorized"]["scope"] == "mcp:read"

    # 范围外的 server：不存在 404；没配 OAuth / 已停用 409
    assert start_url(admin, "nope_server").status_code == 404
    plain = start_url(admin, "adm_plain")
    assert plain.status_code == 409 and plain.json()["error"]["code"] == "WEB_MCP_OAUTH_UNAVAILABLE"
    off = start_url(admin, "adm_off")
    assert off.status_code == 409 and off.json()["error"]["code"] == "WEB_MCP_SERVER_DISABLED"
    assert admin.delete("/api/admin/mcp/servers/adm_plain/oauth", headers=_headers(admin)).status_code == 404


@pytest.mark.parametrize(
    "value",
    ["", "http://agent.example.com", "ftp://agent.example.com", "https://u:p@agent.example.com", "https://agent.example.com/?x=1",
     "https://agent.example.com/#frag"],
)
def test_authorization_is_unavailable_without_a_valid_public_base_url(
    app: Any, database: Any, layers: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, value: str  # noqa: F811
) -> None:
    """P7-S8-A08：WEB_PUBLIC_BASE_URL 未配 / 不合法就不提供授权——不从请求的 Host 头推导回调地址。"""

    monkeypatch.setenv("WEB_PUBLIC_BASE_URL", value)
    admin = _login(app, database, UserRole.ADMIN)
    response = admin.post(
        "/api/admin/mcp/servers/adm_oauth/oauth/start",
        headers={**_headers(admin), "Host": "attacker.example.com", "X-Forwarded-Host": "attacker.example.com"},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "WEB_MCP_OAUTH_UNAVAILABLE"
    assert "WEB_PUBLIC_BASE_URL" in response.json()["error"]["message"]
    assert "attacker" not in response.text
    assert len(oauth.PENDING) == 0
    assert callback(admin, {"state": "x", "code": "y"}).status_code == 409


def test_a_localhost_base_url_is_allowed_and_the_redirect_uri_comes_only_from_it(
    app: Any, database: Any, layers: SimpleNamespace, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setenv("WEB_PUBLIC_BASE_URL", "http://localhost:8000/")
    admin = _login(app, database, UserRole.ADMIN)
    response = start_url(admin)
    assert response.status_code == 200, response.text
    query = parse_qs(urlparse(response.json()["data"]["authorization_url"]).query)
    assert query["redirect_uri"] == ["http://localhost:8000/api/admin/mcp/oauth/callback"]


def test_tokens_never_leave_the_credential_volume(app: Any, database: Any, layers: SimpleNamespace) -> None:  # noqa: F811
    """P7-S8-A08：令牌只存凭据卷（0600、目录 0700）；响应、审计事件、Web 管理层文件、部署覆盖层里都没有
    令牌 / 授权码 / verifier / state 原文；刷新被拒绝的事件也只含 server_id / issuer / 原因码。"""

    admin = _login(app, database, UserRole.ADMIN)
    start = start_url(admin)
    # 发起授权的响应里有授权 URL——state 本来就要随它交给管理员的浏览器；但 verifier、授权码、令牌不会在里面
    url_body = start.text
    bodies: list[str] = []
    params = browse(start.json()["data"]["authorization_url"])
    response = callback(admin, params)
    bodies.append(response.headers["location"])
    bodies.append(admin.get("/api/admin/mcp/servers").text)
    revoke_probe = admin.post("/api/admin/mcp/servers/adm_oauth/probe", headers=_headers(admin))
    bodies.append(revoke_probe.text)

    token_file = layers.tmp / "credentials" / "mcp" / "adm_oauth" / "oauth.json"
    assert token_file.is_file()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700

    stored = json.loads(token_file.read_text())
    secrets = [*layers.fake.secrets(), stored["access_token"], stored["refresh_token"]]
    assert len(secrets) >= 4 and any(item.startswith("AT-") for item in secrets)

    oauth._emit("refresh_failed", "adm_oauth", issuer="https://auth.example", reason="invalid_grant")
    events = json.dumps(_get_events(), ensure_ascii=False)
    assert "mcp.oauth_refresh_failed" in events and "invalid_grant" in events
    bodies.append(events)

    outside_volume = [
        path for path in layers.tmp.rglob("*")
        if path.is_file() and "credentials" not in path.parts and "fake" not in path.parts
    ]
    for path in outside_volume:
        bodies.append(path.read_text(errors="ignore"))
    for secret in secrets:
        for body in bodies:
            assert secret not in body, (secret[:6], body[:80])
        if secret != params["state"]:
            assert secret not in url_body, secret[:6]
    # 令牌不进任何 URL 查询串，也不进 trace 目录之外的日志
    assert not any("access_token=" in e.get("path", "") for e in layers.fake.log())
