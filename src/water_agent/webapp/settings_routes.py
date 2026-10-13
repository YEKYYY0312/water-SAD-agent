"""P5 设置路由：LLM 凭据的状态查询与写入。

凭据值只进不出。GET 只报告「是否已配置、来自哪一层、何时更新」，既不返回明文，
也不返回末尾几位——末几位配上供应商固定前缀会缩小暴力搜索范围，而它对「我配的
到底是哪个 key」这个问题并不能给出可靠答案。要确认填对了，看能否成功对话。

权限刻意分两档：写入是管理操作，与 /api/admin/users 一致用 ``require_admin``；
查询用 ``require_password_changed``，因为普通成员也该看到「模型尚未配置」这个
事实，否则他们只会在提问时撞上 424 而不知缘由。

P7-S10 对话模型端点（``/api/settings/llm-endpoint``）：面板能写端点就等于能决定把用户问题与检索条文发到哪，
所以写入只给管理员 + CSRF、只收 https（本机例外见 ``generation/endpoints.py``）、运维 env 设了即只读、改了
端点或模型就作废旧兼容报告；兼容性检查同一时间只跑一个，结果写报告文件。成员只看到主机名与模型名。
审计只记主机名、模型名与结果码，**不记 Key、不记完整 URL**。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from water_agent.config.credentials import (
    CREDENTIALS,
    CredentialStatus,
    CredentialStoreUnavailableError,
    InvalidCredentialError,
    UnknownCredentialError,
    credential_status,
    delete_credential,
    write_credential,
)
from water_agent.generation.chat_model import ModelListError, list_models
from water_agent.generation.compatibility import (
    PANEL_REPORT_FILE,
    CompatibilityReport,
    compatibility_report_path,
    endpoint_verification,
    read_report,
)
from water_agent.generation.endpoints import (
    SOURCE_LOCAL_MODEL,
    EndpointConfigError,
    chat_env_configured,
    endpoint_host,
    resolve_chat_endpoint,
    write_panel_endpoint,
)
from water_agent.generation.local_model import local_model_configured
from water_agent.webapp.auth.audit import (
    audit_llm_compatibility_checked,
    audit_llm_endpoint_updated,
    audit_llm_models_listed,
)
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_admin, require_password_changed
from water_agent.webapp.auth.models import UserRole

router = APIRouter()
_API_VERSION = "v1"

_ERROR_STATUS: dict[str, int] = {
    "WEB_CREDENTIAL_UNKNOWN": 404,
    "WEB_CREDENTIAL_INVALID": 422,
    "WEB_CREDENTIAL_STORE_UNAVAILABLE": 503,
    "WEB_LLM_ENDPOINT_INVALID": 422,
    "WEB_LLM_ENDPOINT_MANAGED": 409,
    "WEB_LLM_COMPATIBILITY_RUNNING": 409,
    "WEB_LLM_MODELS_UNAUTHORIZED": 422,
    "WEB_LLM_MODELS_UNSUPPORTED": 422,
    "WEB_LLM_MODELS_DESTINATION_FORBIDDEN": 422,
    "WEB_LLM_MODELS_UNREACHABLE": 502,
}


class CredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 上限只为挡住误粘整个文件；具体形态校验在 validate_credential_value，
    # 那里能给出「不能含空格」这类可操作的原因。
    value: str = Field(min_length=1, max_length=4096)


def _ok(data: object) -> JSONResponse:
    # no-store：凭据状态是实时配置，被浏览器缓存后面板会显示陈旧的「未配置」，
    # 用户会以为保存没生效而重复填写。
    return JSONResponse(
        status_code=200,
        content={"version": _API_VERSION, "ok": True, "data": data},
        headers={"Cache-Control": "no-store"},
    )


def _error(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=_ERROR_STATUS.get(code, 400),
        content={"version": _API_VERSION, "ok": False, "error": {"code": code, "message": message}},
    )


def _rendered(status: CredentialStatus) -> dict[str, object]:
    return {
        "name": status.name,
        "configured": status.configured,
        "source": status.source,
        "updated_at": status.updated_at,
    }


def _unknown(name: str) -> JSONResponse:
    # 不回显请求里的名字，避免把任意输入反射进响应体。
    return _error("WEB_CREDENTIAL_UNKNOWN", "该凭据不可配置")


def _store_unavailable() -> JSONResponse:
    return _error(
        "WEB_CREDENTIAL_STORE_UNAVAILABLE",
        "凭据存储不可写：请确认已挂载 app_credentials 卷，或改用环境变量配置",
    )


@router.get("/api/settings/credentials")
async def list_credentials(
    auth: tuple = Depends(require_password_changed),
) -> JSONResponse:
    _user, _principal = auth
    return _ok({"credentials": [_rendered(credential_status(name)) for name in CREDENTIALS]})


@router.put("/api/settings/credentials/{name}", dependencies=[Depends(validate_csrf)])
async def put_credential(
    name: str,
    body: CredentialRequest,
    auth: tuple = Depends(require_admin),
) -> JSONResponse:
    _user, _principal = auth
    try:
        write_credential(name, body.value)
    except UnknownCredentialError:
        return _unknown(name)
    except InvalidCredentialError as error:
        return _error("WEB_CREDENTIAL_INVALID", str(error))
    except CredentialStoreUnavailableError:
        return _store_unavailable()
    return _ok({"credential": _rendered(credential_status(name))})


@router.delete("/api/settings/credentials/{name}", dependencies=[Depends(validate_csrf)])
async def remove_credential(
    name: str,
    auth: tuple = Depends(require_admin),
) -> JSONResponse:
    _user, _principal = auth
    try:
        delete_credential(name)
    except UnknownCredentialError:
        return _unknown(name)
    except CredentialStoreUnavailableError:
        return _store_unavailable()
    # 删除后仍可能「已配置」——环境变量或 Compose secret 那两层不由本进程管理，
    # 如实报告，免得用户以为删干净了却还在用旧 key。
    return _ok({"credential": _rendered(credential_status(name))})


# ---------------------------------------------------------------------------
# 对话模型端点（P7-S10）
# ---------------------------------------------------------------------------


class EndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 只有两个短字符串（JSON 请求体最坏每字符 12 字节，不加长文本字段）
    base_url: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=200)


class ModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1, max_length=512)
    # 表单里刚填、尚未保存的 Key；缺省则用服务端已存的有效 Key（前端永远拿不到已存的值）。只用于这一次调用。
    api_key: str | None = Field(default=None, max_length=4096)


class _CompatibilityCheck:
    """兼容性检查同一时间只跑一个（进程内；Web 是单 worker 部署，见 compose.yml）。后台线程里跑。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.last: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None

    def start(self, target: Callable[[], dict[str, Any]]) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self._thread = threading.Thread(target=self._run, args=(target,), daemon=True)
            self._thread.start()
            return True

    def _run(self, target: Callable[[], dict[str, Any]]) -> None:
        try:
            result = target()
        except Exception as exc:  # noqa: BLE001 — 结果如实落到 last，不能让线程无声死掉
            result = {"status": "blocked", "error": type(exc).__name__}
        result["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self.last = result
            self.running = False

    def join(self, timeout: float | None = None) -> None:
        """测试用：等当前检查结束。"""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)


