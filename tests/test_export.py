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


def _run(client: TestClient, thread_id: str, query: str = "请计算并出清单") -> str:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    response = client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": query},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["data"]["run_id"]
    # 读完事件流 = 等到 finalize
    assert client.get(f"/api/runs/{run_id}/events").status_code == 200
    return run_id


def _assert_no_staging_files(export_root: Path) -> None:
    assert {p.name for p in export_root.iterdir()} == {".snapshots"}
    assert all(p.is_file() and p.suffix == ".json" for p in (export_root / ".snapshots").iterdir())


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
    _assert_no_staging_files(export_root)


@pytest.mark.parametrize("range_header", ["bytes=999999999-", "bytes=abc", "bytes=0-9"])
def test_range_request_does_not_leave_a_staging_directory(
    setup, export_root: Path, range_header: str
) -> None:
    """回归：FileResponse 遇到非法 Range 直接回 416/400，不执行 background 清理任务。

    此前每个这样的请求在导出卷上留下一个暂存目录（实测 416 留 1 个、400 再留 1 个）。
    现在导出整份读进内存后即删目录；Range 不再被处理，总是 200 返回完整文件。
    """

    factory, _, client, _, thread_id = setup
    factory.script = {
        "answer": "材料清单见附件。",
        "messages": _material_messages(_ROWS, "call-m"),
    }
    run_id = _run(client, thread_id)

    response = client.get(
        f"/api/runs/{run_id}/export?format=xlsx", headers={"Range": range_header}
    )

    _assert_no_staging_files(export_root)
    assert response.status_code == 200, response.text
    assert "content-range" not in response.headers
    workbook = openpyxl.load_workbook(io.BytesIO(response.content))
    assert [cell.value for cell in workbook["材料清单"][2]][:3] == [1, "球墨铸铁管", "DN150"]


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


def test_answer_citing_an_undeclared_clause_is_refused_not_exported(
    setup, export_root: Path
) -> None:
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
    _assert_no_staging_files(export_root)


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
    app.state.run_manager._handles.clear()
    assert member.get(f"/api/runs/{run_id}/events").status_code == 200
    assert member.get(f"/api/runs/{run_id}/export?format=docx").status_code == 403


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


def test_run_from_another_process_exports_the_original_answer(
    setup, database: Database, tmp_path: Path, export_root: Path
) -> None:
    """同一会话的新回答不能覆盖较早run的导出；重开后仍取各自原稿。"""

    factory, _, client, user_id, thread_id = setup
    factory.script = {"answer": "重启前的回答。"}
    run_id = _run(client, thread_id)
    factory.script = {"answer": "第二轮的不同回答。"}
    later_run = _run(client, thread_id)

    restarted_app = _app(database, tmp_path, export_root, _ScriptedFactory(tmp_path / "traces"))
    restarted = TestClient(restarted_app, raise_server_exceptions=True)
    username = PostgresUserStore(database).get_by_id(user_id).username
    assert restarted.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    ).status_code == 200
    response = restarted.get(f"/api/runs/{run_id}/export?format=docx")
    assert response.status_code == 200, response.text
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(response.content)).paragraphs)
    assert "重启前的回答。" in text and "第二轮的不同回答。" not in text
    later = restarted.get(f"/api/runs/{later_run}/export?format=docx")
    assert later.status_code == 200
    assert "第二轮的不同回答。" in "\n".join(p.text for p in docx.Document(io.BytesIO(later.content)).paragraphs)


def test_restart_recovers_material_table_and_replays_final_exports(
    setup, database: Database, tmp_path: Path, export_root: Path,
) -> None:
    factory, _, client, user_id, thread_id = setup
    factory.script = {"answer": "材料清单见附件。", "messages": _material_messages(_ROWS, "m-persist")}
    run_id = _run(client, thread_id)
    before = client.get(f"/api/runs/{run_id}/events").text
    final_id = next(int(block.splitlines()[0].split(":")[1]) for block in before.split("\n\n") if "event: finalize" in block)
    restarted_app = _app(database, tmp_path, export_root, _ScriptedFactory(tmp_path / "traces"))
    restarted = TestClient(restarted_app)
    username = PostgresUserStore(database).get_by_id(user_id).username
    assert restarted.post("/api/auth/login", json={"username": username, "password": _PASSWORD}).status_code == 200
    response = restarted.get(f"/api/runs/{run_id}/export?format=xlsx")
    assert response.status_code == 200
    sheet = openpyxl.load_workbook(io.BytesIO(response.content))["材料清单"]
    assert sheet["E2"].value == 200.0
    replay = restarted.get(f"/api/runs/{run_id}/events")
    assert replay.status_code == 200 and "event: finalize" in replay.text
    assert '"export_formats":["docx","xlsx"]' in replay.text
    assert "event: tool_result" not in replay.text
    assert restarted.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(final_id)}).text == ""
    tail = restarted.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(final_id - 1)}).text
    assert "event: finalize" in tail and "event: stream_end" in tail


