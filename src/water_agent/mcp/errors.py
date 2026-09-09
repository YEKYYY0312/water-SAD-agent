"""MCP 网关稳定错误码（P4 Step 23）。

错误码是**稳定契约**：测试、决策记录与 trace 都依赖它，改名等同破坏兼容。
与治理错误码（``runtime/governance/contracts.py``）分开命名空间 ——
MCP 是外部协议面，不是治理面；治理错误码集合有测试锁死，
不得被外部协议错误挤占。

语义纪律与 AGENTS.md 的 SKIP/blocked 同源：
- 前置条件缺失（连接失败、schema 不支持）与**真实失败**（远程报错、超时）
  必须可区分；
- 每个错误都应可映射到"谁需要做什么"：配置错误找配置、协议错误看 server、
  白名单/绑定拒绝找装配。
"""
from __future__ import annotations

#: 配置文件存在但解析/校验失败（响亮失败，不静默跳过全部 server）。
MCP_CONFIG_INVALID = "MCP_CONFIG_INVALID"
#: server 进程启动失败或 initialize 超时/失败/提前退出。
MCP_CONNECT_FAILED = "MCP_CONNECT_FAILED"
#: 未握手即调用 tools/call（或连接已关闭）。
MCP_NOT_INITIALIZED = "MCP_NOT_INITIALIZED"
#: 消息无法按 JSON-RPC 2.0 解析（坏 JSON / 批量数组 / 非法 shape）。
MCP_PROTOCOL_ERROR = "MCP_PROTOCOL_ERROR"
#: 单次调用超过 deadline（``call_timeout_seconds`` 或显式 ``deadline_seconds``）。
MCP_TIMEOUT = "MCP_TIMEOUT"
#: 单条响应超过 ``max_response_bytes``。
MCP_RESPONSE_TOO_LARGE = "MCP_RESPONSE_TOO_LARGE"
#: server 以 ``isError=true``（或 JSON-RPC error）返回的**远端业务失败**。
MCP_TOOL_REMOTE_ERROR = "MCP_TOOL_REMOTE_ERROR"
#: 调用了 tools/list 清单中不存在的工具。
MCP_TOOL_NOT_FOUND = "MCP_TOOL_NOT_FOUND"
#: inputSchema 含本网关不支持的特性，该工具不暴露（fail closed）。
MCP_UNSUPPORTED_SCHEMA = "MCP_UNSUPPORTED_SCHEMA"
#: 工具描述命中注入/提示索取规则，该工具不暴露。
MCP_DESCRIPTION_UNSAFE = "MCP_DESCRIPTION_UNSAFE"
#: 工具/server 被配置为需人工确认，但确认门未接线，不暴露。
MCP_CONFIRMATION_UNAVAILABLE = "MCP_CONFIRMATION_UNAVAILABLE"
#: 桥接后的本地工具名与既有登记冲突。
MCP_NAME_COLLISION = "MCP_NAME_COLLISION"
#: 工具不在 server 配置的 allow 白名单内（None=全拒）。
MCP_NOT_IN_ALLOWLIST = "MCP_NOT_IN_ALLOWLIST"

#: 全部 MCP 稳定错误码（测试锁死不缩小）。
MCP_ERROR_CODES: frozenset[str] = frozenset(
    {
        MCP_CONFIG_INVALID,
        MCP_CONNECT_FAILED,
        MCP_NOT_INITIALIZED,
        MCP_PROTOCOL_ERROR,
        MCP_TIMEOUT,
        MCP_RESPONSE_TOO_LARGE,
        MCP_TOOL_REMOTE_ERROR,
        MCP_TOOL_NOT_FOUND,
        MCP_UNSUPPORTED_SCHEMA,
        MCP_DESCRIPTION_UNSAFE,
        MCP_CONFIRMATION_UNAVAILABLE,
        MCP_NAME_COLLISION,
        MCP_NOT_IN_ALLOWLIST,
    }
)


#: 每个错误码的中文语义前缀。``MCPError`` 把它拼进 ``message``，
#: 让"只有错误码 + 英文 detail"的构造也能产出可行动的提示（测试按这些短语断言）。
_CODE_HINTS: dict[str, str] = {
    MCP_CONFIG_INVALID: "MCP 配置无效：",
    MCP_CONNECT_FAILED: "MCP server 连接失败：",
    MCP_NOT_INITIALIZED: "MCP server 未完成 initialize 握手：",
    MCP_PROTOCOL_ERROR: "MCP protocol 错误：",
    MCP_TIMEOUT: "MCP 调用超时：",
    MCP_RESPONSE_TOO_LARGE: "MCP 响应超过大小上限：",
    MCP_TOOL_REMOTE_ERROR: "MCP 远端工具执行失败：",
    MCP_TOOL_NOT_FOUND: "MCP 工具不在清单中：",
    MCP_UNSUPPORTED_SCHEMA: "MCP 工具 schema 不在支持子集：",
    MCP_DESCRIPTION_UNSAFE: "MCP 工具描述命中注入/提示索取规则：",
    MCP_CONFIRMATION_UNAVAILABLE: "MCP 工具需人工确认但确认门未接线：",
    MCP_NAME_COLLISION: "MCP 工具本地名冲突：",
    MCP_NOT_IN_ALLOWLIST: "MCP 工具不在 allow 白名单：",
}


class MCPError(RuntimeError):
    """MCP 网关结构化失败。

    ``code`` 必须来自 :data:`MCP_ERROR_CODES`。``message`` 是给调用方的可行动
    说明（detail）；实际对外文本 = 错误码语义前缀 + detail，保证只有 code 的
    构造也能读。远端消息（server 返回的文本）在进入 detail 前由调用方截断 ——
    不把不可信内容原样扩散。
    与 ``ToolRegistry`` 的结构化错误同风格：失败要响亮、可行动、不裸抛。
    """

    def __init__(self, code: str, message: str) -> None:
        if code not in MCP_ERROR_CODES:
            raise ValueError(f"未知 MCP 错误码 {code!r}；合法值：{sorted(MCP_ERROR_CODES)}")
        super().__init__(f"{_CODE_HINTS[code]}{message}")
        self.code = code
        self.message = f"{_CODE_HINTS[code]}{message}"

    def to_dict(self) -> dict[str, object]:
        """结构化输出（与 ToolRegistry 错误形状一致）。"""
        return {"error": True, "code": self.code, "message": self.message}


__all__ = [
    "MCP_CONFIG_INVALID",
    "MCP_CONFIRMATION_UNAVAILABLE",
    "MCP_CONNECT_FAILED",
    "MCP_DESCRIPTION_UNSAFE",
    "MCP_ERROR_CODES",
    "MCP_NAME_COLLISION",
    "MCP_NOT_INITIALIZED",
    "MCP_NOT_IN_ALLOWLIST",
    "MCP_PROTOCOL_ERROR",
    "MCP_RESPONSE_TOO_LARGE",
    "MCP_TIMEOUT",
    "MCP_TOOL_NOT_FOUND",
    "MCP_TOOL_REMOTE_ERROR",
    "MCP_UNSUPPORTED_SCHEMA",
    "_CODE_HINTS",
    "MCPError",
]