_CHECK = _CompatibilityCheck()


def _locked_reason() -> str | None:
    if chat_env_configured():
        return "deployment"
    if local_model_configured():
        return "local_model"
    return None


def _endpoint_view(user: Any) -> dict[str, Any]:
    is_admin = user.role is UserRole.ADMIN
    locked = _locked_reason()
    base: dict[str, Any] = {
        "editable": is_admin and locked is None,
        "locked_reason": locked,
        "can_manage": is_admin,
        "check": {"running": _CHECK.running, "last": _CHECK.last},
    }
    try:
        endpoint = resolve_chat_endpoint()
    except EndpointConfigError as exc:
        return {
            **base,
            "endpoint": None,
            "api_key": None,
            "compatibility": None,
            "error": {"code": "WEB_LLM_ENDPOINT_INVALID", "message": str(exc)},
        }
    verification = endpoint_verification(endpoint)
    report = read_report(compatibility_report_path(), CompatibilityReport)
    report_view = None
    if report is not None and report.matches(endpoint=endpoint.base_url, model=endpoint.model):
        report_view = {
            "status": report.status,
            "checks": dict(report.checks),
            "probe_version": report.version,
            "checked_at": report.evidence.get("checked_at"),
        }
    shown: dict[str, Any] = {
        "host": endpoint.host,
        "model": endpoint.model,
        "source": endpoint.source,
        "is_default": endpoint.is_default,
    }
    if is_admin:
        shown["base_url"] = endpoint.base_url
    return {
        **base,
        "endpoint": shown,
        "api_key": (
            {"name": endpoint.api_key_name, "configured": endpoint.missing_key() is None}
            if endpoint.api_key_name
            else None
        ),
        "compatibility": {
            "status": verification.status,
            "reason": verification.reason,
            "report": report_view,
        },
        "error": None,
    }


def _invalidate_panel_report() -> None:
    """改了端点或模型：作废面板写的兼容报告（运维用 env 指定的报告不由面板删除，精确匹配照样作废它）。"""

    import os

    from water_agent.config.credentials import credentials_dir
    from water_agent.generation.compatibility import COMPATIBILITY_REPORT_ENV

    if (os.environ.get(COMPATIBILITY_REPORT_ENV) or "").strip():
        return
    (credentials_dir() / PANEL_REPORT_FILE).unlink(missing_ok=True)


