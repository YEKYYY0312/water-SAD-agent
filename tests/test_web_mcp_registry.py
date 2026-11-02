"""P7-S14 管理员目录接口（``webapp/mcp_registry_routes.py``）：管理员 + CSRF、请求体白名单、服务端重取、限额、审计、不泄漏。

SQLite 上的真实鉴权（``create_app(database=...)``，与 ``tests/test_web_mcp_admin.py`` 同法）；目录是进程内假目录
（``tests/fixtures/fake_mcp_registry.py``），条目地址指向 ``EntrySink``（只数连接与字节）。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from tests.fixtures.fake_mcp_registry import (
    ENV_SECRET,
    HEADER_SECRET,
    QUERY_SECRET,
    UNKNOWN_FIELD_SECRET,
    UPSTREAM_SECRET_BODY,
    EntrySink,
    FakeMcpRegistry,
)
from water_agent.mcp import registry as reg
from water_agent.runtime.governance.gateway import ProviderGateway
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.audit import _clear_events, _get_events
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

PASSWORD = "McpRegistryPass2026!"
SEARCH = "/api/admin/mcp/registry/search"
PROPOSAL = "/api/admin/mcp/registry/proposal"
STATUS = "/api/admin/mcp/registry/status"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp_registry.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    yield db
    db.dispose()


class Rig:
    """一个假目录 + 条目地址探针 + 已挂载的应用。``modes`` 可在用例内切换。"""

    def __init__(self, database: Database, tmp_path: Path, modes: str = "ok") -> None:
        self.sink = EntrySink().start()
        self.server = FakeMcpRegistry(modes, sink_url=self.sink.url).start()
        self.app = create_app(database=database, static_dir=tmp_path)
        self.set_config(reg.RegistryConfig(base_url=self.server.base_url, allow_private=True))
        self.database = database

    def set_config(self, config: reg.RegistryConfig) -> None:
        self.app.state.mcp_registry_overrides = {"config": config, "gateway": ProviderGateway()}

    def close(self) -> None:
        self.server.stop()
        self.sink.stop()


@pytest.fixture
def rig(database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    monkeypatch.delenv(reg.ENV_URL, raising=False)
    monkeypatch.delenv(reg.ENV_ALLOW_PRIVATE, raising=False)
    _clear_events()
    instance = Rig(database, tmp_path)
    yield instance
    assert instance.sink.contacts == 0, "任何接口都不得连接条目地址"
    assert instance.sink.bytes_received == 0
    instance.close()


def _login(app: Any, database: Database, role: UserRole, *, must_change: bool = False) -> TestClient:
    username = f"{role.value}-{uuid4().hex[:8]}"
    PostgresUserStore(database).create_user(username, PASSWORD, role, must_change_password=must_change)
    client = TestClient(app, raise_server_exceptions=True)
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def _headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {"X-CSRF-Token": token}


def _admin(rig: Rig) -> TestClient:
    return _login(rig.app, rig.database, UserRole.ADMIN)


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> Any:
    return client.post(url, json=payload, headers=_headers(client))


def _events(name: str) -> list[dict[str, Any]]:
    return [event for event in _get_events() if event["event"] == name]


def test_authorization_matrix(rig: Rig) -> None:
    """P7-S14-A06 全部接口 ``require_admin``：未登录读 401 / 匿名写 403（CSRF 先拦）、成员 403、临时密码管理员 403；写接口缺 CSRF 403（零请求发往目录）。"""

    anonymous = TestClient(rig.app)
    assert anonymous.get(STATUS).status_code == 401
    # 匿名写请求先被 CSRF 依赖拦成 403（与既有 /api/admin/mcp/* 写接口一致：CSRF 校验排在会话解析之前），仍到不了路由体
    for url in (SEARCH, PROPOSAL):
        assert anonymous.post(url, json={}).status_code == 403

    member = _login(rig.app, rig.database, UserRole.MEMBER)
    assert member.get(STATUS).status_code == 403
    assert member.get(STATUS).json()["error"]["code"] == "AUTH_FORBIDDEN"
    assert _post(member, SEARCH, {"query": ""}).status_code == 403
    assert _post(member, PROPOSAL, {"name": "weather-station", "version": "1.0.0"}).status_code == 403

    temporary = _login(rig.app, rig.database, UserRole.ADMIN, must_change=True)
    response = temporary.get(STATUS)
    assert response.status_code == 403 and response.json()["error"]["code"] == "AUTH_MUST_CHANGE_PASSWORD"

    admin = _admin(rig)
    for url, payload in ((SEARCH, {"query": ""}), (PROPOSAL, {"name": "weather-station", "version": "1.0.0"})):
        response = admin.post(url, json=payload)
        assert response.status_code == 403 and response.json()["error"]["code"] == "AUTH_CSRF_INVALID"
    assert rig.server.requests == [], "未通过鉴权 / CSRF 的请求一个都没到达目录"
    assert _events("mcp.registry_searched") == [] and _events("mcp.registry_proposed") == []


def test_status_reports_only_host_and_flags(rig: Rig) -> None:
    """P7-S14-A06 ``GET status``：是否配置 / 目录**主机名** / 是否放行私网 / 上限；不含完整 URL、路径、端口。"""

    admin = _admin(rig)
    data = admin.get(STATUS).json()["data"]
    assert data["configured"] is True and data["registry_host"] == "127.0.0.1" and data["allow_private_network"] is True
    assert data["limits"] == {"page_limit": 20, "max_pages": 5, "max_query_chars": 64}
    assert str(rig.server.port) not in json.dumps(data) and "http" not in json.dumps(data["registry_host"])

    rig.set_config(reg.RegistryConfig(base_url=""))
    data = admin.get(STATUS).json()["data"]
    assert data["configured"] is False and data["registry_host"] is None and data["allow_private_network"] is None


def test_unconfigured_returns_409_without_any_request(rig: Rig) -> None:
    """P7-S14-A06 未配置目录基址：两个 POST 都是 409 ``WEB_MCP_REGISTRY_NOT_CONFIGURED``，不发请求、不记审计。"""

    rig.set_config(reg.RegistryConfig(base_url=""))
    admin = _admin(rig)
    for url, payload in ((SEARCH, {"query": "x"}), (PROPOSAL, {"name": "weather-station", "version": "1.0.0"})):
        response = _post(admin, url, payload)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "WEB_MCP_REGISTRY_NOT_CONFIGURED"
    assert rig.server.requests == []
    assert _events("mcp.registry_searched") == []


def test_search_returns_plain_text_entries(rig: Rig) -> None:
    """P7-S14-A06 搜索：返回条目纯文本视图（含不可提案原因）、页数、``truncated``；响应不缓存；固定提示语在。"""

    admin = _admin(rig)
    response = _post(admin, SEARCH, {"query": ""})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert [item["name"] for item in data["entries"]] == ["weather-station", "pipe-calc", "local-cad", "legacy-sse"]
    assert data["count"] == 4 and data["pages"] == 1 and data["truncated"] is False
    assert data["registry_host"] == "127.0.0.1" and "不会连接条目里的任何地址" in data["notice"]
    by_name = {item["name"]: item for item in data["entries"]}
    assert by_name["weather-station"]["proposable"] is True
    assert by_name["local-cad"]["not_proposable_reason"] == "no_remote"
    assert _post(admin, SEARCH, {"query": "pipe"}).json()["data"]["count"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "x", "limit": 100},
        {"query": "x", "url": "https://evil.example/"},
        {"query": "x" * 129},
        {"query": "line1\nline2"},
        {"query": "bell\x07"},
        {"query": "zero\u200bwidth"},
        {"cursor": "<script>"},
        {"cursor": "x" * 129},
        {"query": 5},
    ],
)
def test_search_body_is_a_whitelist(rig: Rig, payload: dict[str, Any]) -> None:
    """P7-S14-A06 搜索请求体 ``extra=forbid``：多余字段 / 超长 / 控制字符 / 非法游标一律 422（统一信封，不回显提交值），零请求发往目录。"""

    admin = _admin(rig)
    response = _post(admin, SEARCH, payload)
    assert response.status_code == 422 and response.json()["error"]["code"] == "WEB_INVALID_REQUEST"
    assert rig.server.requests == []
    assert "evil.example" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "weather-station"},
        {"name": "weather-station", "version": "1.0.0", "url": "https://evil.example/mcp"},
        {"name": "weather-station", "version": "1.0.0", "remotes": [{"type": "streamable-http", "url": "https://evil.example/mcp"}]},
        {"name": "Weather Station", "version": "1.0.0"},
        {"name": "weather-station", "version": "latest"},
        {"name": "", "version": ""},
    ],
)
def test_proposal_body_is_exactly_name_and_version(rig: Rig, payload: dict[str, Any]) -> None:
    """P7-S14-A06 提案请求体只有 ``{name, version}``：客户端想回传条目内容（url / remotes / description……）一律 422。"""

    admin = _admin(rig)
    response = _post(admin, PROPOSAL, payload)
    assert response.status_code == 422 and response.json()["error"]["code"] == "WEB_INVALID_REQUEST"
    assert rig.server.requests == []
    assert "evil.example" not in response.text


def test_proposal_refetches_the_entry_from_the_directory(rig: Rig) -> None:
    """P7-S14-A06 服务端重取：提案由**目录当前返回的条目**生成。目录里换了地址，提案跟着换，哈希也变；客户端无法让提案指向别处。"""

    admin = _admin(rig)
    body = {"name": "weather-station", "version": "1.0.0"}
    first = _post(admin, PROPOSAL, body).json()["data"]["proposal"]
    assert "mcp.weather-station.example" in first["proposal_text"]
    assert len(rig.server.requests) == 1 and rig.server.requests[0]["query"] == "weather-station"

    rig.server.modes = frozenset({"ok", "weather_moved"})
    second = _post(admin, PROPOSAL, body).json()["data"]["proposal"]
    assert "moved.example.com" in second["proposal_text"] and "mcp.weather-station.example" not in second["proposal_text"]
    assert second["proposal_hash"] != first["proposal_hash"]


def test_proposal_payload_and_refusals(rig: Rig) -> None:
    """P7-S14-A06 提案响应：已停用、无 ``allow`` 的覆盖层文本 + 哈希 + 备注；目录里没有的 → 404；unsafe → 422；不可提案 → 422。"""

    admin = _admin(rig)
    data = _post(admin, PROPOSAL, {"name": "weather-station", "version": "1.0.0"}).json()["data"]
    proposal = data["proposal"]
    server = json.loads(proposal["proposal_text"])["servers"][0]
    assert server["disabled"] is True and "allow" not in server and server["transport"] == "streamable_http"
    assert len(proposal["proposal_hash"]) == 64 and proposal["server_id"] == "weather_station"

    missing = _post(admin, PROPOSAL, {"name": "weather-station", "version": "9.9.9"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "MCP_REGISTRY_ENTRY_NOT_FOUND"
    not_proposable = _post(admin, PROPOSAL, {"name": "local-cad", "version": "0.3.1"})
    assert not_proposable.status_code == 422 and not_proposable.json()["error"]["code"] == "MCP_REGISTRY_NOT_PROPOSABLE"

    rig.server.modes = frozenset({"ok", "injection_desc"})
    unsafe = _post(admin, PROPOSAL, {"name": "evil-injector", "version": "1.0.0"})
    assert unsafe.status_code == 422 and unsafe.json()["error"]["code"] == "MCP_REGISTRY_ENTRY_UNSAFE"
    assert "Ignore all previous" not in unsafe.text


def test_directory_failures_map_to_stable_codes_without_upstream_content(rig: Rig) -> None:
    """P7-S14-A06 目录 5xx / 401 / 429 / 坏响应 → 502 / 502 / 429 / 502 与稳定码；响应里没有上游正文 / 网址 / 密钥 / 回环地址。"""

    admin = _admin(rig)
    expected = {
        "status_503": (502, "MCP_REGISTRY_UNAVAILABLE"),
        "status_401": (502, "MCP_REGISTRY_AUTH"),
        "status_429": (429, "MCP_REGISTRY_RATE_LIMITED"),
        "junk": (502, "MCP_REGISTRY_BAD_RESPONSE"),
    }
    for mode, (status, code) in expected.items():
        rig.server.modes = frozenset({mode})
        response = _post(admin, SEARCH, {"query": ""})
        assert (response.status_code, response.json()["error"]["code"]) == (status, code), mode
        for marker in (UPSTREAM_SECRET_BODY, "SECRET", "upstream.internal", "sk-live", "127.0.0.1", "://"):
            assert marker not in response.text, (mode, marker)


def test_per_admin_rate_limit(rig: Rig) -> None:
    """P7-S14-A06 每管理员每分钟 10 次（搜索 + 提案合计）：第 11 次 429 ``WEB_MCP_REGISTRY_RATE_LIMITED``，且**不再发请求**；
    另一个管理员不受影响。"""

    first = _admin(rig)
    for _ in range(10):
        assert _post(first, SEARCH, {"query": ""}).status_code == 200
    before = len(rig.server.requests)
    response = _post(first, SEARCH, {"query": ""})
    assert response.status_code == 429 and response.json()["error"]["code"] == "WEB_MCP_REGISTRY_RATE_LIMITED"
    assert _post(first, PROPOSAL, {"name": "weather-station", "version": "1.0.0"}).status_code == 429
    assert len(rig.server.requests) == before
    second = _admin(rig)
    assert _post(second, SEARCH, {"query": ""}).status_code == 200


def test_audit_records_only_whitelisted_fields(rig: Rig) -> None:
    """P7-S14-A06 审计 ``mcp.registry_searched`` / ``mcp.registry_proposed``：只记管理员 id、目录主机名、``name@version``、结果码、
    条数 / 页数 / 提案哈希——**不记查询原文、不记条目网址、不记提案文本**。"""

    admin = _admin(rig)
    _post(admin, SEARCH, {"query": "my-secret-query-text"})
    _post(admin, PROPOSAL, {"name": "weather-station", "version": "1.0.0"})
    searched = _events("mcp.registry_searched")[0]
    proposed = _events("mcp.registry_proposed")[0]
    assert set(searched) == {"event", "ts", "actor_user_id", "registry_host", "result", "count", "pages"}
    assert searched["registry_host"] == "127.0.0.1" and searched["result"] == "ok" and searched["pages"] == 1
    assert set(proposed) == {"event", "ts", "actor_user_id", "registry_host", "name", "version", "result", "proposal_hash"}
    assert (proposed["name"], proposed["version"], proposed["result"]) == ("weather-station", "1.0.0", "ok")
    assert len(proposed["proposal_hash"]) == 64
    everything = json.dumps(_get_events(), ensure_ascii=False)
    for forbidden in ("my-secret-query-text", "mcp.weather-station.example", "streamable_http", "https://", str(rig.server.port)):
        assert forbidden not in everything, forbidden


def test_failures_and_refusals_are_audited_with_the_code(rig: Rig) -> None:
    """P7-S14-A06 失败也留一条审计（结果码），同样不含内容。"""

    admin = _admin(rig)
    rig.server.modes = frozenset({"status_503"})
    _post(admin, SEARCH, {"query": ""})
    rig.server.modes = frozenset({"ok"})
    _post(admin, PROPOSAL, {"name": "local-cad", "version": "0.3.1"})
    assert _events("mcp.registry_searched")[0]["result"] == "MCP_REGISTRY_UNAVAILABLE"
    refused = _events("mcp.registry_proposed")[0]
    assert refused["result"] == "MCP_REGISTRY_NOT_PROPOSABLE" and refused["proposal_hash"] == ""


def test_no_secret_from_a_hostile_entry_reaches_any_response_or_audit(rig: Rig) -> None:
    """P7-S14-A06 带令牌的敌意条目（查询串 / 请求头 / env / 未知字段）：搜索、提案的响应与审计里都没有这些值。"""

    rig.server.modes = frozenset({"ok", "query_secret_url", "header_secret", "unknown_fields"})
    admin = _admin(rig)
    blobs = [_post(admin, SEARCH, {"query": ""}).text]
    for name in ("query-secret", "hdr-secret", "extras"):
        blobs.append(_post(admin, PROPOSAL, {"name": name, "version": "1.0.0"}).text)
    blobs.append(json.dumps(_get_events(), ensure_ascii=False))
    text = "\n".join(blobs)
    for secret in (QUERY_SECRET, HEADER_SECRET, ENV_SECRET, UNKNOWN_FIELD_SECRET):
        assert secret not in text, secret


def test_the_directory_routes_never_write_the_overlay_or_web_layer(rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S14-A04 提案阶段不写覆盖层 / Web 管理层：三层配置路径指向临时目录，调用前后目录内容逐字节相同。"""

    watched = tmp_path / "mcp-config"
    watched.mkdir()
    (watched / "overlay.json").write_text('{"servers": []}', encoding="utf-8")
    monkeypatch.setenv("WATER_AGENT_MCP_OVERLAY", str(watched / "overlay.json"))
    monkeypatch.setenv("WATER_AGENT_MCP_WEB_LAYER", str(watched / "web_layer.json"))
    before = {path.name: path.read_bytes() for path in watched.iterdir()}
    admin = _admin(rig)
    _post(admin, SEARCH, {"query": ""})
    _post(admin, PROPOSAL, {"name": "weather-station", "version": "1.0.0"})
    assert {path.name: path.read_bytes() for path in watched.iterdir()} == before
    assert not (watched / "web_layer.json").exists()


def test_admin_limiter_bounds_its_own_memory() -> None:
    """P7-S14-A06 限流表有上限：跟踪满 1024 个管理员后，新管理员 fail closed 到"限流"（不是无界增长）；窗口过期后条目被回收。"""

    from water_agent.webapp.mcp_registry_routes import _MAX_TRACKED_ADMINS, AdminLimiter

    now = [0.0]
    limiter = AdminLimiter(limit=1, clock=lambda: now[0])
    for index in range(_MAX_TRACKED_ADMINS):
        assert limiter.allow(f"admin-{index}") is True
    assert limiter.allow("one-too-many") is False
    now[0] = 61.0
    assert limiter.allow("one-too-many") is True
