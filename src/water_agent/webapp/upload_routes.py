"""P5-S48 authorized document upload routes.

Deliberately thin, same discipline as ``memory_routes.py``: no parsing, no
guardrail policy, no storage layout is defined here.  Everything is delegated to
``water_agent.documents`` (``ingest_upload`` owns the order of operations, and
that order is the security property).

Two things this module does own, because they are HTTP concerns:

* **the byte ceiling is enforced while reading the stream**, not after.  A plain
  ``await file.read()`` would buffer a multi-gigabyte body into memory before the
  size check could ever run -- the check would be decorative.
* **authorization precedes everything else**, including parsing.  An upload
  aimed at a project the caller is not a member of must answer 404 without
  revealing whether that project exists, and without spending a parser on the
  bytes.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from water_agent.documents.errors import (
    CODE_UPLOAD_TOO_LARGE,
    UPLOAD_ERROR_STATUS,
    UploadError,
)
from water_agent.documents.extractors import MAX_UPLOAD_BYTES
from water_agent.documents.repository import UploadRecord, UploadRepository
from water_agent.documents.service import ingest_upload
from water_agent.documents.storage import remove_upload_files, resolve_upload_dir
from water_agent.storage.repository import ProjectRepository, ResourceNotFoundError
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_password_changed
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.run_manager import DEFAULT_TENANT_ID
from water_agent.webapp.workspace_routes import _can_write_project

router = APIRouter()
_API_VERSION = "v1"

#: Read granularity.  Small enough that an oversized body is refused after one
#: chunk past the limit, large enough not to make a 10 MB upload a syscall storm.
_READ_CHUNK_BYTES = 256 * 1024


def _ok(data: object, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"version": _API_VERSION, "ok": True, "data": data},
    )


def _error(code: str, message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {"code": code, "message": message},
        },
    )


def _upload_error(exc: UploadError) -> JSONResponse:
    return _error(exc.code, exc.message, status_code=UPLOAD_ERROR_STATUS[exc.code])


def _not_found() -> JSONResponse:
    return _error("WEB_NOT_FOUND", "Resource not found", status_code=404)


def _database_unavailable() -> JSONResponse:
    return _error(
        "WEB_DATABASE_UNAVAILABLE", "Web persistence is not configured", status_code=503
    )


def _database(request: Request):
    return getattr(request.app.state, "database", None)


def _tenant_id(request: Request) -> UUID:
    value = getattr(request.app.state, "tenant_id", None)
    return value if isinstance(value, UUID) else DEFAULT_TENANT_ID


def _upload_root(request: Request):
    """Uploads root: app-state override (tests) → the standard resolver."""

    return resolve_upload_dir(getattr(request.app.state, "upload_dir", None))


def _record_payload(record: UploadRecord) -> dict[str, Any]:
    """List/detail projection.  Never carries document body -- only metadata."""

    return {
        "upload_id": record.upload_id,
        "filename": record.safe_filename,
        "suffix": record.suffix,
        "size_bytes": record.size_bytes,
        "extracted_text_length": record.extracted_chars,
        "page_count": record.page_count,
        "chunk_count": record.chunk_count,
        "suspicious": record.suspicious,
        "redacted": record.redacted,
        "project_id": record.project_id,
        "created_at": record.created_at.isoformat(),
    }


async def _read_bounded(file: UploadFile) -> bytes:
    """Read the body, refusing as soon as it exceeds :data:`MAX_UPLOAD_BYTES`."""

    buffer = bytearray()
    while True:
        chunk = await file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_UPLOAD_BYTES:
            raise UploadError(
                CODE_UPLOAD_TOO_LARGE,
                f"文件超过上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
            )
    return bytes(buffer)


@router.post("/api/uploads", dependencies=[Depends(validate_csrf)])
async def create_upload(
    request: Request,
    file: Annotated[UploadFile, File()],
    project_id: Annotated[str | None, Form()] = None,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    database = _database(request)
    if database is None:
        return _database_unavailable()
    if user.role is UserRole.VIEWER:
        return _error("WEB_FORBIDDEN", "This action is not authorized", status_code=403)

    target_project: UUID | None = None
    raw_project = (project_id or "").strip()
    if raw_project:
        try:
            target_project = UUID(raw_project)
        except ValueError:
            return _error("WEB_INVALID_REQUEST", "Request data is invalid", status_code=422)
        # Membership first: an unowned project must be indistinguishable from a
        # non-existent one, and must not cost a parse of the uploaded bytes.
        try:
            role = ProjectRepository(database).membership_role(target_project, user.user_id)
        except ResourceNotFoundError:
            return _not_found()
        if not _can_write_project(role, user.role):
            return _not_found()

    try:
        payload = await _read_bounded(file)
        result = ingest_upload(
            database=database,
            tenant_id=_tenant_id(request),
            owner_user_id=user.user_id,
            project_id=target_project,
            filename=file.filename or "",
            payload=payload,
            upload_root=_upload_root(request),
        )
    except UploadError as exc:
        return _upload_error(exc)
    except OSError:
        # The uploads volume is not writable (a misconfigured deployment).  Say
        # so as a service condition instead of blaming the file.
        return _error(
            "WEB_UPLOAD_STORAGE_UNAVAILABLE",
            "Upload storage is not writable",
            status_code=503,
        )
    return _ok(result.response_payload(), status_code=201)


@router.get("/api/uploads")
async def list_personal_uploads(
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    database = _database(request)
    if database is None:
        return _database_unavailable()
    records = UploadRepository(database).list_personal(user.user_id)
    return _ok({"uploads": [_record_payload(record) for record in records]})


@router.get("/api/projects/{project_id}/uploads")
async def list_project_uploads(
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
    except ResourceNotFoundError:
        return _not_found()
    records = UploadRepository(database).list_for_project(project_id, user.user_id)
    return _ok({"uploads": [_record_payload(record) for record in records]})


@router.get("/api/uploads/{upload_id}")
async def get_upload(
    upload_id: UUID,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    database = _database(request)
    if database is None:
        return _database_unavailable()
    try:
        record = UploadRepository(database).get_for_user(str(upload_id), user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    return _ok(_record_payload(record))


@router.delete("/api/uploads/{upload_id}", dependencies=[Depends(validate_csrf)])
async def delete_upload(
    upload_id: UUID,
    request: Request,
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    user, _ = auth
    database = _database(request)
    if database is None:
        return _database_unavailable()
    try:
        record = UploadRepository(database).delete_owned(str(upload_id), user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    # Row first, then files: a row without files is invisible to retrieval
    # (chunks went with it), whereas files without a row would be unreachable
    # material nobody can delete through the API any more.
    files_removed = remove_upload_files(_upload_root(request), record.upload_id)
    return _ok({"upload_id": record.upload_id, "deleted": True, "files_removed": files_removed})


__all__ = ["router"]
