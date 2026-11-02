"""P7-S14 Web 扩展面板的「从目录发现」接口（规格 ``docs/design/17-p7-external-capabilities.md`` §7，P7-I13）。

薄适配器：目录读取、条目归一与提案生成全在 ``mcp/registry.py``，提案格式的唯一真源是 ``mcp/importer.py``。这里不另写任何一份。

边界：

- 全部接口 ``require_admin``（读接口也是：这是部署细节）；写接口（POST）另加 CSRF；请求体 ``extra="forbid"``；
- **只读**：管理员能搜索 / 浏览目录、为合适的条目生成待审提案文本；**不能连接条目地址、不能应用提案**（应用 = 运维把提案
  文本贴进部署覆盖层）。本模块与 ``mcp/registry.py`` 都不导入任何 MCP 客户端（``tests/test_mcp_registry_invariants.py`` 用 AST 判定）；
- 提案接口只收 ``{name, version}``：服务端**重新**从目录取该条目，不信客户端回传的任何内容；
- 每个管理员每分钟 ≤ ``ADMIN_LIMIT_PER_MINUTE`` 次（进程内，失控保护量级，不是生产调优值；网关另有按主体的限流）；
- 审计 ``mcp.registry_searched`` / ``mcp.registry_proposed`` 只记管理员 id、目录主机名、``name@version``、结果码、条数 / 页数 /
  提案哈希——不记查询原文、不记条目网址、不记提案文本；
- 响应与错误信息都不含目录的上游正文、网址或密钥（固定文案）。
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from water_agent.mcp.config import read_operator_layers
from water_agent.mcp.errors import (
    MCP_REGISTRY_AUTH,
    MCP_REGISTRY_BAD_RESPONSE,
    MCP_REGISTRY_ENTRY_NOT_FOUND,
    MCP_REGISTRY_ENTRY_UNSAFE,
    MCP_REGISTRY_NOT_CONFIGURED,
    MCP_REGISTRY_NOT_PROPOSABLE,
    MCP_REGISTRY_RATE_LIMITED,
    MCP_REGISTRY_UNAVAILABLE,
    MCPError,
)
from water_agent.mcp.registry import (
    MAX_CURSOR_CHARS,
    MAX_PAGES,
    MAX_QUERY_CHARS,
    PAGE_LIMIT,
    McpRegistryError,
    RegistryCatalogClient,
    RegistryConfig,
    build_proposal,
    registry_context,
    resolve_registry_config,
    validate_cursor,
    validate_query,
)
from water_agent.webapp.auth.audit import audit_mcp_registry_proposed, audit_mcp_registry_searched
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_admin

router = APIRouter()
_API_VERSION = "v1"

NOT_CONFIGURED = "WEB_MCP_REGISTRY_NOT_CONFIGURED"
RATE_LIMITED = "WEB_MCP_REGISTRY_RATE_LIMITED"

#: 每个管理员每分钟最多多少次目录操作（搜索 + 提案合计）。失控保护量级，不是生产调优值。
ADMIN_LIMIT_PER_MINUTE = 10
#: 限流表最多跟踪多少个管理员（满了先清掉窗口已空的条目；仍满则拒绝新条目的计数——fail closed 到"限流"）。
_MAX_TRACKED_ADMINS = 1024

NOTICE = (
    "目录条目是第三方写的文本：这里只生成已停用、全拒的待审提案，不会连接条目里的任何地址，也不会应用到任何配置；"
    "运维核对后把提案贴进部署覆盖层，启用与放行是之后的独立步骤"
)

_STATUS: dict[str, int] = {
    NOT_CONFIGURED: 409,
    RATE_LIMITED: 429,
    MCP_REGISTRY_RATE_LIMITED: 429,
    MCP_REGISTRY_UNAVAILABLE: 502,
    MCP_REGISTRY_AUTH: 502,
    MCP_REGISTRY_BAD_RESPONSE: 502,
    MCP_REGISTRY_ENTRY_NOT_FOUND: 404,
    MCP_REGISTRY_ENTRY_UNSAFE: 422,
    MCP_REGISTRY_NOT_PROPOSABLE: 422,
}


def _ok(data: object) -> JSONResponse:
    # no-store：目录搜索是实时读取，被浏览器缓存后面板会显示陈旧结果
    return JSONResponse(
        status_code=200,
        content={"version": _API_VERSION, "ok": True, "data": data},
        headers={"Cache-Control": "no-store"},
    )


def _error(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=_STATUS.get(code, 400),
        content={"version": _API_VERSION, "ok": False, "error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


def _registry_error(exc: McpRegistryError) -> JSONResponse:
    """目录错误 → 响应。``NOT_CONFIGURED`` 换成 Web 层的稳定码；其余沿用 ``MCP_REGISTRY_*``。消息是固定文案。"""

    code = NOT_CONFIGURED if exc.code == MCP_REGISTRY_NOT_CONFIGURED else exc.code
    return _error(code, exc.message)


# ---------------------------------------------------------------------------
# 每管理员限额
# ---------------------------------------------------------------------------


class AdminLimiter:
    """滑动窗口（60 s）。进程内，不跨 worker。线程安全。"""

    def __init__(self, limit: int = ADMIN_LIMIT_PER_MINUTE, *, clock: Any = time.monotonic) -> None:
        self.limit = limit
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, admin_id: str) -> bool:
        now = self._clock()
        with self._lock:
            window = self._hits.get(admin_id)
            if window is None:
                if len(self._hits) >= _MAX_TRACKED_ADMINS:
                    for key in [key for key, item in self._hits.items() if not item or now - item[-1] >= 60.0]:
                        del self._hits[key]
                    if len(self._hits) >= _MAX_TRACKED_ADMINS:
                        return False
                window = self._hits[admin_id] = deque()
            while window and now - window[0] >= 60.0:
                window.popleft()
            if len(window) >= self.limit:
                return False
            window.append(now)
            return True


def _limiter(request: Request) -> AdminLimiter:
    limiter = getattr(request.app.state, "mcp_registry_limiter", None)
    if limiter is None:
        limiter = AdminLimiter()
        request.app.state.mcp_registry_limiter = limiter
    return limiter


# ---------------------------------------------------------------------------
# 客户端装配
# ---------------------------------------------------------------------------


def _overrides(request: Request) -> dict[str, Any]:
    """测试与评测注入点（``app.state.mcp_registry_overrides``：``config`` / ``resolver`` / ``ssl_context`` / ``gateway``）。
    生产不设：配置读运维环境变量，网关用进程级默认单例。"""

    value = getattr(request.app.state, "mcp_registry_overrides", None)
    return value if isinstance(value, dict) else {}


def _config(request: Request) -> RegistryConfig:
    injected = _overrides(request).get("config")
    return injected if isinstance(injected, RegistryConfig) else resolve_registry_config()


def _client(request: Request, config: RegistryConfig, admin_id: str) -> RegistryCatalogClient:
    overrides = _overrides(request)
    return RegistryCatalogClient(
        config,
        registry_context(admin_id, request_id=f"mcp-registry-{admin_id[:32]}"),
        resolver=overrides.get("resolver"),
        ssl_context=overrides.get("ssl_context"),
        gateway=overrides.get("gateway"),
    )


def _existing_server_ids() -> list[str]:
    """运维两层配置里已有的 server id：提案遇到同名会改名（同名 id 贴进覆盖层会改写那台 server 的配置）。
    与 ``import_preview`` 同口径；读不了配置不阻止生成提案（只是少一项冲突检查）。"""

    try:
        operator = read_operator_layers().validate()
    except MCPError:
        return []
    return [server.server_id for server in operator.servers] if operator is not None else []


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=MAX_QUERY_CHARS * 2)
    cursor: str | None = Field(default=None, max_length=MAX_CURSOR_CHARS)

    @field_validator("query")
    @classmethod
    def _query_ok(cls, value: str) -> str:
        return validate_query(value)

    @field_validator("cursor")
    @classmethod
    def _cursor_ok(cls, value: str | None) -> str | None:
        return validate_cursor(value)


class ProposalRequest(BaseModel):
    """提案请求体只有 ``name`` 与 ``version``——条目的其余内容由服务端重新从目录取，客户端回传什么都不会被采纳。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    version: str = Field(min_length=5, max_length=20, pattern=r"^[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}$")


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@router.get("/api/admin/mcp/registry/status")
async def registry_status(request: Request, auth: tuple = Depends(require_admin)) -> JSONResponse:
    """只读：是否配置、目录**主机名**、是否放行私网。不含完整 URL（路径与查询串可能带租户信息）。"""
    del auth
    config = _config(request)
    return _ok(
        {
            "configured": config.configured,
            "registry_host": config.host if config.configured else None,
            "allow_private_network": config.allow_private if config.configured else None,
            "notice": NOTICE,
            "limits": {"page_limit": PAGE_LIMIT, "max_pages": MAX_PAGES, "max_query_chars": MAX_QUERY_CHARS},
        }
    )


