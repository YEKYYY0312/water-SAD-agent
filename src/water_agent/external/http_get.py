"""受治理的 GET 内核（P7-S11，规格 ``docs/design/17-p7-external-capabilities.md`` §4.1 E2）。

所有"联网获取"都只经这里：它包装 ``mcp/http_transport.py::open_exchange``（解析后**整体**分类全部应答地址、
连接钉死校验过的 IP、3xx 不跟随、响应字节与时间上限）并经 ``ProviderGateway.call``（限流 / 熔断 / 对账；
usage 记**未知**，不估算）。本模块**没有任何原始网络原语**——只能调用 ``open_exchange``，其调用方由
P7-I08（``tests/test_plugin_invariants.py``）登记并用 AST 判定。

错误统一映射成本模块的稳定码（不泄漏 ``MCP_*`` 命名空间）。错误文本只含码与一句固定的话，**不含**上游正文、
网址、主机、查询串或密钥：调用方（将来的 ``web_search``）会把它原样放进 observation 回填给模型。

S11 没有任何生产调用方；网关调用意图（``ProviderCall``）由调用方传入，S12 随 ``web_search`` 新增自己的
``ProviderKind`` / ``CallCategory`` / 画像并登记出站站点（8 → 9）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from water_agent.mcp.errors import (
    MCP_DESTINATION_FORBIDDEN,
    MCP_RESPONSE_TOO_LARGE,
    MCPError,
)
from water_agent.mcp.http_transport import (
    Resolver,
    TransportFailure,
    TransportHttpStatus,
    TransportTimeout,
    open_exchange,
    parse_server_url,
)
from water_agent.runtime.governance.binding import default_gateway
from water_agent.runtime.governance.contracts import GovernanceError, ProviderCall, RequestContext
from water_agent.runtime.governance.gateway import ProviderGateway, ProviderUsage

EXTERNAL_DESTINATION_FORBIDDEN = "EXTERNAL_DESTINATION_FORBIDDEN"
EXTERNAL_TIMEOUT = "EXTERNAL_TIMEOUT"
EXTERNAL_UNAVAILABLE = "EXTERNAL_UNAVAILABLE"
EXTERNAL_AUTH = "EXTERNAL_AUTH"
EXTERNAL_RATE_LIMITED = "EXTERNAL_RATE_LIMITED"
EXTERNAL_BAD_RESPONSE = "EXTERNAL_BAD_RESPONSE"
EXTERNAL_TOO_LARGE = "EXTERNAL_TOO_LARGE"

#: 稳定码 → 固定的一句话。**不含**任何请求或响应里的内容。
_MESSAGES: dict[str, str] = {
    EXTERNAL_DESTINATION_FORBIDDEN: "目的地不被允许，未发出任何请求",
    EXTERNAL_TIMEOUT: "外部服务响应超时",
    EXTERNAL_UNAVAILABLE: "外部服务暂时不可用",
    EXTERNAL_AUTH: "外部服务拒绝了本次凭据",
    EXTERNAL_RATE_LIMITED: "外部服务触发限流",
    EXTERNAL_BAD_RESPONSE: "外部服务返回了无法接受的响应",
    EXTERNAL_TOO_LARGE: "外部服务返回的内容超过上限",
}
EXTERNAL_ERROR_CODES = frozenset(_MESSAGES)


class ExternalGetError(RuntimeError):
    """``governed_get`` 的失败。``str(exc)`` 与 ``message`` 都只含稳定码对应的固定文案。"""

    def __init__(self, code: str) -> None:
        if code not in _MESSAGES:
            raise ValueError(f"未知外部获取错误码 {code!r}")
        super().__init__(f"{code}：{_MESSAGES[code]}")
        self.code = code
        self.message = _MESSAGES[code]


@dataclass(frozen=True, slots=True)
class ExternalResponse:
    """一次成功的 GET。``body`` 已按 ``max_bytes`` 读完；``usage_known`` 恒为 False（HTTP 交换没有 token 用量）。"""

    status: int
    media_type: str
    body: bytes
    usage_known: bool = False


def _map_status(status: int) -> str:
    if status in (401, 403):
        return EXTERNAL_AUTH
    if status == 429:
        return EXTERNAL_RATE_LIMITED
    if status == 408 or status >= 500:
        return EXTERNAL_UNAVAILABLE
    return EXTERNAL_BAD_RESPONSE


def _map_cause(exc: BaseException | None) -> str:
    """把网关保留的原始异常映射成稳定码。未知的一律 ``EXTERNAL_UNAVAILABLE``（不泄漏异常类型与消息）。"""

    if isinstance(exc, ExternalGetError):
        return exc.code
    if isinstance(exc, MCPError):
        if exc.code == MCP_DESTINATION_FORBIDDEN:
            return EXTERNAL_DESTINATION_FORBIDDEN
        if exc.code == MCP_RESPONSE_TOO_LARGE:
            return EXTERNAL_TOO_LARGE
        # ``open_exchange`` 对 3xx / 协议错误抛 ``MCP_PROTOCOL_ERROR``：都是"不接受的响应"
        return EXTERNAL_BAD_RESPONSE
    if isinstance(exc, TransportHttpStatus):
        return _map_status(int(exc.status_code or 0))
    if isinstance(exc, (TransportTimeout, TimeoutError)):
        return EXTERNAL_TIMEOUT
    if isinstance(exc, TransportFailure):
        return EXTERNAL_UNAVAILABLE
    return EXTERNAL_UNAVAILABLE


def governed_get(
    url: str,
    *,
    headers: Mapping[str, str],
    max_bytes: int,
    timeout: float,
    allow_private_network: bool,
    ctx: RequestContext,
    call: ProviderCall,
    require_media_type: str | None = None,
    resolver: Resolver | None = None,
    ssl_context: object | None = None,
    gateway: ProviderGateway | None = None,
) -> ExternalResponse:
    """对 ``url`` 发一次受治理的 GET。

    - 目的地：``parse_server_url``（https，``allow_private_network`` 才收 http；拒 userinfo / fragment / 非规范 IPv4）
      → ``open_exchange`` 解析全部应答并整体分类，任一禁止即 ``EXTERNAL_DESTINATION_FORBIDDEN``，**零字节发出**；
      ``allow_private_network`` 只放行回环与私有，链路本地与云元数据地址永远拒；
    - 3xx 不跟随；响应体超过 ``max_bytes`` 即中止；``timeout`` 是整次交换的总预算；
    - ``require_media_type`` 给出时，响应 ``Content-Type`` 必须一致，否则 ``EXTERNAL_BAD_RESPONSE``；
    - 失败一律抛 :class:`ExternalGetError`（稳定码 + 固定文案），**不回退**到任何别的地址或方式；
    - 网关 usage 记未知（:meth:`ProviderUsage.unknown`），成本不估算。
    """

    try:
        target = parse_server_url(url, allow_private_network=allow_private_network)
    except (ValueError, MCPError):
        raise ExternalGetError(EXTERNAL_DESTINATION_FORBIDDEN) from None

    def executor() -> tuple[ExternalResponse, ProviderUsage]:
        exchange = open_exchange(
            target,
            "GET",
            headers=dict(headers),
            body=None,
            timeout=timeout,
            allow_private_network=allow_private_network,
            resolver=resolver,
            ssl_context=ssl_context,  # type: ignore[arg-type]
        )
        try:
            status = exchange.status
            if status >= 400:
                # 不读响应体：错误响应的正文不进任何异常、日志或返回值
                raise TransportHttpStatus(status, exchange.headers, b"")
            if status != 200:
                raise ExternalGetError(EXTERNAL_BAD_RESPONSE)
            media = exchange.media_type
            if require_media_type is not None and media != require_media_type.lower():
                raise ExternalGetError(EXTERNAL_BAD_RESPONSE)
            body = exchange.read_body(max_bytes)
        finally:
            exchange.close()
        return ExternalResponse(status=status, media_type=media, body=body), ProviderUsage.unknown()

    try:
        return (gateway if gateway is not None else default_gateway()).call(ctx, call, executor).value
    except GovernanceError as exc:
        # 网关把 executor 里的异常包成 GovernanceError 并保留 ``__cause__``；
        # 熔断 / 限流 / 预算等网关自己的拒绝没有 cause，按"暂时不可用 / 限流"处理
        cause = exc.__cause__
        if cause is None:
            raise ExternalGetError(EXTERNAL_RATE_LIMITED if exc.code == "RATE_LIMITED" else EXTERNAL_UNAVAILABLE) from None
        raise ExternalGetError(_map_cause(cause)) from None


__all__ = [
    "EXTERNAL_AUTH",
    "EXTERNAL_BAD_RESPONSE",
    "EXTERNAL_DESTINATION_FORBIDDEN",
    "EXTERNAL_ERROR_CODES",
    "EXTERNAL_RATE_LIMITED",
    "EXTERNAL_TIMEOUT",
    "EXTERNAL_TOO_LARGE",
    "EXTERNAL_UNAVAILABLE",
    "ExternalGetError",
    "ExternalResponse",
    "governed_get",
]
