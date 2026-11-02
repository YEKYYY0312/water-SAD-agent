"""设置面板「获取模型」（``POST /api/settings/llm-endpoint/models``）。

对接进程内的假 OpenAI 兼容端点（``tests/fixtures/endpoint_compat/fake_openai_server.py``，明文回环）；
SSRF 用例全部是 IP 字面量或注入的解析器，不发任何真实网络请求。
"""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from water_agent.config.credentials import CREDENTIALS, forget_injected_credentials
from water_agent.generation import chat_model
from water_agent.generation import compatibility as compat
from water_agent.generation import endpoints as ep
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.audit import _clear_events, _get_events
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = REPO_ROOT / "tests" / "fixtures" / "endpoint_compat" / "fake_openai_server.py"
PASSWORD = "ModelListPanelPass2026!"
KEY = "sk-model-list-key-0001"
URL = "/api/settings/llm-endpoint/models"


def _fake() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fake_openai_server_models", FAKE_SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = _fake()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    engine = create_engine(f"sqlite:///{tmp_path / 'models.sqlite'}")
    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    yield db
    db.dispose()


@pytest.fixture
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    for name in (ep.CHAT_BASE_URL_ENV, ep.CHAT_MODEL_ENV, compat.COMPATIBILITY_REPORT_ENV, "LOCAL_MODEL_ENABLED"):
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
    # 面板经 write_credential 直接注入 os.environ，monkeypatch 还原不掉
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
    assert client.post("/api/auth/login", json={"username": username, "password": PASSWORD}).status_code == 200
    return client


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""}


def _models(client: TestClient, base_url: str, api_key: str | None = None) -> Any:
    body: dict[str, Any] = {"base_url": base_url}
    if api_key is not None:
        body["api_key"] = api_key
    return client.post(URL, json=body, headers=_headers(client))


def test_models_listing_is_admin_only_and_needs_csrf(app: Any, database: Database) -> None:
    member = _login(app, database, UserRole.MEMBER)
    admin = _login(app, database, UserRole.ADMIN)
    assert _models(member, "https://llm.example/v1", KEY).status_code == 403
    assert admin.post(URL, json={"base_url": "https://llm.example/v1", "api_key": KEY}).status_code == 403
    extra = admin.post(URL, json={"base_url": "https://llm.example/v1", "model": "m"}, headers=_headers(admin))
    assert extra.status_code == 422, "请求体不收别的字段"


def test_typed_key_lists_filtered_models_without_leaking(app: Any, database: Database) -> None:
    """表单里刚填、未保存的 Key 用于这一次调用；只回模型 id（非法名、超长名被滤掉），Key 不进响应与审计。"""

    admin = _login(app, database, UserRole.ADMIN)
    with FAKE.FakeOpenAIServer(api_key=KEY, models=["zeta-model", "alpha-model"]) as server:
        response = _models(admin, server.base_url, KEY)
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"models": ["alpha-model", "zeta-model"]}
        assert server.received_keys == [KEY]
        assert server.requests[0]["path"] == "/v1/models"
    assert KEY not in response.text
    events = [e for e in _get_events() if e["event"] == "settings.llm_models_listed"]
    assert len(events) == 1
    assert events[0]["host"] == "127.0.0.1" and events[0]["result"] == "ok" and events[0]["count"] == 2
    assert KEY not in json.dumps(_get_events(), ensure_ascii=False)
    assert not (Path(os.environ["WATER_AGENT_CREDENTIALS_DIR"]) / "water_agent_llm_api_key").exists(), "不落盘"


def test_stored_key_is_used_when_none_typed(app: Any, database: Database) -> None:
    admin = _login(app, database, UserRole.ADMIN)
    assert (
        admin.put(
            "/api/settings/credentials/WATER_AGENT_LLM_API_KEY", json={"value": KEY}, headers=_headers(admin)
        ).status_code
        == 200
    )
    with FAKE.FakeOpenAIServer(api_key=KEY, models=["m1"]) as server:
        response = _models(admin, server.base_url)
        assert response.status_code == 200 and response.json()["data"]["models"] == ["m1"]
        assert server.received_keys == [KEY]


def test_no_key_anywhere_is_reported_without_a_request(app: Any, database: Database) -> None:
    admin = _login(app, database, UserRole.ADMIN)
    with FAKE.FakeOpenAIServer(api_key=KEY, models=["m1"]) as server:
        response = _models(admin, server.base_url)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "WEB_LLM_MODELS_UNAUTHORIZED"
        assert server.requests == []


