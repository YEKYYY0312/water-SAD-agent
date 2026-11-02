"""model_choice_p0：模型目录与按会话 / 按消息选择的机制层评测（P7-S15，规格 §7 P7-S15-A11）。

用例走**生产 Web 层**（``create_app`` + SQLite 上的真实鉴权 + 真实 ``RunManager``），只把会话工厂换成记录型替身
（或 ``WebAgenticSessionFactory`` 的子类，让校验走真实工厂路径），把对话端点换成进程内的假 OpenAI 兼容服务。
harness 观测：

- **web**：目录读写、选择校验（目录 / 角色 / 锁定 / 逐对验证 / 请求体字段）、优先级、落库、trace 与 payload；
- **verification**：逐对兼容性证据（文件名不是信任根、旧探针版本、原单文件路径、免检不扩大）；
- **probe**：探针按目录项的思考配置发请求（与生产装配同一份 ``extra_body``）；
- **cost**：成本按模型归因，不知道价格就是 ``None``；
- **resume / failing**：进程重启后恢复到当时选的模型；选中的模型失败时**从不**被换成别的。

**不测**任何真实模型的质量、延迟或价格，也不证明目录里的模型在真实端点上存在（P7-S15-A13，无 Key 即 blocked）。
**不得据此声称"换模型后质量不变"或"高级模型更准"**（威胁模型 T14）。

数据集在 S15 Step 1 冻结；之后改它必须同时重新固化入仓基线并写明原因。退出码：0 全部硬指标达标；1 有硬指标不达标
或用例执行出错；2 加载问题、0 条用例或参数错误。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = REPO_ROOT / "data" / "eval" / "model_choice_p0.jsonl"
FAKE_SERVER = REPO_ROOT / "tests" / "fixtures" / "endpoint_compat" / "fake_openai_server.py"

SUITE = "model_choice_p0"
GENERATED_BY = "water_agent.harness.model_choice_eval"
REPORT_FORMAT = 1

#: 用例里"Key"的占位值。任何响应 / 审计里出现它都计入 ``key_leak_count``。
KEY_MARKER = "sk-" + "model-choice-eval-key-0001"
PASSWORD = "ModelChoiceEval2026!"
DEFAULT_ENDPOINT = "https://api.deepseek.com"
RELAY_ENDPOINT = "https://llm.relay.example/v1"

#: 硬指标与目标值。速率为 ``None``（分母为 0、未测量）一律判不达标：未测量不是通过。
HARD_TARGETS: dict[str, float | int] = {
    "selection_validation_correct_rate": 1.0,
    "unauthorized_model_use_count": 0,
    "base_url_accepted_count": 0,
    "silent_fallback_count": 0,
    "default_behavior_identical_rate": 1.0,
    "per_pair_verification_isolation_rate": 1.0,
    "cost_attribution_correct_rate": 1.0,
    "local_model_override_leak_count": 0,
    "key_leak_count": 0,
    "benign_selection_rejected_rate": 0.0,
    "harness_error_count": 0,
}

_ENV_CLEARED = (
    "WATER_AGENT_LLM_BASE_URL",
    "WATER_AGENT_LLM_MODEL",
    "WATER_AGENT_LLM_COMPATIBILITY_REPORT",
    "WATER_AGENT_LLM_PRICE",
    "WATER_AGENT_LLM_TIMEOUT_SECONDS",
    "LOCAL_MODEL_ENABLED",
    "LOCAL_MODEL_BASE_URL",
    "LOCAL_MODEL_NAME",
    "LOCAL_MODEL_COMPATIBILITY_REPORT",
    "WATER_AGENT_RETRIEVAL_MODE",
    "WATER_AGENT_INDEX_PATH",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "WATER_AGENT_LLM_API_KEY",
    "WATER_AGENT_EMBEDDING_API_KEY",
)


class HarnessUsageError(RuntimeError):
    """参数或环境不对，评测无法开始（退出码 2）。"""


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class ModelChoiceCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    category: str
    kind: str
    description: str
    setup: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    labels: dict[str, bool] = Field(default_factory=dict)


def load_cases(path: Path) -> tuple[list[ModelChoiceCase], list[str]]:
    cases: list[ModelChoiceCase] = []
    problems: list[str] = []
    seen: set[str] = set()
    if not path.is_file():
        return [], [f"数据集不存在：{path}"]
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = ModelChoiceCase.model_validate(json.loads(line))
        except (ValueError, ValidationError) as exc:
            problems.append(f"第 {number} 行不是合法用例：{str(exc).splitlines()[0]}")
            continue
        if case.id in seen:
            problems.append(f"用例 id 重复：{case.id}")
            continue
        seen.add(case.id)
        cases.append(case)
    return cases, problems


@dataclass
class CaseResult:
    id: str
    category: str
    kind: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(check["ok"] for check in self.checks)

    def count(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": "" if ok else detail})
        return bool(ok)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "kind": self.kind,
            "passed": self.passed,
            "error": self.error,
            "checks": self.checks,
            "counters": dict(sorted(self.counters.items())),
        }


# ---------------------------------------------------------------------------
# 环境隔离
# ---------------------------------------------------------------------------


@contextmanager
def _environment(updates: dict[str, str | None]) -> Iterator[None]:
    saved = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _isolated_env(case_dir: Path, extra: dict[str, str]) -> dict[str, str | None]:
    env: dict[str, str | None] = {name: None for name in _ENV_CLEARED}
    env.update({f"{name}_FILE": None for name in _ENV_CLEARED if name.endswith("_KEY")})
    env["WATER_AGENT_CREDENTIALS_DIR"] = str(case_dir / "credentials")
    env.update(extra)
    return env


def _load_fake_server() -> Any:
    spec = importlib.util.spec_from_file_location("fake_openai_server_mc_eval", FAKE_SERVER)
    if spec is None or spec.loader is None:
        raise HarnessUsageError(f"缺少假端点夹具：{FAKE_SERVER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mechanism_present() -> bool:
    """S15 的机制是否存在（目录模块 + 逐对验证）。不存在时报告标 ``absent``，比较器拒绝它作为门禁对象。"""

    try:
        importlib.import_module("water_agent.generation.model_catalog")
        from water_agent.generation import compatibility

        return hasattr(compatibility, "pair_report_path")
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# 会话工厂替身
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, recorder: Any) -> None:
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


def _make_factories(trace_dir: Path, database: Any) -> dict[str, Any]:
    from water_agent.observability.json_trace import JsonTraceRecorder
    from water_agent.storage.repository import WebSessionRepository
    from water_agent.webapp.run_manager import WebAgenticSessionFactory

    class Recording:
        supports_model_choice = True

        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def create(self, *, run_id: str, thread_id: str, user_id: UUID, principal, project_id, stack, **kwargs: Any):
            del run_id, thread_id, user_id, principal, project_id, stack
            self.calls.append(kwargs.get("model"))
            return _Session(JsonTraceRecorder(trace_dir))

    class Plain:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def create(self, *, run_id: str, thread_id: str, user_id: UUID, principal, project_id, stack):
            del run_id, thread_id, user_id, principal, project_id, stack
            self.calls.append(None)
            return _Session(JsonTraceRecorder(trace_dir))

    class Failing(Recording):
        def create(self, **kwargs: Any):  # type: ignore[override]
            self.calls.append(kwargs.get("model"))
            raise RuntimeError("provider rejected the selected model")

    class Real(WebAgenticSessionFactory):
        #: SQLite 上没有 PG checkpointer：关掉"读 checkpoint 判定挂起的确认"，其余真实工厂的标志位保持
        supports_confirmation = False

        def __init__(self) -> None:
            super().__init__(database, WebSessionRepository(database), trace_dir=trace_dir, mock_generator=False)
            self.calls: list[str | None] = []

        def create(self, *, run_id, thread_id, user_id, principal, project_id, stack, model=None):  # type: ignore[override]
            del run_id, thread_id, user_id, principal, project_id, stack
            self.calls.append(model)
            return _Session(JsonTraceRecorder(trace_dir))

    return {"recording": Recording, "plain": Plain, "failing": Failing, "real": Real}


# ---------------------------------------------------------------------------
# web 用例
# ---------------------------------------------------------------------------


class _WebWorld:
    """一条 web 用例的运行环境：数据库、应用、会话工厂、各角色的已登录客户端、当前会话。"""

    def __init__(self, case: ModelChoiceCase, case_dir: Path, result: CaseResult) -> None:
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine, event

        from water_agent.storage.database import Database
        from water_agent.storage.models import Base
        from water_agent.webapp.app import create_app

        self.case, self.result, self.case_dir = case, result, case_dir
        engine = create_engine(f"sqlite:///{case_dir / 'eval.sqlite'}")

        @event.listens_for(engine, "connect")
        def _fk(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(engine)
        self.database = Database(engine=engine)
        factories = _make_factories(case_dir / "traces", self.database)
        self.factory = factories[str(case.setup.get("factory", "recording"))]()
        self.app = create_app(
            database=self.database,
            static_dir=case_dir,
            trace_dir=case_dir / "traces",
            session_factory=self.factory,
        )
        self.clients: dict[str, tuple[TestClient, UUID]] = {}
        self.thread_id: str | None = None
        self.responses: list[str] = []
        self._TestClient = TestClient

    def login(self, who: str) -> tuple[Any, UUID]:
        from water_agent.webapp.auth.db_store import PostgresUserStore
        from water_agent.webapp.auth.models import UserRole

        if who not in self.clients:
            role = UserRole.ADMIN if who == "admin" else UserRole.MEMBER
            username = f"{who}-{uuid4().hex[:8]}"
            user = PostgresUserStore(self.database).create_user(username, PASSWORD, role)
            client = self._TestClient(self.app, raise_server_exceptions=True)
            response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
            if response.status_code != 200:
                raise RuntimeError(f"登录失败 {response.status_code}")
            self.clients[who] = (client, user.user_id)
        return self.clients[who]

    def headers(self, client: Any) -> dict[str, str]:
        from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME

        return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""}

    def thread(self) -> str:
        from water_agent.storage.repository import WebSessionRepository

        if self.thread_id is None:
            owner = str(self.case.setup.get("owner", "member"))
            _, user_id = self.login(owner)
            self.thread_id = WebSessionRepository(self.database).create(user_id, title="eval").thread_id
        return self.thread_id

    def remember(self, response: Any) -> None:
        self.responses.append(response.text)


def _wait_for_finalize(world: _WebWorld, client: Any, run_id: str) -> dict[str, Any] | None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        text = client.get(f"/api/runs/{run_id}/events").text
        for block in text.strip().split("\n\n"):
            lines = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
            if not lines:
                continue
            envelope = json.loads("\n".join(lines))
            if envelope["type"] == "finalize":
                return envelope["payload"]  # type: ignore[no-any-return]
        time.sleep(0.05)
    return None


_MISSING = object()


def _compare(result: CaseResult, name: str, observed: Any, expected: Any) -> bool:
    return result.check(name, observed == expected, f"期望 {expected!r}，实际 {observed!r}")


def _step_send(world: _WebWorld, step: dict[str, Any]) -> None:
    from water_agent.storage.repository import RunRepository

    result = world.result
    client, user_id = world.login(str(step.get("as", world.case.setup.get("owner", "member"))))
    body: dict[str, Any] = {"message": "给水管流速怎么算"}
    if step.get("model") is not None:
        body["model"] = step["model"]
    body.update(step.get("extra", {}))
    before = len(world.factory.calls)
    response = client.post(f"/api/sessions/{world.thread()}/messages", json=body, headers=world.headers(client))
    world.remember(response)
    expect = step.get("expect", {})
    ok_status = response.status_code == expect.get("status", response.status_code)
    code = None if response.status_code < 400 else (response.json().get("error") or {}).get("code")
    ok_code = ("code" not in expect) or code == expect["code"]
    if "status" in expect:
        result.count("selection_checks")
        result.count("selection_correct", int(ok_status and ok_code))
        result.check(f"send:{step.get('label', 'status')}", ok_status and ok_code, f"{response.status_code} {code}")
    refused = response.status_code >= 400
    if refused and len(world.factory.calls) > before:
        result.count("unauthorized_model_use")
    if any(key in body for key in ("base_url", "api_key")) and not refused:
        result.count("base_url_accepted")

    def check_local_model_leak() -> None:
        if world.case.labels.get("local_model") and any(call is not None for call in world.factory.calls):
            result.count("local_model_override_leak")

    if world.case.labels.get("benign"):
        result.count("benign_total")
        result.count("benign_rejected", int(refused))
    if response.status_code != 202:
        check_local_model_leak()
        if "calls" in expect:
            _compare(result, "calls", list(world.factory.calls), expect["calls"])
        return
    run_id = response.json()["data"]["run_id"]
    payload = _wait_for_finalize(world, client, run_id)
    # 工厂在 run 线程里异步建会话：必须等 run 收口后再看它收到过什么模型，否则 202 路径上恒为空
    check_local_model_leak()
    if payload is None:
        result.check("send:finalize", False, "run 没有收口")
        return
    if "calls" in expect:
        ok = _compare(result, "calls", list(world.factory.calls), expect["calls"])
        if world.case.labels.get("no_swap") and not ok:
            result.count("silent_fallback")
    run = RunRepository(world.database).get_for_user(run_id, user_id)
    if "run_model" in expect:
        _compare(result, "run_model", run.model, expect["run_model"])
    if "payload_model" in expect:
        observed = payload.get("model", "absent")
        _compare(result, "payload_model", observed, expect["payload_model"])
    if "trace_model_selected" in expect:
        trace = client.get(f"/api/runs/{run_id}/trace").json()["data"]
        seen = any(item.get("event_type") == "model_selected" for item in trace.get("events", []))
        _compare(result, "trace_model_selected", seen, expect["trace_model_selected"])
    if world.case.labels.get("default_behavior"):
        identical = (
            all(call is None for call in world.factory.calls)
            and run.model is None
            and "model" not in payload
        )
        result.count("default_checks")
        result.count("default_identical", int(identical))
        result.check("default_behavior", identical, f"calls={world.factory.calls} run_model={run.model!r}")


def _step_patch_session(world: _WebWorld, step: dict[str, Any]) -> None:
    result = world.result
    client, _ = world.login(str(step.get("as", world.case.setup.get("owner", "member"))))
    thread = step.get("thread") or world.thread()
    body: dict[str, Any] = {"model": step.get("model"), **step.get("extra", {})}
    response = client.patch(f"/api/sessions/{thread}", json=body, headers=world.headers(client))
    world.remember(response)
    expect = step.get("expect", {})
    code = None if response.status_code < 400 else (response.json().get("error") or {}).get("code")
    ok = response.status_code == expect.get("status", response.status_code) and (
        "code" not in expect or code == expect["code"]
    )
    result.count("selection_checks")
    result.count("selection_correct", int(ok))
    result.check(f"patch:{step.get('label', 'status')}", ok, f"{response.status_code} {code}")
    if "session_model" in expect and response.status_code == 200:
        _compare(result, "session_model", response.json()["data"].get("model", "absent"), expect["session_model"])


def _step_put_catalog(world: _WebWorld, step: dict[str, Any]) -> None:
    result = world.result
    client, _ = world.login(str(step.get("as", "admin")))
    response = client.put("/api/settings/llm-models", json={"models": step.get("models", [])}, headers=world.headers(client))
    world.remember(response)
    expect = step.get("expect", {})
    code = None if response.status_code < 400 else (response.json().get("error") or {}).get("code")
    ok = response.status_code == expect.get("status", 200) and ("code" not in expect or code == expect["code"])
    result.check(f"put_catalog:{step.get('label', 'status')}", ok, f"{response.status_code} {code}")
    carries_endpoint_fields = any(
        "base_url" in item or "api_key" in item for item in step.get("models", []) if isinstance(item, dict)
    )
    if carries_endpoint_fields and response.status_code < 400:
        result.count("base_url_accepted")
    for needle in expect.get("body_excludes", []):
        result.check(f"put_catalog:no_echo:{needle}", needle not in response.text, "说明里回显了请求值")


def _step_get_catalog(world: _WebWorld, step: dict[str, Any]) -> None:
    result = world.result
    client, _ = world.login(str(step.get("as", "member")))
    response = client.get("/api/settings/llm-models")
    world.remember(response)
    expect = step.get("expect", {})
    data = response.json()["data"] if response.status_code == 200 else {}
    if "ids" in expect:
        _compare(result, "catalog_ids", [item["id"] for item in data.get("models", [])], expect["ids"])
    if "fields" in expect and data.get("models"):
        _compare(result, "catalog_fields", sorted(data["models"][0]), sorted(expect["fields"]))
    if "editable" in expect:
        _compare(result, "catalog_editable", data.get("editable"), expect["editable"])
    if "selectable" in expect:
        _compare(
            result,
            "catalog_selectable",
            {item["id"]: item["selectable"] for item in data.get("models", [])},
            expect["selectable"],
        )
    if world.case.labels.get("default_behavior"):
        empty = data.get("models") == [] and data.get("error") is None
        result.count("default_checks")
        result.count("default_identical", int(empty))
        result.check("default_catalog_empty", empty, "缺省不配目录时目录视图必须为空且无错误")


def _step_get_session(world: _WebWorld, step: dict[str, Any]) -> None:
    result = world.result
    client, _ = world.login(str(step.get("as", world.case.setup.get("owner", "member"))))
    response = client.get(f"/api/sessions/{world.thread()}")
    world.remember(response)
    payload = response.json()["data"]
    expect = step.get("expect", {})
    if "session_model" in expect:
        _compare(result, "session_model", payload.get("model", "absent"), expect["session_model"])
    if world.case.labels.get("default_behavior"):
        result.count("default_checks")
        result.count("default_identical", int("model" not in payload))
        result.check("default_session_payload", "model" not in payload, "未设置默认模型的会话 payload 不应有 model")


_STEPS: dict[str, Callable[[_WebWorld, dict[str, Any]], None]] = {
    "send": _step_send,
    "patch_session": _step_patch_session,
    "put_catalog": _step_put_catalog,
    "get_catalog": _step_get_catalog,
    "get_session": _step_get_session,
}


def _write_pair_report(model: str, url: str = DEFAULT_ENDPOINT, *, version: str | None = None, broken: bool = False) -> None:
    from water_agent.generation import compatibility as compat

    checks = {name: "passed" for name in compat.ENDPOINT_COMPATIBILITY_CHECKS}
    if broken:
        checks["streaming_usage"] = "blocked"
    compat.write_report(
        compat.pair_report_path(url, model),
        compat.CompatibilityReport.model_validate(
            {
                "version": version or compat.ENDPOINT_PROBE_VERSION,
                "status": "blocked" if broken else "compatible",
                "model": model,
                "endpoint": url,
                "checks": checks,
                "schema_tokens": 1200,
                "evidence": {"probe_version": version or compat.ENDPOINT_PROBE_VERSION},
            }
        ),
    )


def _catalog_entries(raw: list[dict[str, Any]]) -> Any:
    return [{"label": f"模型 {item['id']}", "tier": "fast", **item} for item in raw]


def _no_retrieval_problem(_args: Any) -> None:
    """评测只关心模型选择：检索预检（索引 / embedding）固定放行，与本套件无关。"""

    return


def run_web_case(case: ModelChoiceCase, case_dir: Path, result: CaseResult) -> None:
    from water_agent.generation import model_catalog as mc
    from water_agent.webapp import run_routes
    from water_agent.webapp.auth.audit import _clear_events, _get_events

    setup = case.setup
    env: dict[str, str] = dict(setup.get("env", {}))
    if setup.get("key", False):
        env["DEEPSEEK_API_KEY"] = KEY_MARKER
    with _environment(_isolated_env(case_dir, env)):
        _clear_events()
        if setup.get("catalog"):
            mc.write_catalog(mc.parse_entries(_catalog_entries(setup["catalog"])))
        for model in setup.get("verified", []):
            _write_pair_report(model)
        original = run_routes.retrieval_problem
        run_routes.retrieval_problem = _no_retrieval_problem  # type: ignore[assignment]
        world = _WebWorld(case, case_dir, result)
        try:
            if setup.get("session_default"):
                from water_agent.storage.repository import WebSessionRepository

                owner_id = world.login(str(setup.get("owner", "member")))[1]
                repo = WebSessionRepository(world.database)
                world.thread_id = repo.create(owner_id, title="eval").thread_id
                repo.set_model(world.thread_id, owner_id, setup["session_default"])
            for step in case.steps:
                handler = _STEPS.get(str(step.get("op")))
                if handler is None:
                    raise HarnessUsageError(f"未知步骤 op：{step.get('op')!r}")
                handler(world, step)
            leaked = sum(text.count(KEY_MARKER) for text in world.responses) + json.dumps(_get_events(), ensure_ascii=False).count(KEY_MARKER)
            result.count("key_leaks", leaked)
            if case.labels.get("check_audit_key"):
                result.check("no_key_in_responses_or_audit", leaked == 0, f"{leaked} 处泄漏")
        finally:
            run_routes.retrieval_problem = original  # type: ignore[assignment]
            world.database.dispose()


# ---------------------------------------------------------------------------
# 其余用例类型
# ---------------------------------------------------------------------------


def run_verification_case(case: ModelChoiceCase, case_dir: Path, result: CaseResult) -> None:
    from water_agent.generation import compatibility as compat
    from water_agent.generation import endpoints as ep
    from water_agent.generation import model_catalog as mc

    setup = case.setup
    env: dict[str, str] = {"WATER_AGENT_LLM_BASE_URL": setup.get("endpoint", RELAY_ENDPOINT), "WATER_AGENT_LLM_API_KEY": KEY_MARKER}
    env.update(setup.get("env", {}))
    if setup.get("default_endpoint"):
        env.pop("WATER_AGENT_LLM_BASE_URL")
        env["DEEPSEEK_API_KEY"] = KEY_MARKER
        env.pop("WATER_AGENT_LLM_API_KEY")
    with _environment(_isolated_env(case_dir, env)):
        if setup.get("catalog"):
            mc.write_catalog(mc.parse_entries(_catalog_entries(setup["catalog"])))
        endpoint_url = setup.get("endpoint", RELAY_ENDPOINT) if not setup.get("default_endpoint") else DEFAULT_ENDPOINT
        for step in case.steps:
            op = step.get("op")
            if op == "write_report":
                path_model = step["model"]
                report_model = step.get("report_model", path_model)
                report_path = compat.pair_report_path(endpoint_url, path_model)
                checks = {name: "passed" for name in compat.ENDPOINT_COMPATIBILITY_CHECKS}
                if step.get("broken"):
                    checks["streaming_usage"] = "blocked"
                version = step.get("version") or compat.ENDPOINT_PROBE_VERSION
                compat.write_report(
                    report_path,
                    compat.CompatibilityReport.model_validate(
                        {
                            "version": version,
                            "status": "blocked" if step.get("broken") else "compatible",
                            "model": report_model,
                            "endpoint": endpoint_url,
                            "checks": checks,
                            "schema_tokens": 1200,
                            "evidence": {"probe_version": version},
                        }
                    ),
                )
            elif op == "write_single":
                path = compat.compatibility_report_path()
                checks = {name: "passed" for name in compat.ENDPOINT_COMPATIBILITY_CHECKS}
                compat.write_report(
                    path,
                    compat.CompatibilityReport.model_validate(
                        {
                            "version": compat.ENDPOINT_PROBE_VERSION,
                            "status": "compatible",
                            "model": step["model"],
                            "endpoint": endpoint_url,
                            "checks": checks,
                            "schema_tokens": 1200,
                            "evidence": {"probe_version": compat.ENDPOINT_PROBE_VERSION},
                        }
                    ),
                )
            elif op == "verify":
                verdict = compat.endpoint_verification(ep.resolve_chat_endpoint(model=step.get("model")))
                expect = step["expect"]
                ok = verdict.status == expect["status"] and expect.get("reason_contains", "") in verdict.reason
                result.count("pair_checks")
                result.count("pair_correct", int(ok))
                result.check(f"verify:{step.get('label', step.get('model'))}", ok, f"{verdict.status}: {verdict.reason}")
            else:
                raise HarnessUsageError(f"未知步骤 op：{op!r}")


def run_probe_case(case: ModelChoiceCase, case_dir: Path, result: CaseResult) -> None:
    """探针按目录项的思考配置构造被测模型：每个请求体都带（或都不带）同一份 ``extra_body``。"""

    from water_agent.generation import compatibility as compat
    from water_agent.generation import endpoints as ep
    from water_agent.generation import model_catalog as mc
    from water_agent.harness import endpoint_compat

    fake = _load_fake_server()
    setup = case.setup
    with (
        fake.FakeOpenAIServer(api_key=KEY_MARKER, models=["global-m", setup["model"]]) as server,
        _environment(_isolated_env(case_dir, {"WATER_AGENT_LLM_API_KEY": KEY_MARKER})),
    ):
        ep.write_panel_endpoint(server.base_url, "global-m")
        mc.write_catalog(mc.parse_entries(_catalog_entries(setup["catalog"])))
        report, skipped = endpoint_compat.run_endpoint_compatibility(model=setup["model"])
        if report is None:
            result.check("probe:ran", False, f"探针被跳过：{skipped}")
            return
        posts = [item for item in server.requests if item["path"].endswith("/chat/completions")]
        expected = setup["expect_thinking"]
        sent_all = bool(posts) and all(item["thinking"] == expected for item in posts)
        result.count("pair_checks")
        result.count("pair_correct", int(sent_all))
        result.check("probe:request_bodies_match_profile", sent_all, f"期望 {expected!r}，实际 {[i['thinking'] for i in posts]!r}")
        verified = compat.endpoint_verification(ep.resolve_chat_endpoint(model=setup["model"])).status == "verified"
        others = compat.endpoint_verification(ep.resolve_chat_endpoint()).usable
        result.count("pair_checks")
        result.count("pair_correct", int(verified and not others))
        result.check("probe:only_that_pair_verified", verified and not others, f"verified={verified} global_usable={others}")
        result.count("key_leaks", json.dumps(report.model_dump(mode="json")).count(KEY_MARKER))


def run_cost_case(case: ModelChoiceCase, case_dir: Path, result: CaseResult) -> None:
    from water_agent.generation import model_catalog as mc
    from water_agent.generation.cost import estimate_deepseek_cost, estimate_model_cost

    with _environment(_isolated_env(case_dir, {})):
        if case.setup.get("catalog"):
            mc.write_catalog(mc.parse_entries(_catalog_entries(case.setup["catalog"])))
        for item in case.steps:
            observed = estimate_model_cost(item["model"], int(item["input"]), int(item["output"]))
            expected = item["expect"]
            if expected == "legacy":
                expected = estimate_deepseek_cost(int(item["input"]), int(item["output"]))
            ok = (observed is None) if expected is None else (observed is not None and abs(observed - float(expected)) < 1e-9)
            result.count("cost_checks")
            result.count("cost_correct", int(ok))
            result.check(f"cost:{item['model']}", ok, f"期望 {expected!r}，实际 {observed!r}")


def run_resume_case(case: ModelChoiceCase, case_dir: Path, result: CaseResult) -> None:
    """进程重启后 handle 没了：以库里的 ``runs.model`` 重建，会话工厂收到当时选的模型。"""

    from sqlalchemy import create_engine

    from water_agent.runtime.governance.contracts import Principal
    from water_agent.storage.database import Database
    from water_agent.storage.models import Base
    from water_agent.storage.repository import RunRepository, WebSessionRepository
    from water_agent.webapp.auth.db_store import PostgresUserStore
    from water_agent.webapp.auth.models import UserRole
    from water_agent.webapp.run_manager import RunManager

    engine = create_engine(f"sqlite:///{case_dir / 'resume.sqlite'}")
    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    try:
        with _environment(_isolated_env(case_dir, {})):
            factories = _make_factories(case_dir / "traces", database)
            factory = factories["recording"]()
            user = PostgresUserStore(database).create_user(f"resume-{uuid4().hex[:6]}", PASSWORD, UserRole.MEMBER)
            thread = WebSessionRepository(database).create(user.user_id, title="eval").thread_id
            runs = RunRepository(database)
            run = runs.create(thread, user.user_id, model=case.setup.get("run_model"))
            runs.set_status(run.run_id, user.user_id, "running", expected_statuses=("queued",))
            runs.set_status(run.run_id, user.user_id, "waiting_confirmation", expected_statuses=("running",))
            manager = RunManager(database, session_factory=factory, memory_enabled=False, uploads_enabled=False, trace_dir=case_dir / "traces")
            try:
                manager.resume(
                    run_id=run.run_id,
                    thread_id=thread,
                    owner_user_id=user.user_id,
                    principal=Principal.local("resume-eval"),
                    project_id=None,
                    decision={"confirmation_id": "c1", "approved": True, "reason": ""},
                    model=runs.get_for_user(run.run_id, user.user_id).model,
                )
                deadline = time.monotonic() + 15
                while not factory.calls and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                manager.close()
            expected = [case.setup.get("run_model")]
            ok = factory.calls == expected
            if not ok:
                result.count("silent_fallback")
            result.check("resume:restores_run_model", ok, f"期望 {expected!r}，实际 {factory.calls!r}")
    finally:
        database.dispose()


def run_failing_case(case: ModelChoiceCase, case_dir: Path, result: CaseResult) -> None:
    """选中的模型装配失败：工厂只被调用一次、只收到用户选的那个，run 以 failed 收口——从不换成别的。"""

    from sqlalchemy import create_engine

    from water_agent.runtime.governance.contracts import Principal
    from water_agent.storage.database import Database
    from water_agent.storage.models import Base
    from water_agent.storage.repository import RunRepository, WebSessionRepository
    from water_agent.webapp.auth.db_store import PostgresUserStore
    from water_agent.webapp.auth.models import UserRole
    from water_agent.webapp.run_manager import RunManager

    engine = create_engine(f"sqlite:///{case_dir / 'failing.sqlite'}")
    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    try:
        with _environment(_isolated_env(case_dir, {})):
            factory = _make_factories(case_dir / "traces", database)["failing"]()
            user = PostgresUserStore(database).create_user(f"fail-{uuid4().hex[:6]}", PASSWORD, UserRole.MEMBER)
            thread = WebSessionRepository(database).create(user.user_id, title="eval").thread_id
            runs = RunRepository(database)
            chosen = case.setup["model"]
            run = runs.create(thread, user.user_id, model=chosen)
            manager = RunManager(database, session_factory=factory, memory_enabled=False, uploads_enabled=False, trace_dir=case_dir / "traces")
            try:
                manager.start(
                    run_id=run.run_id,
                    thread_id=thread,
                    owner_user_id=user.user_id,
                    principal=Principal.local("failing-eval"),
                    project_id=None,
                    query="问题",
                    model=chosen,
                )
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and runs.get_for_user(run.run_id, user.user_id).status != "failed":
                    time.sleep(0.05)
            finally:
                manager.close()
            swapped = factory.calls != [chosen]
            if swapped:
                result.count("silent_fallback")
            result.check("failing:only_the_chosen_model_was_tried", not swapped, f"工厂收到 {factory.calls!r}")
            result.check("failing:run_failed", runs.get_for_user(run.run_id, user.user_id).status == "failed", "run 应以 failed 收口")
    finally:
        database.dispose()


_KINDS: dict[str, Callable[[ModelChoiceCase, Path, CaseResult], None]] = {
    "web": run_web_case,
    "verification": run_verification_case,
    "probe": run_probe_case,
    "cost": run_cost_case,
    "resume": run_resume_case,
    "failing": run_failing_case,
}


def run_case(case: ModelChoiceCase, workdir: Path) -> CaseResult:
    result = CaseResult(id=case.id, category=case.category, kind=case.kind)
    runner = _KINDS.get(case.kind)
    if runner is None:
        result.error = f"未知用例类型：{case.kind!r}"
        return result
    case_dir = workdir / case.id
    case_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 审计事件会打印到 stdout（每次登录一行）；评测输出只留指标与失败明细
        with contextlib.redirect_stdout(io.StringIO()):
            runner(case, case_dir, result)
    except Exception as exc:  # noqa: BLE001 — 记为用例错误（硬指标 harness_error_count）
        result.error = f"{type(exc).__name__}: {exc}"
    return result


# ---------------------------------------------------------------------------
# 指标与报告
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def metrics_from_case_dicts(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """从报告里的用例明细算出全部指标。harness 写报告与比较器核对"报告没被手改"共用这一个函数。"""

    totals: dict[str, int] = {}
    for case in cases:
        for name, value in (case.get("counters") or {}).items():
            totals[name] = totals.get(name, 0) + int(value)

    def total(name: str) -> int:
        return totals.get(name, 0)

    return {
        "selection_validation_correct_rate": _rate(total("selection_correct"), total("selection_checks")),
        "unauthorized_model_use_count": total("unauthorized_model_use"),
        "base_url_accepted_count": total("base_url_accepted"),
        "silent_fallback_count": total("silent_fallback"),
        "default_behavior_identical_rate": _rate(total("default_identical"), total("default_checks")),
        "per_pair_verification_isolation_rate": _rate(total("pair_correct"), total("pair_checks")),
        "cost_attribution_correct_rate": _rate(total("cost_correct"), total("cost_checks")),
        "local_model_override_leak_count": total("local_model_override_leak"),
        "key_leak_count": total("key_leaks"),
        "benign_selection_rejected_rate": _rate(total("benign_rejected"), total("benign_total")),
        "harness_error_count": sum(1 for case in cases if case.get("error") is not None),
    }


def hard_violations(metrics: dict[str, Any]) -> list[str]:
    return [
        f"{name}={metrics.get(name)!r}（目标 {target!r}）"
        for name, target in HARD_TARGETS.items()
        if metrics.get(name) != target
    ]


def build_report(results: Sequence[CaseResult], *, dataset: Path) -> dict[str, Any]:
    metrics = metrics_from_case_dicts([result.as_dict() for result in results])
    violations = hard_violations(metrics)
    try:
        dataset_label = dataset.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        dataset_label = dataset.name
    return {
        "suite": SUITE,
        "generated_by": GENERATED_BY,
        "report_format": REPORT_FORMAT,
        "mechanism": "present" if mechanism_present() else "absent",
        "dataset": dataset_label,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "total": len(results),
        "passed": sum(1 for result in results if result.passed),
        "metrics": metrics,
        "hard_targets": dict(HARD_TARGETS),
        "hard_violations": violations,
        "ok": not violations,
        "cases": [result.as_dict() for result in results],
    }


def run(dataset: Path) -> tuple[dict[str, Any] | None, list[str]]:
    cases, problems = load_cases(dataset)
    if problems:
        return None, problems
    if not cases:
        return None, ["0 条可执行用例"]
    if not FAKE_SERVER.is_file():
        raise HarnessUsageError(f"缺少假端点夹具：{FAKE_SERVER}")
    with tempfile.TemporaryDirectory(prefix="wa-model-choice-") as tmp:
        workdir = Path(tmp)
        results = [run_case(case, workdir) for case in cases]
    return build_report(results, dataset=dataset), []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or SUITE).splitlines()[0])
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--report-path", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        report, problems = run(args.dataset)
    except HarnessUsageError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2
    if report is None:
        for problem in problems:
            print(f"!! 加载问题：{problem}", file=sys.stderr)
        return 2
    if args.report_path is not None:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{SUITE} mechanism={report['mechanism']} passed={report['passed']}/{report['total']}")
    for name, value in report["metrics"].items():
        print(f"  {name}: {value}")
    for case in report["cases"]:
        if not case["passed"]:
            detail = case["error"] or "; ".join(
                f"{check['name']}: {check['detail']}" for check in case["checks"] if not check["ok"]
            )
            print(f"  FAIL {case['id']}: {detail}")
    if report["hard_violations"]:
        print("!! 硬指标不达标：" + "；".join(report["hard_violations"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GENERATED_BY",
    "HARD_TARGETS",
    "SUITE",
    "ModelChoiceCase",
    "build_report",
    "hard_violations",
    "load_cases",
    "main",
    "metrics_from_case_dicts",
    "run",
    "run_case",
]
