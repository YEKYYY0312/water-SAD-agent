"""P5 Step 43 server half: authorized long-term memory Web API.

Step 43 shipped the memory workspace UI and its API client
(``web/app/src/memoryApi.ts``) against ``/api/memories*``.  Those routes did not
exist on the server, so the committed panel could only ever 404.  This module is
the missing server half, added during the Step 45 audit and kept deliberately
thin:

* it defines **no** governance, memory policy, ranking, or agent assembly of its
  own — every request is delegated to the existing
  :class:`~water_agent.memory.service.MemoryService`,
  :class:`~water_agent.memory.write_service.ExplicitMemoryWriteService` and
  :class:`~water_agent.memory.recall.MemoryRecallService`;
* the actor identity is always the authenticated session's ``UserRecord`` and
  the tenant is the server-side default — no user, tenant, or principal value is
  ever read from the request body or query string;
* ``tenant_id`` never appears in a request or response payload.

Known P0 limitation (recorded, not hidden): the pending-candidate store is
in-process (15-minute TTL, see ``memory/pending_candidates.py``).  A candidate
previewed by one worker must be confirmed by the same worker; multi-worker
deployments need a persisted candidate table, which P0 does not have.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from water_agent.memory.contracts import MemoryError as DomainMemoryError
from water_agent.memory.contracts import (
    MemoryKind,
    MemoryRecord,
    MemoryScopeDeniedError,
    MemoryScopeType,
    MemoryStatus,
)
from water_agent.memory.recall import MemoryRecallService
from water_agent.memory.service import MemoryService
from water_agent.memory.write_contracts import (
    ConflictResolution,
    MemoryCandidate,
    MemoryConfirmation,
)
from water_agent.memory.write_service import ExplicitMemoryWriteService
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_password_changed

router = APIRouter()
_API_VERSION = "v1"

#: Stable error code for the honest "P0 memory has no reactivation path" answer.
MEMORY_STATUS_TRANSITION_INVALID = "MEMORY_STATUS_TRANSITION_INVALID"

_ERROR_STATUS: dict[str, int] = {
    "MEMORY_NOT_FOUND": 404,
    "MEMORY_SCOPE_DENIED": 404,
    "MEMORY_SECRET_BLOCKED": 422,
    "MEMORY_PII_BLOCKED": 422,
    "MEMORY_INJECTION_BLOCKED": 422,
    "MEMORY_KIND_INVALID": 422,
    "MEMORY_REVISION_CONFLICT": 409,
    "MEMORY_CONFLICT_REQUIRES_CONFIRMATION": 409,
    MEMORY_STATUS_TRANSITION_INVALID: 409,
    "MEMORY_EXPIRED": 410,
    "MEMORY_FORGOTTEN": 410,
    "MEMORY_STORE_UNAVAILABLE": 503,
    "MEMORY_RECALL_BUDGET_EXCEEDED": 400,
}


def _ok(data: object, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"version": _API_VERSION, "ok": True, "data": data},
    )


def _error(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=_ERROR_STATUS.get(code, 400),
        content={"version": _API_VERSION, "ok": False, "error": {"code": code, "message": message}},
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _memory_payload(memory: MemoryRecord) -> dict[str, Any]:
    """Serialize one record; a tombstone never carries a body."""

    return {
        "memory_id": str(memory.memory_id),
        "scope_type": memory.scope_type.value,
        "project_id": str(memory.project_id) if memory.project_id is not None else None,
        "kind": memory.kind.value,
        "semantic_key": memory.semantic_key,
        "content": "" if memory.tombstone else memory.content,
        "status": memory.status.value,
        "revision": memory.revision,
        "origin": memory.origin.value,
        "created_at": _iso(memory.created_at),
        "updated_at": _iso(memory.updated_at),
        "expires_at": _iso(memory.expires_at),
        "source_thread_id": (
            str(memory.source_thread_id) if memory.source_thread_id is not None else None
        ),
        "source_message_id": (
            str(memory.source_message_id) if memory.source_message_id is not None else None
        ),
        "tombstone": memory.tombstone,
    }


def _candidate_payload(candidate: MemoryCandidate) -> dict[str, Any]:
    conflict = candidate.conflict
    return {
        "candidate_id": str(candidate.candidate_id),
        "operation": candidate.operation.value,
        "disposition": candidate.disposition.value,
        "scope_type": candidate.scope_type.value,
        "project_id": str(candidate.project_id) if candidate.project_id is not None else None,
        "kind": candidate.kind.value,
        "semantic_key": candidate.semantic_key,
        "content": candidate.content,
        "content_hash": candidate.content_hash,
        "memory_expires_at": _iso(candidate.memory_expires_at),
        "requires_confirmation": candidate.requires_confirmation,
        "target_memory_id": (
            str(candidate.target_memory_id) if candidate.target_memory_id is not None else None
        ),
        "expected_revision": candidate.expected_revision,
        "value_basis": candidate.value_basis.value,
        "guard_rule_ids": list(candidate.guard_rule_ids),
        "conflict": (
            {
                "memory_id": str(conflict.memory_id),
                "revision": conflict.revision,
                "current_content": conflict.current_content,
                "current_content_hash": conflict.current_content_hash,
                "proposed_content": conflict.proposed_content,
                "proposed_content_hash": conflict.proposed_content_hash,
            }
            if conflict is not None
            else None
        ),
    }


def _services(request: Request) -> tuple[MemoryService, ExplicitMemoryWriteService] | None:
    service = getattr(request.app.state, "memory_service", None)
    writes = getattr(request.app.state, "memory_write_service", None)
    if isinstance(service, MemoryService) and isinstance(writes, ExplicitMemoryWriteService):
        return service, writes
    return None


def _unavailable() -> JSONResponse:
    return _error("MEMORY_STORE_UNAVAILABLE", "Long-term memory is not configured")


def _actor(auth: tuple) -> tuple[Any, UUID]:
    user, _principal = auth
    return user, UUID(str(user.user_id))


def _tenant(request: Request) -> UUID:
    return UUID(str(request.app.state.tenant_id))


def _handle(exc: DomainMemoryError) -> JSONResponse:
    return _error(exc.code, exc.message)


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: MemoryScopeType
    kind: MemoryKind
    semantic_key: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=4000)
    project_id: UUID | None = None
    expires_at: datetime | None = None


class ConfirmationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: bool
    content_hash: str = Field(min_length=1, max_length=128)
    resolution: ConflictResolution | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    alternative_semantic_key: str | None = Field(default=None, min_length=1, max_length=255)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    confirmation: ConfirmationBody


class PatchRequest(BaseModel):
    """Correct a body, or ask for enable/disable.  Exactly one intent per call."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    enabled: bool | None = None


