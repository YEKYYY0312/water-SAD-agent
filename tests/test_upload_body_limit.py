"""Upload body guards enforced below FastAPI: before auth, before reading.

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

The byte ceiling bounded that, but under the ceiling the order was unchanged:
an anonymous 5 MiB upload was still received and spooled in full (5 242 880
bytes into a temp file) before the route's dependencies answered 403 -- every
anonymous request could still make the server take ~10 MB.  The pre-auth check
replays the upload route's own auth dependencies on the headers alone and
refuses before the first body byte; the route's dependencies stay the authority.

The same order bites every JSON route: FastAPI reads a JSON body with ``await
request.body()`` -- whole, into memory -- before the route's dependencies run,
and nothing capped it: 30 MiB to ``POST /api/sessions`` were read in full and
only then answered 422.  Every other write now has one request-body ceiling, also
below FastAPI.

An HTTP status alone cannot tell "refused before reading" from "read everything,
then refused"; the second one *is* the defect.  So these tests drive the real
``create_app()`` application at the ASGI level and count what it pulled from
``receive`` -- and, for the spool tests, what it wrote into temp files.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import starlette.formparsers
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError

from water_agent.documents.extractors import MAX_UPLOAD_BYTES
from water_agent.storage.database import Database
from water_agent.storage.models import AuthSession, Base
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME, validate_csrf
from water_agent.webapp.auth.db_store import PostgresSessionStore, PostgresUserStore
from water_agent.webapp.auth.dependencies import require_password_changed
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.auth.session_store import SessionNotFoundError

_PASSWORD = "BodyLimitPass2026!"
_BOUNDARY = "wa-body-limit-test"
#: One ``http.request`` message from the scripted client -- the order of what a
#: real server hands an ASGI app per socket read.
_CHUNK = 64 * 1024
#: The reproduction size from the defect report: three times the per-file limit.
_HUGE = 30 * 1024 * 1024
#: Under the ceiling, past Starlette's 1 MiB in-memory spool: before the pre-auth
#: check an anonymous body of this size was received in full and rolled to disk.
_UNDER_CEILING = 5 * 1024 * 1024
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
def route_only_app(database: Database, tmp_path: Path, upload_root: Path) -> FastAPI:
    """The same application without the pre-auth check: the route answers alone.

    Its answers are the reference the pre-auth check must reproduce byte for
    byte -- after reading the whole body, which is exactly what it must not do.
    Filtered by name so that this fixture builds the same app before the check
    exists.
    """

    control = create_app(
        static_dir=tmp_path,
        database=database,
        memory_enabled=False,
        trace_dir=tmp_path / "traces-control",
        session_dir=tmp_path / "sessions-control",
        upload_dir=upload_root,
    )
    control.user_middleware = [
        middleware
        for middleware in control.user_middleware
        if getattr(middleware.cls, "__name__", "") != "UploadPreauthMiddleware"
    ]
    return control


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


def _login(app: FastAPI, database: Database, role: UserRole = UserRole.MEMBER) -> TestClient:
    username = f"limit-{uuid4()}"
    PostgresUserStore(database).create_user(username, _PASSWORD, role)
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


def _drive(
    app: Any,
    *,
    path: str,
    headers: list[tuple[bytes, bytes]],
    client: _Client,
    method: str = "POST",
) -> _Result:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(_scope(path, headers, method), client.receive, send))
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


def _framing(content_length: int | None) -> tuple[bytes, bytes]:
    """``content_length=None`` means chunked transfer: no length is declared."""

    if content_length is None:
        return (b"transfer-encoding", b"chunked")
    return (b"content-length", str(content_length).encode())


def _multipart_headers(*, content_length: int | None) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-type", f"multipart/form-data; boundary={_BOUNDARY}".encode()),
        _framing(content_length),
    ]


def _json_headers(*, content_length: int | None) -> list[tuple[bytes, bytes]]:
    return [(b"content-type", b"application/json"), _framing(content_length)]


def _blocks(size: int) -> Iterator[bytes]:
    """``size`` bytes of filler, one ``_CHUNK`` per message."""

    full, rest = divmod(size, _CHUNK)
    for _ in range(full):
        yield b"x" * _CHUNK
    if rest:
        yield b"x" * rest


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

def test_chunked_body_is_cut_as_soon_as_it_passes_the_ceiling(
    app: FastAPI, database: Database
) -> None:
    """Before the fix: 413 only after the whole 30 MiB body had been received.

    Signed in, so that the request gets past the pre-auth check and the ceiling
    is what stops it; an anonymous chunked body is not read at all any more
    (``test_anonymous_upload_under_the_ceiling_is_refused_before_its_body_is_read``).
    """

    headers = _multipart_headers(content_length=None) + _session_headers(_login(app, database))
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
    from water_agent.webapp.body_limit import RequestBodyLimitMiddleware

    # The ceiling is in this very app -- otherwise the checks below prove nothing.
    assert RequestBodyLimitMiddleware in [middleware.cls for middleware in app.user_middleware]

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
# 4. Outside the upload prefix: the request ceiling, not the upload one
# ---------------------------------------------------------------------------

def test_routes_outside_the_upload_prefix_get_the_request_ceiling_not_the_upload_one(
    app: FastAPI,
) -> None:
    """Before the request ceiling every JSON route here read all 20 MiB first and
    only then answered 422.  Now there are two ceilings: the upload one for the
    upload prefix, the request one for every other write -- both bite unread."""

    # Only the upload route parses a form body before auth.  Read from the
    # OpenAPI schema, so a new multipart route elsewhere turns this red instead
    # of quietly living under the (much smaller) request ceiling.
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
    headers = _json_headers(content_length=size)

    # Aimed at the upload route: the upload ceiling.
    control = _Client(_blocks(size))
    refused = _drive(app, path="/api/uploads", headers=headers, client=control)
    assert (refused.status, refused.error_code) == (413, "UPLOAD_TOO_LARGE"), refused.body
    assert control.bytes_pulled == 0

    # JSON routes and lookalike paths: the request ceiling, equally unread.
    for path in (
        "/api/sessions",
        "/api/memories/candidates",
        "/api/projects",
        "/api/uploadsx",
        f"/api/projects/{uuid4()}/uploads",
    ):
        stream = _Client(_blocks(size))
        result = _drive(app, path=path, headers=headers, client=stream)
        assert (result.status, result.error_code) == (413, "WEB_REQUEST_TOO_LARGE"), (
            path,
            result.status,
            stream.bytes_pulled,
        )
        assert stream.receive_calls == 0, path

    # Between the two ceilings: too large for a JSON route, not for an upload --
    # the upload route judges that request by its credentials, not its size.
    from water_agent.webapp.body_limit import REQUEST_BODY_LIMIT_BYTES, UPLOAD_BODY_LIMIT_BYTES

    between = REQUEST_BODY_LIMIT_BYTES + 1
    assert between < UPLOAD_BODY_LIMIT_BYTES
    json_route = _drive(
        app, path="/api/sessions", headers=_json_headers(content_length=between), client=_Client(_blocks(between))
    )
    assert json_route.error_code == "WEB_REQUEST_TOO_LARGE"
    upload_route = _drive(
        app, path="/api/uploads", headers=_json_headers(content_length=between), client=_Client(_blocks(between))
    )
    assert upload_route.error_code == "AUTH_CSRF_INVALID", upload_route.body


# ---------------------------------------------------------------------------
# 5. An anonymous oversized upload no longer lands in a temp file
# ---------------------------------------------------------------------------

def test_anonymous_oversized_upload_is_not_spooled_any_more(
    app: FastAPI, database: Database, spooled: list[Any]
) -> None:
    """Before the fix: 403 ``AUTH_CSRF_INVALID`` *after* the 31 457 280-byte file
    had been spooled and rolled over to a temp file (tmpfs in the container)."""

    # Probe check first.  A signed-in body under the ceiling is still parsed by
    # FastAPI before the route runs, so the probe does see a spool, and an empty
    # spool list below is a real "nothing was spooled".  (Not a real .docx: the
    # documents layer refuses it by magic number -- i.e. past both guards.)
    signed_in = _session_headers(_login(app, database))
    small = 2 * 1024 * 1024  # past Starlette's 1 MiB in-memory spool: rolls to disk
    total, chunks = _upload_body(small, filename="probe.docx")
    result = _drive(
        app,
        path="/api/uploads",
        headers=_multipart_headers(content_length=total) + signed_in,
        client=_Client(chunks),
    )
    assert (result.status, result.error_code) == (400, "UPLOAD_MAGIC_MISMATCH"), result.body
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

    # Chunked oversized and anonymous: no length to judge by, so the pre-auth
    # check is what refuses it -- still before a single temp-file byte.
    _, chunks = _upload_body(_HUGE)
    result = _drive(
        app, path="/api/uploads", headers=_multipart_headers(content_length=None), client=_Client(chunks)
    )
    assert result.status == 403 and result.error_code == "AUTH_CSRF_INVALID", result.body
    assert spooled == []

    # Chunked oversized and signed in: what arrived before the cut is bounded by
    # the ceiling, and the temp file is closed when the read is aborted.
    from water_agent.webapp.body_limit import UPLOAD_BODY_LIMIT_BYTES

    _, chunks = _upload_body(_HUGE)
    result = _drive(
        app,
        path="/api/uploads",
        headers=_multipart_headers(content_length=None) + signed_in,
        client=_Client(chunks),
    )
    assert result.status == 413
    assert 0 < sum(spool.bytes_written for spool in spooled) <= UPLOAD_BODY_LIMIT_BYTES
    assert all(spool.closed for spool in spooled)


# ---------------------------------------------------------------------------
# 6. Under the ceiling: an upload its own dependencies refuse is refused unread
# ---------------------------------------------------------------------------

#: Every way the upload route's dependencies refuse a request, with the answer
#: they give: CSRF first (route-level ``dependencies``), then the session
#: (``require_password_changed``).
_REFUSALS: dict[str, tuple[int, str, str]] = {
    "no-cookie-no-token": (403, "AUTH_CSRF_INVALID", "CSRF token missing"),
    "token-cookie-only": (403, "AUTH_CSRF_INVALID", "CSRF token missing"),
    "token-mismatch": (403, "AUTH_CSRF_INVALID", "CSRF token mismatch"),
    "no-session": (401, "AUTH_SESSION_INVALID", "Not authenticated"),
    "unknown-session": (401, "AUTH_SESSION_INVALID", "Session not found or expired"),
    "expired-session": (401, "AUTH_SESSION_INVALID", "Session expired"),
    "inactive-user": (403, "AUTH_USER_INACTIVE", "Account is disabled"),
    "must-change-password": (
        403,
        "AUTH_MUST_CHANGE_PASSWORD",
        "Temporary password must be changed before continuing",
    ),
}


def _cookies(session_id: str | None, csrf: str, *, header: str | None) -> list[tuple[bytes, bytes]]:
    jar = [f"{CSRF_COOKIE_NAME}={csrf}"] + ([f"wa_session={session_id}"] if session_id else [])
    headers = [(b"cookie", "; ".join(jar).encode())]
    if header is not None:
        headers.append((b"x-csrf-token", header.encode()))
    return headers


def _refused_credentials(database: Database, case: str) -> tuple[list[tuple[bytes, bytes]], str | None]:
    """Fresh cookies and headers for one refusal case, plus the session they name.

    Fresh per call: the route deletes an expired session on first sight, so each
    run needs its own.
    """

    if case == "no-cookie-no-token":
        return [], None
    if case == "token-cookie-only":
        return _cookies(None, "token", header=None), None
    if case == "token-mismatch":
        return _cookies(None, "token", header="another-token"), None
    if case == "no-session":
        return _cookies(None, "token", header="token"), None
    if case == "unknown-session":
        return _cookies("no-such-session", "token", header="token"), "no-such-session"
    users = PostgresUserStore(database)
    user = users.create_user(
        f"refused-{uuid4()}",
        _PASSWORD,
        UserRole.MEMBER,
        must_change_password=case == "must-change-password",
    )
    session = PostgresSessionStore(database).create(user.user_id)
    if case == "expired-session":
        with database.transaction() as transaction:
            row = transaction.get(AuthSession, session.session_id)
            assert row is not None
            row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    elif case == "inactive-user":
        users.set_active(user.user_id, active=False)
    else:
        assert case == "must-change-password", case
    return _cookies(session.session_id, session.csrf_token, header=session.csrf_token), session.session_id


@pytest.fixture
def session_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every session id the auth layer looks up, in order."""

    looked_up: list[str] = []
    original = PostgresSessionStore.get

    def counting_get(self: PostgresSessionStore, session_id: str) -> Any:
        looked_up.append(session_id)
        return original(self, session_id)

    monkeypatch.setattr(PostgresSessionStore, "get", counting_get)
    return looked_up


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
@pytest.mark.parametrize(
    ("credentials", "status", "code"),
    [
        # No cookie and no token at all: CSRF is the route's first dependency.
        ("none", 403, "AUTH_CSRF_INVALID"),
        # A self-made, matching double-submit pair gets past CSRF, not past the session.
        ("forged-csrf-pair", 401, "AUTH_SESSION_INVALID"),
    ],
)
def test_anonymous_upload_under_the_ceiling_is_refused_before_its_body_is_read(
    app: FastAPI, spooled: list[Any], declared: bool, credentials: str, status: int, code: str
) -> None:
    """Before the fix: the same 403/401 -- after all 5 MiB had been received and
    5 242 880 bytes written to a temp file rolled over to disk."""

    total, chunks = _upload_body(_UNDER_CEILING)
    headers = _multipart_headers(content_length=total if declared else None)
    if credentials == "forged-csrf-pair":
        headers += _cookies(None, "forged", header="forged")
    stream = _Client(chunks)
    result = _drive(app, path="/api/uploads", headers=headers, client=stream)

    assert (result.status, result.error_code) == (status, code), result.body
    assert stream.receive_calls == 0, f"拒绝之前已读了 {stream.bytes_pulled} 字节 body"
    assert stream.bytes_pulled == 0
    assert spooled == [], f"被拒绝的匿名上传写了 {sum(s.bytes_written for s in spooled)} 字节临时文件"
    if declared:
        # A declared remainder is bounded by the ceiling: the server discards it
        # and keeps the connection, so a client still writing its body gets to
        # read this answer instead of a reset.
        assert "connection" not in result.headers
    else:
        # No length: the remainder is unbounded, and the connection is not reused.
        assert result.headers.get("connection") == "close"


