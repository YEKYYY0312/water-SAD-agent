"""P5-S48 Step 4: GET /api/runs/{run_id}/export?format=docx|xlsx.

Covers spec Testing (xlsx 200 / xlsx 404 / docx 200 / non-owner 403) plus the
boundaries this implementation adds:

- export reads only what the programmatic tools produced (``calc_velocity``
  values + their numeric call arguments, the *last* ``material_stats`` table);
- a run this process did not execute (restart) answers 404, same as ``/trace``;
- an answer whose declared citations do not cover the body is refused with 422
  instead of being shipped as a formal document;
- non-ASCII filenames carry both ``filename=`` and ``filename*=``;
- the staging file on the exports volume is removed after the response.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from uuid import UUID, uuid4

import docx
import openpyxl
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import create_engine, event

from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.storage.repository import ProjectRepository, WebSessionRepository
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.export_routes import content_disposition

_PASSWORD = "ExportPass2026!"

_VELOCITY_CALL = AIMessage(
    content="",
    tool_calls=[{"id": "call-v", "name": "calc_velocity", "args": {"flow_lps": 12.5, "diameter_mm": 100}}],
)
_VELOCITY_RESULT = ToolMessage(
    name="calc_velocity",
    tool_call_id="call-v",
    content=json.dumps({"velocity_mps": 1.5915, "assumptions": ["满管流"]}),
)


def _material_messages(rows: list[dict], call_id: str) -> list:
    return [
        AIMessage(content="", tool_calls=[{"id": call_id, "name": "material_stats", "args": {}}]),
        ToolMessage(
            name="material_stats",
            tool_call_id=call_id,
            content=json.dumps(
                {
                    "ok": True,
                    "rows": rows,
                    "total_length_m": sum(row["quantity"] for row in rows),
                    "assumptions": [],
                },
                ensure_ascii=False,
            ),
        ),
    ]


_ROWS = [
    {
        "material": "球墨铸铁管",
        "spec": "DN150",
        "unit": "m",
        "quantity": 200.0,
        "raw_quantity": 200.0,
        "segments": ["S1", "S2"],
        "detail": "S1(120m)、S2(80m)",
    }
]


class _ScriptedSession:
    def __init__(self, recorder: JsonTraceRecorder, script: dict) -> None:
        self.recorder = recorder
        self.script = script

    def start_trace(self, run_id: str, query: str) -> None:
        self.recorder.start(run_id=run_id, trace_id=f"trace-{run_id}", query=query)

    async def aask(self, query, *, run_id, memory_context, on_token, on_event, cancel_check, **extra):
        del query, run_id, memory_context, cancel_check, extra
        on_event("values", {"status": "running", "messages": list(self.script.get("messages", []))})
        answer = self.script.get("answer", "")
        if answer:
            on_token(answer)
        self.recorder.finish(self.script.get("status", "succeeded"))
        return {
            "status": self.script.get("status", "succeeded"),
            "answer": answer,
            "citations": [{"source_id": cid} for cid in self.script.get("citations", [])],
            "errors": [],
            "warnings": [],
        }


class _ScriptedFactory:
    def __init__(self, trace_dir: Path) -> None:
        self.trace_dir = trace_dir
        self.script: dict = {}

    def create(self, *, run_id, thread_id, user_id, principal, project_id, stack):
        del run_id, thread_id, user_id, principal, project_id, stack
        return _ScriptedSession(JsonTraceRecorder(self.trace_dir), self.script)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'export.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    yield db
    db.dispose()


@pytest.fixture
def export_root(tmp_path: Path) -> Path:
    root = tmp_path / "exports"
    root.mkdir()
    return root


def _app(database: Database, tmp_path: Path, export_root: Path, factory):
    return create_app(
        static_dir=tmp_path,
        database=database,
        session_factory=factory,
        memory_enabled=False,
        trace_dir=tmp_path / "traces",
        session_dir=tmp_path / "sessions",
        upload_dir=tmp_path / "uploads",
        export_dir=export_root,
    )


def _login(app, database: Database) -> tuple[TestClient, UUID]:
    username = f"export-{uuid4()}"
    user = PostgresUserStore(database).create_user(username, _PASSWORD, UserRole.MEMBER)
    client = TestClient(app, raise_server_exceptions=True)
    assert client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    ).status_code == 200
    return client, user.user_id


def _run(client: TestClient, thread_id: str) -> str:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    response = client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": "请计算并出清单"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["data"]["run_id"]
    # 读完事件流 = 等到 finalize
    assert client.get(f"/api/runs/{run_id}/events").status_code == 200
    return run_id


@pytest.fixture
def setup(database: Database, tmp_path: Path, export_root: Path):
    factory = _ScriptedFactory(tmp_path / "traces")
    app = _app(database, tmp_path, export_root, factory)
    client, user_id = _login(app, database)
    session = WebSessionRepository(database).create(user_id, title="给水干管计算")
    return factory, app, client, user_id, session.thread_id


def test_xlsx_export_of_material_statistics(setup, export_root: Path) -> None:
    factory, _, client, _, thread_id = setup
    factory.script = {
        "answer": "材料清单见附件。",
        "messages": _material_messages(_ROWS, "call-m"),
    }
    run_id = _run(client, thread_id)
    response = client.get(f"/api/runs/{run_id}/export?format=xlsx")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.headers["content-disposition"].startswith("attachment;")
    workbook = openpyxl.load_workbook(io.BytesIO(response.content))
    sheet = workbook["材料清单"]
    assert [cell.value for cell in sheet[2]][:5] == [1, "球墨铸铁管", "DN150", "m", 200.0]
    # 暂存文件在响应后删除：导出卷是中转区，不是档案库
    assert list(export_root.iterdir()) == []


def test_xlsx_uses_only_the_last_material_table(setup) -> None:
    factory, _, client, _, thread_id = setup
    second = [dict(_ROWS[0], quantity=80.0, raw_quantity=80.0, segments=["S2"], detail="S2(80m)")]
    factory.script = {
        "answer": "重算后的清单。",
        "messages": _material_messages(_ROWS, "call-1") + _material_messages(second, "call-2"),
    }
    run_id = _run(client, thread_id)
    workbook = openpyxl.load_workbook(
        io.BytesIO(client.get(f"/api/runs/{run_id}/export?format=xlsx").content)
    )
    rows = list(workbook["材料清单"].iter_rows(min_row=2, values_only=True))
    assert [row[4] for row in rows] == [80.0], "重复调用累加就是重复计量"


def test_xlsx_without_material_statistics_is_404(setup) -> None:
    factory, _, client, _, thread_id = setup
    factory.script = {"answer": "只有文字回答。"}
    run_id = _run(client, thread_id)
    response = client.get(f"/api/runs/{run_id}/export?format=xlsx")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WEB_EXPORT_UNAVAILABLE"


def test_docx_export_of_a_calculation_run(setup) -> None:
    factory, _, client, _, thread_id = setup
    factory.script = {
        "answer": "流速 1.5915 m/s，依据 GB50015-2019 3.7.6。",
        "citations": ["GB50015-2019 3.7.6"],
        "messages": [_VELOCITY_CALL, _VELOCITY_RESULT],
    }
    run_id = _run(client, thread_id)
    response = client.get(f"/api/runs/{run_id}/export?format=docx")
    assert response.status_code == 200, response.text
    assert response.content[:4] == b"PK\x03\x04"
    document = docx.Document(io.BytesIO(response.content))
    text = "\n".join(p.text for p in document.paragraphs)
    cells = "\n".join(c.text for t in document.tables for r in t.rows for c in r.cells)
    assert "计算书" in text
    assert "1.5915" in cells, "数值来自 calc_velocity 的程序化结果"
    assert "12.5" in cells and "100" in cells, "输入参数来自工具调用参数原值"
    assert "[1] GB50015-2019 3.7.6" in text


def test_docx_export_of_a_text_answer_is_a_design_note(setup) -> None:
    factory, _, client, _, thread_id = setup
    factory.script = {
        "answer": "本工程采用雨污分流制，依据 GB50014-2021 4.1.1。",
        "citations": ["GB50014-2021 4.1.1"],
    }
    run_id = _run(client, thread_id)
    response = client.get(f"/api/runs/{run_id}/export?format=docx")
    assert response.status_code == 200, response.text
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(response.content)).paragraphs)
    assert "设计说明" in text
    assert "雨污分流" in text


def test_answer_citing_an_undeclared_clause_is_refused_not_exported(setup) -> None:
    factory, _, client, _, thread_id = setup
    factory.script = {
        "answer": "最小管径按 GB50014-2021 5.2.10 取值。",
        "citations": ["GB50014-2021 4.1.1"],
    }
    run_id = _run(client, thread_id)
    response = client.get(f"/api/runs/{run_id}/export?format=docx")
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "WEB_EXPORT_SELF_CHECK_FAILED"
    assert "GB50014-2021 5.2.10" in body["message"]


def test_non_owner_project_member_gets_403(
    database: Database, tmp_path: Path, export_root: Path
) -> None:
    factory = _ScriptedFactory(tmp_path / "traces")
    factory.script = {"answer": "项目回答。"}
    app = _app(database, tmp_path, export_root, factory)
    owner, owner_id = _login(app, database)
    projects = ProjectRepository(database)
    project = projects.create(owner_id, "共享项目")
    member, member_id = _login(app, database)
    projects.add_member(project.project_id, owner_id, member_id)
    session = WebSessionRepository(database).create(owner_id, project_id=project.project_id, title="项目会话")
    run_id = _run(owner, session.thread_id)

    # 成员看得见这个 run（trace 200），但导出是 owner 动作
    assert member.get(f"/api/runs/{run_id}/trace").status_code == 200
    response = member.get(f"/api/runs/{run_id}/export?format=docx")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "WEB_FORBIDDEN"


def test_outsider_gets_404_not_403(setup, database: Database) -> None:
    factory, app, client, _, thread_id = setup
    factory.script = {"answer": "私有回答。"}
    run_id = _run(client, thread_id)
    outsider, _ = _login(app, database)
    response = outsider.get(f"/api/runs/{run_id}/export?format=docx")
    assert response.status_code == 404, "看不见的 run 必须与不存在的 run 不可区分"


def test_unsupported_format_is_400(setup) -> None:
    _, _, client, _, _ = setup
    response = client.get(f"/api/runs/{uuid4()}/export?format=pdf")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WEB_EXPORT_FORMAT_UNSUPPORTED"


def test_run_from_another_process_is_404(
    setup, database: Database, tmp_path: Path, export_root: Path
) -> None:
    """重启后 run 结果不在内存：与 /trace 同一已知限制，如实 404。"""

    factory, _, client, user_id, thread_id = setup
    factory.script = {"answer": "重启前的回答。"}
    run_id = _run(client, thread_id)

    restarted_app = _app(database, tmp_path, export_root, _ScriptedFactory(tmp_path / "traces"))
    restarted = TestClient(restarted_app, raise_server_exceptions=True)
    username = PostgresUserStore(database).get_by_id(user_id).username
    assert restarted.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    ).status_code == 200
    response = restarted.get(f"/api/runs/{run_id}/export?format=docx")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WEB_EXPORT_UNAVAILABLE"


def test_failed_run_is_not_exportable(setup) -> None:
    factory, _, client, _, thread_id = setup
    factory.script = {"answer": "半截回答", "status": "failed"}
    run_id = _run(client, thread_id)
    response = client.get(f"/api/runs/{run_id}/export?format=docx")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WEB_EXPORT_UNAVAILABLE"


def test_chinese_filename_has_ascii_fallback_and_rfc5987() -> None:
    header = content_disposition("给水干管计算-1a2b3c4d.docx")
    assert header.startswith('attachment; filename="')
    fallback = header.split('"')[1]
    assert fallback == "1a2b3c4d.docx", "非 ASCII 字符剥掉后剩下 run 短 id 与扩展名"
    assert fallback.isascii()
    assert "filename*=UTF-8''%E7%BB%99" in header


def test_export_response_carries_the_session_title(setup) -> None:
    factory, _, client, _, thread_id = setup
    factory.script = {"answer": "回答。"}
    run_id = _run(client, thread_id)
    response = client.get(f"/api/runs/{run_id}/export?format=docx")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    from urllib.parse import unquote

    encoded = response.headers["content-disposition"].split("filename*=UTF-8''", 1)[1]
    assert unquote(encoded).startswith("给水干管计算-")
    assert response.headers["cache-control"] == "no-store"
