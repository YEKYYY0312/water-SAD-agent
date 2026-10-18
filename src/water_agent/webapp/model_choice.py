"""按消息 / 按会话选模型的校验（P7-S15，规格 ``docs/design/18-p7-multi-model.md`` §3.2、§3.4）。

**用户显式选择不是路由**：模型从不因失败、成本或问题难度被自动更换（P7-I21）。这里只回答"这个用户现在能不能选这个
模型 id"，一律失败关闭：

1. 部署把对话端点锁住（env 配了端点 / 模型，或 ``LOCAL_MODEL_ENABLED``）→ ``WEB_MODEL_LOCKED``（409）。本地模型模式
   下只带 ``model`` 的显式覆盖会跳过本地分支、落到 DeepSeek 缺省端点——数据去向翻转，必须拒绝（P7-I16）；
2. 目录读不出 → ``WEB_MODEL_CATALOG_INVALID``（503）；不在目录里 / 已停用 → ``WEB_MODEL_UNKNOWN``（422）；
3. 目录项声明 ``thinking: roundtrip`` → ``WEB_MODEL_THINKING_UNSUPPORTED``（422，S16 才放行）；
4. 角色不在 ``allowed_roles`` 里 → ``WEB_MODEL_FORBIDDEN``（403）——**先于**配置 / 验证类的 424，授权在前、配置披露在后；
5. 真实工厂下：``chat_model_problem(model=…)``——缺 Key → ``WEB_MODEL_NOT_CONFIGURED``、没有通过的兼容性报告 →
   ``WEB_MODEL_NOT_VERIFIED``（424，逐对验证）。

请求体、会话 PATCH 都**不接受** ``base_url`` / ``api_key``：首版同端点、同 Key 槽，密钥只发给所属服务商（P7-I15）。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.responses import JSONResponse

from water_agent.generation.endpoints import chat_env_configured
from water_agent.generation.local_model import local_model_configured
from water_agent.generation.model_catalog import load_catalog
from water_agent.runtime.agentic import chat_model_problem
from water_agent.webapp.auth.models import UserRole

_API_VERSION = "v1"

MODEL_UNKNOWN = "WEB_MODEL_UNKNOWN"
MODEL_FORBIDDEN = "WEB_MODEL_FORBIDDEN"
MODEL_LOCKED = "WEB_MODEL_LOCKED"
MODEL_THINKING_UNSUPPORTED = "WEB_MODEL_THINKING_UNSUPPORTED"
MODEL_CATALOG_INVALID = "WEB_MODEL_CATALOG_INVALID"
MODEL_UNSUPPORTED = "WEB_MODEL_UNSUPPORTED"

_STATUS = {
    MODEL_UNKNOWN: 422,
    MODEL_FORBIDDEN: 403,
    MODEL_LOCKED: 409,
    MODEL_THINKING_UNSUPPORTED: 422,
    MODEL_CATALOG_INVALID: 503,
    MODEL_UNSUPPORTED: 422,
}


def locked_reason() -> str | None:
    """部署是否把对话端点锁住了（面板只读、逐条选择被拒）：``deployment`` / ``local_model`` / ``None``。"""

    if chat_env_configured():
        return "deployment"
    if local_model_configured():
        return "local_model"
    return None


@dataclass(frozen=True, slots=True)
class ModelChoiceProblem:
    code: str
    message: str
    status: int

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content={
                "version": _API_VERSION,
                "ok": False,
                "error": {"code": self.code, "message": self.message},
            },
        )


def effective_model(requested: str | None, session_default: str | None) -> str | None:
    """优先级：消息 > 会话默认 > 全局（``None``）。"""

    return requested or session_default or None


def check_model_choice(
    model_id: str, *, role: UserRole, verify: bool
) -> ModelChoiceProblem | None:
    """这个角色能不能选这个模型。返回 ``None`` 表示可以；说明里不回显请求值。

    ``verify=False`` 用于 mock 生成器 / 测试替身工厂：目录、角色、锁定仍然校验，只跳过需要真实端点的
    ``chat_model_problem``（与现有预检块"只对真实工厂做配置检查"一致）。
    """

    reason = locked_reason()
    if reason is not None:
        return ModelChoiceProblem(
            MODEL_LOCKED,
            "对话端点由部署配置或本地模型路径管理，不允许按会话 / 按消息选择模型",
            _STATUS[MODEL_LOCKED],
        )
    state = load_catalog()
    if state.error is not None:
        return ModelChoiceProblem(MODEL_CATALOG_INVALID, "模型目录不可用，请联系管理员", _STATUS[MODEL_CATALOG_INVALID])
    entry = state.find(model_id)
    if entry is None or not entry.enabled:
        return ModelChoiceProblem(MODEL_UNKNOWN, "所选模型不在可用目录里", _STATUS[MODEL_UNKNOWN])
    if entry.thinking == "roundtrip":
        return ModelChoiceProblem(
            MODEL_THINKING_UNSUPPORTED, "该模型是思考型模型，当前版本尚不支持选用", _STATUS[MODEL_THINKING_UNSUPPORTED]
        )
    if entry.allowed_roles is not None and role.value not in entry.allowed_roles:
        return ModelChoiceProblem(MODEL_FORBIDDEN, "当前账号无权选用该模型", _STATUS[MODEL_FORBIDDEN])
    if verify:
        problem = chat_model_problem(model=model_id)
        if problem is not None:
            return ModelChoiceProblem(problem[0], problem[1], 424)
    return None


__all__ = [
    "MODEL_CATALOG_INVALID",
    "MODEL_FORBIDDEN",
    "MODEL_LOCKED",
    "MODEL_THINKING_UNSUPPORTED",
    "MODEL_UNKNOWN",
    "MODEL_UNSUPPORTED",
    "ModelChoiceProblem",
    "check_model_choice",
    "effective_model",
    "locked_reason",
]
