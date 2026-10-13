"""设置面板的对话模型端点（P7-S10-A08，``docs/design/16-p7-plugin-mcp.md`` §7 S10）。

SQLite 上的真实鉴权（``create_app(database=...)``）；兼容性检查对接进程内的假 OpenAI 兼容端点
（``tests/fixtures/endpoint_compat/fake_openai_server.py``，明文回环），不连真实服务商、不计费。
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from water_agent.config.credentials import CREDENTIALS, forget_injected_credentials
from water_agent.generation import compatibility as compat
from water_agent.generation import endpoints as ep
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.storage.repository import WebSessionRepository
from water_agent.webapp import settings_routes
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.audit import _clear_events, _get_events
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = REPO_ROOT / "tests" / "fixtures" / "endpoint_compat" / "fake_openai_server.py"
PASSWORD = "EndpointPanelPass2026!"
KEY = "sk-panel-endpoint-key-0001"


def _fake() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fake_openai_server", FAKE_SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = _fake()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    engine = create_engine(f"sqlite:///{tmp_path / 'endpoint.sqlite'}")
    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    yield db
    db.dispose()


@pytest.fixture
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in (
        ep.CHAT_BASE_URL_ENV,
        ep.CHAT_MODEL_ENV,
        compat.COMPATIBILITY_REPORT_ENV,
        "LOCAL_MODEL_ENABLED",
        "WATER_AGENT_RETRIEVAL_MODE",
        "WATER_AGENT_INDEX_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    directory = tmp_path / "credentials"
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(directory))
    monkeypatch.setattr("water_agent.retrieval.trivium.DEFAULT_INDEX_DIR", tmp_path / "no-indexes")
    forget_injected_credentials()
    _clear_events()
    yield directory
    settings_routes._CHECK.join(10)
    settings_routes._CHECK.last = None
    # 面板经 write_credential 用 os.environ[...] 直接注入有效值，monkeypatch 追踪不到（同
    # test_web_settings_api.py 的 isolated_credentials）；不清掉就串到后面的测试里
    for name in CREDENTIALS:
        os.environ.pop(name, None)
    forget_injected_credentials()


@pytest.fixture
def app(database: Database, tmp_path: Path, credentials: Path) -> Any:
    return create_app(database=database, static_dir=tmp_path, trace_dir=tmp_path / "traces")


def _login(app: Any, database: Database, role: UserRole) -> TestClient:
    username = f"{role.value}-{uuid4().hex[:8]}"
    PostgresUserStore(database).create_user(username, PASSWORD, role)
    client = TestClient(app, raise_server_exceptions=True)
    assert (
        client.post(
            "/api/auth/login", json={"username": username, "password": PASSWORD}
        ).status_code
        == 200
    )
    return client


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""}


def _put(client: TestClient, base_url: str, model: str = "other-model") -> Any:
    return client.put(
        "/api/settings/llm-endpoint",
        json={"base_url": base_url, "model": model},
        headers=_headers(client),
    )


def _get(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/settings/llm-endpoint")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_endpoint_settings_are_admin_only_and_validated(
    app: Any, database: Database, credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S10-A08 成员只读（只有主机名与模型名，没有完整 URL，不能改）；管理员 + CSRF 才能写，只收 https
    （``http://127.0.0.1`` 等本机例外），拒绝 userinfo / fragment / 非法模型名且不回显值；写进凭据卷旁的
    ``llm_endpoint.json``（0600）后解析来源变为 ``panel``；运维 env 设了或本地模型启用时面板只读；专用 Key
    槽位只写不读；审计只记主机名、模型名，不含 Key 与完整 URL。"""

    member = _login(app, database, UserRole.MEMBER)
    admin = _login(app, database, UserRole.ADMIN)

    view = _get(member)
    assert view["endpoint"] == {
        "host": "api.deepseek.com",
        "model": "deepseek-chat",
        "source": "default",
        "is_default": True,
    }
    assert view["can_manage"] is False and view["editable"] is False
    assert view["compatibility"]["status"] == "default_exempt"
    assert _put(member, "https://llm.other-provider.example/v1").status_code == 403
    assert (
        admin.put(
            "/api/settings/llm-endpoint",
            json={"base_url": "https://llm.other-provider.example/v1", "model": "m"},
        ).status_code
        == 403
    ), "没有 CSRF 令牌"

    for bad in (
        "http://llm.other-provider.example/v1",
        "https://user:secret@llm.other-provider.example/v1",
        "https://llm.other-provider.example/v1#frag",
        "ftp://llm.other-provider.example/v1",
    ):
        response = _put(admin, bad)
        assert response.status_code == 422, bad
        assert response.json()["error"]["code"] == "WEB_LLM_ENDPOINT_INVALID"
        assert (
            "secret" not in response.text
            and "other-provider" not in response.json()["error"]["message"]
        )
    assert (
        _put(admin, "https://llm.other-provider.example/v1", model="bad model").status_code == 422
    )
    assert (
        admin.put(
            "/api/settings/llm-endpoint",
            json={"base_url": "https://x.example/v1", "model": "m", "api_key": "sk-x"},
            headers=_headers(admin),
        ).status_code
        == 422
    ), "请求体不收别的字段（Key 走凭据槽位）"

    saved = _put(admin, "https://llm.other-provider.example/v1/")
    assert saved.status_code == 200, saved.text
    data = saved.json()["data"]
    assert data["endpoint"]["base_url"] == "https://llm.other-provider.example/v1"
    assert data["endpoint"]["source"] == "panel" and data["endpoint"]["is_default"] is False
    assert data["compatibility"]["status"] == "unverified"
    assert data["api_key"] == {"name": "WATER_AGENT_LLM_API_KEY", "configured": False}
    stored = credentials / "llm_endpoint.json"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert json.loads(stored.read_text(encoding="utf-8")) == {
        "base_url": "https://llm.other-provider.example/v1",
        "model": "other-model",
    }
    assert ep.resolve_chat_endpoint().source == "panel"
    assert "base_url" not in _get(member)["endpoint"]
    assert _put(admin, "http://127.0.0.1:11434/v1", model="qwen3:8b").status_code == 200

    written = admin.put(
        "/api/settings/credentials/WATER_AGENT_LLM_API_KEY",
        json={"value": KEY},
        headers=_headers(admin),
    )
    assert written.status_code == 200 and KEY not in written.text
    assert _get(admin)["api_key"]["configured"] is True
    assert KEY not in admin.get("/api/settings/llm-endpoint").text
    assert KEY not in admin.get("/api/settings/credentials").text

    events = [
        event for event in _get_events() if event["event"] == "settings.llm_endpoint_updated"
    ]
    assert [event["host"] for event in events] == [
        "llm.other-provider.example",
        "127.0.0.1",
    ]
    assert all(
        set(event) == {"event", "ts", "actor_user_id", "host", "model", "changed"} for event in events
    )
    assert KEY not in json.dumps(_get_events(), ensure_ascii=False)

    monkeypatch.setenv(ep.CHAT_BASE_URL_ENV, "https://llm.deployment.example/v1")
    view = _get(admin)
    assert view["editable"] is False and view["locked_reason"] == "deployment"
    assert view["endpoint"]["source"] == "env"
    managed = _put(admin, "https://llm.other-provider.example/v1")
    assert (
        managed.status_code == 409 and managed.json()["error"]["code"] == "WEB_LLM_ENDPOINT_MANAGED"
    )
    monkeypatch.delenv(ep.CHAT_BASE_URL_ENV)
    monkeypatch.setenv("LOCAL_MODEL_ENABLED", "true")
    assert _get(admin)["locked_reason"] == "local_model"
    assert _put(admin, "https://llm.other-provider.example/v1").status_code == 409