@pytest.mark.parametrize(
    ("token", "message"),
    [(None, "CSRF token missing"), ("not-the-cookie-value", "CSRF token mismatch")],
    ids=["missing", "mismatch"],
)
def test_signed_in_upload_without_a_matching_csrf_header_is_refused_before_its_body_is_read(
    app: FastAPI, database: Database, spooled: list[Any], token: str | None, message: str
) -> None:
    """A valid session is not enough: without the double-submit header the route
    answers 403 -- before the fix, after the whole body had been spooled."""

    client = _login(app, database)
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    headers = [(b"cookie", cookie.encode())]
    if token is not None:
        headers.append((b"x-csrf-token", token.encode()))
    total, chunks = _upload_body(_UNDER_CEILING)
    stream = _Client(chunks)
    result = _drive(
        app,
        path="/api/uploads",
        headers=_multipart_headers(content_length=total) + headers,
        client=stream,
    )

    assert result.status == 403, result.body
    assert result.payload["error"] == {"code": "AUTH_CSRF_INVALID", "message": message}
    assert stream.receive_calls == 0, f"拒绝之前已读了 {stream.bytes_pulled} 字节 body"
    assert spooled == []


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_every_refusal_is_the_routes_own_answer_given_before_the_body_is_read(
    app: FastAPI, route_only_app: FastAPI, database: Database, case: str
) -> None:
    """Same status, same bytes, same side effects as the route's dependencies --
    which answer only after the whole body has been read."""

    status, code, message = _REFUSALS[case]
    answers: dict[str, tuple[_Result, _Client, str | None]] = {}
    for name, target in (("route", route_only_app), ("guarded", app)):
        headers, session_id = _refused_credentials(database, case)
        total, chunks = _upload_body(256 * 1024)
        stream = _Client(chunks)
        result = _drive(
            target,
            path="/api/uploads",
            headers=_multipart_headers(content_length=total) + headers,
            client=stream,
        )
        answers[name] = (result, stream, session_id)

    route, route_stream, _ = answers["route"]
    guarded, guarded_stream, guarded_session = answers["guarded"]
    # The reference: the route alone reads everything, then its dependencies refuse.
    assert route.status == status, route.body
    assert route.payload["error"] == {"code": code, "message": message}
    assert not route_stream.body_left_unread
    # The pre-auth check: the very same answer, without a single body byte.
    assert guarded_stream.receive_calls == 0, f"拒绝之前已读了 {guarded_stream.bytes_pulled} 字节 body"
    assert guarded.status == route.status
    assert guarded.body == route.body
    assert guarded.headers == route.headers
    if case == "expired-session":
        # Same side effect as the route: an expired session is deleted on sight.
        assert guarded_session is not None
        with pytest.raises(SessionNotFoundError):
            PostgresSessionStore(database).get(guarded_session)


