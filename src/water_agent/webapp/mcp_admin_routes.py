"""P7-S6 Web 扩展面板：MCP server 的启停、白名单、凭据槽位、探测与导入预览。

薄适配器：配置语义只在 ``mcp/config.py`` / ``mcp/web_layer.py``，凭据在 ``mcp/credentials.py``，导入在
``mcp/importer.py``，连接只经 Web 会话工厂的连接池（``mcp/pool.py``）。这里不另写任何一份。

边界（``docs/design/16-p7-plugin-mcp.md`` §3.8）：

- 面板只能改 ``disabled`` / ``allow``（写进 Web 管理层）、给运维已声明的凭据槽位填值；不能新增 server，
  不能写 ``command`` / ``args`` / ``env`` / ``env_file`` / ``cwd`` / ``lifecycle`` / ``human_confirmation`` /
  超时，也不能决定环境变量名。
- 全部接口 ``require_admin``（读接口也是：这是部署细节）；写接口另加 CSRF。
- 读接口不返回 ``args``、``env`` 的值、``env_file`` 内容、完整命令路径或任何凭据值；错误信息不回显请求值。
- 改动从**下一个 run** 生效：Web 每个 run 都重新 ``load_mcp_gateway()``，连接池按单台 server 指纹自动换
  新进程；正在跑的 run 不受影响。
- 每次变更一条结构化审计事件（``webapp/auth/audit.py``），凭据只记槽位名与动作。
"""
from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from water_agent.config.credentials import CredentialStoreUnavailableError, InvalidCredentialError
from water_agent.mcp.config import (
    MCPGatewayConfig,
    MCPServerConfig,
    OperatorLayers,
    missing_paths,
    read_operator_layers,
    web_layer_path,
)
from water_agent.mcp.credentials import delete_slot, slot_status, write_slot
from water_agent.mcp.errors import MCPError
from water_agent.mcp.importer import ImportRejected, preview_import
from water_agent.mcp.pool import McpConnectionPool
from water_agent.mcp.web_layer import (
    WebLayerDocument,
    WebLayerRejected,
    WebLayerRevisionConflict,
    WebLayerStore,
    WebLayerStoreUnavailable,
    check_candidate,
    field_sources,
    load_web_layer,
    merged_config,
    orphan_entries,
    with_override,
    without_override,
)
from water_agent.mcp.wiring import MCPGatewayAssembly, load_mcp_gateway
from water_agent.runtime.governance.inventory import mcp_tool_binding
from water_agent.webapp.auth.audit import (
    audit_mcp_credential_changed,
    audit_mcp_override_removed,
    audit_mcp_server_probed,
    audit_mcp_server_updated,
    audit_mcp_web_layer_reset,
)
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_admin
from water_agent.webapp.settings_routes import CredentialRequest

router = APIRouter()
_API_VERSION = "v1"

SERVER_UNKNOWN = "WEB_MCP_SERVER_UNKNOWN"
REVISION_CONFLICT = "WEB_MCP_REVISION_CONFLICT"
CONFIG_REJECTED = "WEB_MCP_CONFIG_REJECTED"
SERVER_DISABLED = "WEB_MCP_SERVER_DISABLED"
PROBE_BUSY = "WEB_MCP_PROBE_BUSY"
STORE_UNAVAILABLE = "WEB_MCP_ADMIN_STORE_UNAVAILABLE"
CREDENTIAL_INVALID = "WEB_CREDENTIAL_INVALID"
CREDENTIAL_STORE_UNAVAILABLE = "WEB_CREDENTIAL_STORE_UNAVAILABLE"

_ERROR_STATUS: dict[str, int] = {
    SERVER_UNKNOWN: 404,
    REVISION_CONFLICT: 409,
    CONFIG_REJECTED: 422,
    SERVER_DISABLED: 409,
    PROBE_BUSY: 409,
    STORE_UNAVAILABLE: 503,
    CREDENTIAL_INVALID: 422,
    CREDENTIAL_STORE_UNAVAILABLE: 503,
}

