"""模型目录与按会话 / 按消息选择（P7-S15-A01/A04/A05/A07/A08，``docs/design/18-p7-multi-model.md`` §3）。

SQLite 上的真实鉴权（``create_app(database=...)``）。需要"会话工厂收到了哪个模型"的用例用记录型工厂；需要真实
工厂校验路径（逐对验证 → 424）的用例用默认的 ``WebAgenticSessionFactory``（非 mock），请求在建 run 之前就被拒。
兼容性检查对接进程内的假 OpenAI 兼容端点（明文回环），不连真实服务商、不计费。
"""

from __future__ import annotations

import importlib.util
import json
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from water_agent.config.credentials import CREDENTIALS, forget_injected_credentials
from water_agent.generation import compatibility as compat
from water_agent.generation import endpoints as ep
from water_agent.generation import model_catalog as mc
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.storage.repository import RunRepository, WebSessionRepository
from water_agent.webapp import run_routes, settings_routes
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.audit import _clear_events, _get_events
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.run_manager import RunManager, WebAgenticSessionFactory

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = REPO_ROOT / "tests" / "fixtures" / "endpoint_compat" / "fake_openai_server.py"
PASSWORD = "ModelChoicePass2026!"
KEY = "sk-model-choice-key-0001"


def _fake() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fake_openai_server_sel", FAKE_SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = _fake()


class _Session:
    def __init__(self, recorder: JsonTraceRecorder) -> None:
        self.recorder = recorder

    def start_trace(self, run_id: str, query: str) -> None:
        self.recorder.start(run_id=run_id, trace_id=f"trace-{run_id}", query=query)

    async def aask(self, query: str, *, run_id: str, **_: Any) -> dict[str, Any]:
        del query, run_id
        self.recorder.finish("succeeded")
        return {"status": "succeeded", "answer": "好的", "citations": [], "errors": [], "warnings": []}

    async def aresume(self, decision: dict[str, Any], *, run_id: str, **_: Any) -> dict[str, Any]:
        del decision, run_id
        self.recorder.finish("succeeded")
        return {"status": "succeeded", "answer": "已继续", "citations": [], "errors": [], "warnings": []}


class _ModelFactory:
    """声明 ``supports_model_choice`` 的记录型工厂：``calls`` 记下每次 create 收到的 model（没传 = ``None``）。"""

    supports_model_choice = True

    def __init__(self, trace_dir: Path) -> None:
        self.trace_dir = trace_dir
        self.calls: list[str | None] = []

    def create(self, *, run_id: str, thread_id: str, user_id: UUID, principal, project_id, stack, **kwargs: Any):
        del run_id, thread_id, user_id, principal, project_id, stack
        assert set(kwargs) <= {"model"}, kwargs
        self.calls.append(kwargs.get("model"))
        return _Session(JsonTraceRecorder(self.trace_dir))


class _PlainFactory:
    """没有 ``supports_model_choice`` 的严格签名工厂（评测 / 测试替身的形态）：传 model 会 ``TypeError``。"""

    def __init__(self, trace_dir: Path) -> None:
        self.trace_dir = trace_dir
        self.created = 0

    def create(self, *, run_id: str, thread_id: str, user_id: UUID, principal, project_id, stack):
        del run_id, thread_id, user_id, principal, project_id, stack
        self.created += 1
        return _Session(JsonTraceRecorder(self.trace_dir))


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    engine = create_engine(f"sqlite:///{tmp_path / 'model-choice.sqlite'}")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    yield db
    db.dispose()


@pytest.fixture
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
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
    import os

    for name in CREDENTIALS:
        os.environ.pop(name, None)
    forget_injected_credentials()


def _app(database: Database, tmp_path: Path, factory: object | None = None) -> Any:
    return create_app(
        database=database,
        static_dir=tmp_path,
        trace_dir=tmp_path / "traces",
        session_factory=factory,  # type: ignore[arg-type]
    )


def _login(app: Any, database: Database, role: UserRole) -> tuple[TestClient, UUID]:
    username = f"{role.value}-{uuid4().hex[:8]}"
    user = PostgresUserStore(database).create_user(username, PASSWORD, role)
    client = TestClient(app, raise_server_exceptions=True)
    assert client.post("/api/auth/login", json={"username": username, "password": PASSWORD}).status_code == 200
    return client, user.user_id


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""}


