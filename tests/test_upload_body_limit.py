"""Upload body ceiling enforced below FastAPI: before auth, before reading.

The defect these tests pin down: FastAPI parses a multipart body
(``await request.form()`` in ``fastapi.routing.get_request_handler``) *before*
it solves the route's dependencies -- and authentication and CSRF are
dependencies.  ``_read_bounded`` could only answer 413 after Starlette had
already received the whole body and spooled the file part into a temp file
(rolled over to ``/tmp`` past 1 MB; a tmpfs in the container).  Measured on main
before the fix, with a 30 MiB file:

- anonymous, no cookie, no CSRF token: 403 ``AUTH_CSRF_INVALID`` -- after all
  31 457 280 file bytes had been received and written to a temp file;
- signed in: 413 from ``_read_bounded`` -- after the same 31 457 280 bytes.

An HTTP status alone cannot tell "refused before reading" from "read everything,
then refused"; the second one *is* the defect.  So these tests drive the real
``create_app()`` application at the ASGI level and count what it pulled from
``receive`` -- and, for the spool test, what it wrote into temp files.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import starlette.formparsers
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from water_agent.documents.extractors import MAX_UPLOAD_BYTES
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

_PASSWORD = "BodyLimitPass2026!"
_BOUNDARY = "wa-body-limit-test"
#: One ``http.request`` message from the scripted client -- the order of what a
#: real server hands an ASGI app per socket read.
_CHUNK = 64 * 1024
#: The reproduction size from the defect report: three times the per-file limit.
_HUGE = 30 * 1024 * 1024
_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures (same shape as tests/test_upload.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    engine = create_engine(f"sqlite:///{tmp_path / 'uploads.sqlite'}")

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
def upload_root(tmp_path: Path) -> Path:
    root = tmp_path / "uploads"
    root.mkdir()
    return root


@pytest.fixture
def app(database: Database, tmp_path: Path, upload_root: Path) -> FastAPI:
    return create_app(
        static_dir=tmp_path,
        database=database,
        memory_enabled=False,
        trace_dir=tmp_path / "traces",
        session_dir=tmp_path / "sessions",
        upload_dir=upload_root,
    )


@pytest.fixture
def spooled(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every temp file Starlette's multipart parser creates, with what went into it."""

    created: list[Any] = []

    class _CountingSpool(tempfile.SpooledTemporaryFile):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.bytes_written = 0
            self.rolled_to_disk = False
            created.append(self)

        def write(self, data: Any) -> int:
            self.bytes_written += len(data)
            return super().write(data)

        def rollover(self) -> None:
            self.rolled_to_disk = True
            super().rollover()

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", _CountingSpool)
    return created


def _login(app: FastAPI, database: Database) -> TestClient:
    username = f"limit-{uuid4()}"
    PostgresUserStore(database).create_user(username, _PASSWORD, UserRole.MEMBER)
    client = TestClient(app, raise_server_exceptions=True)
    response = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    assert response.status_code == 200, response.text
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    value = client.cookies.get(CSRF_COOKIE_NAME)
    assert value
    return {"X-CSRF-Token": value}


def _session_headers(client: TestClient) -> list[tuple[bytes, bytes]]:
    """A signed-in client's cookies and CSRF header, for a hand-driven ASGI request."""

    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    return [(b"cookie", cookie.encode()), (b"x-csrf-token", _csrf(client)["X-CSRF-Token"].encode())]


# ---------------------------------------------------------------------------
# ASGI-level driver: the only place where "how much was read" is observable
# ---------------------------------------------------------------------------

class _Client:
    """The ``receive`` side of one request.

    Serves the body message by message and records exactly how much of it the
    application pulled -- and whether some of it was never pulled at all.
    """

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self._next: bytes | None = next(self._chunks, b"")
        self.receive_calls = 0
        self.bytes_pulled = 0

    @property
    def body_left_unread(self) -> bool:
        return self._next is not None

    async def receive(self) -> dict[str, Any]:
        self.receive_calls += 1
        chunk = self._next
        if chunk is None:
            # Body fully delivered; a real server would now wait for the client
            # to go away.  Nothing in these requests waits for that.
            return {"type": "http.disconnect"}
        self._next = next(self._chunks, None)
        self.bytes_pulled += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": self._next is not None}


@dataclass
class _Result:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def payload(self) -> Any:
        return json.loads(self.body)

    @property
    def error_code(self) -> str | None:
        try:
            return self.payload["error"]["code"]
        except (ValueError, KeyError, TypeError):
            return None


def _scope(path: str, headers: list[tuple[bytes, bytes]], method: str = "POST") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


