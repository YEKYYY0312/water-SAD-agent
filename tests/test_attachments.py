"""P5-S49 A4a：显式附件 = 真正"读这个文件"。

事故（本地 compose 截图）：用户问"我给你 CAD 设计图的 PDF，你能读取并提建议吗"，
agent 答"我没有文件解析能力"。抽取器其实早把文字入库了，但：消息体不带附件 id、
召回只认字面重合（"总结这份文件"与任何文档零重合）、系统提示从未提过上传渠道。

这里守住：
- 点名的附件**无论问题怎么问**都按原文顺序进提示词（零字面重合也在）；
- 读不全时明说"未读全"，不让模型以为读完了；
- 0 字的 PDF（扫描件/CAD 矢量图）注入"读不到"的结构化说明，而不是什么都不给；
- 授权：别人的 upload_id 与不存在的 id 同答 404，且不留下 queued run；
- 装配：文件能力说明由运行时推导（read_drawing 是否注册），只在 Web 路径渲染。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select

from water_agent.documents.attachments import (
    ATTACHMENT_CONTEXT_PREAMBLE,
    MAX_ATTACHMENTS_PER_MESSAGE,
    AttachmentContext,
    read_attachments,
)
from water_agent.documents.repository import UploadRepository
from water_agent.prompting.assembler import SEGMENT_BUDGETS, PromptAssembler
from water_agent.storage.database import Database
from water_agent.storage.models import Base, Run
from water_agent.storage.repository import ResourceNotFoundError, WebSessionRepository
from water_agent.tools.registry import ToolRegistry
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

_PASSWORD = "AttachPass2026!"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'attach.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    yield db
    db.dispose()


class _RecordingSession:
    supports_attachment_context = True

    def __init__(self, recorder) -> None:
        self.recorder = recorder
        self.attachment_contexts: list[object] = []

    def start_trace(self, run_id: str, query: str) -> None:
        self.recorder.start(run_id=run_id, trace_id=f"trace-{run_id}", query=query)

    async def aask(self, query, *, run_id, memory_context, on_token, on_event, cancel_check, **extra):
        del query, run_id, memory_context, cancel_check
        self.attachment_contexts.append(extra.get("attachment_context"))
        on_event("values", {"status": "running", "messages": []})
        on_token("回答")
        self.recorder.finish("succeeded")
        return {"status": "succeeded", "answer": "回答", "citations": [], "errors": [], "warnings": []}


class _LegacySession(_RecordingSession):
    """不声明支持的会话（评测/测试夹具）：绝不能收到 attachment_context 关键字。"""

    supports_attachment_context = False

    async def aask(self, query, *, run_id, memory_context, on_token, on_event, cancel_check, **extra):
        assert "attachment_context" not in extra
        return await super().aask(
            query, run_id=run_id, memory_context=memory_context,
            on_token=on_token, on_event=on_event, cancel_check=cancel_check, **extra,
        )


class _Factory:
    def __init__(self, trace_dir: Path, session_cls=_RecordingSession) -> None:
        self.trace_dir = trace_dir
        self.session_cls = session_cls
        self.sessions: list[_RecordingSession] = []

    def create(self, *, run_id, thread_id, user_id, principal, project_id, stack):
        from water_agent.observability.json_trace import JsonTraceRecorder

        del run_id, thread_id, user_id, principal, project_id, stack
        session = self.session_cls(JsonTraceRecorder(self.trace_dir))
        self.sessions.append(session)
        return session


def _client(database: Database, tmp_path: Path, factory) -> tuple[TestClient, UUID]:
    upload_root = tmp_path / "uploads"
    upload_root.mkdir(exist_ok=True)
    username = f"attach-{uuid4()}"
    user = PostgresUserStore(database).create_user(username, _PASSWORD, UserRole.MEMBER)
    app = create_app(
        static_dir=tmp_path,
        database=database,
        session_factory=factory,
        memory_enabled=False,
        trace_dir=tmp_path / "traces",
        session_dir=tmp_path / "sessions",
        upload_dir=upload_root,
    )
    client = TestClient(app, raise_server_exceptions=True)
    assert client.post("/api/auth/login", json={"username": username, "password": _PASSWORD}).status_code == 200
    return client, user.user_id


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {"X-CSRF-Token": token}


def _upload(client: TestClient, filename: str, body: bytes, mime: str = "text/plain") -> str:
    response = client.post("/api/uploads", files={"file": (filename, body, mime)}, headers=_csrf(client))
    assert response.status_code == 201, response.text
    return response.json()["data"]["upload_id"]


def _ask(client: TestClient, thread_id: str, message: str, attachments: list[str]):
    return client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": message, "attachments": attachments},
        headers=_csrf(client),
    )


def _events(client: TestClient, run_id: str) -> list[dict]:
    response = client.get(f"/api/runs/{run_id}/events")
    events = []
    for block in response.text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


_DESIGN_NOTE = (
    "工程概况：本工程为 18 层住宅，地下 1 层。\n\n"
    "给水系统：市政直供 1-3 层，4 层以上由变频泵组加压供水。\n\n"
    "热水系统：采用集中热水，闭式循环。\n\n"
    "排水系统：污废合流，伸顶通气。"
)


# ---------------------------------------------------------------------------
# 1. The named file reaches the model, whatever the question looks like
# ---------------------------------------------------------------------------


def test_attached_file_is_injected_even_with_zero_literal_overlap(database: Database, tmp_path: Path) -> None:
    factory = _Factory(tmp_path / "traces")
    client, user_id = _client(database, tmp_path, factory)
    upload_id = _upload(client, "设计说明.txt", _DESIGN_NOTE.encode("utf-8"))
    thread = WebSessionRepository(database).create(user_id, title="附件")

    response = _ask(client, thread.thread_id, "帮我看看这份文件，给点建议", [upload_id])
    assert response.status_code == 202, response.text
    events = _events(client, response.json()["data"]["run_id"])

    context = factory.sessions[-1].attachment_contexts[-1]
    assert isinstance(context, AttachmentContext)
    text = context.context_text
    assert text.startswith(ATTACHMENT_CONTEXT_PREAMBLE.rstrip())
    for fragment in ("18 层住宅", "变频泵组", "闭式循环", "伸顶通气"):
        assert fragment in text, "点名的附件必须按原文进提示词，与问题是否字面重合无关"
    assert "已读全" in text

    read = [e for e in events if e["type"] == "attachment_read"]
    assert len(read) == 1 and read[0]["payload"]["upload_ids"] == [upload_id]
    assert read[0]["payload"]["files"][0]["complete"] is True
    # SSE 进浏览器：只带计数与 id，绝不带正文
    assert "闭式循环" not in json.dumps(read, ensure_ascii=False)


def test_long_attachment_declares_it_was_not_read_in_full(database: Database, tmp_path: Path) -> None:
    factory = _Factory(tmp_path / "traces")
    client, user_id = _client(database, tmp_path, factory)
    paragraphs = [f"第{i}节：管道敷设要求说明，编号 S{i:03d}，" + "施工内容描述" * 40 for i in range(60)]
    upload_id = _upload(client, "长说明.txt", "\n\n".join(paragraphs).encode("utf-8"))
    thread = WebSessionRepository(database).create(user_id, title="长附件")

    response = _ask(client, thread.thread_id, "总结一下", [upload_id])
    assert response.status_code == 202
    _events(client, response.json()["data"]["run_id"])

    context = factory.sessions[-1].attachment_contexts[-1]
    reading = context.readings[0]
    assert not reading.complete
    assert "未读全" in context.context_text
    assert f"共 {reading.total_chunks} 段" in context.context_text
    assert "S000" in context.context_text, "开头必须先读：目录与工程概况几乎总在前面"
    assert len(context.context_text) <= context.character_budget


def test_long_attachment_also_reads_the_chunks_the_question_is_about(database: Database, tmp_path: Path) -> None:
    factory = _Factory(tmp_path / "traces")
    client, user_id = _client(database, tmp_path, factory)
    paragraphs = [f"第{i}节：一般施工说明，" + "常规内容描述" * 40 for i in range(60)]
    paragraphs[45] = "第45节：屋面雨水采用虹吸排水系统，溢流口设于女儿墙。"
    upload_id = _upload(client, "长说明.txt", "\n\n".join(paragraphs).encode("utf-8"))
    thread = WebSessionRepository(database).create(user_id, title="长附件")

    response = _ask(client, thread.thread_id, "这份文件里屋面雨水用的什么排水系统", [upload_id])
    _events(client, response.json()["data"]["run_id"])
    assert "虹吸排水" in factory.sessions[-1].attachment_contexts[-1].context_text


def test_textless_pdf_injects_an_honest_cannot_read_note(database: Database, tmp_path: Path) -> None:
    """CAD 导出的矢量 PDF / 扫描件：0 字。必须明说读不到，不能什么都不给。"""

    from water_agent.documents.chunking import UploadChunk  # noqa: F401 -- 与入库同一模型

    factory = _Factory(tmp_path / "traces")
    client, user_id = _client(database, tmp_path, factory)
    record = UploadRepository(database).create(
        upload_id=str(uuid4()),
        tenant_id=uuid4(),
        owner_user_id=user_id,
        project_id=None,
        filename="给水平面图.pdf",
        safe_filename="给水平面图.pdf",
        suffix=".pdf",
        size_bytes=2048,
        content_hash="0" * 64,
        extracted_chars=0,
        page_count=1,
        extract_engine="pypdf",
        suspicious=False,
        redacted=False,
        chunks=[],
    )
    thread = WebSessionRepository(database).create(user_id, title="CAD PDF")

    response = _ask(client, thread.thread_id, "你能读取这张图纸并提出建议吗", [record.upload_id])
    assert response.status_code == 202
    _events(client, response.json()["data"]["run_id"])

    text = factory.sessions[-1].attachment_contexts[-1].context_text
    assert "给水平面图.pdf" in text
    assert "未抽取到任何文字" in text
    assert "不得猜测文件内容" in text


# ---------------------------------------------------------------------------
# 2. Authorization: 404 without leaking existence, and no queued run left behind
# ---------------------------------------------------------------------------


def _run_count(database: Database) -> int:
    with database.transaction() as session:
        return int(session.execute(select(func.count()).select_from(Run)).scalar_one())


def test_another_users_upload_cannot_be_attached(database: Database, tmp_path: Path) -> None:
    factory = _Factory(tmp_path / "traces")
    owner, _ = _client(database, tmp_path, factory)
    foreign_id = _upload(owner, "别人的.txt", "机密工程资料".encode())

    intruder, intruder_id = _client(database, tmp_path, factory)
    thread = WebSessionRepository(database).create(intruder_id, title="越权")
    before = _run_count(database)

    foreign = _ask(intruder, thread.thread_id, "读这个", [foreign_id])
    missing = _ask(intruder, thread.thread_id, "读这个", [str(uuid4())])
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"]
    assert _run_count(database) == before, "被拒绝的请求不得留下 queued run"


def test_attachment_count_is_bounded_by_the_schema(database: Database, tmp_path: Path) -> None:
    factory = _Factory(tmp_path / "traces")
    client, user_id = _client(database, tmp_path, factory)
    thread = WebSessionRepository(database).create(user_id, title="超限")
    too_many = [str(uuid4()) for _ in range(MAX_ATTACHMENTS_PER_MESSAGE + 1)]
    assert _ask(client, thread.thread_id, "读这些", too_many).status_code == 422


def test_read_attachments_refuses_invisible_ids_directly(database: Database, tmp_path: Path) -> None:
    factory = _Factory(tmp_path / "traces")
    owner, _ = _client(database, tmp_path, factory)
    upload_id = _upload(owner, "a.txt", "内容".encode())
    _, other_id = _client(database, tmp_path, factory)
    with pytest.raises(ResourceNotFoundError):
        read_attachments(database, actor_user_id=other_id, upload_ids=[upload_id], query="")


def test_sessions_that_do_not_declare_support_never_get_the_keyword(database: Database, tmp_path: Path) -> None:
    factory = _Factory(tmp_path / "traces", session_cls=_LegacySession)
    client, user_id = _client(database, tmp_path, factory)
    upload_id = _upload(client, "a.txt", "内容".encode())
    thread = WebSessionRepository(database).create(user_id, title="协议")
    response = _ask(client, thread.thread_id, "读这个", [upload_id])
    events = _events(client, response.json()["data"]["run_id"])
    assert events[-1]["payload"].get("status") == "succeeded"


# ---------------------------------------------------------------------------
# 3. Assembly: the capability note is derived from the runtime, Web-only
# ---------------------------------------------------------------------------


def test_file_context_segment_is_web_only() -> None:
    prompt = PromptAssembler(tool_registry=ToolRegistry()).assemble_prompt({"messages": []})
    assert "【文件能力】" not in prompt.content, "CLI/harness 不传 attachment_context，提示词不得变化"


def test_capability_note_renders_even_before_anything_is_uploaded() -> None:
    """截图里的能力询问恰恰发生在还没上传任何文件时。"""

    prompt = PromptAssembler(tool_registry=ToolRegistry()).assemble_prompt(
        {"messages": [], "attachment_context": AttachmentContext()}
    )
    content = prompt.content
    assert "【文件能力】" in content
    assert "Word(.docx)、PDF、TXT" in content
    assert "已上传资料：暂无" in content
    assert "没有文字层" in content
    assert "网页端暂不支持" in content, "read_drawing 未注册时不得声称能读 DXF"
    assert "用户随本条消息附上了以下文件" not in content, "没有附件时不得渲染附件段"


def test_capability_note_follows_the_read_drawing_registration(tmp_path: Path) -> None:
    from water_agent.drawing.synth import build_synthetic_drawing
    from water_agent.tools.read_drawing import maybe_build_read_drawing_tool

    root = tmp_path / "drawings"
    build_synthetic_drawing(root, drawing_id="P-01")
    tool, _ = maybe_build_read_drawing_tool(str(root))
    assert tool is not None
    registry = ToolRegistry()
    registry.register(tool)
    content = PromptAssembler(tool_registry=registry).assemble_prompt(
        {"messages": [], "attachment_context": AttachmentContext()}
    ).content
    assert "可用 read_drawing" in content
    assert "网页端暂不支持" not in content


def test_file_context_rejects_other_types() -> None:
    with pytest.raises(TypeError):
        PromptAssembler().assemble_prompt({"messages": [], "attachment_context": "伪造的附件正文"})


def test_file_context_budget_is_declared() -> None:
    assert SEGMENT_BUDGETS["file_context"] > SEGMENT_BUDGETS["upload_context"]