def _entry(model_id: str, **overrides: object) -> dict[str, object]:
    return {"id": model_id, "label": f"模型 {model_id}", "tier": "fast", **overrides}


def _put_catalog(client: TestClient, models: list[dict[str, object]]) -> Any:
    return client.put("/api/settings/llm-models", json={"models": models}, headers=_headers(client))


def _new_thread(database: Database, user_id: UUID) -> str:
    return WebSessionRepository(database).create(user_id, title="选模型").thread_id


def _send(client: TestClient, thread_id: str, **body: object) -> Any:
    return client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": "给水管流速怎么算", **body},
        headers=_headers(client),
    )


def _write_pair_report(model: str, url: str = "https://api.deepseek.com") -> None:
    compat.write_report(
        compat.pair_report_path(url, model),
        compat.CompatibilityReport.model_validate(
            {
                "version": compat.ENDPOINT_PROBE_VERSION,
                "status": "compatible",
                "model": model,
                "endpoint": url,
                "checks": {name: "passed" for name in compat.ENDPOINT_COMPATIBILITY_CHECKS},
                "schema_tokens": 1200,
                "evidence": {"probe_version": compat.ENDPOINT_PROBE_VERSION},
            }
        ),
    )


# ---------------------------------------------------------------------------
# 目录（A01）
# ---------------------------------------------------------------------------


def test_catalog_is_admin_managed_and_member_readable(
    database: Database, tmp_path: Path, credentials: Path
) -> None:
    """P7-S15-A01 管理员 + CSRF 才能写目录；成员只读，且只看到自己现在能选的条目的身份字段（无价格 / 角色限制 /
    停用项）；非法条目 422 且不回显请求值；审计只记模型 id 与条数。"""

    app = _app(database, tmp_path, _ModelFactory(tmp_path / "traces"))
    admin, _ = _login(app, database, UserRole.ADMIN)
    member, _ = _login(app, database, UserRole.MEMBER)

    models = [
        _entry("flash-a", thinking="off", price={"input_per_1m": 0.3, "output_per_1m": 1.2}),
        _entry("pro-b", tier="advanced", allowed_roles=["admin"]),
        _entry("legacy-c", enabled=False),
    ]
    assert member.put("/api/settings/llm-models", json={"models": models}, headers=_headers(member)).status_code == 403
    assert admin.put("/api/settings/llm-models", json={"models": models}).status_code == 403, "没有 CSRF 令牌"

    saved = _put_catalog(admin, models)
    assert saved.status_code == 200, saved.text
    full = saved.json()["data"]
    assert full["editable"] is True and full["can_manage"] is True
    assert [item["id"] for item in full["models"]] == ["flash-a", "pro-b", "legacy-c"]
    assert full["models"][0]["price"] == {"input_per_1m": 0.3, "output_per_1m": 1.2, "cached_input_per_1m": None}
    assert all(item["verified"] is False and item["selectable"] is False for item in full["models"])

    seen = member.get("/api/settings/llm-models").json()["data"]
    assert seen["can_manage"] is False and seen["editable"] is False
    assert [item["id"] for item in seen["models"]] == ["flash-a"], "停用项与管理员专属项不给成员看"
    assert set(seen["models"][0]) == {"id", "label", "tier", "thinking", "verified", "selectable"}

    bad = _put_catalog(admin, [_entry("a", base_url="https://evil.example/v1", api_key="sk-secret-000000")])
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "WEB_MODEL_CATALOG_INVALID"
    assert "evil.example" not in bad.text and "sk-secret" not in bad.text
    assert len(mc.load_catalog().entries) == 3, "非法请求不改目录"

    events = [e for e in _get_events() if e["event"] == "settings.llm_models_updated"]
    assert len(events) == 1 and events[0]["ids"] == ["flash-a", "pro-b", "legacy-c"] and events[0]["count"] == 3
    assert "0.3" not in json.dumps(_get_events()), "价格不进审计"


