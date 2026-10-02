"""P5-S48 Step 4: download a finalized run as .docx or .xlsx.

``GET /api/runs/{run_id}/export?format=docx|xlsx``

Thin adapter, same discipline as the other webapp routes: rendering is owned by
``docgen/docx_export.py`` and ``materials/export.py``; this module only decides
*whether* a run may be exported and streams the file back.

Order of checks (the order is the authorization property):

1. ``format`` is validated first -- a 400 for an unknown format leaks nothing;
2. **visibility** (``get_for_user``) -- an unknown or invisible run answers 404,
   indistinguishable from a non-existent one;
3. **ownership** (``get_owned_for_user``) -- a project member who can *see* the
   run but did not start it gets 403.  Export materialises the answer as a file
   that leaves the system, so it is an owner action like cancellation;
4. **in-process result** -- the run's answer lives only in the ``RunManager``
   handle (runs rows carry status only), so a run from before a restart, or one
   still streaming, answers 404 ``WEB_EXPORT_UNAVAILABLE``.  Same lifetime as
   ``/api/runs/{id}/trace``; recorded as a known limitation, not hidden.

Exports are written to the exports volume (``resolve_export_dir``) under a fresh
per-request directory, read back into memory and the directory removed before
the response is returned, on every branch -- the volume is a staging area, not
an archive.  ``Range`` is not honoured: the response is always the whole file.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from water_agent.docgen.docx_export import (
    DocxExportError,
    render_calculation_book_docx,
    render_design_note_docx,
)
from water_agent.documents.storage import resolve_export_dir
from water_agent.domain.citation import CitationClaim
from water_agent.materials.core import MaterialTable, material_rows_from_dicts
from water_agent.materials.export import write_materials_xlsx
from water_agent.storage.repository import (
    ResourceNotFoundError,
    RunRepository,
    WebSessionRepository,
)
from water_agent.tools.hydraulics import CalcResult
from water_agent.webapp.auth.dependencies import require_password_changed
from water_agent.webapp.run_manager import RunManager, RunNotManagedError

router = APIRouter()
_API_VERSION = "v1"

EXPORT_FORMATS: dict[str, str] = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _error(code: str, message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {"code": code, "message": message},
        },
    )


def _not_found() -> JSONResponse:
    return _error("WEB_NOT_FOUND", "Resource not found", status_code=404)


def _unavailable(message: str) -> JSONResponse:
    return _error("WEB_EXPORT_UNAVAILABLE", message, status_code=404)


def _manager(request: Request) -> RunManager | None:
    value = getattr(request.app.state, "run_manager", None)
    return value if isinstance(value, RunManager) else None


def content_disposition(filename: str) -> str:
    """``attachment`` header with an ASCII fallback plus RFC 5987 ``filename*``.

    ``filename=`` alone with a non-ASCII name is mangled or truncated by some
    browsers; ``filename*=`` alone is ignored by a few old clients.  Both.
    """

    suffix = Path(filename).suffix
    ascii_stem = "".join(
        char if char.isascii() and (char.isalnum() or char in "-_") else "_"
        for char in Path(filename).stem
    ).strip("_-") or "export"
    return (
        f'attachment; filename="{ascii_stem}{suffix}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _safe_title(title: str, run_id: str) -> str:
    cleaned = "".join(char for char in title.strip() if char not in '/\\:*?"<>|\r\n\t')
    return (cleaned or "工程会话")[:60] + f"-{run_id[:8]}"


def _citations(snapshot: dict[str, Any]) -> list[CitationClaim]:
    claims: list[CitationClaim] = []
    for item in snapshot["citations"]:
        source_id = str(item.get("source_id") or "") if isinstance(item, dict) else ""
        if len(source_id) >= 3:
            claims.append(CitationClaim(source_id=source_id))
    return claims


def _calc_results(snapshot: dict[str, Any]) -> list[CalcResult]:
    results: list[CalcResult] = []
    for item in snapshot["calculations"]:
        values = item.get("values") if isinstance(item, dict) else None
        if not isinstance(values, dict):
            continue
        numeric = {
            str(key): float(value)
            for key, value in values.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        assumptions = [str(text) for text in item.get("assumptions") or []]
        results.append(CalcResult(ok=bool(numeric), values=numeric, assumptions=assumptions))
    return results


def _render_docx(snapshot: dict[str, Any], title: str, target: Path) -> Path:
    citations = _citations(snapshot)
    calc_results = _calc_results(snapshot)
    if calc_results:
        return render_calculation_book_docx(
            title=title,
            input_params=dict(snapshot["calc_inputs"]),
            calc_results=calc_results,
            citations=citations,
            output_path=target,
        )
    return render_design_note_docx(
        title=title,
        sections=[{"heading": "回答正文", "content": snapshot["answer"]}],
        citations=citations,
        output_path=target,
    )


def _render_xlsx(snapshot: dict[str, Any], title: str, target: Path) -> Path:
    table_data = snapshot["material_table"]
    table = MaterialTable(
        rows=material_rows_from_dicts(table_data["rows"]),
        total_length_m=float(table_data["total_length_m"]),
        assumptions=list(table_data["assumptions"]),
    )
    return write_materials_xlsx(table, target, title=title)


@router.get("/api/runs/{run_id}/export")
async def export_run(
    run_id: UUID,
    request: Request,
    # 公开查询参数名是 format（spec D3）；Python 侧改名以免遮蔽内建 format。
    requested_format: str = Query(default="docx", alias="format"),
    auth: tuple = Depends(require_password_changed),
):
    user, _ = auth
    export_format = requested_format.strip().lower()
    if export_format not in EXPORT_FORMATS:
        return _error(
            "WEB_EXPORT_FORMAT_UNSUPPORTED",
            "Supported formats: docx, xlsx",
            status_code=400,
        )
    database = getattr(request.app.state, "database", None)
    manager = _manager(request)
    if database is None or manager is None:
        return _error(
            "WEB_DATABASE_UNAVAILABLE", "Web persistence is not configured", status_code=503
        )

    runs = RunRepository(database)
    try:
        run = runs.get_for_user(run_id, user.user_id)
    except ResourceNotFoundError:
        return _not_found()
    try:
        runs.get_owned_for_user(run_id, user.user_id)
    except ResourceNotFoundError:
        return _error("WEB_FORBIDDEN", "Only the run owner can export it", status_code=403)

    try:
        snapshot = manager.export_snapshot(run_id)
    except RunNotManagedError:
        return _unavailable("Run result is not available in this server process")
    if snapshot["status"] != "succeeded":
        return _unavailable("Only succeeded runs can be exported")
    if export_format == "docx" and not snapshot["answer"].strip():
        return _unavailable("Run has no answer to export")
    if export_format == "xlsx" and not snapshot["material_table"]:
        return _unavailable("Run has no material statistics to export")

    try:
        session = WebSessionRepository(database).get_for_user(run.thread_id, user.user_id)
        title = _safe_title(session.title, str(run.run_id))
    except ResourceNotFoundError:
        title = _safe_title("", str(run.run_id))

    root = resolve_export_dir(getattr(request.app.state, "export_dir", None))
    try:
        root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="export-", dir=root)).resolve()
    except OSError:
        return _error(
            "WEB_EXPORT_STORAGE_UNAVAILABLE", "Export storage is not writable", status_code=503
        )
    filename = f"{title}.{export_format}"
    target = workdir / f"export.{export_format}"
    try:
        if export_format == "docx":
            _render_docx(snapshot, title, target)
        else:
            _render_xlsx(snapshot, title, target)
        content = target.read_bytes()
    except DocxExportError as exc:
        # 自检失败不是服务故障：答案里有未声明的条款号或自相矛盾的数值，
        # 如实说"这份内容不能成为正式文档"，问题清单给用户看。
        return _error("WEB_EXPORT_SELF_CHECK_FAILED", str(exc)[:1000], status_code=422)
    except (OSError, ValueError, ImportError):
        return _error("WEB_EXPORT_FAILED", "Export could not be generated", status_code=500)
    finally:
        # 暂存目录在返回之前删除，与响应走哪条分支无关。此前用 FileResponse 的
        # background 删除：Range 非法时它直接回 416/400、不执行 background，每个这样
        # 的请求都在导出卷上留下一个目录。单个导出文件很小，整份读进内存即可。
        shutil.rmtree(workdir, ignore_errors=True)

    return Response(
        content=content,
        media_type=EXPORT_FORMATS[export_format],
        headers={
            "Content-Disposition": content_disposition(filename),
            "Cache-Control": "no-store",
        },
    )


__all__ = ["EXPORT_FORMATS", "content_disposition", "router"]