def test_failed_run_is_not_exportable(setup) -> None:
    factory, _, client, _, thread_id = setup
    factory.script = {"answer": "半截回答", "status": "failed"}
    run_id = _run(client, thread_id)
    response = client.get(f"/api/runs/{run_id}/export?format=docx")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WEB_EXPORT_UNAVAILABLE"


def test_snapshot_failure_keeps_answer_and_live_export_with_warning(setup, monkeypatch) -> None:
    factory, app, client, _, thread_id = setup
    factory.script = {"answer": "仍应交付的回答。"}
    def fail(_record):
        raise PermissionError("private-path-must-not-appear")
    monkeypatch.setattr(app.state.run_manager.export_store, "write", fail)
    run_id = _run(client, thread_id)
    stream = client.get(f"/api/runs/{run_id}/events").text
    assert "导出结果未能保存" in stream and "仍应交付的回答" in stream
    assert "private-path-must-not-appear" not in stream
    assert client.get(f"/api/runs/{run_id}/export?format=docx").status_code == 200


def test_unavailable_legacy_snapshot_does_not_reuse_the_next_run(setup) -> None:
    factory, app, client, _, thread_id = setup
    factory.script = {"answer": "旧run。"}
    run_id = _run(client, thread_id)
    (app.state.run_manager.export_store.root / f"{run_id}.json").unlink()
    factory.script = {"answer": "另一个run。"}
    _run(client, thread_id)
    app.state.run_manager._handles.clear()
    response = client.get(f"/api/runs/{run_id}/export?format=docx")
    assert response.status_code == 404


def test_corrupt_nested_snapshot_is_unavailable_not_a_server_error(setup) -> None:
    factory, app, client, _, thread_id = setup
    factory.script = {"answer": "材料清单。", "messages": _material_messages(_ROWS, "m-corrupt")}
    run_id = _run(client, thread_id)
    p = app.state.run_manager.export_store.root / f"{run_id}.json"
    saved = json.loads(p.read_text())
    saved["snapshot"]["material_table"]["rows"] = [{}]
    p.write_text(json.dumps(saved))
    app.state.run_manager._handles.clear()
    assert client.get(f"/api/runs/{run_id}/export?format=xlsx").status_code == 404
    assert client.get(f"/api/runs/{run_id}/events").status_code == 404


def test_restored_exports_check_owner_before_reading_any_snapshot(setup, database, monkeypatch) -> None:
    factory, app, client, _, thread_id = setup
    factory.script = {"answer": "私有导出内容。"}
    run_id = _run(client, thread_id)
    app.state.run_manager._handles.clear()
    outsider, _ = _login(app, database)
    monkeypatch.setattr(app.state.run_manager.export_store, "read", lambda *_: pytest.fail("unauthorized snapshot read"))
    assert outsider.get(f"/api/runs/{run_id}/export?format=docx").status_code == 404


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


