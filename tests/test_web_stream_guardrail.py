"""SSE 不得在输出护栏之前下发模型正文（review 2026-09-30）。

此前 RunManager 把 ``astream_agentic`` 的每个 token 与 messages 事件的文本原样推给浏览器：
检索工具的原始 JSON、校验器内部回流提示、被驳回的草稿都在里面；输出护栏只在 finalize
对最终答案跑，被拦截的 ``rm -rf /`` 原文早已随 token 事件下发，前端再把累积的 token 当
成 blocked 运行的"回答"显示出来。

这里用**真实** ``AgenticSession``（只把模型换成逐步脚本、语料换成 fixture）驱动
RunManager——Web 层其余测试全用假会话，正是这类缺陷一直没被看见的原因。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, event

from water_agent.harness.doubles import ScriptedChatModel
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.trivium import TriviumRetriever
from water_agent.runtime.agentic import AgenticSession
from water_agent.runtime.governance.contracts import RequestContext
from water_agent.session import default_checkpoint_serde
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.storage.repository import WebSessionRepository
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "rag_fixture.jsonl"
PASSWORD = "StreamGuard2026!"


def _retrieve(query: str) -> dict:
    return {"tool_calls": [{"name": "retrieve_clauses", "args": {"query": query, "top_k": 5}}]}


def _cite(*ids: str) -> dict:
    return {"tool_calls": [{"name": "cite_clauses", "args": {"article_ids": list(ids)}}]}


class _ScriptedFactory:
    """每个 run 一个真实 AgenticSession（同 WebAgenticSessionFactory），模型按脚本作答。"""

    def __init__(self, tmp_path: Path, scripts: list[list[dict]]) -> None:
        self.tmp_path = tmp_path
        self.scripts = list(scripts)
        self.saver = InMemorySaver(serde=default_checkpoint_serde())
        self.clauses = load_clauses_jsonl(FIXTURE)

    def create(self, *, run_id: str, thread_id: str, user_id: UUID, principal, project_id, stack):
        del user_id, project_id
        args = argparse.Namespace(
            db_path=None, dimensions=None, rerank="local", rerank_model=None, rerank_base_url=None,
            rerank_api_key=None, top_k=5, recall_top_k=20, hybrid_alpha=0.7, model="unused",
            base_url="unused", api_key_env="DEEPSEEK_API_KEY", api_key=None, mock_generator=True,
            session_dir=str(self.tmp_path / "sessions"), trace_dir=str(self.tmp_path / "traces"),
            stream=False, drawing_root="",
        )
        return AgenticSession(
            args,
            stack=stack,
            thread_id=thread_id,
            request_context=RequestContext(principal=principal, request_id=f"req-{run_id[:12]}", thread_id=thread_id),
            checkpointer=self.saver,
            model=ScriptedChatModel(steps=self.scripts.pop(0)),
            retriever=TriviumRetriever(db_path=None, fallback_clauses=self.clauses),
            mcp_enabled=False,
        )


@pytest.fixture
def database(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stream-guard.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    database = Database(engine=engine)
    yield database
    database.dispose()


def _run(database: Database, tmp_path: Path, script: list[dict], question: str) -> list[dict]:
    username = f"stream-{uuid4().hex[:8]}"
    user = PostgresUserStore(database).create_user(username, PASSWORD, UserRole.MEMBER)
    thread_id = WebSessionRepository(database).create(user.user_id, title="stream").thread_id
    app = create_app(
        static_dir=tmp_path,
        database=database,
        session_factory=_ScriptedFactory(tmp_path, [script]),
        memory_enabled=False,
        trace_dir=tmp_path / "traces",
    )
    client = TestClient(app, raise_server_exceptions=True)
    assert client.post("/api/auth/login", json={"username": username, "password": PASSWORD}).status_code == 200
    created = client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": question},
        headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)},
    )
    assert created.status_code == 202, created.text
    response = client.get(f"/api/runs/{created.json()['data']['run_id']}/events")
    events: list[dict] = []
    for block in response.text.strip().split("\n\n"):
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


def _before_finalize(events: list[dict]) -> str:
    return json.dumps([e for e in events if e["type"] != "finalize"], ensure_ascii=False)


def test_blocked_answer_text_never_reaches_the_browser(database: Database, tmp_path: Path) -> None:
    dangerous = "依据 FIX 1.1.1 处理即可。然后在服务器上执行 rm -rf / --no-preserve-root 清理缓存。"
    events = _run(
        database, tmp_path,
        [_retrieve("给水管道的设计秒流量"), _cite("FIX 1.1.1"), {"content": dangerous}],
        "设计秒流量如何确定",
    )
    finalize = next(e for e in events if e["type"] == "finalize")
    assert finalize["payload"]["status"] == "blocked"
    assert finalize["payload"]["answer"] == ""
    assert "rm -rf" not in json.dumps(events, ensure_ascii=False)


def test_tool_output_and_validator_feedback_are_not_streamed(database: Database, tmp_path: Path) -> None:
    final = "依据 FIX 1.1.1，设计秒流量按建筑类型选用相应的计算方法确定。"
    events = _run(
        database, tmp_path,
        [
            _retrieve("给水管道的设计秒流量"),
            {"content": "第一版回答，没有声明引用。"},
            _cite("FIX 1.1.1"),
            {"content": final},
        ],
        "设计秒流量如何确定",
    )
    finalize = next(e for e in events if e["type"] == "finalize")
    assert finalize["payload"]["status"] == "succeeded"
    assert finalize["payload"]["answer"] == final
    streamed = _before_finalize(events)
    assert '\\"hits\\"' not in streamed and "source_id" not in streamed, "检索工具的原始输出不得下发"
    assert "已返回相关条款" not in streamed and "声明回答依据" not in streamed, "校验器的内部回流提示不得下发"
    assert "第一版回答" not in streamed, "被驳回的草稿不得下发"
    assert final not in streamed, "回答正文只随 finalize（输出护栏之后）下发"


def test_validator_boundary_is_not_shown_as_a_running_tool(database: Database, tmp_path: Path) -> None:
    """校验器注入的合成 citation_validator 调用不是工具：不得出现 tool_started（前端卡片会一直"运行中"）。"""
    events = _run(
        database, tmp_path,
        [
            _retrieve("给水管道的设计秒流量"),
            {"content": "第一版回答，没有声明引用。"},
            _cite("FIX 1.1.1"),
            {"content": "依据 FIX 1.1.1 确定。"},
        ],
        "设计秒流量如何确定",
    )
    started = [e["payload"].get("tool_name") for e in events if e["type"] == "tool_started"]
    assert "citation_validator" not in started
    assert "retrieve_clauses" in started
