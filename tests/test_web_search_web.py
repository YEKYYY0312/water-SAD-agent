"""P7-S12 Web：``MessageRequest.web_search``、只读状态接口、密钥槽位、"面板不能写地址 / provider / 开关"、勾选不落库。

真实 FastAPI 应用 + SQLite + 真实鉴权与 CSRF；会话工厂换成记录型替身（不连模型）。
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect

from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.storage.repository import RunRepository, WebSessionRepository
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

PASSWORD = "WebSearchWebPass2026!"
SECRET = "brave-secret-key-0123456789abcdef"


class _Session:
    def __init__(self, recorder: JsonTraceRecorder) -> None:
        self.recorder = recorder

    def start_trace(self, run_id: str, query: str) -> None:
        self.recorder.start(run_id=run_id, trace_id=f"trace-{run_id}", query=query)

    async def aask(self, query: str, *, run_id: str, **_: Any) -> dict[str, Any]:
        self.recorder.finish("succeeded")
        return {"status": "succeeded", "answer": "好的", "citations": [], "errors": [], "warnings": []}


class _RecordingFactory:
    supports_web_search = True

    def __init__(self, trace_dir: Path) -> None:
        self.trace_dir = trace_dir
        self.calls: list[dict[str, Any]] = []

    def create(self, *, run_id: str, thread_id: str, user_id: UUID, principal: Any, project_id: Any, stack: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _Session(JsonTraceRecorder(self.trace_dir))


class _PlainFactory(_RecordingFactory):
    """协议型替身：不声明 ``supports_web_search``（严格签名，不收 web_search）。"""

    supports_web_search = False

    def create(self, *, run_id: str, thread_id: str, user_id: UUID, principal: Any, project_id: Any, stack: Any) -> Any:  # type: ignore[override]
        self.calls.append({})
        return _Session(JsonTraceRecorder(self.trace_dir))


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    for name in ("WATER_AGENT_WEB_SEARCH", "WATER_AGENT_WEB_SEARCH_PROVIDER", "WATER_AGENT_WEB_SEARCH_BASE_URL",
                 "WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE", "WATER_AGENT_WEB_SEARCH_API_KEY", "WATER_AGENT_WEB_SEARCH_API_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'web.sqlite'}")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    state: dict[str, Any] = {"database": database, "tmp": tmp_path, "clients": []}
    yield state
    for client in state["clients"]:
        client.close()
    database.dispose()


def _app(world: dict[str, Any], factory: Any) -> Any:
    return create_app(database=world["database"], static_dir=world["tmp"], trace_dir=world["tmp"] / "traces", session_factory=factory)


def _login(world: dict[str, Any], app: Any, role: UserRole) -> tuple[TestClient, UUID]:
    user = PostgresUserStore(world["database"]).create_user(f"{role.value}-{uuid4().hex[:8]}", PASSWORD, role)
    client = TestClient(app, raise_server_exceptions=True)
    world["clients"].append(client)
    assert client.post("/api/auth/login", json={"username": user.username, "password": PASSWORD}).status_code == 200
    return client, user.user_id


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""}


def _send(world: dict[str, Any], client: TestClient, user_id: UUID, body: dict[str, Any]) -> tuple[Any, str]:
    thread = WebSessionRepository(world["database"]).create(user_id, title="ws").thread_id
    return client.post(f"/api/sessions/{thread}/messages", json=body, headers=_csrf(client)), thread


def _deploy(monkeypatch: pytest.MonkeyPatch, provider: str = "searxng", base_url: str = "https://search.example/") -> None:
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH", "on")
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH_PROVIDER", provider)
    monkeypatch.setenv("WATER_AGENT_WEB_SEARCH_BASE_URL", base_url)


def _wait(factory: _RecordingFactory) -> dict[str, Any] | None:
    deadline = time.monotonic() + 10
    while not factory.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    return factory.calls[-1] if factory.calls else None


def test_message_field_defaults_to_false_and_is_strictly_boolean(world: dict[str, Any]) -> None:
    """P7-S12-A11 ``MessageRequest.web_search`` 缺省 false：不传时工厂不收到 ``web_search``；字符串 ``"true"`` 一律 422。"""

    factory = _RecordingFactory(world["tmp"] / "traces")
    client, user_id = _login(world, _app(world, factory), UserRole.MEMBER)
    response, _ = _send(world, client, user_id, {"message": "你好"})
    assert response.status_code == 202
    assert "web_search" not in (_wait(factory) or {})
    bad, _ = _send(world, client, user_id, {"message": "你好", "web_search": "true"})
    assert bad.status_code == 422


def test_ticked_message_reaches_a_supporting_factory(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S12-A11 勾选 + 部署就绪 + 工厂声明 ``supports_web_search`` → 202，工厂收到 ``web_search=True``。"""

    _deploy(monkeypatch)
    factory = _RecordingFactory(world["tmp"] / "traces")
    client, user_id = _login(world, _app(world, factory), UserRole.MEMBER)
    response, _ = _send(world, client, user_id, {"message": "查一下", "web_search": True})
    assert response.status_code == 202
    assert (_wait(factory) or {}).get("web_search") is True