@pytest.mark.parametrize("user_default", [False, True])
def test_darcy_receipt_result_formula_inputs_survive_word_and_restart(setup, user_default) -> None:
    from test_darcy_calculation import ARGS, NAME, QUERY, invoke, turn, validate
    factory, app, client, _, thread_id = setup
    query = QUERY.replace("，重力加速度9.81 m/s²", "") if user_default else QUERY
    call, observation = invoke(query=query)
    answer = validate(turn(query))["messages"][0].content
    factory.script = {"answer": answer, "messages": [AIMessage(content="", tool_calls=[call]), observation]}
    run_id = _run(client, thread_id)
    snapshot = app.state.run_manager.export_snapshot(UUID(run_id))
    assert snapshot["calculations"][0]["tool_name"] == NAME
    assert snapshot["calc_inputs"] == ARGS
    before = client.get(f"/api/runs/{run_id}/export?format=docx")
    assert before.status_code == 200, before.text

    def contents(response):
        doc = docx.Document(io.BytesIO(response.content))
        return ([p.text for p in doc.paragraphs], [[[c.text for c in row.cells] for row in table.rows] for table in doc.tables])

    paragraphs, tables = contents(before)
    assert any("hf = f" in text for text in paragraphs)
    assert any("未核验" in text for text in paragraphs)
    origin = "采用工具显式默认值" if user_default else "用户明确给定，未采用默认值"
    assert any(origin in text for text in paragraphs)
    assert len(tables) == 2
    assert ["gravity_mps2", "9.81", "m/s²"] in tables[0]
    assert ["head_loss_m", "2.038735983690112", "m"] in tables[1]
    # Lose all live handles, then reload only the original persisted run snapshot.
    app.state.run_manager._handles.clear()
    after = client.get(f"/api/runs/{run_id}/export?format=docx")
    assert after.status_code == 200, after.text
    assert contents(after) == (paragraphs, tables)
    assert app.state.run_manager.export_snapshot(UUID(run_id))["calc_inputs"] == ARGS


def test_diameter_receipt_result_inputs_word_and_persisted_run(setup, database, tmp_path, export_root) -> None:
    from test_diameter_calculation import ARGS, NAME, invoke, turn, validate
    factory, app, client, user_id, thread_id = setup
    call, observation = invoke()
    answer = validate(turn())["messages"][0].content
    factory.script = {"answer": answer, "messages": [AIMessage(content="", tool_calls=[call]), observation]}
    run_id = _run(client, thread_id)
    snapshot = app.state.run_manager.export_snapshot(UUID(run_id))
    assert snapshot["calculations"][0]["tool_name"] == NAME
    assert snapshot["calc_inputs"] == ARGS
    before = client.get(f"/api/runs/{run_id}/export?format=docx")
    assert before.status_code == 200, before.text

    def contents(response):
        document = docx.Document(io.BytesIO(response.content))
        return ([p.text for p in document.paragraphs], [[[c.text for c in r.cells] for r in t.rows] for t in document.tables])

    paragraphs, tables = contents(before)
    assert any("d = sqrt(4Q/(πv))" in p for p in paragraphs)
    assert any("未进行规范符合性判断" in p for p in paragraphs)
    assert ["flow_lps", "10", "L/s"] in tables[0]
    assert ["velocity_mps", "1", "m/s"] in tables[0]
    assert ["diameter_mm", "112.83791670955127", "mm"] in tables[1]
    outsider, _ = _login(app, database)
    assert outsider.get(f"/api/runs/{run_id}/export?format=docx").status_code == 404
    restarted_app = _app(database, tmp_path, export_root, _ScriptedFactory(tmp_path / "traces"))
    restarted = TestClient(restarted_app)
    username = PostgresUserStore(database).get_by_id(user_id).username
    assert restarted.post("/api/auth/login", json={"username": username, "password": _PASSWORD}).status_code == 200
    after = restarted.get(f"/api/runs/{run_id}/export?format=docx")
    assert after.status_code == 200
    assert contents(after) == (paragraphs, tables)
    assert restarted_app.state.run_manager.export_snapshot(UUID(run_id))["calc_inputs"] == ARGS


@pytest.mark.parametrize("mutation", ["no_receipt", "id", "args", "body"])
def test_forged_diameter_not_projected_as_calculation(setup, mutation):
    from test_diameter_calculation import invoke
    factory, app, client, _, thread_id = setup
    call, observation = invoke()
    if mutation == "no_receipt": observation.artifact = None
    if mutation == "id": observation.tool_call_id = "wrong"
    if mutation == "args": call["args"] = {"flow_lps": 20, "velocity_mps": 1}
    if mutation == "body": observation.content = observation.content.replace("112.83791670955127", "999")
    factory.script = {"answer": "参数需澄清。", "messages": [AIMessage(content="", tool_calls=[call]), observation]}
    run_id = _run(client, thread_id)
    snapshot = app.state.run_manager.export_snapshot(UUID(run_id))
    assert snapshot["calculations"] == [] and snapshot["calc_inputs"] == {}


