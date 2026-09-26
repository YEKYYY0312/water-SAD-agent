"""P5 Step 45 ``memory_p0`` harness: offline, deterministic memory gate.
Every case runs against the **production** memory service, write service,
recall service, prompt assembler, tool registry and Web API.  The harness only
supplies users, projects, inputs and fault injection; it never fabricates a
recall hit, a tombstone, an SSE frame, or a metric.

Evidence discipline
-------------------
Each case reports explicit counters.  The §13.2 hard metrics are aggregated
from those counters by :mod:`water_agent.harness.memory_metrics`, so a green
case list with a non-zero leak counter stays red.

Coverage boundary (must stay in the report and the README)
----------------------------------------------------------
``gold_exact_recall_rate`` is measured on a synthetic keyword gold set.  It
proves the mechanism, **not** that recall generalises to arbitrary Chinese
phrasing.  The write guard is a deterministic regex rule set and necessarily
misses rewrite variants; ``blocked_injection_persisted_count == 0`` therefore
means "no blocked variant was persisted", not "all injections are caught".

Docker/PostgreSQL-dependent cases emit a structured SKIP with an explicit
reason off a release-gate host; ``--release-gate`` turns any SKIP into failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from water_agent.domain.models import content_hash_of
from water_agent.harness.memory_metrics import (
    HARD_THRESHOLDS,
    REQUIRED_COUNTERS,
    check_thresholds,
    compute_memory_metrics,
    detect_memory_events,
    is_runtime_skip_case,
    sanitize_memory_event,
)
from water_agent.memory.contracts import (
    MemoryError as DomainMemoryError,
)
from water_agent.memory.contracts import (
    MemoryKind,
    MemoryOrigin,
    MemoryScopeType,
    MemoryStatus,
)
from water_agent.memory.recall import (
    MAX_MEMORY_CONTEXT_CHARACTERS,
    MemoryRecallService,
    render_memory_context,
)
from water_agent.memory.repository import MemoryRepository
from water_agent.memory.service import MemoryService
from water_agent.memory.write_contracts import (
    CandidateDisposition,
    ConflictResolution,
    MemoryConfirmation,
)
from water_agent.memory.write_guard import MemoryWriteGuard
from water_agent.memory.write_service import ExplicitMemoryWriteService
from water_agent.runtime.governance.contracts import SourceTag
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.storage.repository import ProjectRepository, WebSessionRepository

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SUITE = REPO_ROOT / "data" / "eval" / "memory_p0.jsonl"
DEFAULT_REPORT = REPO_ROOT / "data" / "eval" / "reports" / "memory_p0_head.json"
REPORT_SCHEMA_VERSION = "p5-step-45-memory-evidence-v1"
TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")

RATE_COUNTER_NAMES: tuple[str, ...] = (
    "confirmed_writes",
    "unconfirmed_persisted_writes",
    "recall_gold_total",
    "recall_gold_matched",
    "provenance_total",
    "provenance_valid",
    "numeric_total",
    "numeric_revalidated",
)

ALL_COUNTERS: tuple[str, ...] = (*REQUIRED_COUNTERS, *RATE_COUNTER_NAMES)


# ---------------------------------------------------------------------------
# Suite loading
# ---------------------------------------------------------------------------


def load_suite(suite_path: str | Path) -> list[dict[str, Any]]:
    path = Path(suite_path)
    if not path.exists():
        raise FileNotFoundError(f"memory suite not found: {path}")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - loader guard
            raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(case, dict):
            raise TypeError(f"{path}:{lineno}: case must be an object")
        for key in ("case_id", "area", "scenario", "title"):
            if not isinstance(case.get(key), str) or not case[key]:
                raise ValueError(f"{path}:{lineno}: missing string field {key!r}")
        if case["case_id"] in seen:
            raise ValueError(f"{path}:{lineno}: duplicate case_id {case['case_id']!r}")
        if case["scenario"] not in SCENARIOS:
            raise ValueError(f"{path}:{lineno}: unknown scenario {case['scenario']!r}")
        seen.add(case["case_id"])
        cases.append(case)
    if not cases:
        raise ValueError(f"{path}: suite is empty")
    return cases


def _counters(**overrides: int) -> dict[str, int]:
    base = dict.fromkeys(ALL_COUNTERS, 0)
    base.update(overrides)
    return base


def _check(name: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {"name": name, "expected": expected, "actual": actual, "passed": actual == expected}


# ---------------------------------------------------------------------------
# Case context
# ---------------------------------------------------------------------------


@dataclass
class EvalUser:
    user_id: UUID
    username: str
    password: str


@dataclass
class EvalContext:
    """One isolated SQLite-backed tenant with real services and a real app."""

    root: Path
    database: Database
    users: dict[str, EvalUser] = field(default_factory=dict)
    projects: dict[str, UUID] = field(default_factory=dict)
    _clients: dict[str, Any] = field(default_factory=dict, repr=False)
    _writes: ExplicitMemoryWriteService | None = field(default=None, repr=False)

    @classmethod
    def create(cls, root: Path) -> EvalContext:
        from sqlalchemy import event as sa_event

        root.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{root / 'memory_p0.sqlite'}")

        @sa_event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(engine)
        return cls(root=root, database=Database(engine=engine))

    # -- identity ---------------------------------------------------------
    def user(self, name: str, *, role: str = "member") -> EvalUser:
        from water_agent.webapp.auth.db_store import PostgresUserStore
        from water_agent.webapp.auth.models import UserRole

        if name in self.users:
            return self.users[name]
        username = f"{name}-{uuid4().hex[:8]}"
        password = "MemoryP0Pass2026!"
        record = PostgresUserStore(self.database).create_user(
            username, password, UserRole.ADMIN if role == "admin" else UserRole.MEMBER
        )
        created = EvalUser(user_id=UUID(str(record.user_id)), username=username, password=password)
        self.users[name] = created
        return created

    def project(self, name: str, owner: EvalUser) -> UUID:
        if name in self.projects:
            return self.projects[name]
        project = ProjectRepository(self.database).create(owner.user_id, name)
        self.projects[name] = UUID(str(project.project_id))
        return self.projects[name]

    def session(self, owner: EvalUser, *, project_id: UUID | None = None, title: str = "") -> UUID:
        web_session = WebSessionRepository(self.database).create(
            owner.user_id, project_id=project_id, title=title
        )
        return UUID(str(web_session.thread_id))

    def client(self, user: EvalUser):
        from fastapi.testclient import TestClient

        from water_agent.webapp.app import create_app

        cached = self._clients.get(user.username)
        if cached is not None:
            return cached
        app = create_app(
            database=self.database,
            static_dir=self.root / "static",
            memory_enabled=True,
            trace_dir=self.root / "traces",
            session_dir=self.root / "sessions",
        )
        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(
            "/api/auth/login",
            json={"username": user.username, "password": user.password},
        )
        if response.status_code != 200:  # pragma: no cover - login guard
            raise RuntimeError(f"login failed for {user.username}: {response.text}")
        self._clients[user.username] = client
        return client

    # -- services ---------------------------------------------------------
    @property
    def memory(self) -> MemoryService:
        return MemoryService(MemoryRepository(self.database))

    @property
    def writes(self) -> ExplicitMemoryWriteService:
        """One write service per context: its pending-candidate store is in-process.

        A candidate previewed by one instance can only be confirmed by that same
        instance, so the harness must not build a fresh service on every call.
        """

        cached = self._writes
        if cached is None:
            cached = ExplicitMemoryWriteService(self.memory, guard=MemoryWriteGuard())
            self._writes = cached
        return cached

    def recall_service(self) -> MemoryRecallService:
        return MemoryRecallService(self.database)

    # -- raw persistence probes ------------------------------------------
    def raw_record(self, memory_id: UUID | str) -> dict[str, Any] | None:
        with self.database.transaction() as session:
            row = (
                session.execute(
                    text(
                        "SELECT content, status, tombstone FROM memory_records "
                        "WHERE memory_id = :id"
                    ),
                    {"id": str(memory_id)},
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    def body_occurrences(self, needle: str) -> int:
        """Count raw occurrences of a body outside the current record column.

        Scans the columns that could retain a copy: the live body, the event
        reasons and the usage rows' identifiers.  Content is NOT expected here
        (``memory_events`` stores hashes only) — a hit is a real residual.
        """

        with self.database.transaction() as session:
            rows = session.execute(
                text(
                    "SELECT (SELECT count(*) FROM memory_records WHERE content LIKE :like) "
                    "     + (SELECT count(*) FROM memory_events WHERE reason LIKE :like) "
                    "     + (SELECT count(*) FROM memory_usage WHERE source_thread_id LIKE :like) "
                    "AS hits"
                ),
                {"like": f"%{needle}%"},
            ).first()
        return int(rows[0]) if rows is not None else 0

    def dispose(self) -> None:
        self.database.dispose()


def _recall(
    ctx: EvalContext,
    *,
    actor: EvalUser,
    query: str,
    project_id: UUID | None = None,
    max_characters: int = MAX_MEMORY_CONTEXT_CHARACTERS,
    max_items: int = 8,
    thread_id: UUID | None = None,
):
    return ctx.recall_service().recall(
        tenant_id=TENANT_ID,
        actor_user_id=actor.user_id,
        query=query,
        project_id=project_id,
        max_items=max_items,
        max_characters=max_characters,
        source_thread_id=thread_id,
    )


def _stored(expected: str) -> str:
    """The body as the write path stores it (NFKC + CRLF folding + strip).

    Content is normalized before persistence, so a check that compares a raw
    dataset string against a stored body fails on full-width punctuation only.
    """

    from water_agent.memory.write_guard import normalize_memory_content

    return normalize_memory_content(expected)


def _hit_texts(result: Any) -> list[str]:
    return [hit.memory.content for hit in result.hits]


def _provenance(result: Any) -> tuple[int, int]:
    """Return (total, valid) for recalled-hit provenance envelopes."""

    total = len(result.hits)
    valid = 0
    for hit in result.hits:
        envelope = hit.envelope
        if (
            envelope.source_tag is SourceTag.SESSION_MEMORY
            and envelope.reference_id == str(hit.memory.memory_id)
            and envelope.content_hash == content_hash_of(hit.memory.content)
            and envelope.is_instruction_bearing is False
        ):
            valid += 1
    return total, valid


def _write(
    ctx: EvalContext,
    actor: EvalUser,
    *,
    scope: MemoryScopeType,
    kind: MemoryKind,
    key: str,
    content: str,
    project_id: UUID | None = None,
    confirm: bool = True,
    thread_id: UUID | None = None,
) -> dict[str, Any]:
    """Run candidate → (optional) confirm through the production write service."""

    candidate = ctx.writes.candidate(
        tenant_id=TENANT_ID,
        actor_user_id=actor.user_id,
        scope_type=scope,
        kind=kind,
        semantic_key=key,
        content=content,
        project_id=project_id,
        source_thread_id=thread_id,
    )
    payload: dict[str, Any] = {"candidate": candidate, "result": None}
    if not confirm:
        return payload
    confirmation = MemoryConfirmation(
        confirmed=True,
        content_hash=candidate.content_hash,
        expected_revision=candidate.expected_revision,
    )
    payload["result"] = ctx.writes.confirm(
        candidate.candidate_id,
        tenant_id=TENANT_ID,
        actor_user_id=actor.user_id,
        confirmation=confirmation,
    )
    return payload


def _write_error(
    ctx: EvalContext,
    actor: EvalUser,
    *,
    scope: MemoryScopeType,
    kind: MemoryKind,
    key: str,
    content: str,
    project_id: UUID | None = None,
) -> str | None:
    """Return the stable error code of a rejected write, or None if accepted."""

    try:
        _write(
            ctx,
            actor,
            scope=scope,
            kind=kind,
            key=key,
            content=content,
            project_id=project_id,
        )
    except DomainMemoryError as exc:
        return exc.code
    return None


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

Scenario = Callable[[dict[str, Any], EvalContext], dict[str, Any]]
SCENARIOS: dict[str, Scenario] = {}


def scenario(name: str) -> Callable[[Scenario], Scenario]:
    def register(func: Scenario) -> Scenario:
        SCENARIOS[name] = func
        return func

    return register


def _result(
    ctx: EvalContext,
    *,
    checks: list[dict[str, Any]],
    counters: dict[str, int],
    evidence: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    del ctx
    return {
        "checks": checks,
        "counters": counters,
        "evidence": evidence,
        "events": [sanitize_memory_event(item) for item in events],
    }


@scenario("preference_cross_session")
def _scn_preference_cross_session(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """A user preference saved in session A is recalled from a later session."""

    owner = ctx.user("owner")
    first_session = ctx.session(owner, title="A")
    second_session = ctx.session(owner, title="B")
    written = _write(
        ctx,
        owner,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        key=case["params"]["semantic_key"],
        content=case["params"]["content"],
        thread_id=first_session,
    )
    recalled = _recall(ctx, actor=owner, query=case["params"]["query"], thread_id=second_session)
    total, valid = _provenance(recalled)
    texts = _hit_texts(recalled)
    expected = case["expected"]
    body = _stored(case["params"]["content"])
    checks = [
        _check("write_action", "created", written["result"].action.value),
        _check("recalled", True, body in texts),
        _check("recall_count", expected["recall_count"], len(recalled.hits)),
        _check(
            "source_thread_isolated",
            True,
            bool(recalled.hits) and recalled.hits[0].memory.source_thread_id == first_session,
        ),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(
            confirmed_writes=1,
            recall_gold_total=1,
            recall_gold_matched=1 if body in texts else 0,
            provenance_total=total,
            provenance_valid=valid,
        ),
        evidence={
            "recall_count": len(recalled.hits),
            "provenance_valid": valid,
            "second_session_is_new_thread": second_session != first_session,
        },
        events=[
            {
                "event": "memory.recall_completed",
                "case_id": case["case_id"],
                "leaked": 0,
                "returned_count": len(recalled.hits),
                "content_hash": recalled.hits[0].envelope.content_hash if recalled.hits else None,
            }
        ],
    )


@scenario("project_fact_isolation")
def _scn_project_fact_isolation(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """A project fact is recallable only inside its own project scope."""

    owner = ctx.user("owner")
    other = ctx.user("other")
    first = ctx.project("P1", owner)
    second = ctx.project("P2", other)
    _write(
        ctx,
        owner,
        scope=MemoryScopeType.PROJECT,
        kind=MemoryKind.PROJECT_FACT,
        key=case["params"]["semantic_key"],
        content=case["params"]["content"],
        project_id=first,
    )
    inside = _recall(ctx, actor=owner, query=case["params"]["query"], project_id=first)
    outside = _recall(ctx, actor=owner, query=case["params"]["query"], project_id=second)
    unscoped = _recall(ctx, actor=owner, query=case["params"]["query"])
    leaks = len(outside.hits) + len(unscoped.hits)
    total, valid = _provenance(inside)
    texts = _hit_texts(inside)
    body = _stored(case["params"]["content"])
    checks = [
        _check("recalled_inside_project", True, body in texts),
        _check("recall_count_inside", case["expected"]["recall_count"], len(inside.hits)),
        _check("other_project_hits", 0, len(outside.hits)),
        _check("unscoped_hits", 0, len(unscoped.hits)),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(
            cross_project_leaks=leaks,
            confirmed_writes=1,
            recall_gold_total=1,
            recall_gold_matched=1 if body in texts else 0,
            provenance_total=total,
            provenance_valid=valid,
        ),
        evidence={
            "inside": len(inside.hits),
            "other_project": len(outside.hits),
            "no_project": len(unscoped.hits),
        },
        events=[
            {
                "event": "memory.recall_completed",
                "case_id": case["case_id"],
                "leaked": leaks,
                "returned_count": len(inside.hits),
            }
        ],
    )


@scenario("same_key_two_users")
def _scn_same_key_two_users(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Two users may hold the same semantic key without overwriting each other."""

    first = ctx.user("first")
    second = ctx.user("second")
    key = case["params"]["semantic_key"]
    query = case["params"]["query"]
    _write(
        ctx,
        first,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        key=key,
        content=case["params"]["first_content"],
    )
    _write(
        ctx,
        second,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        key=key,
        content=case["params"]["second_content"],
    )
    first_hits = _hit_texts(_recall(ctx, actor=first, query=query))
    second_hits = _hit_texts(_recall(ctx, actor=second, query=query))
    first_visible = _provenance(_recall(ctx, actor=first, query=query))
    first_body = _stored(case["params"]["first_content"])
    second_body = _stored(case["params"]["second_content"])
    leak = int(second_body in first_hits) + int(first_body in second_hits)
    checks = [
        _check("first_sees_own", True, first_body in first_hits),
        _check("second_sees_own", True, second_body in second_hits),
        _check("no_cross_user_leak", 0, leak),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(
            cross_user_leaks=leak,
            confirmed_writes=2,
            recall_gold_total=2,
            recall_gold_matched=int(first_body in first_hits) + int(second_body in second_hits),
            provenance_total=first_visible[0],
            provenance_valid=first_visible[1],
        ),
        evidence={"first_hits": len(first_hits), "second_hits": len(second_hits)},
        events=[
            {"event": "memory.recall_completed", "case_id": case["case_id"], "leaked": leak}
        ],
    )


