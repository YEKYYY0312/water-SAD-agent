"""MCP JSON-RPC 2.0 协议层（P4 Step 23）：编解码 + 消息构造 + 结果解析。

只覆盖本网关用到的最小面（规格 §2.2）：
- 请求/通知/错误响应构造与响应匹配（``id`` 配对）；
- 行分隔 UTF-8 JSON 的**解码**（坏 JSON / 批量数组 / 非 object 一律协议错误）；
- ``tools/call`` 结果解析（text content 块收集 + ``isError`` 语义）。

纯函数、无 I/O：传输（子进程管道）在 ``client.py``，可独立测试。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from water_agent.mcp.errors import (
    MCP_PROTOCOL_ERROR,
    MCPError,
)

#: JSON-RPC 协议版本字段值。
JSONRPC_VERSION = "2.0"

#: 本网关发起握手时声明的 MCP 协议版本。
MCP_PROTOCOL_VERSION = "2025-06-18"

#: initialize 的 clientInfo（稳定标识，进 server 日志）。
CLIENT_INFO: dict[str, str] = {"name": "water-agent", "version": "0.1.0"}

#: JSON-RPC 2.0 保留错误码之外、MCP 语义里常用的实现定义错误码。
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# 出站消息构造
# ---------------------------------------------------------------------------


def make_request(request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造 JSON-RPC 2.0 请求。``id`` 为单调整数（本网关不并发，无需字符串 uuid）。"""
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def make_notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造通知（无 id，server 不回包）。"""
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        message["params"] = params
    return message


def make_error_response(request_id: int | str | None, code: int, message: str) -> dict[str, Any]:
    """构造对 server 请求的错误响应（本网关不支持任何 server→client 请求）。"""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def encode_message(message: dict[str, Any]) -> str:
    """单行 UTF-8 JSON + ``\\n``（MCP stdio 线上格式，无 Content-Length 头）。

    ``ensure_ascii=False``：中文参数（图纸登记号、条文号）按原样编码，
    与 server 侧解码约定一致。返回 **str**（一行）；stdin 以二进制写入，
    由调用方（client）负责 encode —— 测试可直接断言文本形态。
    """
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------------------
# 入站消息解码
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RpcMessage:
    """一条解码后的 server 消息。``payload`` 是原始 dict（shape 校验过的）。"""

    payload: dict[str, Any]

    @property
    def message_id(self) -> int | str | None:
        return self.payload.get("id")

    @property
    def is_response(self) -> bool:
        return "id" in self.payload and ("result" in self.payload or "error" in self.payload)

    @property
    def is_request(self) -> bool:
        """server 主动发起的请求（有 id 有 method）—— 本网关不支持，需回错。"""
        return "id" in self.payload and "method" in self.payload

    @property
    def is_notification(self) -> bool:
        """server 通知（无 id 有 method）—— 忽略但可观测记录。"""
        return "id" not in self.payload and "method" in self.payload

    @property
    def method(self) -> str:
        return str(self.payload.get("method", ""))

    @property
    def error(self) -> dict[str, Any] | None:
        error = self.payload.get("error")
        return error if isinstance(error, dict) else None

    @property
    def result(self) -> Any:
        return self.payload.get("result")


def decode_message(raw: bytes | str) -> dict[str, Any]:
    """解码一行 JSON-RPC 消息，返回原始 payload dict。

    任何解析失败都是 :class:`MCPError`（协议错误）。消息的语义视图
    （响应/请求/通知判定）见 :class:`RpcMessage`。

    刻意**不接受批量（数组）**：MCP stdio 不用 JSON-RPC 批处理，接受它只会
    扩大攻击面（一次注入多条消息）。外层数组直接判协议错误。
    """
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MCPError(
                MCP_PROTOCOL_ERROR, f"消息不是合法 UTF-8：{exc.reason}"
            ) from exc
    else:
        text = raw
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPError(
            MCP_PROTOCOL_ERROR, f"消息不是合法 JSON（列 {exc.pos}）：{exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise MCPError(
            MCP_PROTOCOL_ERROR,
            f"消息必须是 JSON object，得到 {type(payload).__name__}（不支持批量/数组消息）",
        )
    if payload.get("jsonrpc") != JSONRPC_VERSION:
        raise MCPError(MCP_PROTOCOL_ERROR, f"缺少 jsonrpc:{JSONRPC_VERSION!r} 字段")
    return payload


# ---------------------------------------------------------------------------
# tools/call 结果解析
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """``tools/call`` 的解析结果。

    ``texts`` 只含 ``{type:"text"}`` 块的文本（顺序保留）；其他类型块**不静默
    丢弃**，记进 ``unsupported``（规格 §4.4）。``is_error=True`` 时 texts 仍保留
    （server 的错误说明文字对排障有用）。``structured_content`` 是 server 按
    MCP 约定可选返回的 ``structuredContent``（结构化结果），缺失为 ``None``。
    """

    texts: tuple[str, ...]
    is_error: bool
    unsupported: tuple[str, ...]
    structured_content: dict[str, Any] | None = None


def parse_tool_call_result(result: Any) -> ToolCallResult:
    """解析 ``tools/call`` 的 ``result`` 字段。

    MCP 约定：业务失败通过 ``result.isError=true + content`` 返回（HTTP 200 式），
    协议失败才走 JSON-RPC error。本函数只做 shape 解析，``is_error`` 的
    结构化转换由调用方（client）完成。
    """
    if not isinstance(result, dict):
        raise MCPError(
            MCP_PROTOCOL_ERROR,
            f"tools/call result 必须是 object，得到 {type(result).__name__}",
        )
    content = result.get("content", [])
    if not isinstance(content, list):
        raise MCPError(MCP_PROTOCOL_ERROR, "content 必须是数组")
    texts: list[str] = []
    unsupported: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            unsupported.append(f"non_object:{type(block).__name__}")
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
            else:
                unsupported.append("text_without_text_field")
        elif block_type is None:
            unsupported.append("block_without_type")
        else:
            # image/resource/audio 等：显式记录类型，不静默丢弃（规格 §4.4）
            unsupported.append(f"unsupported:{block_type}")
    structured = result.get("structuredContent")
    if structured is not None and not isinstance(structured, dict):
        unsupported.append(f"structured_content:{type(structured).__name__}")
        structured = None
    return ToolCallResult(
        texts=tuple(texts),
        is_error=bool(result.get("isError", False)),
        unsupported=tuple(unsupported),
        structured_content=structured,
    )


def remote_error_message(error: dict[str, Any]) -> str:
    """从 JSON-RPC error 对象提取可行动说明（截断，防巨型错误文本刷屏）。"""
    code = error.get("code")
    message = error.get("message")
    text = message if isinstance(message, str) else str(message)
    if len(text) > 500:
        text = text[:500] + "…[截断]"
    return f"JSON-RPC error {code}: {text}"


__all__ = [
    "CLIENT_INFO",
    "JSONRPC_INTERNAL_ERROR",
    "JSONRPC_INVALID_PARAMS",
    "JSONRPC_METHOD_NOT_FOUND",
    "JSONRPC_VERSION",
    "MCP_PROTOCOL_VERSION",
    "RpcMessage",
    "ToolCallResult",
    "decode_message",
    "encode_message",
    "make_error_response",
    "make_notification",
    "make_request",
    "parse_tool_call_result",
    "remote_error_message",
]
