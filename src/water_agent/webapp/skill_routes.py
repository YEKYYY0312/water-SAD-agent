"""P7-S9 Skill 库面板：查看有哪些 skill、管理员看正文与启停。

薄适配器：skill 的解析、安检与两级来源合并只在 ``skills/``，启停写进 S6 的 Web 管理层（``mcp/web_layer.py``，
同一份文件、同一套 revision / 文件锁 / 整份校验）。这里不另写任何一份。

- ``GET /api/skills``：所有已改密的登录用户可看（这是能力说明，不是部署细节）——名称、来源、版本、描述、
  triggers、requires_tools、状态（``enabled`` 或错误码）。**不含正文、不含运维目录路径。**
- ``GET /api/admin/skills/{name}``：管理员看正文（只读）。
- ``PATCH /api/admin/skills/{name}``：管理员停用 / 启用（``{"disabled": bool, "expected_revision": int}``，CSRF）。
  面板不能新增、编辑或上传 skill。

改动从下一个 run 生效：每个 run 装配时重新读 skill 目录与 Web 管理层。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from water_agent.mcp.config import web_layer_path
from water_agent.mcp.errors import MCPError
from water_agent.mcp.web_layer import (
    WebLayerDocument,
    WebLayerRevisionConflict,
    WebLayerStore,
    WebLayerStoreUnavailable,
    load_web_layer,
    with_skill_toggle,
)
from water_agent.skills import SkillLibrary, SkillLibraryError, build_skill_library
from water_agent.skills.errors import SKILL_DISABLED
from water_agent.skills.models import SKILL_NAME_RE, Skill
from water_agent.webapp.auth.audit import audit_skill_updated
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_admin, require_password_changed
from water_agent.webapp.auth.models import UserRole

router = APIRouter()
_API_VERSION = "v1"

SKILL_UNKNOWN = "WEB_SKILL_UNKNOWN"
SKILL_REJECTED = "WEB_SKILL_REJECTED"
SKILL_LIBRARY_UNAVAILABLE = "WEB_SKILL_LIBRARY_UNAVAILABLE"
REVISION_CONFLICT = "WEB_MCP_REVISION_CONFLICT"
STORE_UNAVAILABLE = "WEB_MCP_ADMIN_STORE_UNAVAILABLE"

_ERROR_STATUS: dict[str, int] = {
    SKILL_UNKNOWN: 404,
    SKILL_REJECTED: 422,
    SKILL_LIBRARY_UNAVAILABLE: 503,
    REVISION_CONFLICT: 409,
    STORE_UNAVAILABLE: 503,
}
_PATCH_KEYS = frozenset({"disabled", "expected_revision"})
#: Web 装配总有的本地工具。读图只在 ``DRAWING_ROOT`` 可用时才有（与 ``maybe_build_read_drawing_tool`` 同判定）；
#: MCP 工具不计入——列表只显示"下一个 run 大概率会看到的状态"，真正装配时按实际贡献的工具核对
#: （``plugins/builtin.py::SkillsPlugin``），要求 MCP 工具的 skill 在这里显示为工具不可用。
_BASE_TOOLS = ("retrieve_clauses", "cite_clauses", "calc_velocity", "material_stats")
NOTICE = "改动从下一个 run 生效；skill 只能经仓库评审（内置）或运维只读目录新增，面板只能启停。"


class _Problem(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _ok(data: object) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"version": _API_VERSION, "ok": True, "data": data},
        headers={"Cache-Control": "no-store"},
    )


def _error(problem: _Problem) -> JSONResponse:
    return JSONResponse(
        status_code=_ERROR_STATUS.get(problem.code, 400),
        content={"version": _API_VERSION, "ok": False, "error": {"code": problem.code, "message": problem.message}},
        headers={"Cache-Control": "no-store"},
    )


def _unknown() -> _Problem:
    # 不回显请求里的名字
    return _Problem(SKILL_UNKNOWN, "该 skill 不存在")


def _document() -> tuple[WebLayerDocument | None, str | None]:
    """(层文件, 错误码)；没配 Web 管理层时 (None, None)，层文件坏时 (None, MCP_CONFIG_INVALID)。"""

    path = web_layer_path()
    if path is None:
        return None, None
    try:
        return load_web_layer(path), None
    except MCPError as exc:
        return None, exc.code


def _library() -> SkillLibrary:
    """全部 skill（**不**套用停用列表——停用状态由列表接口单独标出）。"""

    from water_agent.drawing.drawing_root import resolve_drawing_root

    tools = _BASE_TOOLS + (("read_drawing",) if resolve_drawing_root(None).status == "OK" else ())
    try:
        return build_skill_library(available_tools=tools)
    except SkillLibraryError as exc:
        raise _Problem(SKILL_LIBRARY_UNAVAILABLE, "skill 库不可用：运维 skill 目录配置有误，需运维修复") from exc


def _view(skill: Skill, *, status: str, library: SkillLibrary | None = None) -> dict[str, Any]:
    view: dict[str, Any] = {
        "name": skill.name,
        "source": skill.source,
        "version": skill.manifest.version,
        "description": skill.manifest.description,
        "triggers": list(skill.manifest.triggers),
        "requires_tools": list(skill.manifest.requires_tools),
        "status": status,
    }
    # P7-S13：已安装来源多一个 ``installed``（摘要前 8 位 / 批准人 / 批准时间 / 注册表主机名，来自 lock，**不含路径**）。
    # 其余来源的字段逐字不变。读的是 ``skills/installed.py`` 的只读元数据——本模块不得 import ``skills/remote/``（P7-I12）。
    if skill.source == "installed" and library is not None:
        meta = library.installed_meta.get(skill.name)
        if meta is not None:
            view["installed"] = meta.panel_view()
    return view


@router.get("/api/skills")
async def list_skills(auth: tuple = Depends(require_password_changed)) -> JSONResponse:
    user, _principal = auth
    try:
        library = _library()
    except _Problem as problem:
        return _error(problem)
    document, layer_error = _document()
    disabled = set(document.disabled_skills()) if document is not None else set()
    skills = [
        _view(skill, status=SKILL_DISABLED if skill.name in disabled else "enabled", library=library)
        for skill in library.enabled
    ]
    # 未加载的 skill 只给名字与错误码（它们的 frontmatter 可能就是坏的，不能当可信文本展示）
    unavailable = [{"name": name, "status": code} for name, code, _detail in library.skipped]
    return _ok(
        {
            "active": library.active,
            # 只决定前端显示不显示启停与正文入口；真正的判定仍是管理接口上的 require_admin
            "can_manage": user.role is UserRole.ADMIN,
            "notice": NOTICE,
            "revision": document.revision if document is not None else None,
            "web_layer_error": layer_error,
            "skills": skills,
            "unavailable": unavailable,
        }
    )


@router.get("/api/admin/skills/{name}")
async def get_skill(name: str, auth: tuple = Depends(require_admin)) -> JSONResponse:
    _user, _principal = auth
    try:
        if not SKILL_NAME_RE.match(name):
            raise _unknown()
        library = _library()
        skill = library.get(name)
        if skill is None:
            raise _unknown()
    except _Problem as problem:
        return _error(problem)
    return _ok({"skill": {**_view(skill, status="enabled", library=library), "body": skill.body, "digest": skill.digest}})


@router.patch("/api/admin/skills/{name}", dependencies=[Depends(validate_csrf)])
async def update_skill(name: str, request: Request, auth: tuple = Depends(require_admin)) -> JSONResponse:
    user, _principal = auth
    try:
        try:
            body = await request.json()
        except ValueError as exc:
            raise _Problem(SKILL_REJECTED, "请求体不是合法 JSON") from exc
        if not isinstance(body, dict) or set(body) - _PATCH_KEYS or "disabled" not in body:
            raise _Problem(SKILL_REJECTED, "面板只能改 disabled；skill 的正文与来源只能经仓库评审或运维目录修改")
        disabled = body["disabled"]
        revision = body.get("expected_revision")
        if type(disabled) is not bool:
            raise _Problem(SKILL_REJECTED, "disabled 必须是布尔值")
        if type(revision) is not int or revision < 0:
            raise _Problem(SKILL_REJECTED, "expected_revision 必须是非负整数（列表接口返回的 revision）")
        if not SKILL_NAME_RE.match(name) or _library().get(name) is None:
            raise _unknown()
        path = web_layer_path()
        if path is None:
            raise _Problem(STORE_UNAVAILABLE, "未配置 WATER_AGENT_MCP_WEB_LAYER：面板没有可写的 Web 管理层")
        store = WebLayerStore(path)
        try:
            document = store.update(
                lambda current: with_skill_toggle(current, name, user_id=str(user.user_id), disabled=disabled),
                expected_revision=revision,
                validate=lambda _candidate: None,  # skill 启停不改变 MCP 有效配置
            )
        except WebLayerRevisionConflict as exc:
            raise _Problem(REVISION_CONFLICT, "Web 管理层已被别人修改：请刷新后再改") from exc
        except WebLayerStoreUnavailable as exc:
            raise _Problem(STORE_UNAVAILABLE, "Web 管理层所在目录不可写：请确认已挂载 app_mcp_admin 卷") from exc
        except MCPError as exc:
            raise _Problem(SKILL_REJECTED, "Web 管理层文件无法加载：可在 MCP 扩展区「恢复为部署配置」") from exc
    except _Problem as problem:
        return _error(problem)
    audit_skill_updated(
        actor_user_id=str(user.user_id), name=name, disabled=disabled, revision=document.revision
    )
    return _ok({"name": name, "disabled": disabled, "revision": document.revision})


__all__ = ["NOTICE", "router"]