@router.get("/api/memories")
async def list_memories(
    request: Request,
    scope: Annotated[str | None, Query()] = None,
    project_id: Annotated[UUID | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    query: Annotated[str | None, Query(max_length=512)] = None,
    thread_id: Annotated[UUID | None, Query()] = None,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    """List or recall only the authenticated actor's authorized memories."""

    services = _services(request)
    if services is None:
        return _unavailable()
    service, _writes = services
    user, actor = _actor(auth)
    tenant = _tenant(request)
    try:
        if query and query.strip():
            result = MemoryRecallService(
                request.app.state.database,
                provider=getattr(request.app.state, "memory_provider", None),
            ).recall(
                tenant_id=tenant,
                actor_user_id=actor,
                query=query,
                project_id=project_id,
                source_thread_id=thread_id,
            )
            records = [hit.memory for hit in result.hits]
        else:
            records = service.list_active(tenant_id=tenant, actor_user_id=actor)
        payload = [_memory_payload(record) for record in records]
        if scope in {MemoryScopeType.USER.value, MemoryScopeType.PROJECT.value}:
            payload = [item for item in payload if item["scope_type"] == scope]
        if project_id is not None:
            target = str(project_id)
            payload = [item for item in payload if item["project_id"] == target]
        if kind in {member.value for member in MemoryKind}:
            payload = [item for item in payload if item["kind"] == kind]
    except DomainMemoryError as exc:
        return _handle(exc)
    data: dict[str, Any] = {"memories": payload}
    current = _current_answer(request, user.user_id, thread_id)
    if current is not None:
        data["current_answer"] = current
    return _ok(data)


def _current_answer(request: Request, user_id: UUID, thread_id: UUID | None) -> Any | None:
    """Project the last run's real ``memory_recall`` event; never guess bodies."""

    if thread_id is None:
        return None
    manager = getattr(request.app.state, "run_manager", None)
    database = getattr(request.app.state, "database", None)
    if manager is None or database is None:
        return None
    try:
        from water_agent.storage.repository import RunRepository

        runs = RunRepository(database).list_for_session(thread_id, user_id)
        if not runs:
            return None
        latest = runs[0]
        trace = manager.trace(latest.run_id)
    except Exception:  # noqa: BLE001 - a missing projection is not an authorization answer
        return None
    for event in reversed(trace.get("events") or []):
        if not isinstance(event, dict) or event.get("event_type") != "memory_recall":
            continue
        data = event.get("data")
        ids = data.get("memory_ids") if isinstance(data, dict) else None
        if isinstance(ids, list):
            return {"memory_ids": [str(item) for item in ids], "run_id": str(latest.run_id)}
    return None


@router.post("/api/memories/candidates", dependencies=[Depends(validate_csrf)])
async def create_candidate(
    body: CandidateRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    """Preview a governed write; nothing is persisted until it is confirmed."""

    services = _services(request)
    if services is None:
        return _unavailable()
    _service, writes = services
    _user, actor = _actor(auth)
    try:
        candidate = writes.candidate(
            tenant_id=_tenant(request),
            actor_user_id=actor,
            scope_type=body.scope_type,
            kind=body.kind,
            semantic_key=body.semantic_key,
            content=body.content,
            project_id=body.project_id,
            expires_at=body.expires_at,
        )
    except DomainMemoryError as exc:
        if isinstance(exc, MemoryScopeDeniedError):
            # Scope authorization is a permission answer, not a missing resource.
            return JSONResponse(
                status_code=403,
                content={
                    "version": _API_VERSION,
                    "ok": False,
                    "error": {"code": exc.code, "message": exc.message},
                },
            )
        return _handle(exc)
    return _ok(_candidate_payload(candidate), status_code=201)


@router.post("/api/memories", dependencies=[Depends(validate_csrf)])
async def confirm_candidate(
    body: ConfirmRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    """Apply exactly the candidate this actor previewed and confirmed."""

    services = _services(request)
    if services is None:
        return _unavailable()
    _service, writes = services
    _user, actor = _actor(auth)
    confirmation = MemoryConfirmation(
        confirmed=body.confirmation.confirmed,
        content_hash=body.confirmation.content_hash,
        resolution=body.confirmation.resolution,
        expected_revision=body.confirmation.expected_revision,
        alternative_semantic_key=body.confirmation.alternative_semantic_key,
    )
    try:
        result = writes.confirm(
            body.candidate_id,
            tenant_id=_tenant(request),
            actor_user_id=actor,
            confirmation=confirmation,
        )
    except DomainMemoryError as exc:
        return _handle(exc)
    return _ok(
        {
            "action": result.action.value,
            "memory": _memory_payload(result.memory) if result.memory is not None else None,
            "content_hash": result.content_hash,
        },
        status_code=201,
    )


@router.get("/api/memories/{memory_id}")
async def get_memory(
    memory_id: UUID,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    service, _writes = services
    _user, actor = _actor(auth)
    try:
        memory = service.get_memory(
            memory_id, tenant_id=_tenant(request), actor_user_id=actor
        )
    except DomainMemoryError as exc:
        return _handle(exc)
    return _ok(_memory_payload(memory))


@router.patch("/api/memories/{memory_id}", dependencies=[Depends(validate_csrf)])
async def patch_memory(
    memory_id: UUID,
    body: PatchRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    """Correct a body, or disable a memory.  Reactivation has no P0 path."""

    services = _services(request)
    if services is None:
        return _unavailable()
    _service, writes = services
    _user, actor = _actor(auth)
    tenant = _tenant(request)
    if (body.content is None) == (body.enabled is None):
        return _error("MEMORY_KIND_INVALID", "exactly one of content or enabled is required")
    if body.enabled is not None:
        if body.enabled:
            return _error(
                MEMORY_STATUS_TRANSITION_INVALID,
                "P0 memory is forward-only: save a new memory instead of re-enabling",
            )
        try:
            disabled = writes.memory_service.transition_memory(
                memory_id,
                tenant_id=tenant,
                actor_user_id=actor,
                expected_revision=body.expected_revision,
                target_status=MemoryStatus.EXPIRED,
                reason="disabled from the memory workspace",
            )
        except DomainMemoryError as exc:
            return _handle(exc)
        return _ok(_memory_payload(disabled))
    if body.content is None:
        return _error("MEMORY_KIND_INVALID", "content is required to correct a memory")
    try:
        candidate = writes.correct(
            memory_id,
            tenant_id=tenant,
            actor_user_id=actor,
            expected_revision=body.expected_revision,
            content=body.content,
        )
        result = writes.confirm(
            candidate.candidate_id,
            tenant_id=tenant,
            actor_user_id=actor,
            confirmation=MemoryConfirmation(
                confirmed=True,
                content_hash=candidate.content_hash,
                expected_revision=candidate.expected_revision,
            ),
        )
    except DomainMemoryError as exc:
        return _handle(exc)
    if result.memory is None:  # pragma: no cover - confirm always returns a record here
        return _error("MEMORY_STORE_UNAVAILABLE", "correction did not produce a record")
    return _ok(_memory_payload(result.memory))


@router.delete("/api/memories/{memory_id}", dependencies=[Depends(validate_csrf)])
async def forget_memory(
    memory_id: UUID,
    body: PatchRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    """Forget one memory; the tombstone keeps only identifiers and hashes."""

    services = _services(request)
    if services is None:
        return _unavailable()
    _service, writes = services
    _user, actor = _actor(auth)
    try:
        result = writes.forget(
            memory_id,
            tenant_id=_tenant(request),
            actor_user_id=actor,
            expected_revision=body.expected_revision,
            confirmed=True,
        )
    except DomainMemoryError as exc:
        return _handle(exc)
    if result.memory is None:  # pragma: no cover - forget always returns a tombstone
        return _error("MEMORY_STORE_UNAVAILABLE", "forget did not produce a tombstone")
    return _ok(
        {
            "memory_id": str(result.memory.memory_id),
            "status": result.memory.status.value,
            "revision": result.memory.revision,
        }
    )


@router.get("/api/memories/{memory_id}/events")
async def memory_events(
    memory_id: UUID,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    service, _writes = services
    _user, actor = _actor(auth)
    try:
        events = service.list_events(
            memory_id, tenant_id=_tenant(request), actor_user_id=actor
        )
    except DomainMemoryError as exc:
        return _handle(exc)
    return _ok(
        {
            "events": [
                {
                    "event_id": str(event.event_id),
                    "event_type": event.event_type.value,
                    "memory_id": str(event.memory_id),
                    "revision": event.revision,
                    "reason": event.reason,
                    "created_at": _iso(event.created_at),
                }
                for event in events
            ]
        }
    )


@router.get("/api/memories/{memory_id}/usage")
async def memory_usage(
    memory_id: UUID,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    service, _writes = services
    _user, actor = _actor(auth)
    try:
        usage = service.list_usage(memory_id, tenant_id=_tenant(request), actor_user_id=actor)
    except DomainMemoryError as exc:
        return _handle(exc)
    return _ok(
        {
            "usage": [
                {
                    "usage_id": str(item.usage_id),
                    "memory_id": str(item.memory_id),
                    "source_thread_id": (
                        str(item.source_thread_id) if item.source_thread_id is not None else None
                    ),
                    "run_id": str(item.run_id) if item.run_id is not None else None,
                    "score": item.score,
                    "created_at": _iso(item.created_at),
                }
                for item in usage
            ]
        }
    )


__all__ = [
    "MEMORY_STATUS_TRANSITION_INVALID",
    "CandidateRequest",
    "ConfirmRequest",
    "PatchRequest",
    "router",
]
