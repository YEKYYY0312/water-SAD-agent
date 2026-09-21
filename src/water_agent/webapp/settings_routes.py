"""P5 设置路由：LLM 凭据的状态查询与写入。

凭据值只进不出。GET 只报告「是否已配置、来自哪一层、何时更新」，既不返回明文，
也不返回末尾几位——末几位配上供应商固定前缀会缩小暴力搜索范围，而它对「我配的
到底是哪个 key」这个问题并不能给出可靠答案。要确认填对了，看能否成功对话。

权限刻意分两档：写入是管理操作，与 /api/admin/users 一致用 ``require_admin``；
查询用 ``require_password_changed``，因为普通成员也该看到「模型尚未配置」这个
事实，否则他们只会在提问时撞上 424 而不知缘由。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
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
from water_agent.webapp.auth.csrf import validate_csrf
from water_agent.webapp.auth.dependencies import require_admin, require_password_changed

router = APIRouter()
_API_VERSION = "v1"

_ERROR_STATUS: dict[str, int] = {
    "WEB_CREDENTIAL_UNKNOWN": 404,
    "WEB_CREDENTIAL_INVALID": 422,
    "WEB_CREDENTIAL_STORE_UNAVAILABLE": 503,
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
