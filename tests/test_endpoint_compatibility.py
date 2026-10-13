"""换对话模型的兼容性硬门槛与探针（P7-S10-A02，``docs/design/16-p7-plugin-mcp.md`` §7 S10）。

探针对接**进程内的假 OpenAI 兼容端点**（``tests/fixtures/endpoint_compat/fake_openai_server.py``，明文回环），
不连真实服务商、不产生计费调用。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import stat
import time
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType

import pytest
from langgraph.checkpoint.memory import MemorySaver

from water_agent.config.credentials import CREDENTIALS, forget_injected_credentials
from water_agent.generation import compatibility as compat
from water_agent.generation import endpoints as ep
from water_agent.harness import endpoint_compat

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CLAUSES = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
FAKE_SERVER = REPO_ROOT / "tests" / "fixtures" / "endpoint_compat" / "fake_openai_server.py"
CUSTOM_URL = "https://llm.other-provider.example/v1"
CUSTOM_MODEL = "other-model"
CUSTOM_KEY = "sk-other-provider-key-0001"
DEEPSEEK_KEY = "sk-deepseek-key-never-sent-elsewhere"


def _fake() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fake_openai_server", FAKE_SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = _fake()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        ep.CHAT_BASE_URL_ENV,
        ep.CHAT_MODEL_ENV,
        compat.COMPATIBILITY_REPORT_ENV,
        "LOCAL_MODEL_ENABLED",
        "LOCAL_MODEL_BASE_URL",
        "LOCAL_MODEL_NAME",
        "LOCAL_MODEL_COMPATIBILITY_REPORT",
        "WATER_AGENT_LLM_TIMEOUT_SECONDS",
        "WATER_AGENT_LLM_PRICE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    forget_injected_credentials()


def _custom_endpoint(
    monkeypatch: pytest.MonkeyPatch, url: str = CUSTOM_URL, model: str = CUSTOM_MODEL
) -> None:
    monkeypatch.setenv(ep.CHAT_BASE_URL_ENV, url)
    monkeypatch.setenv(ep.CHAT_MODEL_ENV, model)
    monkeypatch.setenv("WATER_AGENT_LLM_API_KEY", CUSTOM_KEY)
    monkeypatch.setenv("DEEPSEEK_API_KEY", DEEPSEEK_KEY)


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db_path=None,
        dimensions=None,
        rerank="local",
        rerank_model=None,
        rerank_base_url=None,
        rerank_api_key=None,
        top_k=5,
        recall_top_k=20,
        hybrid_alpha=0.7,
        model=None,
        base_url=None,
        api_key_env=None,
        api_key=None,
        mock_generator=False,
        session_dir=str(tmp_path / "sessions"),
        trace_dir=str(tmp_path / "traces"),
        stream=False,
        drawing_root="",
    )


def _session(tmp_path: Path):
    from water_agent.retrieval.loader import load_clauses_jsonl
    from water_agent.retrieval.trivium import TriviumRetriever
    from water_agent.runtime.agentic import AgenticSession

    with ExitStack() as stack:
        return AgenticSession(
            _args(tmp_path),
            stack=stack,
            checkpointer=MemorySaver(),
            retriever=TriviumRetriever(
                db_path=None, fallback_clauses=load_clauses_jsonl(FIXTURE_CLAUSES)
            ),
            mcp_enabled=False,
        )


def _web_post(tmp_path: Path) -> tuple[int, dict]:
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from water_agent.storage.database import Database
    from water_agent.storage.models import Base
    from water_agent.storage.repository import WebSessionRepository
    from water_agent.webapp.app import create_app
    from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
    from water_agent.webapp.auth.db_store import PostgresUserStore
    from water_agent.webapp.auth.models import UserRole

    engine = create_engine(f"sqlite:///{tmp_path / 'web.sqlite'}")
    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    try:
        user = PostgresUserStore(database).create_user(
            "endpoint-user", "EndpointPass2026!", UserRole.MEMBER
        )
        thread_id = WebSessionRepository(database).create(user.user_id, title="t").thread_id
        client = TestClient(
            create_app(static_dir=tmp_path, database=database, trace_dir=tmp_path / "traces")
        )
        assert (
            client.post(
                "/api/auth/login",
                json={"username": "endpoint-user", "password": "EndpointPass2026!"},
            ).status_code
            == 200
        )
        response = client.post(
            f"/api/sessions/{thread_id}/messages",
            json={"message": "问题"},
            headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""},
        )
        return response.status_code, response.json()
    finally:
        database.dispose()


def test_unverified_endpoint_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S10-A02 非缺省对话端点没有兼容性报告：会话装配、CLI legacy、runner 真实组件、docgen 润色一律拒绝，
    Web 预检 424 ``WEB_MODEL_NOT_VERIFIED``（说明里给出探针命令、不含 Key）；缺省 DeepSeek 端点免检。"""

    from water_agent.cli import build_app, build_parser
    from water_agent.docgen.quality import RealPolishTransport
    from water_agent.harness.runner import HarnessRunner

    monkeypatch.setenv("DEEPSEEK_API_KEY", DEEPSEEK_KEY)
    assert (
        compat.endpoint_verification(ep.resolve_chat_endpoint()).status
        == compat.VERIFICATION_DEFAULT_EXEMPT
    )
    assert _session(tmp_path).model is not None

    _custom_endpoint(monkeypatch)
    verification = compat.endpoint_verification(ep.resolve_chat_endpoint())
    assert verification.status == compat.VERIFICATION_UNVERIFIED and not verification.usable
    with pytest.raises(compat.EndpointNotVerifiedError, match="endpoint_compat"):
        _session(tmp_path)
    cli_args = build_parser().parse_args(["问题", "--mode", "legacy"])
    with pytest.raises(compat.EndpointNotVerifiedError):
        build_app(args=cli_args)
    eval_path = tmp_path / "empty.jsonl"
    eval_path.write_text("", encoding="utf-8")
    runner = HarnessRunner(eval_path=eval_path, report_dir=tmp_path / "r", trace_dir=tmp_path / "t")
    with pytest.raises(compat.EndpointNotVerifiedError):
        runner._build_real_chat_model()
    with pytest.raises(compat.EndpointNotVerifiedError):
        RealPolishTransport()._ensure_llm()

    status, payload = _web_post(tmp_path)
    assert status == 424
    assert payload["error"]["code"] == "WEB_MODEL_NOT_VERIFIED"
    assert "endpoint_compat" in payload["error"]["message"]
    assert "llm.other-provider.example" in payload["error"]["message"]
    assert "sk-" not in json.dumps(payload)


