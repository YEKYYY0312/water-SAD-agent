"""Step 44 project, session and feedback workspace routes.

The routes are deliberately thin: repositories own resource authorization and
the existing run manager remains the only Agent execution path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.storage.repository import (
    FeedbackCandidateRepository,
    ProjectRepository,
    ResourceNotFoundError,
    RunRepository,
    WebSessionRepository,
)
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_password_changed
from water_agent.webapp.auth.models import UserRole

router = APIRouter()
_API_VERSION = "v1"


class ProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=240)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal["useful", "not_useful"]
    comment: str = Field(default="", max_length=4000)


def _ok(data: object) -> dict[str, object]:
    return {"version": _API_VERSION, "ok": True, "data": data}


def _error(code: str, message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {"code": code, "message": message},
        },
    )


def _database(request: Request):
    return getattr(request.app.state, "database", None)


def _database_unavailable() -> JSONResponse:
    return _error(
        "WEB_DATABASE_UNAVAILABLE",
        "Web persistence is not configured",
        status_code=503,
    )


def _not_found() -> JSONResponse:
    return _error("WEB_NOT_FOUND", "Resource not found", status_code=404)


def _forbidden() -> JSONResponse:
    return _error("WEB_FORBIDDEN", "This action is not authorized", status_code=403)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _project_payload(project, role: str) -> dict[str, str]:
    return {
        "project_id": project.project_id,
        "name": project.name,
        "role": role,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
    }


def _run_payload(run) -> dict[str, str]:
    return {
        "run_id": run.run_id,
        "thread_id": run.thread_id,
        "status": run.status,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
    }


def _session_payload(session, runs: list | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "thread_id": session.thread_id,
        "project_id": session.project_id,
        "title": session.title,
        "created_at": _iso(session.created_at),
        "updated_at": _iso(session.updated_at),
    }
    if runs is not None:
        payload["runs"] = [_run_payload(run) for run in runs]
        payload["latest_run"] = _run_payload(runs[0]) if runs else None
    return payload


def _can_write_project(role: str, user_role: UserRole) -> bool:
    return user_role is not UserRole.VIEWER and role in {"owner", "member"}


def _safe_feedback_comment(value: str) -> str:
    """Keep feedback useful while applying the existing redaction boundary."""

    return JsonTraceRecorder._redact(value.strip())[:4000]


@router.get("/api/projects")
async def list_projects(
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    database = _database(request)
    if database is None:
        return _database_unavailable()
    projects = ProjectRepository(database).list_for_user(user.user_id)
    return JSONResponse(
        status_code=200,
        content=_ok({"projects": [_project_payload(project, role) for project, role in projects]}),
    )


@router.post("/api/projects", dependencies=[Depends(validate_csrf)])
async def create_project(
    body: ProjectRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    if user.role is UserRole.VIEWER:
        return _forbidden()
    name = body.name.strip()
    if not name:
        return _error("WEB_INVALID_REQUEST", "Project name must not be blank", status_code=422)
    database = _database(request)
    if database is None:
        return _database_unavailable()
    project = ProjectRepository(database).create(user.user_id, name)
    return JSONResponse(
        status_code=201,
        content=_ok(_project_payload(project, "owner")),
    )


@router.get("/api/projects/{project_id}")
async def get_project(
    project_id: UUID,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    database = _database(request)
    if database is None:
        return _database_unavailable()
    try:
        repository = ProjectRepository(database)
        project = repository.get_for_user(project_id, user.user_id)
        role = repository.membership_role(project_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    return JSONResponse(status_code=200, content=_ok(_project_payload(project, role)))


@router.get("/api/projects/{project_id}/sessions")
async def list_project_sessions(
    project_id: UUID,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    database = _database(request)
    if database is None:
        return _database_unavailable()
    try:
        ProjectRepository(database).get_for_user(project_id, user.user_id)
        sessions = WebSessionRepository(database).list_for_user(
            user.user_id, project_id=project_id
        )
        runs = RunRepository(database)
    except ResourceNotFoundError:
        return _not_found()
    return JSONResponse(
        status_code=200,
        content=_ok(
            {
                "sessions": [
                    _session_payload(session, runs.list_for_session(session.thread_id, user.user_id))
                    for session in sessions
                ]
            }
        ),
    )


@router.post("/api/projects/{project_id}/sessions", dependencies=[Depends(validate_csrf)])
async def create_project_session(
    project_id: UUID,
    body: SessionRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    database = _database(request)
    if database is None:
        return _database_unavailable()
    try:
        projects = ProjectRepository(database)
        role = projects.membership_role(project_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    if not _can_write_project(role, user.role):
        return _forbidden()
    try:
        session = WebSessionRepository(database).create(
            user.user_id, project_id=project_id, title=body.title.strip()
        )
    except ResourceNotFoundError:
        return _not_found()
    return JSONResponse(status_code=201, content=_ok(_session_payload(session, [])))


@router.post("/api/sessions", dependencies=[Depends(validate_csrf)])
async def create_personal_session(
    body: SessionRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    if user.role is UserRole.VIEWER:
        return _forbidden()
    database = _database(request)
    if database is None:
        return _database_unavailable()
    session = WebSessionRepository(database).create(user.user_id, title=body.title.strip())
    return JSONResponse(status_code=201, content=_ok(_session_payload(session, [])))


@router.get("/api/sessions")
async def list_personal_sessions(
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    database = _database(request)
    if database is None:
        return _database_unavailable()
    sessions = WebSessionRepository(database).list_for_user(user.user_id, personal_only=True)
    runs = RunRepository(database)
    return JSONResponse(
        status_code=200,
        content=_ok(
            {
                "sessions": [
                    _session_payload(session, runs.list_for_session(session.thread_id, user.user_id))
                    for session in sessions
                ]
            }
        ),
    )


@router.get("/api/sessions/{thread_id}")
async def get_session(
    thread_id: UUID,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    database = _database(request)
    if database is None:
        return _database_unavailable()
    try:
        session = WebSessionRepository(database).get_for_user(thread_id, user.user_id)
        runs = RunRepository(database).list_for_session(thread_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    return JSONResponse(
        status_code=200,
        content=_ok(_session_payload(session, runs)),
    )


@router.get("/api/sessions/{thread_id}/messages")
async def get_session_messages(
    thread_id: UUID,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    """Conversation history for the workbench (P5-S49).

    Ownership is checked against ``web_sessions`` **before** the checkpointer is
    opened (same order as ``POST .../messages``): an unowned thread answers 404
    and never costs a checkpoint read.  The saver re-authorizes as well.
    """

    user, _ = auth
    database = _database(request)
    manager = getattr(request.app.state, "run_manager", None)
    if database is None or manager is None:
        return _database_unavailable()
    try:
        WebSessionRepository(database).get_for_user(thread_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    try:
        messages = manager.thread_messages(thread_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    except (RuntimeError, OSError, SQLAlchemyError):
        return _error(
            "WEB_HISTORY_UNAVAILABLE",
            "Conversation history could not be loaded",
            status_code=503,
        )
    return JSONResponse(
        status_code=200,
        content=_ok({"thread_id": str(thread_id), "messages": messages}),
    )


@router.post("/api/runs/{run_id}/feedback", dependencies=[Depends(validate_csrf)])
async def create_run_feedback(
    run_id: UUID,
    body: FeedbackRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    database = _database(request)
    if database is None:
        return _database_unavailable()
    try:
        run = RunRepository(database).get_for_user(run_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    record, created = FeedbackCandidateRepository(database).create_or_get(
        run_id=run.run_id,
        thread_id=run.thread_id,
        user_id=user.user_id,
        rating=body.rating,
        comment=_safe_feedback_comment(body.comment),
    )
    payload = {
        "feedback_id": record.feedback_id,
        "run_id": record.run_id,
        "rating": record.rating,
        "status": record.status,
        "created_at": _iso(record.created_at),
        "created": created,
    }
    return JSONResponse(status_code=201 if created else 200, content=_ok(payload))


__all__ = ["router"]