def test_hazen_receipt_full_precision_word_and_restarted_app(setup, database, tmp_path, export_root):
    from test_hazen_calculation import ARGS, NAME, QUERY, invoke, turn, validate
    factory, app, client, user_id, thread_id = setup
    call, observation = invoke(); answer = validate(turn())['messages'][0].content
    factory.script = {'answer': answer, 'messages': [AIMessage(content='', tool_calls=[call]), observation]}
    run_id = _run(client, thread_id, QUERY)
    snapshot = app.state.run_manager.export_snapshot(UUID(run_id))
    assert snapshot['calculations'][0]['tool_name'] == NAME
    assert snapshot['calc_inputs'] == ARGS
    before = client.get(f'/api/runs/{run_id}/export?format=docx')
    assert before.status_code == 200, before.text
    def contents(response):
        document = docx.Document(io.BytesIO(response.content))
        return ([p.text for p in document.paragraphs], [[[c.text for c in row.cells] for row in t.rows] for t in document.tables])
    paragraphs,tables=contents(before)
    for fragment in ['10.67', '4.8704', '经验近似', '未核验', '10–20']:
        assert any(fragment in p for p in paragraphs)
    assert ['hazen_williams_c', '130', '无量纲'] in tables[0]
    value=json.loads(observation.content)['head_loss_m']
    assert ['head_loss_m', str(value), 'm'] in tables[1]
    outsider,_=_login(app,database)
    assert outsider.get(f'/api/runs/{run_id}/export?format=docx').status_code==404
    restarted_app=_app(database,tmp_path,export_root,_ScriptedFactory(tmp_path/'traces'))
    restarted=TestClient(restarted_app)
    username=PostgresUserStore(database).get_by_id(user_id).username
    assert restarted.post('/api/auth/login',json={'username':username,'password':_PASSWORD}).status_code==200
    after=restarted.get(f'/api/runs/{run_id}/export?format=docx')
    assert after.status_code==200 and contents(after)==(paragraphs,tables)
    assert restarted_app.state.run_manager.export_snapshot(UUID(run_id))['calc_inputs']==ARGS


def test_hazen_receipt_for_different_human_run_never_projected(setup):
    from test_hazen_calculation import invoke
    factory, app, client, _, thread_id = setup
    call, observation = invoke()
    factory.script = {"answer":"仅解释公式。", "messages":[AIMessage(content="",tool_calls=[call]),observation]}
    run_id = _run(client,thread_id,"不要计算，只解释这个公式。")
    snapshot = app.state.run_manager.export_snapshot(UUID(run_id))
    assert snapshot["calculations"] == [] and snapshot["calc_inputs"] == {}


def test_shevelev_receipt_full_precision_word_and_restarted_app(setup, database, tmp_path, export_root):
    from test_shevelev_calculation import ARGS, NAME, QUERY, invoke, turn, validate
    factory, app, client, user_id, thread_id = setup
    call, observation = invoke(); answer = validate(turn())['messages'][0].content
    factory.script = {'answer': answer, 'messages': [AIMessage(content='', tool_calls=[call]), observation]}
    run_id = _run(client, thread_id, QUERY)
    snapshot = app.state.run_manager.export_snapshot(UUID(run_id))
    assert snapshot['calculations'][0]['tool_name'] == NAME
    assert snapshot['calc_inputs'] == ARGS
    before = client.get(f'/api/runs/{run_id}/export?format=docx')
    assert before.status_code == 200, before.text
    def contents(response):
        document = docx.Document(io.BytesIO(response.content))
        return ([p.text for p in document.paragraphs], [[[c.text for c in row.cells] for row in t.rows] for t in document.tables])
    paragraphs,tables=contents(before)
    for fragment in ['v>=1.2', 'v<1.2', '0.00107', '0.000912', '0.867', '经验近似', '未核验', '10°C']:
        assert any(fragment in p for p in paragraphs)
    assert ['velocity_mps', '1.2', 'm/s'] in tables[0]
    value=json.loads(observation.content)['head_loss_m']
    assert ['head_loss_m', str(value), 'm'] in tables[1]
    outsider,_=_login(app,database)
    assert outsider.get(f'/api/runs/{run_id}/export?format=docx').status_code==404
    restarted_app=_app(database,tmp_path,export_root,_ScriptedFactory(tmp_path/'traces'))
    restarted=TestClient(restarted_app)
    username=PostgresUserStore(database).get_by_id(user_id).username
    assert restarted.post('/api/auth/login',json={'username':username,'password':_PASSWORD}).status_code==200
    after=restarted.get(f'/api/runs/{run_id}/export?format=docx')
    assert after.status_code==200 and contents(after)==(paragraphs,tables)
    assert restarted_app.state.run_manager.export_snapshot(UUID(run_id))['calc_inputs']==ARGS