def test_a_session_store_failure_answers_500_as_the_route_would(
    app: FastAPI,
    route_only_app: FastAPI,
    database: Database,
    spooled: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: the lookup error surfaces exactly as it does in the route's
    dependency -- the application's 500 -- only without parsing the body first."""

    def unavailable(self: PostgresSessionStore, session_id: str) -> Any:
        raise OperationalError("SELECT auth_sessions", {}, Exception("database is down"))

    monkeypatch.setattr(PostgresSessionStore, "get", unavailable)
    answers: dict[str, tuple[int, Any, int]] = {}
    for name, target in (("route", route_only_app), ("guarded", app)):
        client = TestClient(target, raise_server_exceptions=False)
        client.cookies.set(CSRF_COOKIE_NAME, "t")
        client.cookies.set("wa_session", "some-session")
        response = client.post(
            "/api/uploads",
            files={"file": ("notes.txt", b"x" * (2 * 1024 * 1024), "text/plain")},
            headers={"X-CSRF-Token": "t"},
        )
        answers[name] = (response.status_code, response.json(), len(spooled))
        spooled.clear()

    internal_error = {
        "version": "v1",
        "ok": False,
        "error": {"code": "WEB_INTERNAL_ERROR", "message": "Internal server error"},
    }
    assert answers["route"] == (500, internal_error, 1), answers["route"]
    assert answers["guarded"] == (500, internal_error, 0), answers["guarded"]


def test_valid_uploads_pass_through_and_the_route_still_decides(
    app: FastAPI, database: Database, upload_root: Path, session_lookups: list[str]
) -> None:
    """The check only refuses; it hands nothing to the route.  The route resolves
    the session again and applies its own rules (a viewer is refused there)."""

    from water_agent.webapp.body_limit import UploadPreauthMiddleware

    assert UploadPreauthMiddleware in [middleware.cls for middleware in app.user_middleware]

    member = _login(app, database)
    session_lookups.clear()
    payload = "给水管道设计流速说明。".encode() * 50
    response = member.post(
        "/api/uploads", files={"file": ("notes.txt", payload, "text/plain")}, headers=_csrf(member)
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["data"]["upload_id"]
    assert (upload_root / upload_id / "notes.txt").read_bytes() == payload
    # Once by the pre-auth check, once by the route's own dependency.
    session_id = member.cookies.get("wa_session")
    assert session_lookups == [session_id, session_id]

    # A viewer's session is valid, so the check lets the request through; the
    # role is the route's business, decided after the body has been read.
    viewer = _session_headers(_login(app, database, role=UserRole.VIEWER))
    total, chunks = _upload_body(64 * 1024)
    stream = _Client(chunks)
    result = _drive(
        app, path="/api/uploads", headers=_multipart_headers(content_length=total) + viewer, client=stream
    )
    assert result.status == 403 and result.error_code == "WEB_FORBIDDEN", result.body
    assert not stream.body_left_unread
    assert "connection" not in result.headers


def test_only_the_upload_form_route_is_checked_before_its_body(app: FastAPI) -> None:
    """The check replays one route's dependencies; anything else answers for itself.

    Chunked on purpose: the check's refusal of a body without a declared length
    is the one that says ``Connection: close``, which tells the answers apart.
    """

    def anonymous(path: str, method: str) -> tuple[_Result, _Client]:
        _, chunks = _upload_body(64 * 1024)
        stream = _Client(chunks)
        result = _drive(
            app,
            path=path,
            method=method,
            headers=_multipart_headers(content_length=None),
            client=stream,
        )
        return result, stream

    # Positive control: the form route, refused by the check -- unread, and the
    # connection not reused.
    refused, stream = anonymous("/api/uploads", "POST")
    assert refused.status == 403 and refused.error_code == "AUTH_CSRF_INVALID"
    assert stream.receive_calls == 0
    assert refused.headers.get("connection") == "close"

    # GET /api/uploads has no CSRF dependency: the route says 401 where the
    # check would have said 403.
    listed, _ = anonymous("/api/uploads", "GET")
    assert (listed.status, listed.error_code) == (401, "AUTH_SESSION_INVALID")
    # DELETE carries the same dependencies, but takes no body: the route answers.
    deleted, _ = anonymous(f"/api/uploads/{uuid4()}", "DELETE")
    assert (deleted.status, deleted.error_code) == (403, "AUTH_CSRF_INVALID")
    answered_by_the_route = [listed, deleted]
    # Lookalike paths are other routes, or none.
    for path in ("/api/uploadsx", f"/api/projects/{uuid4()}/uploads"):
        other, _ = anonymous(path, "POST")
        assert other.status in (404, 405), (path, other.status, other.body)
        answered_by_the_route.append(other)
    for result in answered_by_the_route:
        assert "connection" not in result.headers, result


def test_the_check_replays_exactly_the_upload_routes_dependencies() -> None:
    """If the route's auth chain changes, the replay must change with it.

    Read from the route as declared (``app.routes`` holds included routers
    lazily in this FastAPI); ``create_app`` includes the router with no extra
    dependencies, and the equivalence test above compares the actual answers.
    """

    from water_agent.webapp.body_limit import UPLOAD_AUTH_DEPENDENCIES
    from water_agent.webapp.upload_routes import router

    (route,) = [
        candidate
        for candidate in router.routes
        if isinstance(candidate, APIRoute)
        and candidate.path == "/api/uploads"
        and "POST" in candidate.methods
    ]
    chain = [dependency.call for dependency in route.dependant.dependencies]
    assert chain == [validate_csrf, require_password_changed]
    assert tuple(chain) == UPLOAD_AUTH_DEPENDENCIES, (
        "body_limit.UploadPreauthMiddleware 重放的依赖必须与上传路由声明的一致"
    )


def test_overridden_auth_dependencies_are_left_to_the_route(app: FastAPI, database: Database) -> None:
    """The route's dependencies are the authority.  Where an application replaces
    one (FastAPI's ``dependency_overrides``), the check stands aside instead of
    judging by the originals."""

    def forged_session_upload() -> tuple[_Result, _Client]:
        total, chunks = _upload_body(64 * 1024)
        stream = _Client(chunks)
        result = _drive(
            app,
            path="/api/uploads",
            headers=_multipart_headers(content_length=total) + _cookies(None, "t", header="t"),
            client=stream,
        )
        return result, stream

    refused, stream = forged_session_upload()
    assert (refused.status, refused.error_code) == (401, "AUTH_SESSION_INVALID")
    assert stream.receive_calls == 0

    user = PostgresUserStore(database).create_user(f"override-{uuid4()}", _PASSWORD, UserRole.MEMBER)
    app.dependency_overrides[require_password_changed] = lambda: (user, None)
    try:
        accepted, stream = forged_session_upload()
    finally:
        app.dependency_overrides.clear()
    assert accepted.status == 201, accepted.body
    assert not stream.body_left_unread


def test_declared_size_is_judged_before_the_session_is_looked_up(
    app: FastAPI, session_lookups: list[str]
) -> None:
    """Cheapest first: the declared length (free), then the session (a database
    lookup), and only then the body."""

    from water_agent.webapp.body_limit import RequestBodyLimitMiddleware, UploadPreauthMiddleware

    # Outermost first: the ceiling wraps the check.
    assert [middleware.cls for middleware in app.user_middleware][:2] == [
        RequestBodyLimitMiddleware,
        UploadPreauthMiddleware,
    ]

    credentials = _cookies("no-such-session", "t", header="t")
    total, chunks = _upload_body(_HUGE)
    stream = _Client(chunks)
    too_large = _drive(
        app, path="/api/uploads", headers=_multipart_headers(content_length=total) + credentials, client=stream
    )
    assert (too_large.status, too_large.error_code) == (413, "UPLOAD_TOO_LARGE")
    assert session_lookups == [], "超限的声明长度不该花一次会话查询"
    assert stream.receive_calls == 0

    total, chunks = _upload_body(64 * 1024)
    stream = _Client(chunks)
    unknown = _drive(
        app, path="/api/uploads", headers=_multipart_headers(content_length=total) + credentials, client=stream
    )
    assert (unknown.status, unknown.error_code) == (401, "AUTH_SESSION_INVALID")
    assert session_lookups == ["no-such-session"]
    assert stream.receive_calls == 0


# ---------------------------------------------------------------------------
# 7. Every other write: one request-body ceiling, also below FastAPI
# ---------------------------------------------------------------------------

_TOO_LARGE_JSON = {
    "version": "v1",
    "ok": False,
    "error": {"code": "WEB_REQUEST_TOO_LARGE", "message": "Request body is too large"},
}


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
def test_oversized_json_is_refused_before_its_body_is_read(app: FastAPI, declared: bool) -> None:
    """The defect report: 30 MiB to ``POST /api/sessions`` was read in full --
    into memory, before auth -- and only then answered 422."""

    stream = _Client(_blocks(_HUGE))
    result = _drive(
        app,
        path="/api/sessions",
        headers=_json_headers(content_length=_HUGE if declared else None),
        client=stream,
    )

    assert result.status == 413, (result.status, result.body, stream.bytes_pulled)
    assert result.payload == _TOO_LARGE_JSON
    assert result.headers.get("connection") == "close"

    from water_agent.webapp.body_limit import REQUEST_BODY_LIMIT_BYTES

    if declared:
        assert stream.receive_calls == 0, f"拒绝之前已读了 {stream.bytes_pulled} 字节 body"
    else:
        assert stream.body_left_unread, f"整个 body（{stream.bytes_pulled} 字节）读完才拒绝"
        assert REQUEST_BODY_LIMIT_BYTES < stream.bytes_pulled <= REQUEST_BODY_LIMIT_BYTES + _CHUNK


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/auth/login"),
        ("PUT", "/api/settings/credentials/deepseek_api_key"),
        ("PATCH", "/api/memories/00000000-0000-4000-8000-000000000000"),
        ("DELETE", "/api/memories/00000000-0000-4000-8000-000000000000"),
        ("POST", "/api/drawings/generate"),
    ],
)
def test_every_write_method_gets_the_request_ceiling(app: FastAPI, method: str, path: str) -> None:
    """POST, PUT, PATCH and DELETE alike -- signed in or not, body param or not."""

    stream = _Client(_blocks(_HUGE))
    result = _drive(
        app, path=path, method=method, headers=_json_headers(content_length=_HUGE), client=stream
    )
    assert (result.status, result.error_code) == (413, "WEB_REQUEST_TOO_LARGE"), (
        result.status,
        result.body,
        stream.bytes_pulled,
    )
    assert stream.receive_calls == 0