@router.get("/api/settings/llm-endpoint")
async def get_llm_endpoint(auth: tuple = Depends(require_password_changed)) -> JSONResponse:
    user, _principal = auth
    return _ok(_endpoint_view(user))


@router.put("/api/settings/llm-endpoint", dependencies=[Depends(validate_csrf)])
async def put_llm_endpoint(body: EndpointRequest, auth: tuple = Depends(require_admin)) -> JSONResponse:
    user, _principal = auth
    locked = _locked_reason()
    if locked == "deployment":
        return _error(
            "WEB_LLM_ENDPOINT_MANAGED",
            "对话端点由部署配置（WATER_AGENT_LLM_BASE_URL / WATER_AGENT_LLM_MODEL），面板只读",
        )
    if locked == "local_model":
        return _error("WEB_LLM_ENDPOINT_MANAGED", "本地模型路径（LOCAL_MODEL_ENABLED）启用中，与面板端点二选一")
    if _CHECK.running:
        return _error("WEB_LLM_COMPATIBILITY_RUNNING", "兼容性检查进行中，结束后再改端点")
    try:
        before = resolve_chat_endpoint()
        previous: tuple[str, str] | None = (before.base_url, before.model)
    except EndpointConfigError:
        previous = None
    try:
        base_url, model = write_panel_endpoint(body.base_url, body.model)
    except EndpointConfigError as exc:
        # 说明里不回显请求值
        return _error("WEB_LLM_ENDPOINT_INVALID", str(exc))
    except OSError:
        return _store_unavailable()
    changed = previous != (base_url, model)
    if changed:
        _invalidate_panel_report()
    audit_llm_endpoint_updated(
        actor_user_id=str(user.user_id), host=endpoint_host(base_url), model=model, changed=changed
    )
    return _ok(_endpoint_view(user))


@router.post("/api/settings/llm-endpoint/compatibility", dependencies=[Depends(validate_csrf)])
async def run_llm_compatibility(auth: tuple = Depends(require_admin)) -> JSONResponse:
    """对当前对话端点跑一次兼容性探针（**会产生少量计费调用**，面板上明示）。后台线程，202 后轮询 GET。"""

    user, _principal = auth
    try:
        endpoint = resolve_chat_endpoint()
    except EndpointConfigError as exc:
        return _error("WEB_LLM_ENDPOINT_INVALID", str(exc))
    if endpoint.source == SOURCE_LOCAL_MODEL:
        return _error("WEB_LLM_ENDPOINT_MANAGED", "本地模型路径请用 python -m water_agent.harness.local_model_compat")

    def target() -> dict[str, Any]:
        from water_agent.harness.endpoint_compat import run_endpoint_compatibility

        report, skipped = run_endpoint_compatibility(out_path=compatibility_report_path())
        if report is None:
            result: dict[str, Any] = {"status": "skipped", "requires": list((skipped or {}).get("requires", []))}
        else:
            result = {"status": report.status}
        audit_llm_compatibility_checked(
            actor_user_id=str(user.user_id), host=endpoint.host, model=endpoint.model, result=result["status"]
        )
        return result

    if not _CHECK.start(target):
        return _error("WEB_LLM_COMPATIBILITY_RUNNING", "兼容性检查进行中")
    return JSONResponse(
        status_code=202,
        content={"version": _API_VERSION, "ok": True, "data": {"check": {"running": True}}},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/settings/llm-endpoint/models", dependencies=[Depends(validate_csrf)])
async def fetch_llm_models(body: ModelsRequest, auth: tuple = Depends(require_admin)) -> JSONResponse:
    """用表单里**尚未保存**的 base_url（+ 可选 Key）取模型列表，供面板下拉选择。

    只读操作，部署锁定（env / 本地模型）时同样允许。出站走 ``list_models``（解析后校验目的地址、钉死 IP、
    不跟随重定向、响应有上限）。响应只含模型 id；Key 与完整 URL 不进响应、审计与日志。
    """

    user, _principal = auth
    typed = (body.api_key or "").strip()
    try:
        key = typed or (resolve_chat_endpoint(base_url=body.base_url, model="model-list").api_key() or "")
    except EndpointConfigError as exc:
        return _error("WEB_LLM_ENDPOINT_INVALID", str(exc))
    host = ""
    try:
        host = endpoint_host(body.base_url.strip())
        # 阻塞式网络 IO：放进线程池，免得拖住事件循环
        models = await run_in_threadpool(list_models, body.base_url, key)
    except ModelListError as exc:
        audit_llm_models_listed(actor_user_id=str(user.user_id), host=host, result=exc.code, count=0)
        return _error(exc.code, exc.message)
    audit_llm_models_listed(actor_user_id=str(user.user_id), host=host, result="ok", count=len(models))
    return _ok({"models": models})