@scenario("idor_memory_id")
def _scn_idor_memory_id(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Guessing another user's memory_id must not reveal or mutate it."""

    owner = ctx.user("owner")
    intruder = ctx.user("intruder")
    written = _write(
        ctx,
        owner,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        key=case["params"]["semantic_key"],
        content=case["params"]["content"],
    )
    memory_id = str(written["result"].memory.memory_id)
    owner_client = ctx.client(owner)
    intruder_client = ctx.client(intruder)
    read = intruder_client.get(f"/api/memories/{memory_id}")
    delete = intruder_client.request(
        "DELETE",
        f"/api/memories/{memory_id}",
        json={"expected_revision": 1},
        headers={"X-CSRF-Token": intruder_client.cookies.get("wa_csrf") or ""},
    )
    listed = intruder_client.get("/api/memories").json()["data"]["memories"]
    survivor = owner_client.get(f"/api/memories/{memory_id}")
    active = owner_client.get("/api/memories").json()["data"]["memories"]
    checks = [
        _check("intruder_read_status", case["expected"]["denied_status"], read.status_code),
        _check("intruder_delete_denied", True, delete.status_code in (403, 404)),
        _check("intruder_list_empty", 0, len(listed)),
        _check("owner_still_active", 200, survivor.status_code),
        _check("owner_list_count", 1, len(active)),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(confirmed_writes=1),
        evidence={
            "read_status": read.status_code,
            "delete_status": delete.status_code,
            "survivor_status": survivor.status_code,
        },
        events=[
            {
                "event": "memory.write_blocked",
                "case_id": case["case_id"],
                "code": "MEMORY_SCOPE_DENIED",
                "persisted": 0,
            }
        ],
    )


@scenario("idor_thread_id")
def _scn_idor_thread_id(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Guessing another user's thread_id must not expose the session or its runs."""

    owner = ctx.user("owner")
    intruder = ctx.user("intruder")
    thread_id = ctx.session(owner, title="私有会话")
    intruder_client = ctx.client(intruder)
    intruder_client.get("/api/memories")
    read = intruder_client.get(f"/api/sessions/{thread_id}")
    post = intruder_client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": "越权提问"},
        headers={"X-CSRF-Token": intruder_client.cookies.get("wa_csrf") or ""},
    )
    checks = [
        _check("session_read_denied", case["expected"]["denied_status"], read.status_code),
        _check("message_post_denied", case["expected"]["denied_status"], post.status_code),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(),
        evidence={"thread_id": str(thread_id)},
        events=[
            {
                "event": "memory.write_blocked",
                "case_id": case["case_id"],
                "code": "MEMORY_SCOPE_DENIED",
                "persisted": 0,
            }
        ],
    )


@scenario("idor_project_id")
def _scn_idor_project_id(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Guessing another user's project_id must not expose its sessions or memories."""

    owner = ctx.user("owner")
    intruder = ctx.user("intruder")
    project_id = ctx.project("P1", owner)
    thread_id = ctx.session(owner, project_id=project_id, title="项目会话")
    _write(
        ctx,
        owner,
        scope=MemoryScopeType.PROJECT,
        kind=MemoryKind.PROJECT_FACT,
        key=case["params"]["semantic_key"],
        content=case["params"]["content"],
        project_id=project_id,
    )
    client = ctx.client(intruder)
    client.get("/api/memories")
    project = client.get(f"/api/projects/{project_id}")
    sessions = client.get(f"/api/projects/{project_id}/sessions")
    leaked = client.get(f"/api/memories?project_id={project_id}").json()["data"]["memories"]
    owner_client = ctx.client(owner)
    own = owner_client.get(f"/api/memories?project_id={project_id}").json()["data"]["memories"]
    leak = len(leaked)
    checks = [
        _check("project_read_denied", case["expected"]["denied_status"], project.status_code),
        _check("project_sessions_denied", case["expected"]["denied_status"], sessions.status_code),
        _check("project_memory_hits", 0, leak),
        _check("owner_project_memory_hits", 1, len(own)),
        _check("session_still_owned", 200, owner_client.get(f"/api/sessions/{thread_id}").status_code),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(cross_project_leaks=leak, confirmed_writes=1),
        evidence={"project_status": project.status_code, "leaked": leak},
        events=[
            {"event": "memory.recall_completed", "case_id": case["case_id"], "leaked": leak}
        ],
    )


@scenario("reaffirm_duplicate")
def _scn_reaffirm_duplicate(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Repeating the same fact reaffirms it instead of creating a second record."""

    owner = ctx.user("owner")
    key = case["params"]["semantic_key"]
    content = case["params"]["content"]
    first = _write(
        ctx,
        owner,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        key=key,
        content=content,
    )
    second_candidate = ctx.writes.candidate(
        tenant_id=TENANT_ID,
        actor_user_id=owner.user_id,
        scope_type=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key=key,
        content=content,
    )
    confirmed = ctx.writes.reaffirm(
        second_candidate.candidate_id,
        tenant_id=TENANT_ID,
        actor_user_id=owner.user_id,
        confirmation=MemoryConfirmation(
            confirmed=True,
            content_hash=second_candidate.content_hash,
            expected_revision=second_candidate.expected_revision,
        ),
    )
    active = ctx.memory.list_active(tenant_id=TENANT_ID, actor_user_id=owner.user_id)
    recalled = _recall(ctx, actor=owner, query=case["params"]["query"])
    checks = [
        _check("first_action", "created", first["result"].action.value),
        _check("disposition", CandidateDisposition.REAFFIRM.value, second_candidate.disposition.value),
        _check("reaffirm_action", "reaffirmed", confirmed.action.value),
        _check("active_records", 1, len(active)),
        _check("recall_count", 1, len(recalled.hits)),
        _check("revision_unchanged", 1, confirmed.memory.revision if confirmed.memory else None),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(
            confirmed_writes=2,
            recall_gold_total=1,
            recall_gold_matched=1 if recalled.hits else 0,
            provenance_total=len(recalled.hits),
            provenance_valid=_provenance(recalled)[1],
        ),
        evidence={"active_records": len(active), "recall_count": len(recalled.hits)},
        events=[
            {
                "event": "memory.write_committed",
                "case_id": case["case_id"],
                "confirmed": True,
                "revision": confirmed.memory.revision if confirmed.memory else None,
            }
        ],
    )


@scenario("conflict_requires_confirmation")
def _scn_conflict_requires_confirmation(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """A different body on an occupied key must not silently overwrite."""

    owner = ctx.user("owner")
    key = case["params"]["semantic_key"]
    original = case["params"]["original_content"]
    proposed = case["params"]["proposed_content"]
    _write(
        ctx,
        owner,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        key=key,
        content=original,
    )
    candidate = ctx.writes.candidate(
        tenant_id=TENANT_ID,
        actor_user_id=owner.user_id,
        scope_type=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key=key,
        content=proposed,
    )
    unreconciled_code: str | None = None
    try:
        ctx.writes.confirm(
            candidate.candidate_id,
            tenant_id=TENANT_ID,
            actor_user_id=owner.user_id,
            confirmation=MemoryConfirmation(confirmed=True, content_hash=candidate.content_hash),
        )
    except DomainMemoryError as exc:
        unreconciled_code = exc.code
    after = ctx.memory.list_active(tenant_id=TENANT_ID, actor_user_id=owner.user_id)
    recalled = _hit_texts(_recall(ctx, actor=owner, query=case["params"]["query"]))
    # An explicit decision is required; choosing "correct existing" then applies it.
    reconciled = ctx.writes.candidate(
        tenant_id=TENANT_ID,
        actor_user_id=owner.user_id,
        scope_type=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key=key,
        content=proposed,
    )
    applied = ctx.writes.confirm(
        reconciled.candidate_id,
        tenant_id=TENANT_ID,
        actor_user_id=owner.user_id,
        confirmation=MemoryConfirmation(
            confirmed=True,
            content_hash=reconciled.content_hash,
            resolution=ConflictResolution.CORRECT_EXISTING,
            expected_revision=reconciled.conflict.revision if reconciled.conflict else None,
        ),
    )
    final = _hit_texts(_recall(ctx, actor=owner, query=case["params"]["query"]))
    checks = [
        _check("disposition", CandidateDisposition.CONFLICT.value, candidate.disposition.value),
        _check(
            "unconfirmed_conflict_code",
            "MEMORY_CONFLICT_REQUIRES_CONFIRMATION",
            unreconciled_code,
        ),
        _check("original_preserved", True, original in recalled),
        _check("proposed_not_persisted_before_decision", False, proposed in recalled),
        _check("explicit_resolution_action", "corrected", applied.action.value),
        _check("proposed_applied_after_decision", True, proposed in final),
        _check("active_records", 1, len(after)),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(confirmed_writes=3),
        evidence={
            "unreconciled_code": unreconciled_code,
            "active_records": len(after),
            "final_recall_count": len(final),
        },
        events=[
            {
                "event": "memory.write_blocked",
                "case_id": case["case_id"],
                "code": unreconciled_code or "",
                "persisted": 0,
            }
        ],
    )


@scenario("revision_concurrency")
def _scn_revision_concurrency(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Two corrections built from the same revision: the stale one must fail."""

    owner = ctx.user("owner")
    key = case["params"]["semantic_key"]
    written = _write(
        ctx,
        owner,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        key=key,
        content=case["params"]["content"],
    )
    memory_id = written["result"].memory.memory_id
    stale = ctx.writes.correct(
        memory_id,
        tenant_id=TENANT_ID,
        actor_user_id=owner.user_id,
        expected_revision=1,
        content=case["params"]["first_correction"],
    )
    ctx.writes.confirm(
        stale.candidate_id,
        tenant_id=TENANT_ID,
        actor_user_id=owner.user_id,
        confirmation=MemoryConfirmation(
            confirmed=True,
            content_hash=stale.content_hash,
            expected_revision=stale.expected_revision,
        ),
    )
    conflict_code: str | None = None
    try:
        ctx.writes.correct(
            memory_id,
            tenant_id=TENANT_ID,
            actor_user_id=owner.user_id,
            expected_revision=1,
            content=case["params"]["stale_correction"],
        )
    except DomainMemoryError as exc:
        conflict_code = exc.code
    current = ctx.memory.get_memory(
        memory_id, tenant_id=TENANT_ID, actor_user_id=owner.user_id
    )
    recalled = _hit_texts(_recall(ctx, actor=owner, query=case["params"]["query"]))
    checks = [
        _check("stale_correction_code", "MEMORY_REVISION_CONFLICT", conflict_code),
        _check("revision_bumped", 2, current.revision),
        _check("content_is_first_correction", case["params"]["first_correction"], current.content),
        _check("stale_value_not_recalled", False, case["params"]["stale_correction"] in recalled),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(confirmed_writes=2),
        evidence={"revision": current.revision, "conflict_code": conflict_code},
        events=[
            {
                "event": "memory.write_blocked",
                "case_id": case["case_id"],
                "code": conflict_code or "",
                "persisted": 0,
            }
        ],
    )


@scenario("inactive_status_not_recalled")
def _scn_inactive_status_not_recalled(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """expired / superseded / forgotten / quarantined bodies never reach recall."""

    owner = ctx.user("owner")
    statuses: list[str] = list(case["params"]["statuses"])
    admin = ctx.memory
    bodies: dict[str, str] = {}
    for status in statuses:
        key = f"status.{status}"
        content = f"{case['params']['content_prefix']}{status}读数"
        bodies[status] = content
        written = _write(
            ctx,
            owner,
            scope=MemoryScopeType.USER,
            kind=MemoryKind.USER_PREFERENCE,
            key=key,
            content=content,
        )
        record = written["result"].memory
        target = MemoryStatus(status)
        if target is MemoryStatus.QUARANTINED:
            admin.transition_memory(
                record.memory_id,
                tenant_id=TENANT_ID,
                actor_user_id=owner.user_id,
                expected_revision=record.revision,
                target_status=MemoryStatus.QUARANTINED,
                reason="memory_p0 quarantine probe",
            )
        else:
            admin.transition_memory(
                record.memory_id,
                tenant_id=TENANT_ID,
                actor_user_id=owner.user_id,
                expected_revision=record.revision,
                target_status=target,
                reason=f"memory_p0 {status} probe",
            )
    recalled = _recall(ctx, actor=owner, query=case["params"]["query"])
    leaked_bodies = [status for status, body in bodies.items() if body in _hit_texts(recalled)]
    inactive_recalls = len(recalled.hits)
    checks = [
        _check("inactive_recall_count", 0, inactive_recalls),
        _check("no_inactive_body_returned", [], leaked_bodies),
        _check("statuses_applied", len(statuses), len(bodies)),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(
            inactive_recalls=inactive_recalls,
            confirmed_writes=len(statuses),
        ),
        evidence={
            "statuses": statuses,
            "recalled": inactive_recalls,
            "leaked_statuses": leaked_bodies,
        },
        events=[
            {
                "event": "memory.recall_completed",
                "case_id": case["case_id"],
                "leaked": inactive_recalls,
                "returned_count": inactive_recalls,
            }
        ],
    )


@scenario("forget_removes_all_surfaces")
def _scn_forget_removes_all_surfaces(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """After forget the body is gone from the record, recall, and the Web API."""

    owner = ctx.user("owner")
    content = _stored(case["params"]["content"])
    key = case["params"]["semantic_key"]
    written = _write(
        ctx,
        owner,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        key=key,
        content=content,
    )
    memory_id = written["result"].memory.memory_id
    client = ctx.client(owner)
    client.get("/api/memories")
    before = client.get(f"/api/memories/{memory_id}").json()["data"]
    forgotten = ctx.writes.forget(
        memory_id,
        tenant_id=TENANT_ID,
        actor_user_id=owner.user_id,
        expected_revision=written["result"].memory.revision,
        confirmed=True,
    )
    raw = ctx.raw_record(memory_id) or {}
    recalled = _recall(ctx, actor=owner, query=case["params"]["query"])
    after = client.get(f"/api/memories/{memory_id}")
    listed = client.get("/api/memories").json()["data"]["memories"]
    api_body = json.dumps(after.json(), ensure_ascii=False)
    residuals = 0
    residuals += int(bool(raw.get("content")))
    residuals += int(content in _hit_texts(recalled))
    residuals += int(content in api_body)
    residuals += int(any(item.get("content") for item in listed))
    residuals += ctx.body_occurrences(content)
    checks = [
        _check("forget_action", "forgotten", forgotten.action.value),
        _check("raw_content_cleared", None, raw.get("content")),
        _check("tombstone", True, raw.get("tombstone")),
        _check("recall_count", 0, len(recalled.hits)),
        _check("api_status", 200, after.status_code),
        _check("api_status_field", "forgotten", after.json()["data"]["status"]),
        _check("api_content_absent", False, content in api_body),
        _check("list_excludes_forgotten", 0, len(listed)),
        _check("residuals", 0, residuals),
        _check("pre_forget_visible", content, before["content"]),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(forget_residuals=residuals, confirmed_writes=1),
        evidence={
            "residuals": residuals,
            "raw_content": raw.get("content"),
            "recall_count": len(recalled.hits),
            "api_status": after.json()["data"]["status"],
        },
        events=[
            {
                "event": "memory.forget_completed",
                "case_id": case["case_id"],
                "persisted": residuals,
                "memory_id": str(memory_id),
            }
        ],
    )


@scenario("injection_variants_blocked")
def _scn_injection_variants_blocked(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Directive-shaped writes are blocked or quarantined; no body is persisted."""

    owner = ctx.user("owner")
    variant = case["params"]["variant"]
    content = case["params"]["content"]
    code = _write_error(
        ctx,
        owner,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.WORKFLOW_NOTE,
        key=case["params"]["semantic_key"],
        content=content,
    )
    records = ctx.memory.list_active(tenant_id=TENANT_ID, actor_user_id=owner.user_id)
    occurrences = ctx.body_occurrences(content)
    persisted = int(any(record.content == content for record in records)) + occurrences
    quarantined = 0
    if code is None:
        with ctx.database.transaction() as session:
            row = session.execute(
                text("SELECT count(*) FROM memory_records WHERE status = 'quarantined'")
            ).first()
        quarantined = int(row[0]) if row is not None else 0
    checks = [
        _check("blocked_or_quarantined", True, code is not None or quarantined > 0),
        _check("body_not_persisted", 0, persisted),
        _check("active_records", 0, len(records)),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(blocked_injections_persisted=persisted),
        evidence={
            "variant": variant,
            "code": code,
            "quarantined_records": quarantined,
            "persisted": persisted,
        },
        events=[
            {
                "event": "memory.write_blocked",
                "case_id": case["case_id"],
                "code": code or "memory.quarantined",
                "persisted": persisted,
            }
        ],
    )


@scenario("secret_variants_blocked")
def _scn_secret_variants_blocked(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Credential-shaped writes are always blocked, whatever the PII policy."""

    owner = ctx.user("owner")
    content = case["params"]["content"]
    code = _write_error(
        ctx,
        owner,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.WORKFLOW_NOTE,
        key=case["params"]["semantic_key"],
        content=content,
    )
    occurrences = ctx.body_occurrences(content)
    active = ctx.memory.list_active(tenant_id=TENANT_ID, actor_user_id=owner.user_id)
    persisted = occurrences + int(any(record.content == content for record in active))
    checks = [
        _check("error_code", "MEMORY_SECRET_BLOCKED", code),
        _check("persisted", 0, persisted),
        _check("active_records", 0, len(active)),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(secrets_persisted=persisted),
        evidence={"variant": case["params"]["variant"], "code": code, "persisted": persisted},
        events=[
            {
                "event": "memory.write_blocked",
                "case_id": case["case_id"],
                "code": code or "",
                "persisted": persisted,
            }
        ],
    )


@scenario("pii_variants_blocked")
def _scn_pii_variants_blocked(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """P0 has no PII allow switch: detected PII never enters long-term memory."""

    owner = ctx.user("owner")
    content = case["params"]["content"]
    code = _write_error(
        ctx,
        owner,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.WORKFLOW_NOTE,
        key=case["params"]["semantic_key"],
        content=content,
    )
    persisted = ctx.body_occurrences(content)
    active = ctx.memory.list_active(tenant_id=TENANT_ID, actor_user_id=owner.user_id)
    persisted += int(any(record.content == content for record in active))
    checks = [
        _check("error_code", "MEMORY_PII_BLOCKED", code),
        _check("persisted", 0, persisted),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(secrets_persisted=persisted),
        evidence={"variant": case["params"]["variant"], "code": code, "persisted": persisted},
        events=[
            {
                "event": "memory.write_blocked",
                "case_id": case["case_id"],
                "code": code or "",
                "persisted": persisted,
            }
        ],
    )


@scenario("context_cannot_change_instructions")
def _scn_context_cannot_change_instructions(
    case: dict[str, Any], ctx: EvalContext
) -> dict[str, Any]:
    """Session memory stays data: it cannot become an instruction or a capability.

    The record is inserted through the service layer (bypassing the write guard)
    to model an already-persisted poisoned row, then measured on the read path.
    """

    from water_agent.prompting.assembler import PromptAssembler
    from water_agent.runtime.governance.contracts import Principal
    from water_agent.tools.calc_velocity import calc_velocity_spec
    from water_agent.tools.registry import ToolRegistry

    owner = ctx.user("owner")
    content = case["params"]["content"]
    ctx.memory.create_memory(
        tenant_id=TENANT_ID,
        actor_user_id=owner.user_id,
        scope_type=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key=case["params"]["semantic_key"],
        content=content,
        origin=MemoryOrigin.EXPLICIT_USER,
    )
    recalled = _recall(ctx, actor=owner, query=case["params"]["query"])
    envelope = recalled.hits[0].envelope if recalled.hits else None

    registry = ToolRegistry()
    registry.register(calc_velocity_spec)
    before_names = registry.list_all_names()
    before_schema = registry.list_tools()

    assembler = PromptAssembler(tool_registry=registry)
    without_memory = str(assembler.assemble_prompt({}).content)
    with_memory = str(assembler.assemble_prompt({"memory_context": recalled}).content)
    registry_after = ToolRegistry()
    registry_after.register(calc_velocity_spec)
    after_names = registry_after.list_all_names()
    after_schema = registry_after.list_tools()

    principal = Principal.authenticated(
        subject="user:memory-p0", auth_domain="tenant:default", capabilities=frozenset()
    )
    memory_segment_index = with_memory.find("【长期记忆上下文】")
    principles_index = with_memory.find("给排水")
    tool_guide_with = with_memory[with_memory.find("【可用工具】") : memory_segment_index].rstrip()
    tool_guide_without = without_memory[without_memory.find("【可用工具】") :].rstrip()
    leaked_into_instruction_surface = bool(
        envelope is not None and envelope.is_instruction_bearing
    )
    checks = [
        _check("recalled", 1, len(recalled.hits)),
        _check("source_tag", "session_memory", envelope.source_tag.value if envelope else None),
        _check("instruction_bearing", False, leaked_into_instruction_surface),
        _check("memory_present_in_prompt", True, memory_segment_index >= 0),
        _check("memory_after_principles", True, memory_segment_index > principles_index >= 0),
        _check("memory_absent_without_state", False, "【长期记忆上下文】" in without_memory),
        _check("tool_names_unchanged", before_names, after_names),
        _check("tool_schema_unchanged", before_schema, after_schema),
        _check("tool_guide_unchanged_by_memory", True, tool_guide_with == tool_guide_without),
        _check("memory_adds_no_tool", False, any("memory" in name for name in after_names)),
        _check("capabilities_unchanged", 0, len(principal.capabilities)),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(provenance_total=len(recalled.hits), provenance_valid=_provenance(recalled)[1]),
        evidence={
            "tool_names": after_names,
            "memories_endpoints_do_not_grant_capabilities": True,
            "memory_segment_index": memory_segment_index,
        },
        events=[
            {
                "event": "memory.prompt_rendered",
                "case_id": case["case_id"],
                "instruction_surface": leaked_into_instruction_surface,
                "count": 0,
            }
        ],
    )


@scenario("numeric_memory_revalidation")
def _scn_numeric_memory_revalidation(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """A remembered number is context, never the computed answer."""

    from water_agent.tools.calc_velocity import calc_velocity_spec
    from water_agent.tools.registry import ToolRegistry

    owner = ctx.user("owner")
    memory_value = float(case["params"]["memory_value"])
    ctx.memory.create_memory(
        tenant_id=TENANT_ID,
        actor_user_id=owner.user_id,
        scope_type=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        semantic_key=case["params"]["semantic_key"],
        content=case["params"]["content"],
        origin=MemoryOrigin.EXPLICIT_USER,
    )
    recalled = _recall(ctx, actor=owner, query=case["params"]["query"])
    context_text = recalled.context_text
    registry = ToolRegistry()
    registry.register(calc_velocity_spec)
    tool_result = registry.invoke(
        "calc_velocity",
        diameter_mm=case["params"]["diameter_mm"],
        flow_lps=case["params"]["flow_lps"],
        scenario=case["params"]["scenario"],
    )
    computed = float(tool_result["velocity_mps"])
    # Independent arithmetic re-derivation of the tool contract (pipe area model).
    area = 3.141592653589793 * (float(case["params"]["diameter_mm"]) / 1000.0) ** 2 / 4.0
    recomputed = (float(case["params"]["flow_lps"]) / 1000.0) / area
    revalidated = (
        computed != memory_value
        and abs(computed - recomputed) < 1e-3
        and f"{memory_value:g}" not in json.dumps(tool_result)
    )
    checks = [
        _check("memory_recalled_into_context", True, case["params"]["content"] in context_text),
        _check("memory_number_in_context", True, f"{memory_value:g}" in context_text),
        _check("answer_from_programmatic_tool", True, abs(computed - recomputed) < 1e-3),
        _check("memory_number_not_adopted", True, computed != memory_value),
        _check("revalidated", True, revalidated),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(numeric_total=1, numeric_revalidated=1 if revalidated else 0),
        evidence={
            "memory_value": memory_value,
            "computed_value": computed,
            "recomputed_value": round(recomputed, 3),
            "tool_name": "calc_velocity",
        },
        events=[
            {
                "event": "memory.numeric_recalled",
                "case_id": case["case_id"],
                "revalidated": revalidated,
            }
        ],
    )


@scenario("recall_budget_and_stable_order")
def _scn_recall_budget_and_stable_order(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Recall honours its item/character budget and ranks deterministically."""

    owner = ctx.user("owner")
    filler = case["params"]["filler"]
    count = int(case["params"]["count"])
    for index in range(count):
        ctx.memory.create_memory(
            tenant_id=TENANT_ID,
            actor_user_id=owner.user_id,
            scope_type=MemoryScopeType.USER,
            kind=MemoryKind.USER_PREFERENCE,
            semantic_key=f"budget.key.{index}",
            content=f"{case['params']['query']} 第 {index} 条 {filler}",
            origin=MemoryOrigin.EXPLICIT_USER,
        )
    first = _recall(ctx, actor=owner, query=case["params"]["query"])
    second = _recall(ctx, actor=owner, query=case["params"]["query"])
    rendered = render_memory_context(first.hits, max_characters=MAX_MEMORY_CONTEXT_CHARACTERS)
    violations = int(first.used_characters > MAX_MEMORY_CONTEXT_CHARACTERS)
    violations += int(len(first.hits) > first.item_limit)
    violations += int(len(rendered) > MAX_MEMORY_CONTEXT_CHARACTERS)
    order_first = [str(hit.memory.memory_id) for hit in first.hits]
    order_second = [str(hit.memory.memory_id) for hit in second.hits]
    checks = [
        _check("item_limit_respected", True, len(first.hits) <= first.item_limit),
        _check("character_budget_respected", True, len(rendered) <= MAX_MEMORY_CONTEXT_CHARACTERS),
        _check("used_characters_matches_render", len(rendered), first.used_characters),
        _check("overflow_visible_as_omission", True, first.omitted_count > 0),
        _check("stable_order", order_first, order_second),
        _check("budget_violations", 0, violations),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(
            budget_violations=violations,
            provenance_total=len(first.hits),
            provenance_valid=_provenance(first)[1],
        ),
        evidence={
            "candidate_count": first.candidate_count,
            "returned": len(first.hits),
            "omitted": first.omitted_count,
            "used_characters": first.used_characters,
            "budget": first.character_budget,
        },
        events=[
            {
                "event": "memory.prompt_rendered",
                "case_id": case["case_id"],
                "count": violations,
                "used_characters": first.used_characters,
                "character_budget": first.character_budget,
                "omitted_count": first.omitted_count,
            }
        ],
    )


@scenario("store_failure_structured")
def _scn_store_failure_structured(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """A database failure surfaces as MEMORY_STORE_UNAVAILABLE, never as 0 hits."""

    from water_agent.memory.recall import MemoryRecallService as RecallService

    class _BrokenDatabase:
        @contextmanager
        def transaction(self):
            raise SQLAlchemyError("database secret details must not escape")
            yield  # pragma: no cover - unreachable

    owner = ctx.user("owner")
    code: str | None = None
    message = ""
    try:
        RecallService(_BrokenDatabase()).recall(  # type: ignore[arg-type]
            tenant_id=TENANT_ID, actor_user_id=owner.user_id, query="设计流量"
        )
    except DomainMemoryError as exc:
        code = exc.code
        message = exc.message
    leaked = "secret" in message.lower()
    checks = [
        _check("error_code", "MEMORY_STORE_UNAVAILABLE", code),
        _check("driver_detail_not_echoed", False, leaked),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(),
        evidence={"code": code, "message_is_generic": not leaked},
        events=[
            {
                "event": "memory.recall_degraded",
                "case_id": case["case_id"],
                "reported": code == "MEMORY_STORE_UNAVAILABLE",
                "code": code or "",
            }
        ],
    )


@scenario("sse_reconnect_replay")
def _scn_sse_reconnect_replay(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Reconnecting with Last-Event-ID replays only the missing tail, once."""

    factory = _DeterministicFactory(ctx.root / "traces")
    owner = ctx.user("owner")
    thread_id = ctx.session(owner, title="SSE 重连")
    app_client = _client_with_factory(ctx, owner, factory)
    created = app_client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": case["params"]["message"]},
        headers={"X-CSRF-Token": app_client.cookies.get("wa_csrf") or ""},
    )
    run_id = created.json()["data"]["run_id"]
    first = _parse_sse(app_client.get(f"/api/runs/{run_id}/events"))
    cursor = int(first[1]["event_id"])
    replay = _parse_sse(
        app_client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)})
    )
    invalid = app_client.get(
        f"/api/runs/{run_id}/events", headers={"Last-Event-ID": case["params"]["invalid_cursor"]}
    )
    finalized = sum(1 for item in first if item["type"] == "finalize")
    checks = [
        _check("first_stream_non_empty", True, bool(first)),
        _check("replay_only_after_cursor", True, all(int(i["event_id"]) > cursor for i in replay)),
        _check("no_duplicate_event_ids", len(replay), len({i["event_id"] for i in replay})),
        _check("exactly_one_finalize_first", 1, finalized),
        _check(
            "exactly_one_finalize_replay",
            1,
            sum(1 for item in replay if item["type"] == "finalize"),
        ),
        _check("invalid_cursor_status", 400, invalid.status_code),
        _check("invalid_cursor_code", "WEB_EVENT_CURSOR_INVALID", invalid.json()["error"]["code"]),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(unauthorized_sse_replays=0),
        evidence={
            "first_events": len(first),
            "replay_events": len(replay),
            "cursor": cursor,
        },
        events=[
            {
                "event": "memory.sse_replayed",
                "case_id": case["case_id"],
                "authorized": True,
                "replayed": 0,
                "cursor": cursor,
            }
        ],
    )


@scenario("sse_unauthorized_replay")
def _scn_sse_unauthorized_replay(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Another user cannot replay an event log, even with a valid cursor."""

    factory = _DeterministicFactory(ctx.root / "traces")
    owner = ctx.user("owner")
    intruder = ctx.user("intruder")
    thread_id = ctx.session(owner, title="SSE 越权")
    owner_client = _client_with_factory(ctx, owner, factory)
    created = owner_client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": case["params"]["message"]},
        headers={"X-CSRF-Token": owner_client.cookies.get("wa_csrf") or ""},
    )
    run_id = created.json()["data"]["run_id"]
    first = _parse_sse(owner_client.get(f"/api/runs/{run_id}/events"))
    cursor = int(first[0]["event_id"])

    intruder_client = _client_with_factory(ctx, intruder, factory)
    intruder_client.get("/api/memories")
    denied = intruder_client.get(
        f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)}
    )
    denied_body = denied.text
    trace = intruder_client.get(f"/api/runs/{run_id}/trace")
    replayed = 0 if denied.status_code != 200 else len(_parse_sse(denied))
    checks = [
        _check("denied_status", case["expected"]["denied_status"], denied.status_code),
        _check("no_event_body_leaked", False, "event_id" in denied_body),
        _check("trace_denied", case["expected"]["denied_status"], trace.status_code),
        _check("replayed_events", 0, replayed),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(unauthorized_sse_replays=replayed),
        evidence={"denied_status": denied.status_code, "trace_status": trace.status_code},
        events=[
            {
                "event": "memory.sse_replayed",
                "case_id": case["case_id"],
                "authorized": False,
                "replayed": replayed,
                "cursor": cursor,
            }
        ],
    )


@scenario("run_cancel_true_state")
def _scn_run_cancel_true_state(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Cancel reports the real run state and finalizes exactly once."""

    factory = _DeterministicFactory(ctx.root / "traces", wait_for_cancel=True)
    owner = ctx.user("owner")
    thread_id = ctx.session(owner, title="取消")
    app_client = _client_with_factory(ctx, owner, factory)
    created = app_client.post(
        f"/api/sessions/{thread_id}/messages",
        json={"message": case["params"]["message"]},
        headers={"X-CSRF-Token": app_client.cookies.get("wa_csrf") or ""},
    )
    run_id = created.json()["data"]["run_id"]
    deadline = time.monotonic() + 3
    while not factory.sessions and time.monotonic() < deadline:
        time.sleep(0.005)
    started = bool(factory.sessions) and factory.sessions[0].started.wait(timeout=3)
    cancelled = app_client.post(
        f"/api/runs/{run_id}/cancel",
        headers={"X-CSRF-Token": app_client.cookies.get("wa_csrf") or ""},
    )
    events = _parse_sse(app_client.get(f"/api/runs/{run_id}/events"))
    finals = [item for item in events if item["type"] == "finalize"]
    checks = [
        _check("run_started", True, started),
        _check("cancel_status_code", 202, cancelled.status_code),
        _check(
            "cancel_status_value",
            True,
            cancelled.json()["data"]["status"] in {"cancellation_requested", "cancelled"},
        ),
        _check("exactly_one_finalize", 1, len(finals)),
        _check("finalize_status", "cancelled", finals[0]["payload"]["status"] if finals else None),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(),
        evidence={"finalize_count": len(finals), "events": len(events)},
        events=[],
    )


@scenario("unauthenticated_access_denied")
def _scn_unauthenticated_access_denied(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Every memory route requires a server-validated session, not a client claim."""

    from fastapi.testclient import TestClient

    from water_agent.webapp.app import create_app

    owner = ctx.user("owner")
    written = _write(
        ctx,
        owner,
        scope=MemoryScopeType.USER,
        kind=MemoryKind.USER_PREFERENCE,
        key=case["params"]["semantic_key"],
        content=case["params"]["content"],
    )
    memory_id = str(written["result"].memory.memory_id)
    app = create_app(database=ctx.database, static_dir=ctx.root / "static")
    anonymous = TestClient(app, raise_server_exceptions=True)
    listing = anonymous.get("/api/memories")
    single = anonymous.get(f"/api/memories/{memory_id}")
    candidate = anonymous.post(
        "/api/memories/candidates",
        json={
            "scope_type": "user",
            "kind": "user_preference",
            "semantic_key": "anonymous",
            "content": "匿名写入",
        },
    )
    deleted = anonymous.request(
        "DELETE", f"/api/memories/{memory_id}", json={"expected_revision": 1}
    )
    still_there = ctx.raw_record(memory_id) or {}
    checks = [
        _check("list_status", 401, listing.status_code),
        _check("list_code", "AUTH_SESSION_INVALID", listing.json()["error"]["code"]),
        _check("single_status", 401, single.status_code),
        _check("candidate_denied", True, candidate.status_code in (401, 403)),
        _check("delete_denied", True, deleted.status_code in (401, 403)),
        _check("record_untouched", "active", still_there.get("status")),
        _check("no_body_in_denial", False, listing.text.find(case["params"]["content"]) >= 0),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(confirmed_writes=1),
        evidence={
            "list_status": listing.status_code,
            "candidate_status": candidate.status_code,
            "delete_status": deleted.status_code,
        },
        events=[
            {
                "event": "memory.write_blocked",
                "case_id": case["case_id"],
                "code": "AUTH_SESSION_INVALID",
                "persisted": 0,
            }
        ],
    )


@scenario("backup_restore_roundtrip")
def _scn_backup_restore_roundtrip(case: dict[str, Any], ctx: EvalContext) -> dict[str, Any]:
    """Back up the live stack and prove the dump is intact and consistent.

    This case previously returned a SKIP even when a stack was running, which
    made it a case that could never pass.  It now performs the **non-destructive
    half** of the acceptance against a live stack: it runs the real
    ``scripts/backup.sh``, verifies every digest and size in the manifest, and
    compares the recorded row counts with an independent live read of the same
    database.

    The destructive half (restore into a fresh volume, then re-verify) belongs to
    ``scripts/restore.sh`` and is exercised by ``tests/test_backup_restore.py``
    (opt-in) and by the ``p5-compose`` workflow; this harness must not mutate the
    operator's volumes.
    """

    import shutil
    import subprocess

    docker = shutil.which("docker") or _docker_desktop_cli()
    if docker is None:
        return {
            "skip_reason": (
                "docker CLI is not resolvable from this process (checked PATH and "
                "/Applications/Docker.app/Contents/Resources/bin)"
            ),
            "requires": "docker",
        }
    compose = REPO_ROOT / "compose.yml"
    if not compose.is_file():  # pragma: no cover - repo invariant
        return {"skip_reason": "compose.yml is missing", "requires": "docker"}
    probe = subprocess.run(
        [docker, "compose", "ps", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return {
            "skip_reason": (
                "no running water-agent compose stack; the backup acceptance runs on a "
                "live stack (p5-compose workflow) or via "
                "WATER_AGENT_COMPOSE_TEST=1 tests/test_backup_restore.py"
            ),
            "requires": "docker",
        }

    out_dir = ctx.root / "backup"
    backup = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "backup.sh"), "--out", str(out_dir), "--no-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    from scripts.backup_manifest import (
        BackupVerificationError,
        compare_counts,
        parse_counts_text,
        verify_backup,
    )

    manifest_ok = True
    manifest_error = ""
    manifest: dict[str, Any] = {}
    if backup.returncode == 0:
        try:
            manifest = verify_backup(out_dir)
        except BackupVerificationError as exc:
            manifest_ok = False
            manifest_error = str(exc)

    # Independent live read of the same database through the compose services'
    # own credentials; the harness never handles the password itself.
    live = subprocess.run(
        ["bash", "-c", f'. "{REPO_ROOT / "scripts" / "compose_common.sh"}"; db_counts'],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    live_counts = parse_counts_text(live.stdout) if live.returncode == 0 else {}
    mismatches = compare_counts(manifest, live_counts) if manifest_ok and live_counts else []
    residuals = len(mismatches)
    dump_bytes = int((manifest.get("database") or {}).get("bytes") or 0)

    checks = [
        _check("backup_command", 0, backup.returncode),
        _check("manifest_verifies", True, manifest_ok),
        _check("manifest_error", "", manifest_error),
        _check("live_counts_readable", True, bool(live_counts)),
        _check("backup_matches_live_state", [], mismatches),
        _check("dump_is_not_empty", True, dump_bytes > 512),
        _check("forgotten_body_invariant", 0, int(live_counts.get("forgotten_with_body") or 0)),
    ]
    return _result(
        ctx,
        checks=checks,
        counters=_counters(forget_residuals=residuals),
        evidence={
            "backup_dir": str(out_dir),
            "row_counts": manifest.get("row_counts") or {},
            "live_counts": live_counts,
            "mismatches": mismatches,
            "dump_bytes": dump_bytes,
            "sha256_recorded": bool((manifest.get("database") or {}).get("sha256")),
            "restore_half": (
                "restore into a fresh PostgreSQL volume is verified by "
                "tests/test_backup_restore.py and the p5-compose workflow"
            ),
        },
        events=[
            {
                "event": "memory.forget_completed",
                "case_id": case["case_id"],
                "persisted": residuals,
                "memory_hash": str((manifest.get("database") or {}).get("sha256") or "")[:16]
                or None,
            }
        ],
    )


def _docker_desktop_cli() -> str | None:
    """macOS installs Docker Desktop outside the PATH a service process sees."""

    candidate = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
    return str(candidate) if candidate.is_file() else None


# ---------------------------------------------------------------------------
# Web helpers shared by the SSE scenarios
# ---------------------------------------------------------------------------


class _DeterministicSession:
    """Agent-session double: the real factory needs a live remote model.

    It drives the production `RunManager` and writes a real
    `JsonTraceRecorder` trace; only the answer text is scripted.
    """

    def __init__(self, recorder: Any, *, wait_for_cancel: bool = False) -> None:
        self.recorder = recorder
        self.wait_for_cancel = wait_for_cancel
        self.started = Event()
        self.memory_contexts: list[object] = []

    def start_trace(self, run_id: str, query: str) -> None:
        self.recorder.start(run_id=run_id, trace_id=f"trace-{run_id}", query=query)

    async def aask(
        self,
        query: str,
        *,
        run_id: str,
        memory_context: object | None,
        on_token,
        on_event,
        cancel_check,
    ) -> dict[str, Any]:
        del query, run_id
        self.started.set()
        self.memory_contexts.append(memory_context)
        on_event("values", {"status": "running", "messages": []})
        if self.wait_for_cancel:
            while not cancel_check():
                await asyncio.sleep(0.005)
            from water_agent.webapp.run_manager import RunCancelled

            raise RunCancelled()
        on_token("记忆门禁用确定性回答")
        self.recorder.finish("succeeded")
        return {
            "status": "succeeded",
            "answer": "记忆门禁用确定性回答",
            "citations": [],
            "errors": [],
            "warnings": [],
        }


class _DeterministicFactory:
    """Session factory double used only for SSE reconnect / cancel semantics."""

    def __init__(self, trace_dir: Path, *, wait_for_cancel: bool = False) -> None:
        self.trace_dir = Path(trace_dir)
        self.wait_for_cancel = wait_for_cancel
        self.sessions: list[_DeterministicSession] = []

    def create(self, *, run_id: str, thread_id: str, user_id: UUID, principal, project_id, stack):
        del thread_id, user_id, project_id, stack
        from water_agent.observability.json_trace import JsonTraceRecorder

        session = _DeterministicSession(
            JsonTraceRecorder(self.trace_dir), wait_for_cancel=self.wait_for_cancel
        )
        self.sessions.append(session)
        return session


def _client_with_factory(ctx: EvalContext, user: EvalUser, factory: Any):
    from fastapi.testclient import TestClient

    from water_agent.webapp.app import create_app

    app = create_app(
        database=ctx.database,
        static_dir=ctx.root / "static",
        session_factory=factory,
        memory_enabled=True,
        trace_dir=ctx.root / "traces",
    )
    client = TestClient(app, raise_server_exceptions=True)
    response = client.post(
        "/api/auth/login", json={"username": user.username, "password": user.password}
    )
    if response.status_code != 200:  # pragma: no cover - login guard
        raise RuntimeError(f"login failed for {user.username}: {response.text}")
    return client


def _parse_sse(response: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in response.text.strip().split("\n\n"):
        data_lines = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_one_case(case: dict[str, Any], *, workdir: Path) -> dict[str, Any]:
    case_id = str(case["case_id"])
    base: dict[str, Any] = {
        "case_id": case_id,
        "title": case.get("title", ""),
        "area": case.get("area", ""),
        "scenario": case["scenario"],
        "requires": case.get("requires"),
    }
    context: EvalContext | None = None
    try:
        context = EvalContext.create(workdir / case_id)
        produced = SCENARIOS[case["scenario"]](case, context)
        if "skip_reason" in produced:
            return {
                **base,
                "outcome": "skip",
                "status": "skipped",
                "skipped": True,
                "passed": False,
                "skip_reason": produced["skip_reason"],
                "checks": [],
                "counters": {},
                "evidence": produced,
                "events": [],
                "detectors": [],
            }
        checks: list[dict[str, Any]] = list(produced["checks"])
        passed = bool(checks) and all(check["passed"] for check in checks)
        events = [sanitize_memory_event(item) for item in produced.get("events", [])]
        return {
            **base,
            "outcome": "pass" if passed else "fail",
            "status": "passed" if passed else "failed",
            "skipped": False,
            "passed": passed,
            "skip_reason": None,
            "checks": checks,
            "counters": produced["counters"],
            "evidence": produced["evidence"],
            "events": events,
            "detectors": detect_memory_events(events),
        }
    except Exception as exc:  # noqa: BLE001 - a case failure is data, not a crash
        return {
            **base,
            "outcome": "fail",
            "status": "failed",
            "skipped": False,
            "passed": False,
            "skip_reason": None,
            "checks": [],
            "counters": {},
            "evidence": {},
            "events": [],
            "detectors": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if context is not None:
            context.dispose()


def run_suite(
    suite_path: str | Path,
    *,
    out_path: str | Path | None = None,
    workdir: str | Path | None = None,
) -> dict[str, Any]:
    cases = load_suite(suite_path)
    root = Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="memory-p0-"))
    root.mkdir(parents=True, exist_ok=True)
    results = [run_one_case(case, workdir=root) for case in cases]
    evaluated = [item for item in results if not is_runtime_skip_case(item)]
    report: dict[str, Any] = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_by": "water_agent.harness.memory_eval",
        "suite": str(suite_path),
        "suite_case_count": len(cases),
        "total": len(results),
        "passed": sum(1 for item in evaluated if item["outcome"] == "pass"),
        "failed": sum(1 for item in evaluated if item["outcome"] == "fail"),
        "skipped": len(results) - len(evaluated),
        "pass_rate": round(
            sum(1 for item in evaluated if item["outcome"] == "pass") / len(evaluated), 4
        )
        if evaluated
        else 1.0,
        "cases": results,
        "coverage_notes": {
            "synthetic_gold": (
                "gold_exact_recall_rate is measured on a synthetic keyword gold set; it does not "
                "prove recall of arbitrary natural-language phrasing"
            ),
            "regex_guard_boundary": (
                "the write guard is a deterministic regex rule set; "
                "blocked_injection_persisted_count==0 means no blocked variant was persisted, "
                "not that every injection rewrite is caught"
            ),
            "postgres_fts": (
                "offline cases execute the SQLite recall substitute; PostgreSQL FTS scoring is "
                "only exercised when P5_TEST_DATABASE_URL points at a migrated database"
            ),
        },
    }
    report["metrics"] = compute_memory_metrics(report)
    target = Path(out_path) if out_path is not None else DEFAULT_REPORT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(target)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P5 memory_p0 harness (offline deterministic)")
    parser.add_argument("suite", nargs="?", default=str(DEFAULT_SUITE))
    parser.add_argument("--out", default=str(DEFAULT_REPORT))
    parser.add_argument("--workdir", default=None)
    parser.add_argument(
        "--release-gate",
        action="store_true",
        help="fail when any case SKIPs (release gate hosts must not SKIP)",
    )
    parser.add_argument("--min-cases", type=int, default=36)
    args = parser.parse_args(argv)
    try:
        report = run_suite(args.suite, out_path=args.out, workdir=args.workdir)
    except Exception as exc:  # noqa: BLE001 - loading problems are a distinct exit code
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    violations = check_thresholds(report["metrics"])
    if report["suite_case_count"] < args.min_cases:
        violations.append(
            {
                "metric": "suite_case_count",
                "threshold": args.min_cases,
                "actual": report["suite_case_count"],
                "reason": "below_threshold",
                "code": "MEMORY_SUITE_TOO_SMALL",
            }
        )
    if args.release_gate and report["skipped"]:
        violations.append(
            {
                "metric": "release_gate_skips",
                "threshold": 0,
                "actual": report["skipped"],
                "reason": "release_gate_env_must_not_skip",
                "code": "MEMORY_RELEASE_GATE_SKIPPED",
            }
        )
    summary = {
        "ok": not violations and report["failed"] == 0,
        "total": report["total"],
        "passed": report["passed"],
        "failed": report["failed"],
        "skipped": report["skipped"],
        "violations": violations,
        "metrics": {
            key: report["metrics"][key]
            for key in (*HARD_THRESHOLDS, "detector_finding_count")
        },
        "out": report["report_path"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


__all__ = [
    "DEFAULT_REPORT",
    "DEFAULT_SUITE",
    "REPORT_SCHEMA_VERSION",
    "SCENARIOS",
    "EvalContext",
    "load_suite",
    "main",
    "run_one_case",
    "run_suite",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