def test_shevelev_receipt_for_different_human_run_never_projected(setup):
    from test_shevelev_calculation import invoke
    factory, app, client, _, thread_id = setup
    call, observation = invoke()
    factory.script = {"answer":"仅解释公式。", "messages":[AIMessage(content="",tool_calls=[call]),observation]}
    run_id = _run(client,thread_id,"不要计算，只解释这个公式。")
    snapshot = app.state.run_manager.export_snapshot(UUID(run_id))
    assert snapshot["calculations"] == [] and snapshot["calc_inputs"] == {}


def test_manning_receipt_full_precision_word_and_restarted_app(setup, database, tmp_path, export_root):
    from test_manning_calculation import ARGS, NAME, QUERY, invoke, turn, validate
    factory, app, client, user_id, thread_id = setup
    call, observation = invoke(); answer = validate(turn())['messages'][0].content
    factory.script = {'answer': answer, 'messages': [AIMessage(content='', tool_calls=[call]), observation]}
    run_id = _run(client, thread_id, QUERY)
    snapshot = app.state.run_manager.export_snapshot(UUID(run_id))
    assert snapshot['calculations'][0]['tool_name'] == NAME
    assert snapshot['calc_inputs'] == ARGS
    before = client.get(f'/api/runs/{run_id}/export?format=docx')
    assert before.status_code == 200, before.text
    def contents(response):
        document = docx.Document(io.BytesIO(response.content))
        return ([p.text for p in document.paragraphs], [[[c.text for c in row.cells] for row in t.rows] for t in document.tables])
    paragraphs,tables=contents(before)
    for fragment in ['θ', 'A=d²', 'P=d', 'R=A/P', '工程来源未核验', '适用性未独立核验', '未检索条文']:
        assert any(fragment in p for p in paragraphs)
    assert ['manning_n', '0.013', 's/m^(1/3)'] in tables[0]
    value=json.loads(observation.content)['slope']
    assert ['slope', str(value), 'm/m'] in tables[1]
    outsider,_=_login(app,database)
    assert outsider.get(f'/api/runs/{run_id}/export?format=docx').status_code==404
    restarted_app=_app(database,tmp_path,export_root,_ScriptedFactory(tmp_path/'traces'))
    restarted=TestClient(restarted_app)
    username=PostgresUserStore(database).get_by_id(user_id).username
    assert restarted.post('/api/auth/login',json={'username':username,'password':_PASSWORD}).status_code==200
    after=restarted.get(f'/api/runs/{run_id}/export?format=docx')
    assert after.status_code==200 and contents(after)==(paragraphs,tables)
    assert restarted_app.state.run_manager.export_snapshot(UUID(run_id))['calc_inputs']==ARGS


def test_manning_receipt_for_different_human_run_never_projected(setup):
    from test_manning_calculation import invoke
    factory, app, client, _, thread_id = setup
    call, observation = invoke()
    factory.script = {"answer":"仅解释公式。", "messages":[AIMessage(content="",tool_calls=[call]),observation]}
    run_id = _run(client,thread_id,"不要计算，只解释这个公式。")
    snapshot = app.state.run_manager.export_snapshot(UUID(run_id))
    assert snapshot["calculations"] == [] and snapshot["calc_inputs"] == {}


