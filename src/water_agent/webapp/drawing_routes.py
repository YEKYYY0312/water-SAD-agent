"""P6-S1 Step 6: parametric water-supply drawing generation over HTTP.

Thin adapter, same discipline as ``upload_routes.py`` / ``export_routes.py``: no
geometry, no rules, no rendering is defined here.  Everything is delegated to
``water_agent.drawing.generation_service``; this module only owns HTTP concerns.

* ``GET  /api/drawings/options`` -- form vocabulary projected from the request
  contract and the rule table (the frontend never hard-codes enum values);
* ``POST /api/drawings/generate`` -- CSRF + password-changed session; VIEWER 403.
  The body is validated by ``GenerationRequest`` inside the service so that
  invalid input answers with the stable ``GEN_*`` codes (field paths only, never
  the submitted values).  Generation is synchronous: measured ~0.7 s for the
  largest allowed set (33 floors x 8 units, with DWG), well under the 5 s bar the
  execution prompt sets for moving it to a background task;
* ``GET  /api/drawings/{generation_id}/download`` -- the ZIP, owner only.  A
  generation of another user, an unknown id and a malformed id all answer the
  same 404 -- existence is not observable.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from water_agent.documents.dwg_convert import dwg_roundtrip_available
from water_agent.documents.storage import resolve_export_dir
from water_agent.drawing.building_rules import BuildingType, load_building_rules
from water_agent.drawing.generation_models import (
    DEFAULT_FLOOR_HEIGHT_M,
    FLOOR_HEIGHT_RANGE_M,
    FLOORS_RANGE,
    GEN_INVALID_INPUT,
    PRESSURE_RANGE_MPA,
    SUPPORTED_SYSTEM,
    UNITS_RANGE,
    GenerationError,
    NominalDiameter,
    PipeMaterial,
)
from water_agent.drawing.generation_service import (
    DRAFT_NOTICE,
    find_generation,
    generate_drawing_set,
)
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_password_changed
from water_agent.webapp.auth.models import UserRole
from water_agent.webapp.export_routes import content_disposition

router = APIRouter()
_API_VERSION = "v1"

#: At most two generations render at once.  Each is CPU-bound (~0.1-0.7 s); an
#: unbounded burst would starve the event loop's thread pool for every other
#: request.  Excess requests wait rather than fail.
_GENERATION_SLOTS = asyncio.Semaphore(2)


def _ok(data: object, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"version": _API_VERSION, "ok": True, "data": data})


def _error(code: str, message: str, *, status_code: int, fields: list[dict[str, str]] | None = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    return JSONResponse(status_code=status_code, content={"version": _API_VERSION, "ok": False, "error": error})


def _export_root(request: Request):
    return resolve_export_dir(getattr(request.app.state, "export_dir", None))


@router.get("/api/drawings/options")
async def drawing_options(auth: tuple = Depends(require_password_changed)) -> JSONResponse:
    rules = load_building_rules()
    building_types = []
    for building_type in BuildingType:
        typed = rules.for_type(building_type)
        building_types.append(
            {
                "value": building_type.value,
                "unit_label": typed.unit_label,
                "riser_positions": list(typed.riser.positions),
                "default_riser_position": typed.riser.default_position,
                "routing": typed.routing.method,
                "detail": typed.detail.detail_type,
            }
        )
    return _ok(
        {
            "system_types": [SUPPORTED_SYSTEM],
            "building_types": building_types,
            "diameters": [dn.value for dn in NominalDiameter],
            "materials": [material.value for material in PipeMaterial],
            "ranges": {
                "floors": list(FLOORS_RANGE),
                "units_per_floor": list(UNITS_RANGE),
                "pressure_mpa": list(PRESSURE_RANGE_MPA),
                "floor_height_m": list(FLOOR_HEIGHT_RANGE_M),
            },
            "defaults": {"floor_height_m": DEFAULT_FLOOR_HEIGHT_M},
            "dwg": {"available": dwg_roundtrip_available(), "experimental": True},
            "notice": DRAFT_NOTICE,
            "rules_version": rules.rules_version,
        }
    )


@router.post("/api/drawings/generate", dependencies=[Depends(validate_csrf)])
async def generate_drawings(request: Request, auth: tuple = Depends(require_password_changed)) -> JSONResponse:
    user, _ = auth
    if user.role is UserRole.VIEWER:
        return _error("WEB_FORBIDDEN", "This action is not authorized", status_code=403)
    try:
        payload = json.loads(await request.body() or b"null")
    except (UnicodeDecodeError, ValueError):
        return _error(GEN_INVALID_INPUT, "请求体不是合法的 JSON", status_code=422)
    root = _export_root(request)
    try:
        async with _GENERATION_SLOTS:
            result = await run_in_threadpool(
                generate_drawing_set, payload, export_root=root, owner_user_id=str(user.user_id)
            )
    except GenerationError as exc:
        return _error(exc.code, exc.message, status_code=exc.status_code, fields=exc.fields)
    except OSError:
        # The exports volume is not writable: a deployment condition, not a bad request.
        return _error("WEB_EXPORT_STORAGE_UNAVAILABLE", "Export storage is not writable", status_code=503)
    return _ok(result.to_response(), status_code=201)


@router.get("/api/drawings/{generation_id}/download")
async def download_drawings(
    generation_id: str, request: Request, auth: tuple = Depends(require_password_changed)
):
    user, _ = auth
    stored = find_generation(_export_root(request), generation_id, owner_user_id=str(user.user_id))
    if stored is None:
        return _error("WEB_NOT_FOUND", "Resource not found", status_code=404)
    return FileResponse(
        stored.zip_path,
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition(stored.zip_name), "Cache-Control": "no-store"},
    )


__all__ = ["router"]