def test_changing_endpoint_invalidates_compatibility(
    app: Any, database: Database, credentials: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P7-S10-A08 改了端点或模型就作废旧兼容报告（面板写的报告被删除，状态回到未验证、提问 424
    ``WEB_MODEL_NOT_VERIFIED``）；"运行兼容性检查"（管理员 + CSRF，202，后台跑，同一时间只一个）对端点跑完
    七项后状态变为已验证、报告写进凭据卷，审计只记主机名、模型名与结果码。"""

    admin = _login(app, database, UserRole.ADMIN)
    member = _login(app, database, UserRole.MEMBER)
    with FAKE.FakeOpenAIServer(api_key=KEY, models=["fake-model", "other-model"]) as server:
        assert _put(admin, server.base_url, model="fake-model").status_code == 200
        assert (
            admin.put(
                "/api/settings/credentials/WATER_AGENT_LLM_API_KEY",
                json={"value": KEY},
                headers=_headers(admin),
            ).status_code
            == 200
        )
        assert (
            member.post(
                "/api/settings/llm-endpoint/compatibility", headers=_headers(member)
            ).status_code
            == 403
        )

        started = admin.post("/api/settings/llm-endpoint/compatibility", headers=_headers(admin))
        assert started.status_code == 202, started.text
        deadline = time.monotonic() + 30
        while _get(admin)["check"]["running"] and time.monotonic() < deadline:
            time.sleep(0.05)
        view = _get(admin)
        assert view["check"]["last"]["status"] == "compatible", view["check"]
        assert view["compatibility"]["status"] == "verified"
        assert view["compatibility"]["report"]["checks"] == {
            name: "passed" for name in compat.ENDPOINT_COMPATIBILITY_CHECKS
        }
        report_file = credentials / compat.PANEL_REPORT_FILE
        assert report_file.exists() and KEY not in report_file.read_text(encoding="utf-8")
        checked = [
            e for e in _get_events() if e["event"] == "settings.llm_compatibility_checked"
        ]
        assert [e["result"] for e in checked] == ["compatible"]
        assert (
            checked[0]["host"] == "127.0.0.1"
            and checked[0]["model"] == "fake-model"
        )

        assert _put(admin, server.base_url, model="other-model").status_code == 200
        assert not report_file.exists()
        assert _get(admin)["compatibility"]["status"] == "unverified"

        user = PostgresUserStore(database).create_user(
            f"asker-{uuid4().hex[:6]}", PASSWORD, UserRole.MEMBER
        )
        thread_id = WebSessionRepository(database).create(user.user_id, title="t").thread_id
        asker = TestClient(app)
        assert (
            asker.post(
                "/api/auth/login", json={"username": user.username, "password": PASSWORD}
            ).status_code
            == 200
        )
        refused = asker.post(
            f"/api/sessions/{thread_id}/messages", json={"message": "问题"}, headers=_headers(asker)
        )
        assert (
            refused.status_code == 424
            and refused.json()["error"]["code"] == "WEB_MODEL_NOT_VERIFIED"
        )

        gate = settings_routes._CHECK
        release = threading.Event()
        assert gate.start(lambda: (release.wait(5), {"status": "compatible"})[1])
        try:
            busy = admin.post("/api/settings/llm-endpoint/compatibility", headers=_headers(admin))
            assert (
                busy.status_code == 409
                and busy.json()["error"]["code"] == "WEB_LLM_COMPATIBILITY_RUNNING"
            )
            assert _put(admin, server.base_url, model="fake-model").status_code == 409
        finally:
            release.set()
            gate.join(5)
