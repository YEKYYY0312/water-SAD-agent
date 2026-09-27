"""FastAPI application for the P5 self-hosted Web runtime.

This module deliberately has no dependency on the CLI assembly, Agent registry,
ProviderGateway, or ToolRegistry. Later Web routes must reuse their existing
single points of construction rather than duplicating them here.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from water_agent.config.credentials import load_credentials_into_environ
from water_agent.drawing.drawing_root import resolve_drawing_root
from water_agent.mcp.wiring import load_mcp_gateway
from water_agent.memory.providers import MemoryEmbeddingStore, MemoryProvider
from water_agent.memory.repository import MemoryRepository
from water_agent.memory.service import MemoryService
from water_agent.memory.write_service import ExplicitMemoryWriteService
from water_agent.storage.database import (
    CHECKPOINT_MIGRATION_HEAD,
    MIGRATION_HEAD,
    Database,
)
from water_agent.webapp.auth.routes import router as auth_router
from water_agent.webapp.auth.storage import configure_database_stores
from water_agent.webapp.memory_routes import router as memory_router
from water_agent.webapp.run_manager import (
    DEFAULT_TENANT_ID,
    AgentSessionFactory,
    RunManager,
)
from water_agent.webapp.run_routes import router as run_router
from water_agent.webapp.settings_routes import router as settings_router
from water_agent.webapp.upload_routes import router as upload_router
from water_agent.webapp.workspace_routes import router as workspace_router

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
    from water_agent.runtime.agentic import real_llm_configured

    return real_llm_configured(environment)


def create_app(
    *,
    static_dir: Path | None = None,
    database: Database | None = None,
    session_factory: AgentSessionFactory | None = None,
    memory_enabled: bool | None = None,
    memory_provider: MemoryProvider | None = None,
    tenant_id: UUID = DEFAULT_TENANT_ID,
    # None ⇒ 由 WATER_AGENT_TRACE_DIR 决定（容器里指向可写卷；见 json_trace 模块文档）
    trace_dir: str | Path | None = None,
    session_dir: str | Path = "data/sessions",
    # None ⇒ 由 resolve_upload_dir / resolve_export_dir 决定（容器里指向 compose 卷；
    # 见 documents/storage.py 模块文档）。测试传 tmp_path 以免写进真实卷。
    upload_dir: str | Path | None = None,
    export_dir: str | Path | None = None,
) -> FastAPI:
    """Create the Web app without changing the existing CLI import graph."""
    # Backfill LLM credentials from the Compose secret files and the writable
    # credential volume into this process' environment.  This is what makes a
    # key configured before a restart keep working, and it is what lets
    # embedding.py / retrieval/rerank.py keep reading os.environ unchanged.
    # Values already present in the environment win and are never overwritten.
    load_credentials_into_environ()

    app = FastAPI(title="water-agent Web", version=API_ENVELOPE_VERSION)

    # No CORSMiddleware: the React bundle is served by this same app (see the
    # StaticFiles mount below), so browsers never issue a cross-origin preflight.
    # The previous 8001-origin exception existed only for the standalone
    # login.html scaffolding, which P5-S47 removed.

    app.include_router(auth_router)
    app.include_router(run_router)
    app.include_router(workspace_router)
    app.include_router(memory_router)
    app.include_router(settings_router)
    app.include_router(upload_router)
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
    app.state.run_manager = (
        RunManager(
            configured_database,
            session_factory=session_factory,
            memory_enabled=memory_enabled,
            memory_provider=memory_provider,
            tenant_id=tenant_id,
            trace_dir=trace_dir,
            session_dir=session_dir,
        )
        if configured_database is not None
        else None
    )
    app.state.tenant_id = tenant_id
    app.state.memory_provider = memory_provider
    app.state.upload_dir = upload_dir
    app.state.export_dir = export_dir
    # One write service per app: its pending-candidate store is in-process (15 min
    # TTL), so preview and confirmation must reach the same worker.  Memory
    # governance itself is never re-implemented here (Step 45 audit note).
    if configured_database is not None:
        embedding_store = (
            cast(MemoryEmbeddingStore, memory_provider)
            if memory_provider is not None and callable(getattr(memory_provider, "delete_forget", None))
            else None
        )
        memory_service = MemoryService(
            MemoryRepository(configured_database, embedding_store=embedding_store)
        )
        app.state.memory_service = memory_service
        app.state.memory_write_service = ExplicitMemoryWriteService(memory_service)
    else:
        app.state.memory_service = None
        app.state.memory_write_service = None
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

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _: Request, __: RequestValidationError
    ) -> JSONResponse:
        # Do not echo pydantic locations or submitted values: request data may
        # contain credentials, PII, or prompt-injection text.
        return JSONResponse(
            status_code=422,
            content=_error("WEB_INVALID_REQUEST", "Request data is invalid"),
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
    async def capabilities() -> JSONResponse:
        drawing = resolve_drawing_root()
        drawing_status = {
            "OK": "available",
            "NOT_CONFIGURED": "not_configured",
            "NOT_FOUND": "unavailable",
            "NO_DRAWING_FILES": "unavailable",
        }[drawing.status]
        drawing_detail = {
            "OK": f"DXF 图纸能力可用（{len(drawing.drawings)} 张已登记图纸）",
            "NOT_CONFIGURED": "DRAWING_ROOT 未配置",
            "NOT_FOUND": "DRAWING_ROOT 不可用",
            "NO_DRAWING_FILES": "未发现可用 DXF 图纸",
        }[drawing.status]
        mcp_invalid = False
        try:
            mcp_config = load_mcp_gateway()
        except Exception:  # noqa: BLE001 -- status endpoint must not expose config details
            mcp_config = None
            mcp_invalid = True
        if mcp_invalid:
            mcp_status = "unavailable"
            mcp_detail = "MCP 配置不可用"
            mcp_server_count = None
        elif mcp_config is None:
            mcp_status = "not_configured"
            mcp_detail = "MCP 配置未启用"
            mcp_server_count = 0
        else:
            mcp_status = "blocked"
            mcp_detail = "真实 CAD/BIM 与企业库 MCP 尚未验证；危险确认门仍按配置 fail closed"
            mcp_server_count = len(mcp_config.servers)
        payload = _success(
            {
                "real_llm": _real_llm_configured(),
                "authentication": "session_cookie",
                "persistence": "postgresql" if configured_database is not None else "memory_dev_only",
                "sse": "resumable_last_event_id",
                "drawing": {
                    "status": drawing_status,
                    "configured_status": drawing.status,
                    "message": drawing_detail,
                    "supported_format": "dxf",
                    "dwg": {
                        "status": "blocked",
                        "message": "DWG 直读 blocked；需人工预转换为 DXF",
                    },
                    "real_samples": False,
                    "recall_gate": False,
                },
                "mcp": {
                    "status": mcp_status,
                    "message": mcp_detail,
                    "configured_server_count": mcp_server_count,
                    "real_endpoints_verified": False,
                },
            }
        )
        # This payload is live runtime state, not a document.  Without an
        # explicit no-store the browser may serve a heuristically cached copy,
        # and the workbench then shows "mock / 未配置" after a key has already
        # been configured — observed in a browser while verifying the settings
        # panel.  Stale capability data misleads about what the agent can do.
        return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})

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

    run_manager = app.state.run_manager
    if run_manager is not None:

        async def shutdown_run_manager() -> None:
            # Bounded graceful drain: in-flight runs are cancelled and awaited up
            # to the configured budget instead of being dropped on SIGTERM.  The
            # drain result is stored on app.state so tests can assert it without
            # scraping logs.
            app.state.shutdown_drain = await asyncio.to_thread(run_manager.close)

        app.router.on_shutdown.append(shutdown_run_manager)

    return app


app = create_app()