def _drive(app: Any, *, path: str, headers: list[tuple[bytes, bytes]], client: _Client) -> _Result:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(_scope(path, headers), client.receive, send))
    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert len(starts) == 1, f"一个请求只能有一个响应：{[message['type'] for message in sent]}"
    return _Result(
        status=starts[0]["status"],
        headers={name.decode().lower(): value.decode() for name, value in starts[0]["headers"]},
        body=b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"),
    )


def _multipart_head(filename: str) -> bytes:
    return (
        f"--{_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()


_MULTIPART_TAIL = f"\r\n--{_BOUNDARY}--\r\n".encode()


def _upload_body(file_bytes: int, filename: str = "huge.txt") -> tuple[int, Iterator[bytes]]:
    """A one-file multipart body as ``(total length, chunks)``, generated lazily."""

    head = _multipart_head(filename)

    def chunks() -> Iterator[bytes]:
        yield head
        block = b"x" * _CHUNK
        full, rest = divmod(file_bytes, _CHUNK)
        for _ in range(full):
            yield block
        if rest:
            yield b"x" * rest
        yield _MULTIPART_TAIL

    return len(head) + file_bytes + len(_MULTIPART_TAIL), chunks()


def _multipart_headers(*, content_length: int | None) -> list[tuple[bytes, bytes]]:
    """``content_length=None`` means chunked transfer: no length is declared."""

    headers = [(b"content-type", f"multipart/form-data; boundary={_BOUNDARY}".encode())]
    if content_length is None:
        headers.append((b"transfer-encoding", b"chunked"))
    else:
        headers.append((b"content-length", str(content_length).encode()))
    return headers


# ---------------------------------------------------------------------------
# 1. Declared oversized body: refused before a single byte is read
# ---------------------------------------------------------------------------

def test_oversized_content_length_is_refused_before_any_body_byte_is_read(app: FastAPI) -> None:
    """Anonymous, no cookie, no CSRF token, 30 MiB declared.

    Before the fix: 403 ``AUTH_CSRF_INVALID`` with the whole body already received.
    """

    total, chunks = _upload_body(_HUGE)
    client = _Client(chunks)
    result = _drive(
        app, path="/api/uploads", headers=_multipart_headers(content_length=total), client=client
    )

    assert result.status == 413, (result.status, result.body, client.bytes_pulled)
    assert result.error_code == "UPLOAD_TOO_LARGE"
    assert client.bytes_pulled == 0, f"拒绝之前已读了 {client.bytes_pulled} 字节 body"
    assert client.receive_calls == 0, "只凭声明的长度就该拒绝，receive 一次都不能调"
    # The rest of the body is never read, so the connection cannot carry another
    # request -- the server must not drain an unread body for as long as a client
    # cares to keep sending it.
    assert result.headers.get("connection") == "close"


# ---------------------------------------------------------------------------
# 2. No declared length (chunked): cut at the first message past the ceiling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("signed_in", [False, True], ids=["anonymous", "signed-in"])
def test_chunked_body_is_cut_as_soon_as_it_passes_the_ceiling(
    app: FastAPI, database: Database, signed_in: bool
) -> None:
    """Before the fix: anonymous got 403 and signed-in got 413 -- both only after
    the whole 30 MiB body had been received."""

    headers = _multipart_headers(content_length=None)
    if signed_in:
        headers += _session_headers(_login(app, database))
    _, chunks = _upload_body(_HUGE)
    client = _Client(chunks)
    result = _drive(app, path="/api/uploads", headers=headers, client=client)

    assert result.status == 413, (result.status, result.body, client.bytes_pulled)
    assert result.error_code == "UPLOAD_TOO_LARGE"
    assert client.body_left_unread, f"整个 body（{client.bytes_pulled} 字节）读完才拒绝"

    from water_agent.webapp.body_limit import UPLOAD_BODY_LIMIT_BYTES

    # Cut at the message that crossed the ceiling -- not one message later.
    assert UPLOAD_BODY_LIMIT_BYTES < client.bytes_pulled <= UPLOAD_BODY_LIMIT_BYTES + _CHUNK
    assert result.headers.get("connection") == "close"


# ---------------------------------------------------------------------------
# 3. Normal uploads: unchanged, on both paths, up to exactly the per-file limit
# ---------------------------------------------------------------------------

def test_normal_size_uploads_behave_exactly_as_before(
    app: FastAPI, database: Database, upload_root: Path
) -> None:
    from water_agent.webapp.body_limit import UploadBodyLimitMiddleware

    # The ceiling is in this very app -- otherwise the checks below prove nothing.
    assert UploadBodyLimitMiddleware in [middleware.cls for middleware in app.user_middleware]

    client = _login(app, database)
    payload = "\n\n".join(f"第 {index} 段：给水管道设计流速说明。" for index in range(1, 40)).encode()

    # Content-Length path, sent exactly the way tests/test_upload.py sends it.
    response = client.post(
        "/api/uploads",
        files={"file": ("notes.txt", payload, "text/plain")},
        headers=_csrf(client),
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["size_bytes"] == len(payload)
    assert data["chunk_count"] >= 2
    assert (upload_root / data["upload_id"] / "notes.txt").read_bytes() == payload

    # Chunked path in many small messages: the counting receive must hand every
    # byte through unchanged and in order.
    pieces = [_multipart_head("chunked.txt")]
    pieces += [payload[offset : offset + 97] for offset in range(0, len(payload), 97)]
    pieces.append(_MULTIPART_TAIL)
    stream = _Client(pieces)
    result = _drive(
        app,
        path="/api/uploads",
        headers=_multipart_headers(content_length=None) + _session_headers(client),
        client=stream,
    )
    assert result.status == 201, result.body
    assert not stream.body_left_unread
    upload_id = result.payload["data"]["upload_id"]
    assert (upload_root / upload_id / "chunked.txt").read_bytes() == payload

    # A file of exactly the per-file limit, under a long CJK name, clears the
    # framing allowance and is judged by the documents layer (here: not a ZIP),
    # not refused for its size.
    at_limit = client.post(
        "/api/uploads",
        files={"file": ("给排水设计说明" * 20 + ".docx", b"\0" * MAX_UPLOAD_BYTES, "application/octet-stream")},
        headers=_csrf(client),
    )
    assert at_limit.status_code == 400, at_limit.text
    assert at_limit.json()["error"]["code"] == "UPLOAD_MAGIC_MISMATCH"


# ---------------------------------------------------------------------------
# 4. Everything outside the upload prefix is passed through untouched
# ---------------------------------------------------------------------------

def test_routes_outside_the_upload_prefix_are_not_capped(app: FastAPI) -> None:
    # Only the upload route parses a form body before auth.  Read from the
    # OpenAPI schema, so a new multipart route elsewhere turns this red instead
    # of quietly living without a ceiling.
    schema = app.openapi()
    form_routes = sorted(
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if {"multipart/form-data", "application/x-www-form-urlencoded"}
        & set(operation.get("requestBody", {}).get("content", {}))
    )
    assert form_routes == [("POST", "/api/uploads")]

    size = 2 * MAX_UPLOAD_BYTES
    block = b"x" * _CHUNK

    def body() -> Iterator[bytes]:
        for _ in range(size // _CHUNK):
            yield block

    headers = [(b"content-type", b"application/json"), (b"content-length", str(size).encode())]

    # Control: aimed at the upload route, the very same request is refused unread.
    control = _Client(body())
    refused = _drive(app, path="/api/uploads", headers=headers, client=control)
    assert refused.status == 413, (refused.status, refused.body)
    assert control.bytes_pulled == 0

    # JSON routes keep reading their own body, however large; the ceiling is
    # not a global limit.
    for path in ("/api/sessions", "/api/memories/candidates", "/api/projects"):
        client = _Client(body())
        result = _drive(app, path=path, headers=headers, client=client)
        assert result.status != 413 and result.error_code != "UPLOAD_TOO_LARGE", (path, result.status)
        assert client.bytes_pulled == size, f"{path}：路由自己的 body 被截断了"

    # Lookalikes are not under the prefix.
    for path in ("/api/uploadsx", f"/api/projects/{uuid4()}/uploads"):
        result = _drive(app, path=path, headers=headers, client=_Client(body()))
        assert result.status != 413 and result.error_code != "UPLOAD_TOO_LARGE", (path, result.status)


# ---------------------------------------------------------------------------
# 5. An anonymous oversized upload no longer lands in a temp file
# ---------------------------------------------------------------------------

def test_anonymous_oversized_upload_is_not_spooled_any_more(
    app: FastAPI, spooled: list[Any]
) -> None:
    """Before the fix: 403 ``AUTH_CSRF_INVALID`` *after* the 31 457 280-byte file
    had been spooled and rolled over to a temp file (tmpfs in the container)."""

    # Probe check first.  Under the ceiling an anonymous body is still parsed
    # before auth -- FastAPI's order did not change -- so the probe does see a
    # spool, and an empty spool list below is a real "nothing was spooled".
    small = 2 * 1024 * 1024  # past Starlette's 1 MiB in-memory spool: rolls to disk
    total, chunks = _upload_body(small)
    result = _drive(
        app, path="/api/uploads", headers=_multipart_headers(content_length=total), client=_Client(chunks)
    )
    assert result.status == 403 and result.error_code == "AUTH_CSRF_INVALID", result.body
    assert [(spool.bytes_written, spool.rolled_to_disk) for spool in spooled] == [(small, True)]
    spooled.clear()

    # Declared oversized: refused without creating any temp file.
    total, chunks = _upload_body(_HUGE)
    result = _drive(
        app, path="/api/uploads", headers=_multipart_headers(content_length=total), client=_Client(chunks)
    )
    written = sum(spool.bytes_written for spool in spooled)
    assert written == 0, f"被拒绝的匿名请求仍有 {written} 字节写进了临时文件"
    assert spooled == []
    assert result.status == 413

    # Chunked oversized: what arrived before the cut is bounded by the ceiling,
    # and the temp file is closed when the read is aborted.
    from water_agent.webapp.body_limit import UPLOAD_BODY_LIMIT_BYTES

    _, chunks = _upload_body(_HUGE)
    result = _drive(
        app, path="/api/uploads", headers=_multipart_headers(content_length=None), client=_Client(chunks)
    )
    assert result.status == 413
    assert 0 < sum(spool.bytes_written for spool in spooled) <= UPLOAD_BODY_LIMIT_BYTES
    assert all(spool.closed for spool in spooled)


# ---------------------------------------------------------------------------
# Supporting invariants
# ---------------------------------------------------------------------------

def test_the_ceiling_answers_exactly_like_the_route_does(app: FastAPI, database: Database) -> None:
    """前端按 code 渲染、以 message 兜底：两层的 413 必须不可区分。"""

    client = _login(app, database)
    # Past the per-file limit but inside the framing allowance: only the route's
    # own `_read_bounded` sees it.
    route_413 = client.post(
        "/api/uploads",
        files={"file": ("big.txt", b"x" * (MAX_UPLOAD_BYTES + 1), "text/plain")},
        headers=_csrf(client),
    )
    assert route_413.status_code == 413, route_413.text

    # Far past the ceiling: answered before the body is read.
    total, chunks = _upload_body(_HUGE)
    stream = _Client(chunks)
    result = _drive(
        app,
        path="/api/uploads",
        headers=_multipart_headers(content_length=total) + _session_headers(client),
        client=stream,
    )
    assert stream.bytes_pulled == 0, f"拒绝之前已读了 {stream.bytes_pulled} 字节 body"
    assert result.status == route_413.status_code
    assert result.body == route_413.content
    assert result.headers["content-type"] == route_413.headers["content-type"]


def test_ceiling_is_the_per_file_limit_plus_a_framing_allowance_and_caddy_agrees() -> None:
    from water_agent.webapp.body_limit import MULTIPART_OVERHEAD_BYTES, UPLOAD_BODY_LIMIT_BYTES

    assert UPLOAD_BODY_LIMIT_BYTES == MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES
    # An allowance for the multipart envelope, not a second, looser file limit.
    assert 0 < MULTIPART_OVERHEAD_BYTES <= MAX_UPLOAD_BYTES // 100

    caddyfile = (_REPO_ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert re.search(r"^\s*@uploads path /api/uploads /api/uploads/\*\s*$", caddyfile, re.MULTILINE)
    block = re.search(r"request_body @uploads \{\s*max_size (\d+)KiB\s*\}", caddyfile)
    assert block, "deploy/Caddyfile 必须对 /api/uploads 单独设 request_body max_size"
    assert int(block.group(1)) * 1024 == UPLOAD_BODY_LIMIT_BYTES


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
def test_the_ceiling_itself_is_allowed_one_byte_more_is_not(declared: bool) -> None:
    from water_agent.webapp.body_limit import UploadBodyLimitMiddleware

    received: list[bytes] = []

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        received.append(body)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    limited = UploadBodyLimitMiddleware(downstream, max_body_bytes=10)
    for size, expected in ((10, 204), (11, 413)):
        received.clear()
        headers = (
            [(b"content-length", str(size).encode())]
            if declared
            else [(b"transfer-encoding", b"chunked")]
        )
        chunks = [b"a" * 4, b"a" * 4, b"a" * (size - 8)]
        result = _drive(limited, path="/api/uploads", headers=headers, client=_Client(chunks))
        assert result.status == expected, (size, result.status)
        assert received == ([b"a" * size] if expected == 204 else [])


def test_no_second_response_once_the_application_has_started_one() -> None:
    """An HTTP exchange has exactly one response.  If the application already
    started one when the ceiling trips (the upload route never does), nothing
    more is sent and the abort propagates so the server drops the connection."""

    from water_agent.webapp.body_limit import UploadBodyLimitMiddleware, _BodyTooLarge

    async def answers_early(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.body", "body": b""})

    limited = UploadBodyLimitMiddleware(answers_early, max_body_bytes=10)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    client = _Client([b"a" * 8, b"a" * 8, b"a" * 8])
    scope = _scope("/api/uploads", [(b"transfer-encoding", b"chunked")])
    with pytest.raises(_BodyTooLarge):
        asyncio.run(limited(scope, client.receive, send))
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert client.body_left_unread