def test_a_route_that_reads_its_own_body_is_cut_as_well(app: FastAPI, database: Database) -> None:
    """``POST /api/drawings/generate`` awaits ``request.body()`` itself, after its
    auth dependencies.  Before the fix a signed-in chunked 30 MiB body was read in
    full there and answered 422 ``GEN_INVALID_INPUT``."""

    signed_in = _session_headers(_login(app, database))
    stream = _Client(_blocks(_HUGE))
    result = _drive(
        app,
        path="/api/drawings/generate",
        headers=_json_headers(content_length=None) + signed_in,
        client=stream,
    )
    assert (result.status, result.error_code) == (413, "WEB_REQUEST_TOO_LARGE"), (
        result.status,
        result.body,
        stream.bytes_pulled,
    )
    assert stream.body_left_unread


def test_normal_json_requests_are_unchanged(app: FastAPI, database: Database) -> None:
    client = _login(app, database)
    created = client.post("/api/sessions", json={"title": "给水校核"}, headers=_csrf(client))
    assert created.status_code == 201, created.text

    # The largest legitimate body: a 16 000-character message of astral
    # characters escaped as surrogate pairs (12 bytes each), plus the maximum of
    # five attachment ids.  Parsed, validated and handed to the route, which
    # answers for an unknown thread with its own 404.
    body = json.dumps(
        {"message": "\U0001f6b0" * 16_000, "attachments": [str(uuid4()) for _ in range(5)]}
    ).encode()
    assert len(body) > 192_000
    stream = _Client(body[offset : offset + _CHUNK] for offset in range(0, len(body), _CHUNK))
    result = _drive(
        app,
        path=f"/api/sessions/{uuid4()}/messages",
        headers=_json_headers(content_length=len(body)) + _session_headers(client),
        client=stream,
    )
    assert (result.status, result.error_code) == (404, "WEB_NOT_FOUND"), result.body
    assert not stream.body_left_unread

    # Positive control: one byte past the ceiling, same route, same caller.
    from water_agent.webapp.body_limit import REQUEST_BODY_LIMIT_BYTES

    over = REQUEST_BODY_LIMIT_BYTES + 1
    stream = _Client(_blocks(over))
    refused = _drive(
        app,
        path=f"/api/sessions/{uuid4()}/messages",
        headers=_json_headers(content_length=over) + _session_headers(client),
        client=stream,
    )
    assert (refused.status, refused.error_code) == (413, "WEB_REQUEST_TOO_LARGE")
    assert stream.receive_calls == 0


