"""MCP 客户端网关（P4 Step 23）。

P4 目标（`06-roadmap.md`）：打通专业软件与企业数据 —— 接入 CAD/BIM 与
企业知识库 MCP，网关权限控制 + 危险操作确认。本包交付的是**客户端网关
地基**（stdio JSON-RPC 客户端 + 配置驱动的治理绑定桥）：

- :mod:`water_agent.mcp.errors` —— 13 个稳定错误码；
- :mod:`water_agent.mcp.protocol` —— JSON-RPC 2.0 编解码与消息判定；
- :mod:`water_agent.mcp.config` ——"接什么、暴露什么、谁能用"的声明式配置
  （``data/mcp_servers.json``，人工维护）与环境隔离的子进程 env 构造；
- :mod:`water_agent.mcp.schema` —— MCP inputSchema 确定性子集 → pydantic
  参数模型（不可信描述先清洗、后检测，命中即拒绝工具）；
- :mod:`water_agent.mcp.client` —— stdio 客户端（initialize / tools/list /
  tools/call + 响应大小上限 + deadline；远端业务失败转结构化 dict）；
- :mod:`water_agent.mcp.bridge` —— manifest → ``ToolSpec`` 桥（转换即授权：
  白名单 + schema 子集 + 描述安检 + 确认门 + 名字唯一性五道条件）。

**MCP 不产生护栏旁路**：桥接工具照常经 ``ToolRegistry.invoke`` 走
``GuardrailPipeline.validate_tool_call``（能力 ``MCP_INVOKE`` 默认无主体
持有，只有装配期依据人工配置显式授予），远端返回内容一律按数据隔离。

**诚实边界**：本会话无网络，官方 ``mcp`` SDK 不可装，与真实第三方 server
的互通性未验证（blocked）；当前仅能证明与**协议正确的本地假 server**
（测试内）的往返与全部治理语义。不得声称"已接入真实 CAD/BIM 或企业库"。
"""
from water_agent.mcp.bridge import (
    MCPBridgeResult,
    MCPServerSession,
    SkippedTool,
    bridge_server_tools,
    bridge_tool_name,
    open_bridge,
)
from water_agent.mcp.client import MCPStdioClient, RemoteTool
from water_agent.mcp.config import (
    MCPGatewayConfig,
    MCPServerConfig,
    ServerTransport,
    child_env,
    load_gateway_config,
)
from water_agent.mcp.errors import MCP_ERROR_CODES, MCPError

__all__ = [
    "MCP_ERROR_CODES",
    "MCPBridgeResult",
    "MCPError",
    "MCPGatewayConfig",
    "MCPServerConfig",
    "MCPServerSession",
    "MCPStdioClient",
    "RemoteTool",
    "ServerTransport",
    "SkippedTool",
    "bridge_server_tools",
    "bridge_tool_name",
    "child_env",
    "load_gateway_config",
    "open_bridge",
]