#: 面板上的固定说明。
NOTICE = "改动从下一个 run 生效；命令、参数、环境变量与工作目录只能由运维在部署覆盖层修改"

#: PATCH 请求体只认这三个键。
_PATCH_KEYS = frozenset({"disabled", "allow", "expected_revision"})
MAX_ALLOW_ITEMS = 256
MAX_TOOL_NAME_LENGTH = 128
#: 探测结果里最多列多少个工具（远端 tools/list 翻页上限之内的防御性截断）。
MAX_PROBE_TOOLS = 256

_LAYER_MESSAGES = {
    "operator": "部署配置（仓库默认层 / 部署覆盖层）无法加载：需运维修复，面板改不了它",
    "web": "Web 管理层文件无法加载：可以「恢复为部署配置」整层撤回",
    "effective": "有启用的 server 引用的 env_file / cwd 不存在：停用它，或由运维修复部署覆盖层",
}


class _Problem(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _ok(data: object) -> JSONResponse:
    # no-store：这是实时配置，被浏览器缓存后面板会显示陈旧状态。
    return JSONResponse(
        status_code=200,
        content={"version": _API_VERSION, "ok": True, "data": data},
        headers={"Cache-Control": "no-store"},
    )


def _error(problem: _Problem) -> JSONResponse:
    return JSONResponse(
        status_code=_ERROR_STATUS.get(problem.code, 400),
        content={
            "version": _API_VERSION,
            "ok": False,
            "error": {"code": problem.code, "message": problem.message},
        },
        headers={"Cache-Control": "no-store"},
    )


def _unknown() -> _Problem:
    # 不回显请求里的名字：未声明的槽位与不存在的 server 同答。
    return _Problem(SERVER_UNKNOWN, "该 server 或凭据槽位不存在")


# ---------------------------------------------------------------------------
# 进程内状态：最近一次探测结果与"同一台 server 同时只允许一个探测"
# ---------------------------------------------------------------------------


@dataclass
class McpAdminState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    probes: dict[str, dict[str, Any]] = field(default_factory=dict)
    probing: set[str] = field(default_factory=set)

    def begin_probe(self, server_id: str) -> bool:
        with self.lock:
            if server_id in self.probing:
                return False
            self.probing.add(server_id)
            return True

    def end_probe(self, server_id: str, result: dict[str, Any] | None) -> None:
        with self.lock:
            self.probing.discard(server_id)
            if result is not None:
                self.probes[server_id] = result


def _state(request: Request) -> McpAdminState:
    state = getattr(request.app.state, "mcp_admin", None)
    if state is None:
        state = McpAdminState()
        request.app.state.mcp_admin = state
    return state


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def _operator() -> tuple[OperatorLayers, MCPGatewayConfig | None]:
    try:
        layers = read_operator_layers()
        return layers, layers.validate()
    except MCPError as exc:
        raise _Problem(CONFIG_REJECTED, _LAYER_MESSAGES["operator"]) from exc


def _store() -> WebLayerStore:
    path = web_layer_path()
    if path is None:
        raise _Problem(
            STORE_UNAVAILABLE,
            "未配置 WATER_AGENT_MCP_WEB_LAYER：面板没有可写的 Web 管理层（需运维在部署中挂卷并设置该变量）",
        )
    return WebLayerStore(path)


def _store_unavailable() -> _Problem:
    return _Problem(
        STORE_UNAVAILABLE,
        "Web 管理层所在目录不可写：请确认已挂载 app_mcp_admin 卷",
    )


def _web_layer_writable(path: Any) -> bool:
    if path is None:
        return False
    directory = path.parent
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    return os.access(directory, os.W_OK | os.X_OK)


def _server_view(
    server: MCPServerConfig,
    layers: OperatorLayers,
    document: WebLayerDocument | None,
    state: McpAdminState,
    path_problems: dict[str, list[str]],
) -> dict[str, Any]:
    entry = document.entry(server.server_id) if document is not None else None
    override: dict[str, Any] | None = None
    if entry is not None:
        override = entry.to_json()
        override.pop("server_id", None)
    credentials = []
    for name in server.credential_env:
        status = slot_status(server.server_id, name)
        credentials.append(
            {"name": status.name, "configured": status.configured, "updated_at": status.updated_at}
        )
    with state.lock:
        last_probe = state.probes.get(server.server_id)
    return {
        "server_id": server.server_id,
        "disabled": server.disabled,
        "allow": list(server.allow) if server.allow is not None else None,
        "human_confirmation": server.human_confirmation,
        "lifecycle": server.lifecycle,
        # 只给文件名：完整路径与 args 里常有运维写进去的本机细节与令牌，面板也改不了它们
        "command_name": PurePosixPath(server.command).name,
        "sources": field_sources(layers, document, server.server_id),
        "credentials": credentials,
        "web_override": override,
        "path_problems": path_problems.get(server.server_id, []),
        "last_probe": last_probe,
    }


def _listing(state: McpAdminState) -> dict[str, Any]:
    web_path = web_layer_path()
    config_error: dict[str, Any] | None = None
    revision: int | None = None
    servers: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []

    layers: OperatorLayers | None = None
    operator: MCPGatewayConfig | None = None
    try:
        layers = read_operator_layers()
        operator = layers.validate()
    except MCPError:
        config_error = {"code": "MCP_CONFIG_INVALID", "layer": "operator", "message": _LAYER_MESSAGES["operator"]}

    document: WebLayerDocument | None = None
    if web_path is not None:
        try:
            document = load_web_layer(web_path)
            revision = document.revision
        except MCPError:
            config_error = config_error or {
                "code": "MCP_CONFIG_INVALID",
                "layer": "web",
                "message": _LAYER_MESSAGES["web"],
            }

    shown = operator
    path_problems: dict[str, list[str]] = {}
    if operator is not None and layers is not None and document is not None and web_path is not None:
        try:
            shown = merged_config(layers, document, web_path=web_path)
        except MCPError:
            config_error = config_error or {
                "code": "MCP_CONFIG_INVALID",
                "layer": "web",
                "message": _LAYER_MESSAGES["web"],
            }
    if shown is not None and layers is not None:
        for server_id, field_name in missing_paths(shown):
            path_problems.setdefault(server_id, []).append(field_name)
        if path_problems and config_error is None:
            config_error = {
                "code": "MCP_CONFIG_INVALID",
                "layer": "effective",
                "message": _LAYER_MESSAGES["effective"],
                "servers": sorted(path_problems),
            }
        servers = [
            _server_view(server, layers, document, state, path_problems) for server in shown.servers
        ]
    if layers is not None and document is not None:
        for entry in orphan_entries(layers, document):
            data = entry.to_json()
            orphans.append(data)

    return {
        "notice": NOTICE,
        "web_layer": {
            "configured": web_path is not None,
            "writable": _web_layer_writable(web_path),
            "revision": revision,
        },
        "config_error": config_error,
        "servers": servers,
        "orphans": orphans,
    }


@router.get("/api/admin/mcp/servers")
async def list_servers(request: Request, auth: tuple = Depends(require_admin)) -> JSONResponse:
    _user, _principal = auth
    return _ok(_listing(_state(request)))


# ---------------------------------------------------------------------------
# 启停与白名单
# ---------------------------------------------------------------------------


def _parse_update(body: Any) -> tuple[dict[str, Any], int]:
    """手工校验请求体：给出可操作的原因，且不回显请求值。"""
    if not isinstance(body, dict):
        raise _Problem(CONFIG_REJECTED, "请求体必须是对象")
    if set(body) - _PATCH_KEYS:
        raise _Problem(
            CONFIG_REJECTED,
            "面板只能改 disabled / allow；命令、参数、环境变量、工作目录、生命周期、确认位与凭据槽位"
            "只能由运维在部署覆盖层修改",
        )
    revision = body.get("expected_revision")
    if type(revision) is not int or revision < 0:
        raise _Problem(CONFIG_REJECTED, "expected_revision 必须是非负整数（列表接口返回的 revision）")
    changes: dict[str, Any] = {}
    if "disabled" in body:
        if type(body["disabled"]) is not bool:
            raise _Problem(CONFIG_REJECTED, "disabled 必须是布尔值")
        changes["disabled"] = body["disabled"]
    if "allow" in body:
        allow = body["allow"]
        if allow is not None:
            if not isinstance(allow, list):
                raise _Problem(CONFIG_REJECTED, "allow 必须是工具名列表或 null（全拒）")
            if len(allow) > MAX_ALLOW_ITEMS:
                raise _Problem(CONFIG_REJECTED, f"allow 最多 {MAX_ALLOW_ITEMS} 项")
            for item in allow:
                if not isinstance(item, str) or not item or len(item) > MAX_TOOL_NAME_LENGTH:
                    raise _Problem(
                        CONFIG_REJECTED, f"allow 的每一项都必须是 1–{MAX_TOOL_NAME_LENGTH} 个字符的工具名"
                    )
                if any(character.isspace() for character in item):
                    raise _Problem(CONFIG_REJECTED, "allow 里的工具名不能含空白（与 tools/list 精确匹配）")
            if len(set(allow)) != len(allow):
                raise _Problem(CONFIG_REJECTED, "allow 里有重复的工具名")
        changes["allow"] = allow
    if not changes:
        raise _Problem(CONFIG_REJECTED, "至少要改 disabled 或 allow 中的一项")
    return changes, revision


def _effective_allow(config: MCPGatewayConfig | None, server_id: str) -> set[str]:
    if config is None:
        return set()
    server = next((item for item in config.servers if item.server_id == server_id), None)
    return set(server.allow or ()) if server is not None else set()


@router.patch("/api/admin/mcp/servers/{server_id}", dependencies=[Depends(validate_csrf)])
async def update_server(
    server_id: str,
    request: Request,
    auth: tuple = Depends(require_admin),
) -> JSONResponse:
    user, _principal = auth
    try:
        try:
            body = await request.json()
        except ValueError as exc:
            raise _Problem(CONFIG_REJECTED, "请求体不是合法 JSON") from exc
        changes, expected_revision = _parse_update(body)
        store = _store()
        layers, operator = _operator()
        if operator is None or server_id not in {server.server_id for server in operator.servers}:
            raise _unknown()
        try:
            before_document = store.read()
            before = merged_config(layers, before_document, web_path=store.path)
        except MCPError:
            before = None
        accepted: list[MCPGatewayConfig] = []

        def validate(candidate: WebLayerDocument) -> None:
            accepted.append(check_candidate(layers, candidate, web_path=store.path))

        try:
            document = store.update(
                lambda current: with_override(current, server_id, user_id=str(user.user_id), **changes),
                expected_revision=expected_revision,
                validate=validate,
            )
        except WebLayerRevisionConflict as exc:
            raise _Problem(REVISION_CONFLICT, "Web 管理层已被别人修改：请刷新后再改") from exc
        except WebLayerRejected as exc:
            raise _Problem(CONFIG_REJECTED, exc.reason) from exc
        except WebLayerStoreUnavailable as exc:
            raise _store_unavailable() from exc
        except MCPError as exc:
            raise _Problem(CONFIG_REJECTED, _LAYER_MESSAGES["web"]) from exc
    except _Problem as problem:
        return _error(problem)

    after = accepted[-1]
    old_allow, new_allow = _effective_allow(before, server_id), _effective_allow(after, server_id)
    audit_mcp_server_updated(
        actor_user_id=str(user.user_id),
        server_id=server_id,
        fields=list(changes),
        allow_added=sorted(new_allow - old_allow),
        allow_removed=sorted(old_allow - new_allow),
        revision=document.revision,
    )
    server = next(item for item in after.servers if item.server_id == server_id)
    view = _server_view(server, layers, document, _state(request), {})
    return _ok({"server": view, "revision": document.revision})


@router.delete(
    "/api/admin/mcp/servers/{server_id}/overrides", dependencies=[Depends(validate_csrf)]
)
async def remove_overrides(
    server_id: str,
    expected_revision: int = Query(..., ge=0),
    auth: tuple = Depends(require_admin),
) -> JSONResponse:
    """去掉一台 server 在 Web 管理层的条目（回到部署配置）；孤立条目也用它清理。"""
    user, _principal = auth
    try:
        store = _store()
        try:
            layers = read_operator_layers()
        except MCPError as exc:
            raise _Problem(CONFIG_REJECTED, _LAYER_MESSAGES["operator"]) from exc
        try:
            current = store.read()
        except MCPError as exc:
            raise _Problem(CONFIG_REJECTED, _LAYER_MESSAGES["web"]) from exc
        if current.entry(server_id) is None:
            try:
                operator = layers.validate()
            except MCPError:
                operator = None
            if operator is not None and server_id in {item.server_id for item in operator.servers}:
                # 本来就没有改动：幂等，不递进 revision、不记审计
                return _ok({"revision": current.revision})
            raise _unknown()
        orphan = server_id in {entry.server_id for entry in orphan_entries(layers, current)}

        def validate(candidate: WebLayerDocument) -> None:
            # 去掉孤立条目不改变有效配置；其余的照样整份校验（去掉"停用"可能让一台坏 server 重新启用）
            if not orphan:
                check_candidate(layers, candidate, web_path=store.path)

        try:
            document = store.update(
                lambda doc: without_override(doc, server_id),
                expected_revision=expected_revision,
                validate=validate,
            )
        except WebLayerRevisionConflict as exc:
            raise _Problem(REVISION_CONFLICT, "Web 管理层已被别人修改：请刷新后再改") from exc
        except WebLayerRejected as exc:
            raise _Problem(CONFIG_REJECTED, exc.reason) from exc
        except WebLayerStoreUnavailable as exc:
            raise _store_unavailable() from exc
        except MCPError as exc:
            raise _Problem(CONFIG_REJECTED, _LAYER_MESSAGES["web"]) from exc
    except _Problem as problem:
        return _error(problem)
    audit_mcp_override_removed(
        actor_user_id=str(user.user_id), server_id=server_id, orphan=orphan, revision=document.revision
    )
    return _ok({"revision": document.revision})


@router.delete("/api/admin/mcp/web-layer", dependencies=[Depends(validate_csrf)])
async def reset_web_layer(auth: tuple = Depends(require_admin)) -> JSONResponse:
    """恢复为部署配置：删除整个 Web 管理层。层文件损坏时同样可用。"""
    user, _principal = auth
    try:
        store = _store()
        try:
            existed = store.reset()
        except WebLayerStoreUnavailable as exc:
            raise _store_unavailable() from exc
    except _Problem as problem:
        return _error(problem)
    audit_mcp_web_layer_reset(actor_user_id=str(user.user_id), existed=existed)
    return _ok({"existed": existed, "revision": 0})


# ---------------------------------------------------------------------------
# 凭据槽位（只写不读）
# ---------------------------------------------------------------------------


def _declared(server_id: str, env_name: str) -> None:
    _layers, operator = _operator()
    server = next(
        (item for item in (operator.servers if operator is not None else ()) if item.server_id == server_id),
        None,
    )
    if server is None or env_name not in server.credential_env:
        raise _unknown()


def _slot_view(server_id: str, env_name: str) -> dict[str, Any]:
    status = slot_status(server_id, env_name)
    return {"name": status.name, "configured": status.configured, "updated_at": status.updated_at}


def _credential_store_unavailable() -> _Problem:
    return _Problem(
        CREDENTIAL_STORE_UNAVAILABLE,
        "凭据存储不可写：请确认已挂载 app_credentials 卷",
    )


@router.put(
    "/api/admin/mcp/servers/{server_id}/credentials/{env_name}",
    dependencies=[Depends(validate_csrf)],
)
async def put_credential(
    server_id: str,
    env_name: str,
    body: CredentialRequest,
    auth: tuple = Depends(require_admin),
) -> JSONResponse:
    user, _principal = auth
    try:
        _declared(server_id, env_name)
        try:
            write_slot(server_id, env_name, body.value)
        except InvalidCredentialError as exc:
            raise _Problem(CREDENTIAL_INVALID, str(exc)) from exc
        except CredentialStoreUnavailableError as exc:
            raise _credential_store_unavailable() from exc
    except _Problem as problem:
        return _error(problem)
    audit_mcp_credential_changed(
        actor_user_id=str(user.user_id), server_id=server_id, slot=env_name, action="set"
    )
    return _ok({"credential": _slot_view(server_id, env_name)})


@router.delete(
    "/api/admin/mcp/servers/{server_id}/credentials/{env_name}",
    dependencies=[Depends(validate_csrf)],
)
async def remove_credential(
    server_id: str,
    env_name: str,
    auth: tuple = Depends(require_admin),
) -> JSONResponse:
    user, _principal = auth
    try:
        _declared(server_id, env_name)
        try:
            delete_slot(server_id, env_name)
        except CredentialStoreUnavailableError as exc:
            raise _credential_store_unavailable() from exc
    except _Problem as problem:
        return _error(problem)
    audit_mcp_credential_changed(
        actor_user_id=str(user.user_id), server_id=server_id, slot=env_name, action="cleared"
    )
    return _ok({"credential": _slot_view(server_id, env_name)})


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------


def _probe_result(assembly: MCPGatewayAssembly, server_id: str) -> dict[str, Any]:
    """只有远端工具名、是否暴露与跳过码；远端描述原文、跳过 detail（可能含 stderr 与本机路径）一律不返回。"""
    tools: list[dict[str, Any]] = []
    server_code: str | None = None
    for session in assembly.sessions:
        if session.config.server_id != server_id:
            continue
        for tool in session.result.tools:
            binding = mcp_tool_binding(tool.name)
            tools.append(
                {"name": binding.remote_tool if binding else tool.name, "exposed": True, "code": None}
            )
        for skipped in session.result.skipped:
            tools.append({"name": skipped.remote_tool, "exposed": False, "code": skipped.code})
    for skip in assembly.skipped_servers:
        if skip.server_id != server_id:
            continue
        if skip.remote_tool in (None, "*"):
            server_code = skip.code
        else:
            tools.append({"name": skip.remote_tool, "exposed": False, "code": skip.code})
    return {
        "server_id": server_id,
        "probed_at": datetime.now(UTC).isoformat(),
        "status": "failed" if server_code is not None else "ok",
        "server_code": server_code,
        "tools": tools[:MAX_PROBE_TOOLS],
        "truncated": len(tools) > MAX_PROBE_TOOLS,
    }


def _pool_of(request: Request) -> McpConnectionPool | None:
    manager = getattr(request.app.state, "run_manager", None)
    factory = getattr(manager, "session_factory", None)
    pool = getattr(factory, "mcp_pool", None)
    return pool if isinstance(pool, McpConnectionPool) else None


def _run_probe(
    pool: McpConnectionPool | None, config: MCPGatewayConfig, server: MCPServerConfig
) -> dict[str, Any]:
    """经 Web 会话工厂的连接池借一次租约（``confirmation_ready=True``，与 Web run 同语义），读结果后立即释放。

    租约用的配置视图只含目标 server 与池里已在用的 shared server：不顺带 spawn 其他 per_run server，
    也不让池把在用的 shared 连接当成"已从配置删除"而退役。没有会话工厂的池（测试替身）时临时建一个。
    """
    owned = pool is None
    active_pool = McpConnectionPool() if pool is None else pool
    try:
        active = set(active_pool.snapshot()["active"])
        view = config.model_copy(
            update={
                "servers": tuple(
                    item
                    for item in config.servers
                    if item.server_id == server.server_id
                    or (item.lifecycle == "shared" and item.server_id in active)
                )
            }
        )
        lease = active_pool.lease(view, confirmation_ready=True)
        try:
            return _probe_result(lease.assembly, server.server_id)
        finally:
            lease.release()
    finally:
        if owned:
            active_pool.close()


@router.post("/api/admin/mcp/servers/{server_id}/probe", dependencies=[Depends(validate_csrf)])
async def probe_server(
    server_id: str,
    request: Request,
    auth: tuple = Depends(require_admin),
) -> JSONResponse:
    user, _principal = auth
    state = _state(request)
    try:
        try:
            config = load_mcp_gateway()
        except MCPError as exc:
            raise _Problem(CONFIG_REJECTED, "当前有效配置无法加载：先处理列表里报告的配置错误") from exc
        server = next(
            (item for item in (config.servers if config is not None else ()) if item.server_id == server_id),
            None,
        )
        if server is None:
            raise _unknown()
        if server.disabled:
            raise _Problem(SERVER_DISABLED, "这台 server 已停用：启用后再探测")
        if not state.begin_probe(server_id):
            raise _Problem(PROBE_BUSY, "这台 server 正在探测中")
    except _Problem as problem:
        return _error(problem)
    assert config is not None
    result: dict[str, Any] | None = None
    try:
        result = await asyncio.to_thread(_run_probe, _pool_of(request), config, server)
    finally:
        state.end_probe(server_id, result)
    audit_mcp_server_probed(actor_user_id=str(user.user_id), server_id=server_id, status=result["status"])
    return _ok({"probe": result})


# ---------------------------------------------------------------------------
# 导入预览（无状态）
# ---------------------------------------------------------------------------


#: 经 HTTP 导入的字符上限。JSON 请求体按最坏情况每字符 12 字节计（转义的代理对），请求体上限
#: （``body_limit.REQUEST_BODY_LIMIT_BYTES``）要给最大的合法请求体留 4 倍余量，且最大的仍须是消息请求
#: （``tests/test_upload_body_limit.py``）；64 Ki 个字符最坏要 768 KiB，16 000 个字符约 188 KiB。
#: ``preview_import`` 自己的 UTF-8 字节上限（64 KiB）仍对直接调用方生效。真实的 mcpServers 配置通常只有几 KB。
MAX_IMPORT_CHARACTERS = 16_000


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_IMPORT_CHARACTERS)


@router.post("/api/admin/mcp/import-preview", dependencies=[Depends(validate_csrf)])
async def import_preview(body: ImportRequest, auth: tuple = Depends(require_admin)) -> JSONResponse:
    """把粘贴的 ``mcpServers`` 配置转成覆盖层格式的待审提案。不落盘、不 spawn、不应用。"""
    _user, _principal = auth
    try:
        try:
            _layers, operator = _operator()
            existing = [server.server_id for server in operator.servers] if operator is not None else []
        except _Problem:
            existing = []
        try:
            preview = preview_import(body.text, existing_ids=existing)
        except ImportRejected as exc:
            raise _Problem(CONFIG_REJECTED, exc.reason) from exc
    except _Problem as problem:
        return _error(problem)
    return _ok({"preview": preview.to_dict()})


__all__ = ["MAX_IMPORT_CHARACTERS", "NOTICE", "McpAdminState", "router"]