def test_reads_are_not_capped(app: FastAPI) -> None:
    """No read declares a request body, and FastAPI never reads one for a route
    without body parameters: GET, HEAD and OPTIONS pass through as before."""

    for method, expected in (("GET", 401), ("HEAD", 405), ("OPTIONS", 405)):
        stream = _Client(_blocks(_HUGE))
        result = _drive(
            app, path="/api/sessions", method=method, headers=_json_headers(content_length=_HUGE), client=stream
        )
        assert result.status == expected, (method, result.status, result.body)
        assert stream.receive_calls == 0, method

    # Positive control: the same request as a write is refused for its size.
    stream = _Client(_blocks(_HUGE))
    written = _drive(app, path="/api/sessions", headers=_json_headers(content_length=_HUGE), client=stream)
    assert (written.status, written.error_code) == (413, "WEB_REQUEST_TOO_LARGE"), written.body

    # Read from the OpenAPI schema, so that a read route given a body one day
    # turns this red instead of quietly reading it without a ceiling.
    from water_agent.webapp.body_limit import WRITE_METHODS

    schema = app.openapi()
    reads_with_a_body = sorted(
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if "requestBody" in operation and method.upper() not in WRITE_METHODS
    )
    assert reads_with_a_body == []


#: Worst-case bytes per character of a JSON string: an astral character a client
#: escapes as a surrogate pair (``\ud83d\udeb0``) still counts once for
#: ``max_length``.
_WORST_BYTES_PER_CHARACTER = 12
#: Generous sizes for what a legitimate client sends as a scalar.
_SCALAR_BYTES = {"boolean": 5, "integer": 20, "number": 32, "null": 4}
_FORMAT_BYTES = {"uuid": 38, "date-time": 66}


