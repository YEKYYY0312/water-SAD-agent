"""FastAPI routes for the Step 42 Web Agent run lifecycle."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from water_agent.runtime.agentic import real_llm_configured
from water_agent.storage.repository import (
    ResourceNotFoundError,
    RunRepository,
    WebSessionRepository,
)
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_password_changed
from water_agent.webapp.auth.user_store import UserNotFoundError, get_user_store
from water_agent.webapp.run_manager import (
    RunManager,
    RunNotManagedError,
    WebAgenticSessionFactory,
    parse_last_event_id,
)

router = APIRouter()
_API_VERSION = "v1"


def _ok(data: object) -> dict[str, object]:
    return {"version": _API_VERSION, "ok": True, "data": data}


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=16_000)

    @field_validator("message")
    @classmethod
    def _reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


def _manager(request: Request) -> RunManager | None:
    value = getattr(request.app.state, "run_manager", None)
    return value if isinstance(value, RunManager) else None


def _database_unavailable() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {
                "code": "WEB_DATABASE_UNAVAILABLE",
                "message": "Web persistence is not configured",
            },
        },
    )


def _not_found() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {"code": "WEB_NOT_FOUND", "message": "Resource not found"},
        },
    )


@router.post(
    "/api/sessions/{thread_id}/messages",
    dependencies=[Depends(validate_csrf)],
)
async def create_message(
    thread_id: UUID,
    body: MessageRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, principal = auth
    database = getattr(request.app.state, "database", None)
    manager = _manager(request)
    if database is None or manager is None:
        return _database_unavailable()

    factory = manager.session_factory
    if isinstance(factory, WebAgenticSessionFactory) and not factory.mock_generator and not real_llm_configured():
            return JSONResponse(
                status_code=424,
                content={
                    "version": _API_VERSION,
                    "ok": False,
                    "error": {
                        "code": "WEB_MODEL_NOT_CONFIGURED",
                        "message": "Real agent model is not configured",
                    },
                },
            )

    sessions = WebSessionRepository(database)
    try:
        web_session = sessions.get_for_user(thread_id, user.user_id)
        run = RunRepository(database).create(thread_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()

    try:
        payload = manager.start(
            run_id=run.run_id,
            thread_id=run.thread_id,
            owner_user_id=user.user_id,
            principal=principal,
            project_id=web_session.project_id,
            query=body.message,
        )
    except (RuntimeError, ValueError):
        # A run row without a managed event log is not a successful API call.
        # Keep the public response generic; the repository row remains queued
        # and can be diagnosed by the server trace, never by the browser.
        return JSONResponse(
            status_code=503,
            content={
                "version": _API_VERSION,
                "ok": False,
                "error": {"code": "WEB_RUN_UNAVAILABLE", "message": "Run could not be started"},
            },
        )
    return JSONResponse(status_code=202, content=_ok(payload))


def _authorized_run(request: Request, run_id: UUID, user_id: UUID) -> bool:
    manager = _manager(request)
    if manager is None:
        return False
    try:
        current_user = get_user_store().get_by_id(user_id)
        if not current_user.is_active:
            return False
        manager.get_visible_run(run_id, user_id)
        return True
    except (ResourceNotFoundError, UserNotFoundError):
        return False


@router.get("/api/runs/{run_id}/events")
async def run_events(
    run_id: UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    auth: tuple = Depends(require_password_changed),
):
    user, _ = auth
    manager = _manager(request)
    if manager is None or getattr(request.app.state, "database", None) is None:
        return _database_unavailable()
    try:
        parse_last_event_id(last_event_id)
        manager.get_visible_run(run_id, user.user_id)
        manager.has_event_log(run_id)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={
                "version": _API_VERSION,
                "ok": False,
                "error": {"code": "WEB_EVENT_CURSOR_INVALID", "message": "Invalid Last-Event-ID"},
            },
        )
    except (ResourceNotFoundError, RunNotManagedError):
        return _not_found()
    return StreamingResponse(
        manager.stream(
            run_id,
            last_event_id=last_event_id,
            is_authorized=lambda: _authorized_run(request, run_id, user.user_id),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/api/runs/{run_id}/cancel",
    dependencies=[Depends(validate_csrf)],
)
async def cancel_run(
    run_id: UUID,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    manager = _manager(request)
    if manager is None or getattr(request.app.state, "database", None) is None:
        return _database_unavailable()
    try:
        payload = manager.cancel(run_id, user.user_id)
    except (ResourceNotFoundError, RunNotManagedError):
        return _not_found()
    return JSONResponse(status_code=202, content=_ok(payload))


@router.get("/api/runs/{run_id}/trace")
async def run_trace(
    run_id: UUID,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    manager = _manager(request)
    if manager is None or getattr(request.app.state, "database", None) is None:
        return _database_unavailable()
    try:
        manager.get_visible_run(run_id, user.user_id)
        payload = manager.trace(run_id)
    except (ResourceNotFoundError, RunNotManagedError):
        return _not_found()
    return JSONResponse(status_code=200, content=_ok(payload))


__all__ = ["MessageRequest", "router"]
