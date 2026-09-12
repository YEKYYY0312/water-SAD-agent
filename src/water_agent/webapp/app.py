"""FastAPI application for the P5 self-hosted Web runtime.

This module deliberately has no dependency on the CLI assembly, Agent registry,
ProviderGateway, or ToolRegistry. Later Web routes must reuse their existing
single points of construction rather than duplicating them here.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from water_agent.storage.database import (
    CHECKPOINT_MIGRATION_HEAD,
    MIGRATION_HEAD,
    Database,
)
from water_agent.webapp.auth.routes import router as auth_router
from water_agent.webapp.auth.storage import configure_database_stores

API_ENVELOPE_VERSION = "v1"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STATIC_DIR = PROJECT_ROOT / "web" / "app" / "dist"


def _success(data: dict[str, Any]) -> dict[str, Any]:
    return {"version": API_ENVELOPE_VERSION, "ok": True, "data": data}


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "version": API_ENVELOPE_VERSION,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def _real_llm_configured(environment: Callable[[str], str | None] = os.getenv) -> bool:
    """Only inspect configuration presence; never read or expose secret content."""
    if environment("DEEPSEEK_API_KEY"):
        return True
    secret_file = environment("DEEPSEEK_API_KEY_FILE")
    if not secret_file:
        return False
    try:
        return Path(secret_file).stat().st_size > 0
    except OSError:
        return False


def create_app(*, static_dir: Path | None = None, database: Database | None = None) -> FastAPI:
    """Create the Web app without changing the existing CLI import graph."""
    app = FastAPI(title="water-agent Web", version=API_ENVELOPE_VERSION)
    app.include_router(auth_router)
    configured_database = database
    if configured_database is None and os.getenv("DATABASE_URL"):
        try:
            configured_database = configure_database_stores()
        except (OSError, RuntimeError, SQLAlchemyError):
            # A bad URL must make readiness fail, not prevent the process from
            # serving /health/live or leak driver details at import time.
            configured_database = None
    elif configured_database is not None:
        configure_database_stores(configured_database)
    app.state.database = configured_database
    frontend_dir = static_dir or DEFAULT_STATIC_DIR
    index_path = frontend_dir / "index.html"

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Auth dependencies raise HTTPException with a structured dict detail.
        # Pass that through so callers receive the stable auth error codes.
        if isinstance(exc.detail, dict) and "code" in exc.detail and "message" in exc.detail:
            return JSONResponse(
                status_code=exc.status_code,
                content=_error(exc.detail["code"], exc.detail["message"]),
            )
        if exc.status_code == 404:
            return JSONResponse(
                status_code=404,
                content=_error("WEB_NOT_FOUND", "Resource not found"),
            )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error("WEB_HTTP_ERROR", "Request could not be completed"),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=_error("WEB_INTERNAL_ERROR", "Internal server error"),
        )

    @app.get("/health/live")
    async def live() -> dict[str, Any]:
        return _success({"status": "live"})

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        if configured_database is None:
            return JSONResponse(
                status_code=503,
                content=_success(
                    {
                        "status": "not_ready",
                        "checks": {"database": "not_configured", "migrations": "not_checked"},
                    }
                ),
            )
        database_ok = configured_database.ping()
        revision = configured_database.migration_revision() if database_ok else None
        migration_ok = revision == MIGRATION_HEAD
        checkpoint_revision = (
            configured_database.checkpoint_migration_revision() if database_ok else None
        )
        checkpoint_ok = checkpoint_revision == CHECKPOINT_MIGRATION_HEAD
        payload = _success(
            {
                "status": (
                    "ready" if database_ok and migration_ok and checkpoint_ok else "not_ready"
                ),
                "checks": {
                    "database": "ok" if database_ok else "unavailable",
                    "migrations": revision or "not_applied",
                    "checkpoint_migrations": (
                        checkpoint_revision if checkpoint_revision is not None else "not_applied"
                    ),
                },
            }
        )
        return JSONResponse(
            status_code=200 if database_ok and migration_ok and checkpoint_ok else 503,
            content=payload,
        )

    @app.get("/api/system/capabilities")
    async def capabilities() -> dict[str, Any]:
        return _success(
            {
                "real_llm": _real_llm_configured(),
                "authentication": "session_cookie",
                "persistence": "postgresql" if configured_database is not None else "memory_dev_only",
                "sse": "not_implemented_until_step_42",
            }
        )

    if index_path.is_file():
        app.mount("/assets", StaticFiles(directory=frontend_dir / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        async def workbench() -> FileResponse:
            return FileResponse(index_path)

    else:

        @app.get("/", include_in_schema=False)
        async def missing_frontend() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content=_error("WEB_FRONTEND_UNAVAILABLE", "Frontend assets are not built"),
            )

    return app


app = create_app()