def _worst_json_bytes(schema: dict[str, Any], definitions: dict[str, Any]) -> int | None:
    """An upper bound on the JSON encoding of any value ``schema`` admits; ``None`` = unbounded."""

    if "$ref" in schema:
        return _worst_json_bytes(definitions[schema["$ref"].rsplit("/", 1)[-1]], definitions)
    if "anyOf" in schema:
        options = [_worst_json_bytes(option, definitions) for option in schema["anyOf"]]
        return None if None in options else max(option for option in options if option is not None)
    if "enum" in schema or "const" in schema:
        values = schema.get("enum", [schema.get("const")])
        return max(len(json.dumps(value)) for value in values)
    kind = schema.get("type")
    if kind == "string":
        if schema.get("format") in _FORMAT_BYTES:
            return _FORMAT_BYTES[schema["format"]]
        if "maxLength" not in schema:
            return None
        return 2 + _WORST_BYTES_PER_CHARACTER * int(schema["maxLength"])
    if kind == "array":
        item = _worst_json_bytes(schema["items"], definitions)
        if item is None or "maxItems" not in schema:
            return None
        return 2 + int(schema["maxItems"]) * (item + 1)
    if kind == "object":
        total = 2
        for name, member in schema.get("properties", {}).items():
            size = _worst_json_bytes(member, definitions)
            if size is None:
                return None
            total += len(json.dumps(name)) + 1 + size + 1
        return total
    return _SCALAR_BYTES[str(kind)]


