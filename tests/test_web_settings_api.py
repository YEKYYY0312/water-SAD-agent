"""设置路由：LLM 凭据的状态查询与写入。

最重要的一组断言是「密钥只进不出」：无论 GET、PUT 的回执还是 /api/auth/me，
响应全文都不得出现明文，也不得出现末尾几位（末几位配上供应商固定前缀会缩小
暴力搜索范围）。
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from water_agent.config.credentials import (
    CREDENTIALS,
    CREDENTIALS_DIR_ENV,
    forget_injected_credentials,
)
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

PASSWORD = "SettingsPass2026!"
SECRET_VALUE = "sk-settings-panel-secret-value"
DEEPSEEK = "DEEPSEEK_API_KEY"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'settings.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    yield database
    database.dispose()


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """独立凭据卷 + 清掉密钥环境变量，并保证测试之间不互相污染。"""
    monkeypatch.setenv(CREDENTIALS_DIR_ENV, str(tmp_path / "credentials"))
    for env_name in CREDENTIALS:
        monkeypatch.delenv(env_name, raising=False)
        monkeypatch.delenv(f"{env_name}_FILE", raising=False)
    monkeypatch.delenv("LOCAL_MODEL_ENABLED", raising=False)
    forget_injected_credentials()
    yield
    # 路由经 write_credential 用 os.environ[...] 直接赋值，monkeypatch 追踪不到；
    # 注入记录是模块级状态，同样要清，否则来源判定会串味。
    for env_name in CREDENTIALS:
        os.environ.pop(env_name, None)
    forget_injected_credentials()


def _client(database: Database, tmp_path: Path, role: UserRole) -> TestClient:
    username = f"{role.value}-{uuid4()}"
    PostgresUserStore(database).create_user(username, PASSWORD, role)
    client = TestClient(
        create_app(database=database, static_dir=tmp_path), raise_server_exceptions=True
    )
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def _csrf(client: TestClient) -> str:
    value = client.cookies.get(CSRF_COOKIE_NAME)
    assert value
    return value


def _put(client: TestClient, name: str, value: str):
    return client.put(
        f"/api/settings/credentials/{name}",
        json={"value": value},
        headers={"X-CSRF-Token": _csrf(client)},
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def test_listing_reports_every_managed_credential_as_unconfigured(
    database: Database, tmp_path: Path
) -> None:
    client = _client(database, tmp_path, UserRole.ADMIN)

    response = client.get("/api/settings/credentials")

    assert response.status_code == 200, response.text
    rows = response.json()["data"]["credentials"]
    assert [row["name"] for row in rows] == list(CREDENTIALS)
    assert all(row["configured"] is False for row in rows)
    assert all(row["source"] == "none" for row in rows)


def test_members_may_read_the_listing(database: Database, tmp_path: Path) -> None:
    """成员也该看到「模型没配」，否则只会撞上 424 而不知缘由。"""
    client = _client(database, tmp_path, UserRole.MEMBER)
    assert client.get("/api/settings/credentials").status_code == 200


def test_listing_requires_authentication(database: Database, tmp_path: Path) -> None:
    client = TestClient(create_app(database=database, static_dir=tmp_path))
    response = client.get("/api/settings/credentials")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_SESSION_INVALID"


def test_secret_file_source_is_reported(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "compose_secret"
    secret.write_text(SECRET_VALUE, encoding="utf-8")
    monkeypatch.setenv(f"{DEEPSEEK}_FILE", str(secret))
    client = _client(database, tmp_path, UserRole.ADMIN)

    rows = client.get("/api/settings/credentials").json()["data"]["credentials"]
    deepseek = next(row for row in rows if row["name"] == DEEPSEEK)

    assert deepseek["configured"] is True
    assert deepseek["source"] == "secret_file"
    assert deepseek["updated_at"] is not None


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def test_admin_write_marks_the_credential_configured(
    database: Database, tmp_path: Path
) -> None:
    client = _client(database, tmp_path, UserRole.ADMIN)

    response = _put(client, DEEPSEEK, SECRET_VALUE)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["credential"]["configured"] is True
    listed = client.get("/api/settings/credentials").json()["data"]["credentials"]
    assert next(row for row in listed if row["name"] == DEEPSEEK)["configured"] is True


def test_written_credential_reaches_the_process_environment(
    database: Database, tmp_path: Path
) -> None:
    """embedding.py 与 rerank.py 仍直接读 os.environ，写入必须让它们看到。"""
    client = _client(database, tmp_path, UserRole.ADMIN)

    _put(client, "DASHSCOPE_API_KEY", SECRET_VALUE)

    assert os.environ["DASHSCOPE_API_KEY"] == SECRET_VALUE


def test_write_is_denied_for_members(database: Database, tmp_path: Path) -> None:
    client = _client(database, tmp_path, UserRole.MEMBER)

    response = _put(client, DEEPSEEK, SECRET_VALUE)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_FORBIDDEN"


def test_write_requires_the_csrf_header(database: Database, tmp_path: Path) -> None:
    client = _client(database, tmp_path, UserRole.ADMIN)

    response = client.put(f"/api/settings/credentials/{DEEPSEEK}", json={"value": SECRET_VALUE})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_CSRF_INVALID"


def test_unknown_credential_name_is_rejected(database: Database, tmp_path: Path) -> None:
    client = _client(database, tmp_path, UserRole.ADMIN)

    response = _put(client, "OPENAI_API_KEY", SECRET_VALUE)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WEB_CREDENTIAL_UNKNOWN"


def test_unknown_name_is_not_reflected_back(database: Database, tmp_path: Path) -> None:
    """错误信息不回显请求里的名字，避免把任意输入反射进响应体。"""
    client = _client(database, tmp_path, UserRole.ADMIN)

    response = _put(client, "SOME_INJECTED_MARKER", SECRET_VALUE)

    assert "SOME_INJECTED_MARKER" not in response.text


@pytest.mark.parametrize(
    "bad_value",
    ["   ", "short", "sk-with space", "sk-with\nnewline", "export KEY=sk-pasted-line"],
)
def test_malformed_values_are_rejected_with_a_reason(
    database: Database, tmp_path: Path, bad_value: str
) -> None:
    client = _client(database, tmp_path, UserRole.ADMIN)

    response = _put(client, DEEPSEEK, bad_value)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "WEB_CREDENTIAL_INVALID"
    assert response.json()["error"]["message"], "必须给出可操作的原因"


def test_empty_value_is_rejected_by_the_schema(database: Database, tmp_path: Path) -> None:
    client = _client(database, tmp_path, UserRole.ADMIN)

    response = _put(client, DEEPSEEK, "")

    assert response.status_code == 422
    assert response.json()["ok"] is False


def test_unwritable_store_reports_service_unavailable(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """裸跑开发（未挂 app_credentials 卷）必须给 503，而不是 500。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    monkeypatch.setenv(CREDENTIALS_DIR_ENV, str(blocked / "credentials"))
    client = _client(database, tmp_path, UserRole.ADMIN)

    response = _put(client, DEEPSEEK, SECRET_VALUE)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "WEB_CREDENTIAL_STORE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