@pytest.mark.parametrize('zero',[False,True])
def test_pump_head_receipt_full_precision_word_and_restarted_app(setup, database, tmp_path, export_root,zero):
    from test_pump_head_calculation import ARGS, NAME, QUERY, invoke, turn, validate
    factory, app, client, user_id, thread_id = setup
    args={k:0 for k in ARGS} if zero else ARGS
    query='计算泵扬程。'+ '，'.join(f'{k}={v} m' for k,v in args.items()) if zero else QUERY
    call, observation = invoke(args,query); answer = validate(turn(query,args))['messages'][0].content
    factory.script = {'answer': answer, 'messages': [AIMessage(content='', tool_calls=[call]), observation]}
    run_id = _run(client, thread_id, query)
    snapshot = app.state.run_manager.export_snapshot(UUID(run_id))
    assert snapshot['calculations'][0]['tool_name'] == NAME
    assert snapshot['calc_inputs'] == args
    before = client.get(f'/api/runs/{run_id}/export?format=docx')
    assert before.status_code == 200, before.text
    def contents(response):
        document = docx.Document(io.BytesIO(response.content))
        return ([p.text for p in document.paragraphs], [[[c.text for c in row.cells] for row in t.rows] for t in document.tables])
    paragraphs,tables=contents(before)
    for fragment in ['H_static', 'H_friction', 'H_local', 'H_residual', '工程来源未核验', '适用性未独立核验', '未检索条文']:
        assert any(fragment in p for p in paragraphs)
    assert ['local_head_m', '0', 'm'] in tables[0]
    value=json.loads(observation.content)['pump_head_m']
    assert ['pump_head_m', '0' if zero else str(value), 'm'] in tables[1]
    outsider,_=_login(app,database)
    assert outsider.get(f'/api/runs/{run_id}/export?format=docx').status_code==404
    restarted_app=_app(database,tmp_path,export_root,_ScriptedFactory(tmp_path/'traces'))
    restarted=TestClient(restarted_app)
    username=PostgresUserStore(database).get_by_id(user_id).username
    assert restarted.post('/api/auth/login',json={'username':username,'password':_PASSWORD}).status_code==200
    after=restarted.get(f'/api/runs/{run_id}/export?format=docx')
    assert after.status_code==200 and contents(after)==(paragraphs,tables)
    assert restarted_app.state.run_manager.export_snapshot(UUID(run_id))['calc_inputs']==args
    # A fresh interpreter reads the on-disk T1 snapshot and renders the same Word.
    import subprocess
    import sys
    script = """
import sys
from pathlib import Path
from water_agent.documents.storage import ExportSnapshotStore
from water_agent.webapp.export_routes import _render_docx
record=ExportSnapshotStore(Path(sys.argv[1])).read(sys.argv[2])
assert record is not None
_render_docx(record['snapshot'],sys.argv[3],Path(sys.argv[4]))
"""
    out=tmp_path/'child.docx'
    title=docx.Document(io.BytesIO(before.content)).paragraphs[0].text
    subprocess.run([sys.executable,'-c',script,str(app.state.run_manager.export_store.root),run_id,title,str(out)],check=True)
    child=docx.Document(out)
    assert ([p.text for p in child.paragraphs],[[[c.text for c in r.cells] for r in t.rows] for t in child.tables])==(paragraphs,tables)



def test_pump_head_receipt_for_different_human_run_never_projected(setup):
    from test_pump_head_calculation import invoke
    factory, app, client, _, thread_id = setup
    call, observation = invoke()
    factory.script = {"answer":"仅解释公式。", "messages":[AIMessage(content="",tool_calls=[call]),observation]}
    run_id = _run(client,thread_id,"不要计算，只解释这个公式。")
    snapshot = app.state.run_manager.export_snapshot(UUID(run_id))
    assert snapshot["calculations"] == [] and snapshot["calc_inputs"] == {}


def test_two_pump_runs_keep_independent_words_on_same_thread(setup,database,tmp_path,export_root):
    from test_pump_head_calculation import ARGS, QUERY, invoke, turn, validate
    factory,_app_original,client,user_id,thread_id=setup
    records=[]
    for args,query in [(ARGS,QUERY),({k:0 for k in ARGS},'计算泵扬程。'+ '，'.join(f'{k}=0 m' for k in ARGS))]:
        call,obs=invoke(args,query)
        factory.script={'answer':validate(turn(query,args))['messages'][0].content,'messages':[AIMessage(content='',tool_calls=[call]),obs]}
        run=_run(client,thread_id,query)
        response=client.get(f'/api/runs/{run}/export?format=docx')
        assert response.status_code==200
        records.append((run,args,response.content))
    restarted_app=_app(database,tmp_path,export_root,_ScriptedFactory(tmp_path/'traces'))
    restarted=TestClient(restarted_app)
    username=PostgresUserStore(database).get_by_id(user_id).username
    assert restarted.post('/api/auth/login',json={'username':username,'password':_PASSWORD}).status_code==200
    for run,args,before in records:
        after=restarted.get(f'/api/runs/{run}/export?format=docx')
        assert after.status_code==200
        a,b=docx.Document(io.BytesIO(before)),docx.Document(io.BytesIO(after.content))
        assert [p.text for p in a.paragraphs]==[p.text for p in b.paragraphs]
        assert [[[c.text for c in r.cells] for r in t.rows] for t in a.tables]==[[[c.text for c in r.cells] for r in t.rows] for t in b.tables]
        assert restarted_app.state.run_manager.export_snapshot(UUID(run))['calc_inputs']==args