def test_request_ceiling_leaves_room_for_the_largest_legitimate_body(app: FastAPI) -> None:
    """The basis of ``REQUEST_BODY_LIMIT_BYTES``, recomputed from the request models.

    A field that raises a model's worst case past a quarter of the ceiling, or a
    new body field without a declared bound, turns this red.
    """

    from water_agent.drawing.generation_models import GenerationRequest
    from water_agent.webapp.body_limit import REQUEST_BODY_LIMIT_BYTES

    schema = app.openapi()
    components = schema["components"]["schemas"]
    worst: dict[str, int | None] = {}
    for operations in schema["paths"].values():
        for operation in operations.values():
            content = operation.get("requestBody", {}).get("content", {})
            if "application/json" in content:
                reference = content["application/json"]["schema"]["$ref"]
                worst[reference.rsplit("/", 1)[-1]] = _worst_json_bytes(
                    content["application/json"]["schema"], components
                )
    # The drawing route reads its body itself, so it is not in the schema.
    drawing = GenerationRequest.model_json_schema()
    worst["GenerationRequest"] = _worst_json_bytes(drawing, drawing.get("$defs", {}))

    unbounded = sorted(name for name, size in worst.items() if size is None)
    # Usernames and passwords: no declared limit, a few hundred bytes in practice
    # (users.username is varchar(128)); the ceiling is now their bound.
    assert unbounded == ["_ChangePassBody", "_CreateUserBody", "_LoginBody", "_ResetPasswordBody"]
    bounded = {name: size for name, size in worst.items() if size is not None}
    largest = max(bounded, key=lambda name: bounded[name])
    assert largest == "MessageRequest", bounded
    assert 4 * bounded[largest] <= REQUEST_BODY_LIMIT_BYTES, bounded
    # Not a second upload ceiling either.
    assert REQUEST_BODY_LIMIT_BYTES <= MAX_UPLOAD_BYTES // 8


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