@pytest.mark.parametrize(
    ("variable", "value", "reason"),
    [(ep.CHAT_BASE_URL_ENV, "https://llm.deployment.example/v1", "deployment"), ("LOCAL_MODEL_ENABLED", "true", "local_model")],
)
def test_catalog_is_read_only_when_the_deployment_locks_the_endpoint(
    database: Database,
    tmp_path: Path,
    credentials: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    reason: str,
) -> None:
    """P7-S15-A01/SDD18-I16 env 配了端点 / 模型或本地模型启用时：目录只读（PUT 409），视图标明锁定原因，条目一律不可选。"""

    app = _app(database, tmp_path, _ModelFactory(tmp_path / "traces"))
    admin, _ = _login(app, database, UserRole.ADMIN)
    assert _put_catalog(admin, [_entry("flash-a")]).status_code == 200
    monkeypatch.setenv(variable, value)
    view = admin.get("/api/settings/llm-models").json()["data"]
    assert view["editable"] is False and view["locked_reason"] == reason
    assert all(item["selectable"] is False for item in view["models"])
    locked = _put_catalog(admin, [_entry("flash-b")])
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "WEB_LLM_ENDPOINT_MANAGED"


# ---------------------------------------------------------------------------
# 选择校验（A04）
# ---------------------------------------------------------------------------


def test_selection_refuses_unknown_disabled_thinking_and_endpoint_fields_without_creating_a_run(
    database: Database, tmp_path: Path, credentials: Path
) -> None:
    """P7-S15-A04/SDD18-I15 不在目录 / 已停用 → 422 ``WEB_MODEL_UNKNOWN``；``thinking: roundtrip`` → 422
    ``WEB_MODEL_THINKING_UNSUPPORTED``（S16 才放行）；请求体里的 ``base_url`` / ``api_key`` 一律 422
    ``WEB_INVALID_REQUEST``；这些请求都不建 run 行、不调用会话工厂。"""

    factory = _ModelFactory(tmp_path / "traces")
    app = _app(database, tmp_path, factory)
    admin, admin_id = _login(app, database, UserRole.ADMIN)
    _put_catalog(admin, [_entry("flash-a"), _entry("off-c", enabled=False), _entry("thinker", tier="advanced", thinking="roundtrip")])
    thread = _new_thread(database, admin_id)

    for model, code in (("nope", "WEB_MODEL_UNKNOWN"), ("off-c", "WEB_MODEL_UNKNOWN"), ("thinker", "WEB_MODEL_THINKING_UNSUPPORTED")):
        response = _send(admin, thread, model=model)
        assert response.status_code == 422 and response.json()["error"]["code"] == code, model
        assert model not in response.json()["error"]["message"], "说明里不回显请求值"
    for extra in ({"base_url": "https://evil.example/v1"}, {"api_key": "sk-secret-000000"}):
        response = _send(admin, thread, model="flash-a", **extra)
        assert response.status_code == 422 and response.json()["error"]["code"] == "WEB_INVALID_REQUEST"
    assert _send(admin, thread, model="bad name!").status_code == 422
    assert RunRepository(database).list_for_session(thread, admin_id) == []
    assert factory.calls == []


