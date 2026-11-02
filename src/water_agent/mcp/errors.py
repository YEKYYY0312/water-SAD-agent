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

#: server 在配置里被停用（``disabled=true``，P7-S3）：不 spawn、不暴露任何工具。
MCP_SERVER_DISABLED = "MCP_SERVER_DISABLED"

#: server 声明的凭据槽位（``credential_env``，P7-S6）还没填值：连接前跳过、不 spawn。
MCP_CREDENTIAL_MISSING = "MCP_CREDENTIAL_MISSING"

#: 远程 MCP 的目标地址（URL 主机名解析出的任一地址）落在禁止段——回环 / 私有 / 链路本地（含云元数据）/
#: CGNAT / 未指定 / 组播 / 保留段（P7-S8）。拒绝发生在发出第一个字节之前。
MCP_DESTINATION_FORBIDDEN = "MCP_DESTINATION_FORBIDDEN"

#: server 只讲 2026-07-28 的无状态协议，本网关只实现 2025-11-25 的会话制（P7-S8，只识别不实现）。
MCP_PROTOCOL_UNSUPPORTED = "MCP_PROTOCOL_UNSUPPORTED"

#: server 要求 OAuth 授权而本部署还没有（或已失效的）令牌，需管理员在 Web 扩展面板（重新）授权（P7-S8）。
MCP_OAUTH_REQUIRED = "MCP_OAUTH_REQUIRED"

#: 授权流程无法进行：授权服务器元数据缺 PKCE（S256）、issuer 不在白名单、客户端注册方式缺失……（P7-S8）。
MCP_OAUTH_UNAVAILABLE = "MCP_OAUTH_UNAVAILABLE"

#: 访问令牌的 scope 不够（403 ``insufficient_scope``）：如实报出所需 scope，不自动升级授权（P7-S8）。
MCP_OAUTH_INSUFFICIENT_SCOPE = "MCP_OAUTH_INSUFFICIENT_SCOPE"

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
        MCP_SERVER_DISABLED,
        MCP_CREDENTIAL_MISSING,
        MCP_DESTINATION_FORBIDDEN,
        MCP_PROTOCOL_UNSUPPORTED,
        MCP_OAUTH_REQUIRED,
        MCP_OAUTH_UNAVAILABLE,
        MCP_OAUTH_INSUFFICIENT_SCOPE,
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
    MCP_SERVER_DISABLED: "MCP server 已在配置中停用：",
    MCP_CREDENTIAL_MISSING: "MCP server 的凭据槽位未填写：",
    MCP_DESTINATION_FORBIDDEN: "MCP 远程目标地址被禁止：",
    MCP_PROTOCOL_UNSUPPORTED: "MCP server 的协议版本本网关不支持：",
    MCP_OAUTH_REQUIRED: "MCP server 需要 OAuth 授权：",
    MCP_OAUTH_UNAVAILABLE: "MCP OAuth 授权流程无法进行：",
    MCP_OAUTH_INSUFFICIENT_SCOPE: "MCP 访问令牌 scope 不足：",
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
    "MCP_CREDENTIAL_MISSING",
    "MCP_DESCRIPTION_UNSAFE",
    "MCP_DESTINATION_FORBIDDEN",
    "MCP_ERROR_CODES",
    "MCP_NAME_COLLISION",
    "MCP_NOT_INITIALIZED",
    "MCP_NOT_IN_ALLOWLIST",
    "MCP_OAUTH_INSUFFICIENT_SCOPE",
    "MCP_OAUTH_REQUIRED",
    "MCP_OAUTH_UNAVAILABLE",
    "MCP_PROTOCOL_ERROR",
    "MCP_PROTOCOL_UNSUPPORTED",
    "MCP_RESPONSE_TOO_LARGE",
    "MCP_SERVER_DISABLED",
    "MCP_TIMEOUT",
    "MCP_TOOL_NOT_FOUND",
    "MCP_TOOL_REMOTE_ERROR",
    "MCP_UNSUPPORTED_SCHEMA",
    "_CODE_HINTS",
    "MCPError",
]