#: Each ceiling, by a path it applies to: (path, constructor keyword, 413 code).
_CEILINGS = [
    ("/api/uploads", "upload_limit_bytes", "UPLOAD_TOO_LARGE"),
    ("/api/sessions", "request_limit_bytes", "WEB_REQUEST_TOO_LARGE"),
]


@pytest.mark.parametrize(("path", "keyword", "code"), _CEILINGS, ids=["upload", "request"])
@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
def test_the_ceiling_itself_is_allowed_one_byte_more_is_not(
    declared: bool, path: str, keyword: str, code: str
) -> None:
    from water_agent.webapp.body_limit import RequestBodyLimitMiddleware

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

    limited = RequestBodyLimitMiddleware(downstream, **{keyword: 10})
    for size, expected in ((10, 204), (11, 413)):
        received.clear()
        headers = [_framing(size if declared else None)]
        chunks = [b"a" * 4, b"a" * 4, b"a" * (size - 8)]
        result = _drive(limited, path=path, headers=headers, client=_Client(chunks))
        assert result.status == expected, (size, result.status)
        assert received == ([b"a" * size] if expected == 204 else [])
        if expected == 413:
            assert result.error_code == code


@pytest.mark.parametrize(
    ("path", "keyword"), [ceiling[:2] for ceiling in _CEILINGS], ids=["upload", "request"]
)
def test_no_second_response_once_the_application_has_started_one(path: str, keyword: str) -> None:
    """An HTTP exchange has exactly one response.  If the application already
    started one when the ceiling trips (no route here does), nothing more is
    sent and the abort propagates so the server drops the connection."""

    from water_agent.webapp.body_limit import RequestBodyLimitMiddleware, _BodyTooLarge

    async def answers_early(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.body", "body": b""})

    limited = RequestBodyLimitMiddleware(answers_early, **{keyword: 10})
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    client = _Client([b"a" * 8, b"a" * 8, b"a" * 8])
    scope = _scope(path, [(b"transfer-encoding", b"chunked")])
    with pytest.raises(_BodyTooLarge):
        asyncio.run(limited(scope, client.receive, send))
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert client.body_left_unread