def test_delete_clears_the_credential(database: Database, tmp_path: Path) -> None:
    client = _client(database, tmp_path, UserRole.ADMIN)
    _put(client, DEEPSEEK, SECRET_VALUE)

    response = client.delete(
        f"/api/settings/credentials/{DEEPSEEK}",
        headers={"X-CSRF-Token": _csrf(client)},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["credential"]["configured"] is False


def test_delete_is_denied_for_members(database: Database, tmp_path: Path) -> None:
    client = _client(database, tmp_path, UserRole.MEMBER)

    response = client.delete(
        f"/api/settings/credentials/{DEEPSEEK}",
        headers={"X-CSRF-Token": _csrf(client)},
    )

    assert response.status_code == 403


def test_delete_still_reports_a_compose_secret_as_configured(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """卷内删除不影响 Compose secret；如实报告，免得用户以为删干净了。"""
    secret = tmp_path / "compose_secret"
    secret.write_text(SECRET_VALUE, encoding="utf-8")
    monkeypatch.setenv(f"{DEEPSEEK}_FILE", str(secret))
    client = _client(database, tmp_path, UserRole.ADMIN)
    _put(client, DEEPSEEK, "sk-panel-written-value")

    response = client.delete(
        f"/api/settings/credentials/{DEEPSEEK}",
        headers={"X-CSRF-Token": _csrf(client)},
    )

    credential = response.json()["data"]["credential"]
    assert credential["configured"] is True
    assert credential["source"] == "secret_file"


# ---------------------------------------------------------------------------
# 只进不出
# ---------------------------------------------------------------------------


def test_no_response_body_ever_carries_the_secret(database: Database, tmp_path: Path) -> None:
    client = _client(database, tmp_path, UserRole.ADMIN)

    write_response = _put(client, DEEPSEEK, SECRET_VALUE)
    listing = client.get("/api/settings/credentials")
    identity = client.get("/api/auth/me")
    capabilities = client.get("/api/system/capabilities")

    for response in (write_response, listing, identity, capabilities):
        assert SECRET_VALUE not in response.text, f"{response.url} 泄漏了密钥明文"
        # 末四位同样不得出现：配上供应商固定前缀会缩小暴力搜索范围。
        assert SECRET_VALUE[-4:] not in response.text, f"{response.url} 泄漏了密钥末四位"


def test_listing_exposes_only_the_four_status_fields(
    database: Database, tmp_path: Path
) -> None:
    client = _client(database, tmp_path, UserRole.ADMIN)
    _put(client, DEEPSEEK, SECRET_VALUE)

    rows = client.get("/api/settings/credentials").json()["data"]["credentials"]

    assert all(set(row) == {"name", "configured", "source", "updated_at"} for row in rows)