@router.post("/api/admin/mcp/registry/search", dependencies=[Depends(validate_csrf)])
async def registry_search(
    body: SearchRequest, request: Request, auth: tuple = Depends(require_admin)
) -> JSONResponse:
    """搜索 / 浏览目录。条目是纯文本视图；``unsafe`` 条目只有名字与版本。不连接任何条目地址。"""
    user, _principal = auth
    admin_id = str(user.user_id)
    config = _config(request)
    if not config.configured:
        return _error(NOT_CONFIGURED, "运维没有配置 MCP 目录（WATER_AGENT_MCP_REGISTRY_URL）")
    if not _limiter(request).allow(admin_id):
        audit_mcp_registry_searched(
            actor_user_id=admin_id, registry_host=config.host, result=RATE_LIMITED, count=0, pages=0
        )
        return _error(RATE_LIMITED, "目录操作过于频繁，请稍后再试")
    client = _client(request, config, admin_id)
    try:
        result = await asyncio.to_thread(client.search, body.query, body.cursor)
    except McpRegistryError as exc:
        audit_mcp_registry_searched(
            actor_user_id=admin_id, registry_host=config.host, result=exc.code, count=0, pages=0
        )
        return _registry_error(exc)
    audit_mcp_registry_searched(
        actor_user_id=admin_id, registry_host=config.host, result="ok", count=len(result.entries), pages=result.pages
    )
    return _ok({"registry_host": config.host, "notice": NOTICE, **result.to_dict()})


