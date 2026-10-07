"""FastAPI routes for the Step 42 Web Agent run lifecycle."""

from __future__ import annotations

import contextlib
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy.exc import SQLAlchemyError

from water_agent.documents.attachments import MAX_ATTACHMENTS_PER_MESSAGE
from water_agent.documents.repository import UploadRepository
from water_agent.graphs.confirmation import STATUS_WAITING_CONFIRMATION
from water_agent.runtime.agentic import (
    embedding_configured,
    embedding_required,
    real_llm_configured,
)
from water_agent.storage.repository import (
    ProjectRepository,
    ResourceNotFoundError,
    RunRepository,
    WebSessionRepository,
)
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_password_changed
from water_agent.webapp.auth.user_store import UserNotFoundError, get_user_store
from water_agent.webapp.run_manager import (
    ConfirmationStaleError,
    RunManager,
    RunNotManagedError,
    ThreadBusyError,
    WebAgenticSessionFactory,
    parse_last_event_id,
)
from water_agent.webapp.workspace_routes import _can_write_project

router = APIRouter()
_API_VERSION = "v1"


def _ok(data: object) -> dict[str, object]:
    return {"version": _API_VERSION, "ok": True, "data": data}


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=16_000)
    #: P5-S49：随消息附上的上传 id。上限与 documents.attachments 一致。
    attachments: list[UUID] = Field(default_factory=list, max_length=MAX_ATTACHMENTS_PER_MESSAGE)

    @field_validator("message")
    @classmethod
    def _reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class ConfirmationDecisionRequest(BaseModel):
    """对挂起的人工确认给出决定（P7-S7）。``approved`` 只认严格 bool（``"yes"``、``1`` 一律 422）。"""

    model_config = ConfigDict(extra="forbid")

    confirmation_id: str = Field(min_length=1, max_length=128)
    approved: StrictBool
    reason: str | None = Field(default=None, max_length=500)


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


def _thread_busy() -> JSONResponse:
    """409：该会话还有未收口的 run（等它结束或先取消，再发下一条）。"""

    return JSONResponse(
        status_code=409,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {
                "code": "WEB_RUN_IN_PROGRESS",
                "message": "This session is still answering the previous message",
            },
        },
    )


def _confirmation_pending() -> JSONResponse:
    """409：会话停在人工确认门（P7-S7），先对挂起的确认做决定，再发下一条。"""

    return JSONResponse(
        status_code=409,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {
                "code": "WEB_CONFIRMATION_PENDING",
                "message": "This session is waiting for a tool confirmation",
            },
        },
    )


def _confirmation_stale() -> JSONResponse:
    """409：run 没有在等这一个确认（已决定、重复提交、过期页面）。"""

    return JSONResponse(
        status_code=409,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {
                "code": "WEB_CONFIRMATION_STALE",
                "message": "This run is not waiting for this confirmation",
            },
        },
    )


def _run_unavailable() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {"code": "WEB_RUN_UNAVAILABLE", "message": "Run could not be started"},
        },
    )


def _not_configured(code: str, message: str) -> JSONResponse:
    """424：依赖的凭据/配置缺失。每种缺失给独立 code，便于自助排查。"""

    return JSONResponse(
        status_code=424,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {"code": code, "message": message},
        },
    )


