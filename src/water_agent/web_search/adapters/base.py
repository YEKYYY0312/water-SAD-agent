"""适配器的公共契约（P7-S12）。纯数据类型，不含任何网络、模型或文件系统访问。"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

#: 响应体上限（SDD §5.3）：超出即中止，由 ``governed_get(max_bytes=…)`` 强制。
MAX_RESPONSE_BYTES = 256 * 1024
#: 响应的 ``Content-Type`` 必须正是它（``governed_get(require_media_type=…)``）。
JSON_MEDIA_TYPE = "application/json"


class AdapterParseError(ValueError):
    """响应是合法 JSON 但不是该 provider 的形状。映射成 ``WEB_SEARCH_BAD_RESPONSE``；消息**不含**响应内容。"""


@dataclass(frozen=True, slots=True)
class RawHit:
    """一条未经处理的外部结果。三个字段都是**外部文本**，之后一律过结果流水线（网址校验 + 信封归一化）。"""

    title: str
    snippet: str
    url: str


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """相对基址的一次 GET：路径、查询参数、请求头（不含密钥以外的任何调用方数据）。

    ``headers`` 里可能有凭据（brave 的订阅令牌）——它只在发出请求时使用，**不进**日志 / trace / 错误。
    """

    path: str
    params: Mapping[str, str]
    headers: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # 不泄漏头里的凭据
        return f"RequestSpec(path={self.path!r}, params={sorted(self.params)!r}, headers={sorted(self.headers)!r})"


@dataclass(frozen=True, slots=True)
class ProviderAdapter:
    """一个 provider 的全部知识：id、是否要密钥、怎么构造请求、怎么解析响应。"""

    provider_id: str
    needs_key: bool
    build_request: Callable[[str, int, str], RequestSpec]
    parse_response: Callable[[bytes], list[RawHit]]


__all__ = [
    "JSON_MEDIA_TYPE",
    "MAX_RESPONSE_BYTES",
    "AdapterParseError",
    "ProviderAdapter",
    "RawHit",
    "RequestSpec",
]
