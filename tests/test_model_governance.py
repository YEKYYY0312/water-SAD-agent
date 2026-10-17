"""模型选择的治理与可观测（P7-S15-A08，``docs/design/18-p7-multi-model.md`` §3.6）。"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine

from water_agent.generation import model_catalog as mc
from water_agent.generation.cost import estimate_deepseek_cost, estimate_model_cost
from water_agent.graphs.agent_rag import _llm_call_record
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.runtime.governance.config import DEFAULT_GOVERNANCE_CONFIG
from water_agent.runtime.governance.contracts import Principal
from water_agent.runtime.governance.observability import aggregate_metrics
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.storage.repository import RunRepository, WebSessionRepository
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.run_manager import RunManager


@pytest.fixture(autouse=True)
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "credentials"
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(directory))
    monkeypatch.delenv("WATER_AGENT_LLM_PRICE", raising=False)
    return directory


def _catalog(*entries: dict[str, object]) -> None:
    mc.write_catalog(mc.parse_entries([{"label": "模型", "tier": "fast", **entry} for entry in entries]))


def test_cost_is_attributed_per_catalog_model_and_unknown_prices_are_none() -> None:
    """P7-S15-A08 目录之外的模型与此前逐字相同（沿用 DeepSeek 单价口径，缺省 ``deepseek-chat`` / 脚本模型不变）；
    目录里的模型：治理价格表里登记的用登记价、目录项声明了估算单价的用声明价（永远未核对）、都没有就是 ``None`` ——
    不把别家模型按 deepseek-chat 的价格记账。"""

    assert estimate_model_cost("deepseek-chat", 1000, 500) == estimate_deepseek_cost(1000, 500)
    assert estimate_model_cost("ScriptedChatModel", 1000, 500) == estimate_deepseek_cost(1000, 500)

    _catalog(
        {"id": "priced-a", "price": {"input_per_1m": 2.0, "output_per_1m": 8.0}},
        {"id": "unpriced-b"},
        {"id": "deepseek-reasoner"},
    )
    assert estimate_model_cost("priced-a", 1_000_000, 500_000) == pytest.approx(6.0)
    assert estimate_model_cost("unpriced-b", 1_000_000, 500_000) is None
    registered = DEFAULT_GOVERNANCE_CONFIG.price_for("deepseek-reasoner")
    assert registered is not None
    assert estimate_model_cost("deepseek-reasoner", 1000, 500) == registered.estimate_usd(1000, 500)
    assert registered.confirmed is False, "价格永远未核对，不做硬拒绝"


def test_llm_call_record_and_trace_keep_unknown_cost_as_none(tmp_path: Path) -> None:
    """P7-S15-A08 agentic 路径的 llm_call 记录按模型计价；未知价格原样进 trace（``None``），聚合时跳过——
    成本总额是"未知"而不是 0.0（同 ``UsageRecord.usage_known`` 的纪律）。"""

    _catalog({"id": "unpriced-b"}, {"id": "priced-a", "price": {"input_per_1m": 1.0, "output_per_1m": 1.0}})
    message = SimpleNamespace(content="好", tool_calls=[], usage_metadata={"input_tokens": 1000, "output_tokens": 1000})
    unknown = _llm_call_record(message, "unpriced-b", 12.0)
    priced = _llm_call_record(message, "priced-a", 12.0)
    assert unknown["cost_usd"] is None and priced["cost_usd"] == pytest.approx(0.002)

    recorder = JsonTraceRecorder(tmp_path / "traces")
    recorder.start(run_id="r1", trace_id="t1", query="q")
    recorder.record_llm_call(model="unpriced-b", provider="deepseek", cost_usd=unknown["cost_usd"], query="q")
    snapshot = recorder.snapshot()
    event = next(item for item in snapshot["events"] if item["event_type"] == "llm_call")
    assert event["data"]["cost_usd"] is None
    assert aggregate_metrics([snapshot]).total_cost_usd is None, "只有未知成本时总额是未知，不是 0.0"

    recorder.record_llm_call(model="priced-a", provider="deepseek", cost_usd=priced["cost_usd"], query="q")
    assert aggregate_metrics([recorder.snapshot()]).total_cost_usd == pytest.approx(0.002)


class _Session:
    def __init__(self, recorder: JsonTraceRecorder) -> None:
        self.recorder = recorder

    def start_trace(self, run_id: str, query: str) -> None:
        self.recorder.start(run_id=run_id, trace_id=f"trace-{run_id}", query=query)

    async def aask(self, query: str, *, run_id: str, **_: Any) -> dict[str, Any]:
        raise AssertionError("会话创建失败时不应走到这里")


class _FailingFactory:
    supports_model_choice = True

    def __init__(self, trace_dir: Path) -> None:
        self.trace_dir = trace_dir
        self.calls: list[str | None] = []

    def create(self, *, run_id: str, thread_id: str, user_id: UUID, principal, project_id, stack, **kwargs: Any):
        del run_id, thread_id, user_id, principal, project_id, stack
        self.calls.append(kwargs.get("model"))
        raise RuntimeError("provider rejected the selected model")


def test_a_failing_selected_model_is_never_swapped_for_another(tmp_path: Path) -> None:
    """P7-S15-A08/SDD18-I21 选中的模型装配 / 调用失败 → run 以 ``failed`` 收口，会话工厂**只被调用一次**、只收到
    用户选的那个模型；不会因失败而换成全局模型或目录里的另一个模型（模型从不被自动更换）。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'gov.sqlite'}")
    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    try:
        user = PostgresUserStore(database).create_user(f"gov-{uuid4().hex[:6]}", "GovernancePass2026!", UserRole.MEMBER)
        thread = WebSessionRepository(database).create(user.user_id, title="t").thread_id
        runs = RunRepository(database)
        run = runs.create(thread, user.user_id, model="flash-a")
        factory = _FailingFactory(tmp_path / "traces")
        manager = RunManager(database, session_factory=factory, memory_enabled=False, uploads_enabled=False, trace_dir=tmp_path / "traces")
        try:
            manager.start(
                run_id=run.run_id,
                thread_id=thread,
                owner_user_id=user.user_id,
                principal=Principal.local("gov-test"),
                project_id=None,
                query="问题",
                model="flash-a",
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if runs.get_for_user(run.run_id, user.user_id).status == "failed":
                    break
                time.sleep(0.05)
            assert runs.get_for_user(run.run_id, user.user_id).status == "failed"
            assert factory.calls == ["flash-a"], factory.calls
            assert runs.get_for_user(run.run_id, user.user_id).model == "flash-a"
        finally:
            manager.close()
    finally:
        database.dispose()


def test_starting_with_a_model_on_a_factory_without_support_is_an_error(tmp_path: Path) -> None:
    """P7-S15-A05/SDD18-I21 ``RunManager.start(model=…)`` 遇到没声明 ``supports_model_choice`` 的工厂直接抛错，
    不静默用全局模型作答。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'gov2.sqlite'}")
    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    try:

        class _Plain:
            def create(self, *, run_id, thread_id, user_id, principal, project_id, stack):  # pragma: no cover
                raise AssertionError("不应被调用")

        manager = RunManager(database, session_factory=_Plain(), memory_enabled=False, uploads_enabled=False, trace_dir=tmp_path)
        try:
            with pytest.raises(ValueError, match="does not support model choice"):
                manager.start(
                    run_id=str(uuid4()),
                    thread_id=str(uuid4()),
                    owner_user_id=uuid4(),
                    principal=Principal.local("gov-test"),
                    project_id=None,
                    query="问题",
                    model="flash-a",
                )
        finally:
            manager.close()
    finally:
        database.dispose()