@pytest.mark.parametrize(
    ("modes", "typed", "code"),
    [
        ((), "sk-wrong-key-0000", "WEB_LLM_MODELS_UNAUTHORIZED"),
        (("no_models",), KEY, "WEB_LLM_MODELS_UNSUPPORTED"),
        (("models_junk",), KEY, "WEB_LLM_MODELS_UNSUPPORTED"),
    ],
)
def test_upstream_failures_map_to_stable_codes(
    app: Any, database: Database, modes: tuple[str, ...], typed: str, code: str
) -> None:
    admin = _login(app, database, UserRole.ADMIN)
    with FAKE.FakeOpenAIServer(api_key=KEY, models=["m1"], modes=modes) as server:
        response = _models(admin, server.base_url, typed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert typed not in response.text and "127.0.0.1" not in response.json()["error"]["message"]


def test_ollama_shape_is_understood(app: Any, database: Database) -> None:
    admin = _login(app, database, UserRole.ADMIN)
    with FAKE.FakeOpenAIServer(api_key=KEY, models=["qwen3:8b"], modes=("models_ollama",)) as server:
        response = _models(admin, server.base_url, KEY)
    assert response.json()["data"]["models"] == ["qwen3:8b"]


def test_works_while_deployment_locks_the_endpoint(
    app: Any, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """获取模型是只读操作：env 锁定端点时仍可用（保存才被拒）。"""

    admin = _login(app, database, UserRole.ADMIN)
    monkeypatch.setenv(ep.CHAT_BASE_URL_ENV, "https://llm.deployment.example/v1")
    with FAKE.FakeOpenAIServer(api_key=KEY, models=["m1"]) as server:
        assert _models(admin, server.base_url, KEY).status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        "http://llm.example/v1",
        "https://user:secret@llm.example/v1",
        "https://llm.example/v1#frag",
        "ftp://llm.example/v1",
        "not a url",
    ],
)
def test_invalid_base_urls_are_rejected_without_echo(app: Any, database: Database, bad: str) -> None:
    admin = _login(app, database, UserRole.ADMIN)
    response = _models(admin, bad, KEY)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "WEB_LLM_ENDPOINT_INVALID"
    assert "secret" not in response.text and KEY not in response.text


@pytest.mark.parametrize(
    "target",
    [
        "https://169.254.169.254/v1",  # 云元数据
        "https://10.0.0.5/v1",  # 私有网段
        "https://192.168.1.10/v1",
        "https://100.64.0.1/v1",  # CGNAT
        "https://[::1]:8443/v1".replace("::1", "fe80::1"),  # 链路本地
    ],
)
def test_internal_destinations_are_refused_before_any_byte(app: Any, database: Database, target: str) -> None:
    admin = _login(app, database, UserRole.ADMIN)
    response = _models(admin, target, KEY)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "WEB_LLM_MODELS_DESTINATION_FORBIDDEN"
    events = [e for e in _get_events() if e["event"] == "settings.llm_models_listed"]
    assert events[-1]["result"] == "WEB_LLM_MODELS_DESTINATION_FORBIDDEN"


def test_dns_answers_are_classified_as_a_whole() -> None:
    """公开主机名解析出私有地址（含"一个公开 + 一个私有"）整体拒绝，零字节发出。"""

    for answers in (["10.1.2.3"], ["93.184.216.34", "169.254.169.254"]):
        with pytest.raises(chat_model.ModelListError) as raised:
            chat_model.list_models("https://models.example/v1", KEY, resolver=lambda host, port, a=answers: a)
        assert raised.value.code == "WEB_LLM_MODELS_DESTINATION_FORBIDDEN"


def test_response_extraction_bounds() -> None:
    names = [f"model-{index}" for index in range(500)]
    out = chat_model.extract_model_ids({"data": [{"id": n} for n in names] + [{"id": "model-1"}]})
    assert len(out) == chat_model.MODEL_LIST_MAX_ITEMS and len(set(out)) == len(out)
    assert chat_model.extract_model_ids({"data": "nope"}) == []
    assert chat_model.extract_model_ids(["a-b", 3, None, {"id": 5}]) == ["a-b"]