@router.post("/api/admin/mcp/registry/proposal", dependencies=[Depends(validate_csrf)])
async def registry_proposal(
    body: ProposalRequest, request: Request, auth: tuple = Depends(require_admin)
) -> JSONResponse:
    """为目录里的 ``name@version`` 生成已停用、全拒的待审提案。服务端重新取条目；不落盘、不连接、不应用。"""
    user, _principal = auth
    admin_id = str(user.user_id)
    config = _config(request)
    if not config.configured:
        return _error(NOT_CONFIGURED, "运维没有配置 MCP 目录（WATER_AGENT_MCP_REGISTRY_URL）")

    def audit(result: str, proposal_hash: str = "") -> None:
        audit_mcp_registry_proposed(
            actor_user_id=admin_id,
            registry_host=config.host,
            name=body.name,
            version=body.version,
            result=result,
            proposal_hash=proposal_hash,
        )

    if not _limiter(request).allow(admin_id):
        audit(RATE_LIMITED)
        return _error(RATE_LIMITED, "目录操作过于频繁，请稍后再试")
    client = _client(request, config, admin_id)

    def work() -> Any:
        entry = client.find(body.name, body.version)
        return build_proposal(entry, registry_host=config.host, existing_ids=_existing_server_ids())

    try:
        proposal = await asyncio.to_thread(work)
    except McpRegistryError as exc:
        audit(exc.code)
        return _registry_error(exc)
    audit("ok", proposal.proposal_hash)
    return _ok({"registry_host": config.host, "notice": NOTICE, "proposal": proposal.to_dict()})


__all__ = [
    "ADMIN_LIMIT_PER_MINUTE",
    "NOTICE",
    "NOT_CONFIGURED",
    "RATE_LIMITED",
    "AdminLimiter",
    "ProposalRequest",
    "SearchRequest",
    "router",
]