def test_report_must_match_endpoint_and_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P7-S10-A02 报告必须是这一个端点 + 模型、当前探针版本、恰好七项全部 passed；改端点或改模型即作废
    旧报告（指向别处的报告不算证据）。精确匹配的报告让装配放行，chat model 指向配置的端点与模型。"""

    _custom_endpoint(monkeypatch)
    report = tmp_path / "compat.json"
    monkeypatch.setenv(compat.COMPATIBILITY_REPORT_ENV, str(report))
    endpoint = ep.resolve_chat_endpoint()

    def status_with(**overrides: object) -> str:
        FAKE.write_passing_report(
            report, **{"endpoint": CUSTOM_URL, "model": CUSTOM_MODEL, **overrides}
        )
        return compat.endpoint_verification(endpoint).status

    assert status_with() == compat.VERIFICATION_VERIFIED
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert status_with(model="another-model") == compat.VERIFICATION_UNVERIFIED
    assert (
        status_with(endpoint="https://llm.third-provider.example/v1")
        == compat.VERIFICATION_UNVERIFIED
    )
    assert (
        status_with(version="p7-s10-old", evidence={"probe_version": "p7-s10-old"})
        == compat.VERIFICATION_UNVERIFIED
    )
    checks = {name: "passed" for name in compat.ENDPOINT_COMPATIBILITY_CHECKS}
    assert (
        status_with(checks={**checks, "error_handling": "failed"}, status="incomplete")
        == compat.VERIFICATION_UNVERIFIED
    )
    assert status_with(checks={**checks, "memory_p0": "passed"}) == compat.VERIFICATION_UNVERIFIED
    assert status_with(endpoint=CUSTOM_URL + "/") == compat.VERIFICATION_VERIFIED
    report.write_text("{ 不是报告", encoding="utf-8")
    assert compat.endpoint_verification(endpoint).status == compat.VERIFICATION_UNVERIFIED

    status_with()
    model = _session(tmp_path).model
    assert model.inner.openai_api_base == CUSTOM_URL  # type: ignore[attr-defined]
    assert model.inner.model_name == CUSTOM_MODEL  # type: ignore[attr-defined]
    assert model.inner.openai_api_key.get_secret_value() == CUSTOM_KEY  # type: ignore[attr-defined]

    monkeypatch.setenv(ep.CHAT_MODEL_ENV, "other-model-v2")
    with pytest.raises(compat.EndpointNotVerifiedError):
        _session(tmp_path)


def test_probe_report_has_no_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S10-A02 探针对假端点跑满七项全部 passed（流式 usage、写错模型名得到结构化失败、首块后取消在时限内
    断开）；报告 0600、不含 Key 与请求原文，只有检查结果、端点、模型、探针版本与时间；端点只收到它自己的 Key
    （``DEEPSEEK_API_KEY`` 从未发出）。没有 Key 时结构化 SKIP、不发请求不写报告；不合格的端点如实记失败。"""

    with FAKE.FakeOpenAIServer(api_key=CUSTOM_KEY, models=[CUSTOM_MODEL]) as server:
        _custom_endpoint(monkeypatch, url=server.base_url)
        out = tmp_path / "probe.json"
        report, skipped = endpoint_compat.run_endpoint_compatibility(out_path=out)
        assert skipped is None and report is not None
        assert report.checks == {name: "passed" for name in compat.ENDPOINT_COMPATIBILITY_CHECKS}, (
            report.evidence
        )
        assert report.status == "compatible" and report.passes(compat.ENDPOINT_COMPATIBILITY_CHECKS)
        assert (report.endpoint, report.model) == (server.base_url, CUSTOM_MODEL)
        assert report.evidence["probe_version"] == compat.ENDPOINT_PROBE_VERSION
        assert report.evidence["checked_at"]
        assert report.evidence["error_code"] and report.evidence["cancel_close_seconds"] < 10
        assert set(server.received_keys) == {CUSTOM_KEY}
        deadline = time.monotonic() + 5
        while server.disconnects < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.disconnects >= 1, "首块后关闭的流式请求，服务端应当观察到连接断开"

        text = out.read_text(encoding="utf-8")
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        for forbidden in (
            CUSTOM_KEY,
            DEEPSEEK_KEY,
            endpoint_compat.STREAM_PROMPT,
            endpoint_compat.CANCEL_PROMPT,
            "flow_lps=20",
        ):
            assert forbidden not in text
        monkeypatch.setenv(compat.COMPATIBILITY_REPORT_ENV, str(out))
        assert compat.endpoint_verification(ep.resolve_chat_endpoint()).usable

        monkeypatch.delenv("WATER_AGENT_LLM_API_KEY")
        sent_before = len(server.requests)
        report, skipped = endpoint_compat.run_endpoint_compatibility(
            out_path=tmp_path / "skip.json"
        )
        assert report is None and skipped is not None
        assert skipped["status"] == "skipped" and skipped["requires"] == ["WATER_AGENT_LLM_API_KEY"]
        assert len(server.requests) == sent_before and not (tmp_path / "skip.json").exists()

    for mode, failing in (
        ("no_tools", "function_calling"),
        ("no_stream_usage", "streaming_usage"),
        ("accept_unknown_model", "error_handling"),
    ):
        with FAKE.FakeOpenAIServer(
            api_key=CUSTOM_KEY, models=[CUSTOM_MODEL], modes=[mode]
        ) as server:
            _custom_endpoint(monkeypatch, url=server.base_url)
            report, _ = endpoint_compat.run_endpoint_compatibility()
            assert report is not None and report.checks[failing] == "failed", (mode, report.checks)
            assert report.status == "incomplete" and not report.passes(
                compat.ENDPOINT_COMPATIBILITY_CHECKS
            )

    monkeypatch.delenv(ep.CHAT_BASE_URL_ENV)
    monkeypatch.delenv(ep.CHAT_MODEL_ENV)
    monkeypatch.setenv("LOCAL_MODEL_ENABLED", "true")
    with pytest.raises(ep.EndpointConfigError, match="local_model_compat"):
        endpoint_compat.run_endpoint_compatibility()
