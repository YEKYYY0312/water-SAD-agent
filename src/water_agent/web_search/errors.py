"""联网搜索的稳定错误码与固定文案（P7-S12，规格 §5.8）。

错误 observation 只含**码与一句固定的话**：不含上游正文、网址、主机、查询串、密钥。模型会读到它，用户的浏览器会
通过 ``tool_result`` 事件看到它（``message`` 字段）。S11 内核的 ``EXTERNAL_*`` 码在这里映射成 ``WEB_SEARCH_*``，
不泄漏到工具结果里。显式配置的 provider 失败**不回退**到别的 provider、缓存或本地，错误文案也明说"不要重复尝试"。
"""
from __future__ import annotations

from typing import Any

from water_agent.external.http_get import (
    EXTERNAL_AUTH,
    EXTERNAL_BAD_RESPONSE,
    EXTERNAL_DESTINATION_FORBIDDEN,
    EXTERNAL_RATE_LIMITED,
    EXTERNAL_TIMEOUT,
    EXTERNAL_TOO_LARGE,
    EXTERNAL_UNAVAILABLE,
)

WEB_SEARCH_UNAVAILABLE = "WEB_SEARCH_UNAVAILABLE"
WEB_SEARCH_AUTH = "WEB_SEARCH_AUTH"
WEB_SEARCH_RATE_LIMITED = "WEB_SEARCH_RATE_LIMITED"
WEB_SEARCH_BAD_RESPONSE = "WEB_SEARCH_BAD_RESPONSE"
WEB_SEARCH_QUERY_REJECTED = "WEB_SEARCH_QUERY_REJECTED"
WEB_SEARCH_QUOTA = "WEB_SEARCH_QUOTA"
#: 用户勾选了联网，但预算让位使工具本轮没能开放：以 ``warnings`` 条目与 trace 事件发出，不是工具错误。
WEB_SEARCH_NOT_EXPOSED = "WEB_SEARCH_NOT_EXPOSED"

_MESSAGES: dict[str, str] = {
    WEB_SEARCH_UNAVAILABLE: "联网搜索服务暂时不可用；不要重复尝试，请基于已有信息回答或如实告知用户。",
    WEB_SEARCH_AUTH: "联网搜索服务拒绝了本次凭据（需运维检查密钥配置）；不要重复尝试。",
    WEB_SEARCH_RATE_LIMITED: "联网搜索触发限流；不要重复尝试，请基于已有信息回答或如实告知用户。",
    WEB_SEARCH_BAD_RESPONSE: "联网搜索服务返回了无法使用的响应；不要重复尝试。",
    WEB_SEARCH_QUERY_REJECTED: (
        "该搜索词未通过出口检查（可能含个人信息、凭据、网址，或与私有资料重合），未发出任何请求；"
        "请改写成不含私有内容的公开问题，或直接说明。"
    ),
    WEB_SEARCH_QUOTA: "本回合联网搜索次数已用完；请基于已有结果回答。",
    WEB_SEARCH_NOT_EXPOSED: "联网搜索本轮没有开放（工具预算已被其他工具占满），本回答未联网。",
}

WEB_SEARCH_ERROR_CODES: frozenset[str] = frozenset(_MESSAGES)

#: 内核错误码 → 工具错误码。禁止目的地对模型来说就是"服务不可用"（不告诉它网段分类）；超限响应是"坏响应"。
_FROM_EXTERNAL: dict[str, str] = {
    EXTERNAL_DESTINATION_FORBIDDEN: WEB_SEARCH_UNAVAILABLE,
    EXTERNAL_TIMEOUT: WEB_SEARCH_UNAVAILABLE,
    EXTERNAL_UNAVAILABLE: WEB_SEARCH_UNAVAILABLE,
    EXTERNAL_AUTH: WEB_SEARCH_AUTH,
    EXTERNAL_RATE_LIMITED: WEB_SEARCH_RATE_LIMITED,
    EXTERNAL_BAD_RESPONSE: WEB_SEARCH_BAD_RESPONSE,
    EXTERNAL_TOO_LARGE: WEB_SEARCH_BAD_RESPONSE,
}


def code_from_external(external_code: str) -> str:
    """未知的内核码一律当作"服务不可用"（不泄漏内核的命名空间，也不放大故障）。"""

    return _FROM_EXTERNAL.get(external_code, WEB_SEARCH_UNAVAILABLE)


def message_for(code: str) -> str:
    return _MESSAGES[code]


def error_result(code: str) -> dict[str, Any]:
    """工具失败的返回值（业务失败走返回值，不走 ``TOOL_ERROR``——保住稳定码语义）。**不含** ``urls``：请求没成功。"""

    if code not in _MESSAGES:
        raise ValueError(f"未知联网搜索错误码 {code!r}")
    return {"ok": False, "error": True, "code": code, "message": _MESSAGES[code]}


__all__ = [
    "WEB_SEARCH_AUTH",
    "WEB_SEARCH_BAD_RESPONSE",
    "WEB_SEARCH_ERROR_CODES",
    "WEB_SEARCH_NOT_EXPOSED",
    "WEB_SEARCH_QUERY_REJECTED",
    "WEB_SEARCH_QUOTA",
    "WEB_SEARCH_RATE_LIMITED",
    "WEB_SEARCH_UNAVAILABLE",
    "code_from_external",
    "error_result",
    "message_for",
]