def test_role_forbidden_precedes_the_verification_failure(
    database: Database, tmp_path: Path, credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S15-A04 授权在前、配置披露在后：目录项限定了 ``allowed_roles`` 时，无权的成员收到 403
    ``WEB_MODEL_FORBIDDEN``，而不是 424（后者会向他透露这个模型有没有配好）；有权的管理员在没有逐对报告时才收到 424
    ``WEB_MODEL_NOT_VERIFIED``。真实工厂（非 mock）下走这条校验路径。"""

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-key-0001")
    app = _app(database, tmp_path)  # 缺省真实工厂
    admin, admin_id = _login(app, database, UserRole.ADMIN)
    member, member_id = _login(app, database, UserRole.MEMBER)
    _put_catalog(admin, [_entry("pro-b", tier="advanced", allowed_roles=["admin"])])
    member_thread, admin_thread = _new_thread(database, member_id), _new_thread(database, admin_id)

    forbidden = _send(member, member_thread, model="pro-b")
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "WEB_MODEL_FORBIDDEN"
    unverified = _send(admin, admin_thread, model="pro-b")
    assert unverified.status_code == 424 and unverified.json()["error"]["code"] == "WEB_MODEL_NOT_VERIFIED"
    assert RunRepository(database).list_for_session(admin_thread, admin_id) == []


def test_local_model_mode_refuses_every_override(
    database: Database, tmp_path: Path, credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S15-A04/SDD18-I16 ``LOCAL_MODEL_ENABLED`` 时只带 ``model`` 的显式覆盖会跳过本地分支、落到 DeepSeek 缺省端点
    （数据去向翻转）：逐条选择一律 409 ``WEB_MODEL_LOCKED``，哪怕目录里有这个模型。"""

    app = _app(database, tmp_path)
    admin, admin_id = _login(app, database, UserRole.ADMIN)
    _put_catalog(admin, [_entry("flash-a")])
    monkeypatch.setenv("LOCAL_MODEL_ENABLED", "true")
    response = _send(admin, _new_thread(database, admin_id), model="flash-a")
    assert response.status_code == 409 and response.json()["error"]["code"] == "WEB_MODEL_LOCKED"


def test_a_factory_that_cannot_honour_the_choice_refuses_instead_of_ignoring_it(
    database: Database, tmp_path: Path, credentials: Path
) -> None:
    """P7-S15-A05/SDD18-I21 选了模型而会话工厂没声明 ``supports_model_choice``：422 ``WEB_MODEL_UNSUPPORTED``，
    绝不静默用全局模型作答；不选模型时严格签名的替身工厂照常工作（协议兼容）。"""

    factory = _PlainFactory(tmp_path / "traces")
    app = _app(database, tmp_path, factory)
    admin, admin_id = _login(app, database, UserRole.ADMIN)
    _put_catalog(admin, [_entry("flash-a")])
    thread = _new_thread(database, admin_id)
    refused = _send(admin, thread, model="flash-a")
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "WEB_MODEL_UNSUPPORTED"
    assert factory.created == 0
    assert _send(admin, thread).status_code == 202


# ---------------------------------------------------------------------------
# 优先级、下传与落库（A05、A07）
# ---------------------------------------------------------------------------


def _wait_finalized(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        text = client.get(f"/api/runs/{run_id}/events").text
        events = [
            json.loads("\n".join(line[6:] for line in block.splitlines() if line.startswith("data: ")))
            for block in text.strip().split("\n\n")
            if "data: " in block
        ]
        if any(item["type"] == "finalize" for item in events):
            return events
        time.sleep(0.05)
    raise AssertionError("run 没有收口")


def test_message_beats_session_default_beats_global_and_is_persisted(
    database: Database, tmp_path: Path, credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S15-A05 / P7-S15-A07 优先级 消息 > 会话默认 > 全局：会话工厂收到的就是生效的那个；``runs.model`` 落库实际选用的
    模型（没选 = ``None``）；finalize 与 ``model_selected`` 只在选了模型时出现，缺省运行的 payload 逐字不变；
    清除会话默认后回到全局。真实工厂的子类 + 已验证的逐对报告，覆盖 ``WebAgenticSessionFactory`` 的标志位。"""

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-key-0001")
    monkeypatch.setattr(run_routes, "retrieval_problem", lambda _args: None)
    calls: list[str | None] = []

    class _RealishFactory(WebAgenticSessionFactory):
        #: SQLite 上没有 PG checkpointer：关掉"读 checkpoint 判定挂起的确认"，其余真实工厂的标志位保持
        supports_confirmation = False

        def create(self, *, run_id, thread_id, user_id, principal, project_id, stack, model=None):  # type: ignore[override]
            calls.append(model)
            return _Session(JsonTraceRecorder(self.trace_dir))

    sessions = WebSessionRepository(database)
    manager_factory = _RealishFactory(database, sessions, trace_dir=tmp_path / "traces", mock_generator=False)
    assert manager_factory.supports_model_choice is True
    app = _app(database, tmp_path, manager_factory)
    admin, admin_id = _login(app, database, UserRole.ADMIN)
    _put_catalog(admin, [_entry("flash-a", thinking="off"), _entry("fast-b")])
    for model in ("flash-a", "fast-b"):
        _write_pair_report(model)
    thread = _new_thread(database, admin_id)
    runs = RunRepository(database)

    # 1) 没选、会话也没默认 → 全局：不传 model kwarg，run 行没有模型，payload 没有 model
    first = _send(admin, thread)
    assert first.status_code == 202
    events = _wait_finalized(admin, first.json()["data"]["run_id"])
    assert calls == [None]
    assert runs.get_for_user(first.json()["data"]["run_id"], admin_id).model is None
    assert "model" not in next(e for e in events if e["type"] == "finalize")["payload"]
    first_trace = admin.get(f"/api/runs/{first.json()['data']['run_id']}/trace").json()["data"]
    assert not any(item["event_type"] == "model_selected" for item in first_trace["events"]), "全局模型的 trace 不变"

    # 2) 会话默认 → 生效
    patched = admin.patch(f"/api/sessions/{thread}", json={"model": "flash-a"}, headers=_headers(admin))
    assert patched.status_code == 200 and patched.json()["data"]["model"] == "flash-a"
    second = _send(admin, thread)
    run_id = second.json()["data"]["run_id"]
    events = _wait_finalized(admin, run_id)
    assert calls[-1] == "flash-a" and runs.get_for_user(run_id, admin_id).model == "flash-a"
    assert next(e for e in events if e["type"] == "finalize")["payload"]["model"] == "flash-a"
    second_trace = admin.get(f"/api/runs/{run_id}/trace").json()["data"]
    assert any(item["event_type"] == "model_selected" for item in second_trace["events"]), "选了模型才记 model_selected"

    # 3) 消息 > 会话默认
    third = _send(admin, thread, model="fast-b")
    run_id = third.json()["data"]["run_id"]
    _wait_finalized(admin, run_id)
    assert calls[-1] == "fast-b" and runs.get_for_user(run_id, admin_id).model == "fast-b"
    assert WebSessionRepository(database).get_for_user(thread, admin_id).model == "flash-a", "消息级选择不改会话默认"

    # 4) 清除会话默认 → 回到全局
    cleared = admin.patch(f"/api/sessions/{thread}", json={"model": None}, headers=_headers(admin))
    assert cleared.status_code == 200 and "model" not in cleared.json()["data"]
    fourth = _send(admin, thread)
    _wait_finalized(admin, fourth.json()["data"]["run_id"])
    assert calls[-1] is None


def test_session_default_authorization_and_validation(
    database: Database, tmp_path: Path, credentials: Path
) -> None:
    """P7-S15-A07 ``PATCH /api/sessions/{id}``：别人的 / 不存在的会话同答 404；需要 CSRF；``model`` 必须出现；
    目录外 / 思考型 / 无权的模型被拒（与发消息同一套校验）；默认模型必须是**现在就能选**的，所以不接受未验证的
    模型（真实工厂下）。"""

    factory = _ModelFactory(tmp_path / "traces")
    app = _app(database, tmp_path, factory)
    admin, admin_id = _login(app, database, UserRole.ADMIN)
    other, _ = _login(app, database, UserRole.MEMBER)
    _put_catalog(admin, [_entry("flash-a"), _entry("thinker", tier="advanced", thinking="roundtrip")])
    thread = _new_thread(database, admin_id)

    assert other.patch(f"/api/sessions/{thread}", json={"model": "flash-a"}, headers=_headers(other)).status_code == 404
    assert admin.patch(f"/api/sessions/{uuid4()}", json={"model": "flash-a"}, headers=_headers(admin)).status_code == 404
    assert admin.patch(f"/api/sessions/{thread}", json={"model": "flash-a"}).status_code == 403, "没有 CSRF 令牌"
    assert admin.patch(f"/api/sessions/{thread}", json={}, headers=_headers(admin)).status_code == 422
    assert admin.patch(f"/api/sessions/{thread}", json={"model": "flash-a", "base_url": "https://x"}, headers=_headers(admin)).status_code == 422
    unknown = admin.patch(f"/api/sessions/{thread}", json={"model": "nope"}, headers=_headers(admin))
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "WEB_MODEL_UNKNOWN"
    thinker = admin.patch(f"/api/sessions/{thread}", json={"model": "thinker"}, headers=_headers(admin))
    assert thinker.json()["error"]["code"] == "WEB_MODEL_THINKING_UNSUPPORTED"
    assert WebSessionRepository(database).get_for_user(thread, admin_id).model is None


def test_a_disabled_session_default_fails_closed_instead_of_swapping_models(
    database: Database, tmp_path: Path, credentials: Path
) -> None:
    """P7-S15-A07/SDD18-I21 会话默认模型被停用 / 删除之后，下一条消息 422 ``WEB_MODEL_UNKNOWN``，**不会**悄悄换成别的
    目录模型或全局模型；用户清除默认后恢复。"""

    factory = _ModelFactory(tmp_path / "traces")
    app = _app(database, tmp_path, factory)
    admin, admin_id = _login(app, database, UserRole.ADMIN)
    _put_catalog(admin, [_entry("flash-a"), _entry("fast-b")])
    thread = _new_thread(database, admin_id)
    assert admin.patch(f"/api/sessions/{thread}", json={"model": "flash-a"}, headers=_headers(admin)).status_code == 200
    _put_catalog(admin, [_entry("flash-a", enabled=False), _entry("fast-b")])

    refused = _send(admin, thread)
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "WEB_MODEL_UNKNOWN"
    assert factory.calls == []
    assert admin.patch(f"/api/sessions/{thread}", json={"model": None}, headers=_headers(admin)).status_code == 200
    assert _send(admin, thread).status_code == 202


def test_resume_after_a_restart_restores_the_persisted_run_model(
    database: Database, tmp_path: Path, credentials: Path
) -> None:
    """P7-S15-A07 进程重启后 ``RunManager`` 没有这个 run 的 handle：以路由从库里读出的 ``runs.model`` 重建，
    会话工厂收到的就是当时选的模型，而不是静默换回默认（SDD F21）。"""

    factory = _ModelFactory(tmp_path / "traces")
    user = PostgresUserStore(database).create_user(f"resume-{uuid4().hex[:6]}", PASSWORD, UserRole.MEMBER)
    thread = _new_thread(database, user.user_id)
    runs = RunRepository(database)
    run = runs.create(thread, user.user_id, model="flash-a")
    runs.set_status(run.run_id, user.user_id, "running", expected_statuses=("queued",))
    runs.set_status(run.run_id, user.user_id, "waiting_confirmation", expected_statuses=("running",))
    assert runs.get_for_user(run.run_id, user.user_id).model == "flash-a"

    manager = RunManager(database, session_factory=factory, memory_enabled=False, uploads_enabled=False, trace_dir=tmp_path / "traces")
    try:
        from water_agent.runtime.governance.contracts import Principal

        principal = Principal.local("resume-test")
        payload = manager.resume(
            run_id=run.run_id,
            thread_id=thread,
            owner_user_id=user.user_id,
            principal=principal,
            project_id=None,
            decision={"confirmation_id": "c1", "approved": True, "reason": ""},
            model=runs.get_for_user(run.run_id, user.user_id).model,
        )
        assert payload["status"] == "queued"
        deadline = time.monotonic() + 15
        while not factory.calls and time.monotonic() < deadline:
            time.sleep(0.05)
        assert factory.calls == ["flash-a"]
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# 逐对验证与面板（A02）
# ---------------------------------------------------------------------------


def test_panel_runs_the_compatibility_check_for_a_catalog_model(
    database: Database, tmp_path: Path, credentials: Path
) -> None:
    """P7-S15-A02 面板「运行兼容性检查」可带目录里的 ``model``（管理员 + CSRF）：目录外 / 没有目录 → 422
    ``WEB_MODEL_UNKNOWN``；检查通过后该对出现在目录视图里 ``verified`` / ``selectable``，全局模型不受影响；
    ``thinking: off`` 的条目探针请求体带 ``thinking.type=disabled``。"""

    app = _app(database, tmp_path, _ModelFactory(tmp_path / "traces"))
    admin, _ = _login(app, database, UserRole.ADMIN)
    with FAKE.FakeOpenAIServer(api_key=KEY, models=["global-m", "flash-a"]) as server:
        ep.write_panel_endpoint(server.base_url, "global-m")
        admin.put(
            "/api/settings/credentials/WATER_AGENT_LLM_API_KEY", json={"value": KEY}, headers=_headers(admin)
        )
        _put_catalog(admin, [_entry("flash-a", thinking="off")])

        unknown = admin.post("/api/settings/llm-endpoint/compatibility", json={"model": "nope"}, headers=_headers(admin))
        assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "WEB_MODEL_UNKNOWN"

        started = admin.post("/api/settings/llm-endpoint/compatibility", json={"model": "flash-a"}, headers=_headers(admin))
        assert started.status_code == 202, started.text
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            data = admin.get("/api/settings/llm-endpoint").json()["data"]
            if not data["check"]["running"]:
                break
            time.sleep(0.1)
        assert data["check"]["last"]["status"] == "compatible" and data["check"]["last"]["model"] == "flash-a"

        view = admin.get("/api/settings/llm-models").json()["data"]
        flash = next(item for item in view["models"] if item["id"] == "flash-a")
        assert flash["verified"] is True and flash["selectable"] is True
        assert data["compatibility"]["status"] == "unverified", "全局模型 global-m 没有报告，不受影响"
        posts = [item for item in server.requests if item["path"].endswith("/chat/completions")]
        assert posts and all(item["thinking"] == {"type": "disabled"} for item in posts)
        assert KEY not in json.dumps(_get_events())
