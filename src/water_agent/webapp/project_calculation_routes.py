"""Explicit project calculations; feature-off is inert, ordinary QA is unchanged."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from water_agent.storage.repository import ResourceNotFoundError
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_password_changed
from water_agent.webapp.auth.user_store import UserNotFoundError

router = APIRouter()


class PreviewRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    plan: dict[str, Any] = Field(max_length=16)
    base_revision: int = Field(ge=0)
    generation: int = Field(ge=0)


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    confirmation_id: str = Field(min_length=36, max_length=36, pattern=r"^[a-f0-9-]{36}$")
    expected_plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    approved: bool | int | Annotated[str, Field(max_length=16)] | None
    reason: str = Field(min_length=1, max_length=1000)


def _reply(action):
    try:
        return JSONResponse({"version": "v1", "ok": True, "data": action()})
    except (ResourceNotFoundError, UserNotFoundError):
        code, status = "WEB_NOT_FOUND", 404
    except PermissionError:
        code, status = "WEB_FORBIDDEN", 403
    except ValidationError:
        code, status = "PROJECT_INVALID_PLAN", 422
    except (ValueError, RuntimeError) as exc:
        code = str(exc) if str(exc).startswith("PROJECT_") else "PROJECT_PERSISTENCE_FAILED"
        status = (
            404
            if code == "PROJECT_SCOPE_MISMATCH"
            else 503
            if code in ("PROJECT_PERSISTENCE_FAILED", "PROJECT_OUTPUT_BLOCKED")
            else 409
        )
    except (OSError, SQLAlchemyError):
        code, status = "PROJECT_PERSISTENCE_FAILED", 503
    return JSONResponse(
        {
            "version": "v1",
            "ok": False,
            "error": {"code": code, "message": "Project operation unavailable"},
        },
        status_code=status,
    )


def _runtime(request):
    manager = getattr(request.app.state, "run_manager", None)
    runtime = getattr(manager, "project_runtime", None)
    if runtime is None:
        raise ResourceNotFoundError("feature unavailable")
    return runtime


@router.post(
    "/api/sessions/{thread_id}/project-calculations/preview",
    dependencies=[Depends(require_password_changed), Depends(validate_csrf)],
)
def preview(
    thread_id: UUID,
    body: PreviewRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
):
    user, _ = auth
    return _reply(
        lambda: _runtime(request).preview(
            user.user_id, thread_id, body.plan, body.base_revision, body.generation
        )
    )


@router.post(
    "/api/sessions/{thread_id}/project-calculations/execute",
    dependencies=[Depends(require_password_changed), Depends(validate_csrf)],
)
def execute(
    thread_id: UUID,
    body: ExecuteRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
):
    user, _ = auth
    result = _reply(
        lambda: _runtime(request).execute(
            user.user_id, thread_id, body.confirmation_id, body.expected_plan_hash
        )
    )
    if result.status_code == 200:
        result.status_code = 202
    return result


@router.post(
    "/api/runs/{run_id}/project-approval",
    dependencies=[Depends(require_password_changed), Depends(validate_csrf)],
)
def approve(
    run_id: UUID,
    body: ApprovalRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
):
    user, _ = auth
    return _reply(
        lambda: _runtime(request).approve(run_id, user.user_id, body.approved, body.reason)
    )


@router.get("/api/runs/{run_id}/project-calculation")
def state(run_id: UUID, request: Request, auth: tuple = Depends(require_password_changed)):
    user, _ = auth
    return _reply(lambda: _runtime(request).state(run_id, user.user_id))


@router.get("/api/sessions/{thread_id}/project-calculations")
def head(thread_id: UUID, request: Request, auth: tuple = Depends(require_password_changed)):
    user, _ = auth
    return _reply(lambda: _runtime(request).head(user.user_id, thread_id))


@router.get("/api/runs/{run_id}/project-approval")
def approval_annotation(
    run_id: UUID, request: Request, auth: tuple = Depends(require_password_changed)
):
    user, _ = auth
    response = _reply(lambda: _runtime(request).approval_metadata(run_id, user.user_id))
    if response.status_code == 200:
        response.headers["Content-Disposition"] = f'attachment; filename="approval-{run_id}.json"'
    return response