def test_ticked_message_is_refused_when_it_cannot_be_honoured(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S12-A11 勾选而工厂不支持 → 422 ``WEB_SEARCH_UNSUPPORTED``；勾选而部署 off → 422 ``WEB_SEARCH_UNAVAILABLE``；
    两种都不建 run（不静默忽略勾选）。"""

    plain = _PlainFactory(world["tmp"] / "traces")
    client, user_id = _login(world, _app(world, plain), UserRole.MEMBER)
    response, thread = _send(world, client, user_id, {"message": "查一下", "web_search": True})
    assert response.status_code == 422 and response.json()["error"]["code"] == "WEB_SEARCH_UNSUPPORTED"
    assert RunRepository(world["database"]).list_for_session(thread, user_id) == []

    factory = _RecordingFactory(world["tmp"] / "traces")
    client2, user2 = _login(world, _app(world, factory), UserRole.MEMBER)
    off, thread2 = _send(world, client2, user2, {"message": "查一下", "web_search": True})
    assert off.status_code == 422 and off.json()["error"]["code"] == "WEB_SEARCH_UNAVAILABLE"
    assert RunRepository(world["database"]).list_for_session(thread2, user2) == []
    assert factory.calls == []


def test_status_endpoint_is_read_only_and_shows_only_the_host(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S12-A11 ``GET /api/settings/web-search``：成员可读、只给主机名（不含路径 / 端口 / scheme）、``can_manage`` 只对管理员为真；
    brave 时报密钥槽位是否已配置，不给值。"""

    _deploy(monkeypatch, provider="brave", base_url="https://search.internal.example:8443/secret-path/")
    app = _app(world, _RecordingFactory(world["tmp"] / "traces"))
    member, _ = _login(world, app, UserRole.MEMBER)
    admin, _ = _login(world, app, UserRole.ADMIN)
    view = member.get("/api/settings/web-search").json()["data"]
    assert view["enabled"] is True and view["ready"] is False and view["detail"] == "api_key_missing"
    assert view["provider"] == "brave" and view["host"] == "search.internal.example"
    assert view["needs_key"] is True and view["key"]["configured"] is False and view["can_manage"] is False
    text = member.get("/api/settings/web-search").text
    assert "secret-path" not in text and "8443" not in text and "https://" not in text
    assert admin.get("/api/settings/web-search").json()["data"]["can_manage"] is True


def test_invalid_operator_config_is_reported_without_echoing_values(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S12-A11 运维配置不合法（provider 拼错）：只报稳定码，不回显取值。"""

    _deploy(monkeypatch, provider="searxn")
    member, _ = _login(world, _app(world, _RecordingFactory(world["tmp"] / "traces")), UserRole.MEMBER)
    response = member.get("/api/settings/web-search")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["error"]["code"] == "WEB_SEARCH_CONFIG_INVALID" and data["ready"] is False
    assert "searxn" not in response.text


def test_key_slot_is_admin_write_only_and_never_echoed(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S12-A11 密钥槽位复用凭据接口：成员写 403；管理员写 200，响应只报"已配置 / 来源"，永不回显值；写入后 brave 就绪。"""

    _deploy(monkeypatch, provider="brave")
    app = _app(world, _RecordingFactory(world["tmp"] / "traces"))
    member, _ = _login(world, app, UserRole.MEMBER)
    admin, _ = _login(world, app, UserRole.ADMIN)
    slot = "/api/settings/credentials/WATER_AGENT_WEB_SEARCH_API_KEY"
    assert member.put(slot, json={"value": SECRET}, headers=_csrf(member)).status_code == 403
    written = admin.put(slot, json={"value": SECRET}, headers=_csrf(admin))
    assert written.status_code == 200 and SECRET not in written.text
    assert written.json()["data"]["credential"]["configured"] is True
    view = member.get("/api/settings/web-search").json()["data"]
    assert view["ready"] is True and SECRET not in member.get("/api/settings/web-search").text


def test_the_panel_has_no_way_to_change_address_provider_or_switch(world: dict[str, Any]) -> None:
    """P7-S12-A11 地址 / provider / 开关是运维的：没有任何写接口（被盗的管理员账号不能把用户问题改道）；凭据白名单里也没有它们。"""

    from water_agent.config.credentials import CREDENTIALS

    app = _app(world, _RecordingFactory(world["tmp"] / "traces"))
    admin, _ = _login(world, app, UserRole.ADMIN)
    for method in ("put", "post", "patch"):
        response = getattr(admin, method)("/api/settings/web-search", json={"base_url": "https://evil.example/"}, headers=_csrf(admin))
        assert response.status_code in (404, 405), (method, response.status_code)
    for name in ("WATER_AGENT_WEB_SEARCH", "WATER_AGENT_WEB_SEARCH_PROVIDER", "WATER_AGENT_WEB_SEARCH_BASE_URL"):
        assert name not in CREDENTIALS
        response = admin.put(f"/api/settings/credentials/{name}", json={"value": "https://evil.example/"}, headers=_csrf(admin))
        assert response.status_code == 404
    write_routes = [
        (route.path, sorted(route.methods or ()))
        for route in app.routes
        if "web-search" in getattr(route, "path", "") and set(getattr(route, "methods", ()) or ()) - {"GET", "HEAD"}
    ]
    assert write_routes == []


def test_the_tick_is_never_persisted(world: dict[str, Any]) -> None:
    """P7-S12-A11 勾选是逐条消息的瞬时同意：``runs`` 与 ``web_sessions`` 表里没有任何联网相关列（与 S15 的模型选择刻意不同）。"""

    inspector = inspect(world["database"].engine)
    for table in ("runs", "web_sessions"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        assert not any("web" in name and "search" in name for name in columns), (table, columns)