def _forbidden() -> JSONResponse:
    """403：会话可读，但当前账号在该项目里没有写权限。"""

    return JSONResponse(
        status_code=403,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {"code": "WEB_FORBIDDEN", "message": "This action is not authorized"},
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

    # Authorization precedes configuration disclosure: an unowned thread must
    # answer 404 and must not reveal whether a model is configured.  The run row
    # is only created once both checks pass, so a refused request cannot leave a
    # queued run behind (Step 45 memory_p0 mem-p0-0009).
    sessions = WebSessionRepository(database)
    try:
        web_session = sessions.get_for_user(thread_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    # 能读不等于能写：在项目会话里发消息会以本人名义建 run、写该会话的 checkpoint，
    # 与新建项目会话同一条写权限规则（项目 viewer、全局 VIEWER 一律 403）。
    if web_session.project_id is not None:
        try:
            role = ProjectRepository(database).membership_role(
                web_session.project_id, user.user_id
            )
        except ResourceNotFoundError:
            return _not_found()
        if not _can_write_project(role, user.role):
            return _forbidden()

    factory = manager.session_factory
    if isinstance(factory, WebAgenticSessionFactory) and not factory.mock_generator:
        if not real_llm_configured():
            return _not_configured(
                "WEB_MODEL_NOT_CONFIGURED", "Real agent model is not configured"
            )
        # 检索用的 embedding 凭据独立于对话模型。两者配齐才算可跑：只配了对话模型
        # 时，装配会在构造 embedding provider 时抛 ValueError，浏览器只看到通用的
        # "agent run failed"，而这纯粹是配置缺失——独立错误码让它可自助排查。
        # 刻意不降级为 fixture 检索：那会让答案看似正常、实则脱离真实语料。
        if embedding_required(factory.agent_args) and not embedding_configured():
            return _not_configured(
                "WEB_EMBEDDING_NOT_CONFIGURED",
                "Retrieval embedding credential is not configured",
            )

    # 附件归属在建 run 之前校验：不可见与不存在同答 404，且不留下 queued run。
    attachment_ids = [str(item) for item in dict.fromkeys(body.attachments)]
    if attachment_ids:
        uploads = UploadRepository(database)
        try:
            for upload_id in attachment_ids:
                uploads.get_for_user(upload_id, user.user_id)
        except ResourceNotFoundError:
            return _not_found()

    # 同一会话同一时间只跑一个 run：两个 run 写同一个 checkpoint，后写者覆盖先写者，
    # 先问的问题和回答从历史里静默消失。检查放在授权之后（不向无权者透露会话状态）、
    # 建 run 行之前（被拒的请求不留 queued 行）。
    if manager.thread_busy(thread_id):
        return _thread_busy()
    # 会话停在人工确认门（P7-S7）：新问题会叠在挂起的轮次上，checkpoint 里出现没有应答的
    # tool_call。判定读 checkpoint（不读进程内状态：重启后照样拦得住）；判定不了就不建 run。
    try:
        pending = manager.pending_confirmation(thread_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    except (RuntimeError, OSError, SQLAlchemyError):
        return _run_unavailable()
    if pending is not None:
        return _confirmation_pending()

    try:
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
            attachment_ids=attachment_ids,
        )
    except ThreadBusyError:
        # 上面的检查与 start 之间没有 await，单 worker 下走不到这里；兜底时把刚建的行收口，
        # 不留一个永远 queued 的 run。
        with contextlib.suppress(ResourceNotFoundError, SQLAlchemyError):
            RunRepository(database).set_status(
                run.run_id, user.user_id, "cancelled", expected_statuses=("queued",)
            )
        return _thread_busy()
    except (RuntimeError, ValueError):
        # A run row without a managed event log is not a successful API call.
        # Keep the public response generic; the repository row remains queued
        # and can be diagnosed by the server trace, never by the browser.
        return _run_unavailable()
    return JSONResponse(status_code=202, content=_ok(payload))


@router.post(
    "/api/runs/{run_id}/confirmation",
    dependencies=[Depends(validate_csrf)],
)
async def decide_confirmation(
    run_id: UUID,
    body: ConfirmationDecisionRequest,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    """对停在人工确认门的 run 给出决定（P7-S7），在同一个 run 上继续执行。

    检查顺序与发消息一致：归属（只有 run 的所有者，别人的 / 不存在的同答 404）→ 项目写权限（403）
    → run 确实在等**这一个**确认（库里是 ``waiting_confirmation``、checkpoint 挂起的 ``confirmation_id``
    与请求一致，否则 409 ``WEB_CONFIRMATION_STALE``）→ 202。返回的 ``event_cursor`` 是重新订阅事件流时
    应带的 ``Last-Event-ID``。
    """

    user, principal = auth
    database = getattr(request.app.state, "database", None)
    manager = _manager(request)
    if database is None or manager is None:
        return _database_unavailable()
    try:
        run = RunRepository(database).get_owned_for_user(run_id, user.user_id)
        web_session = WebSessionRepository(database).get_for_user(run.thread_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    # 确认的是"我的 agent 要替我做这件事"：与发消息同一条写权限规则（被降为 viewer 后不能再批准）
    if web_session.project_id is not None:
        try:
            role = ProjectRepository(database).membership_role(
                web_session.project_id, user.user_id
            )
        except ResourceNotFoundError:
            return _not_found()
        if not _can_write_project(role, user.role):
            return _forbidden()
    if run.status != STATUS_WAITING_CONFIRMATION:
        return _confirmation_stale()
    try:
        pending = manager.pending_confirmation(run.thread_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    except (RuntimeError, OSError, SQLAlchemyError):
        return _run_unavailable()
    if pending is None or pending.get("confirmation_id") != body.confirmation_id:
        return _confirmation_stale()
    try:
        payload = manager.resume(
            run_id=run.run_id,
            thread_id=run.thread_id,
            owner_user_id=user.user_id,
            principal=principal,
            project_id=web_session.project_id,
            decision={
                "confirmation_id": body.confirmation_id,
                "approved": body.approved,
                "reason": body.reason,
            },
        )
    except ConfirmationStaleError:
        return _confirmation_stale()
    except ThreadBusyError:
        return _thread_busy()
    except (RuntimeError, ValueError):
        return _run_unavailable()
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


__all__ = ["ConfirmationDecisionRequest", "MessageRequest", "router"]
