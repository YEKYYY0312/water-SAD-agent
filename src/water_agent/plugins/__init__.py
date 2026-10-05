"""工具插件（P7-S2，``docs/design/16-p7-plugin-mcp.md`` §3）。

模型可调用的工具全部由插件贡献；面向模型的装配只有 :func:`assemble_tools` 一个入口。
内置工具是内置插件（:mod:`water_agent.plugins.builtin`），MCP 网关是一个插件
（:mod:`water_agent.plugins.mcp_gateway`）。

插件**不是权限主体**：插件只贡献 ``ToolSpec``，执行仍经 ``ToolRegistry.invoke`` 的护栏；
插件能追加给主体的能力只限 :data:`GRANTABLE_CAPABILITIES`。``ToolSpec`` 的定义留在
``tools/``（治理清单的静态扫描只认那里），插件只调用现成的构建函数。
"""
from __future__ import annotations

from water_agent.plugins.assembly import ToolAssembly, assemble_tools
from water_agent.plugins.base import (
    GRANTABLE_CAPABILITIES,
    AssemblyContext,
    PluginContribution,
    ToolPlugin,
)
from water_agent.plugins.builtin import (
    BUILTIN_PLUGIN_ORDER,
    ClausesPlugin,
    DrawingContribution,
    DrawingPlugin,
    HydraulicsPlugin,
    MaterialsPlugin,
    builtin_plugins,
)
from water_agent.plugins.mcp_gateway import McpContribution, McpGatewayPlugin

__all__ = [
    "BUILTIN_PLUGIN_ORDER",
    "GRANTABLE_CAPABILITIES",
    "AssemblyContext",
    "ClausesPlugin",
    "DrawingContribution",
    "DrawingPlugin",
    "HydraulicsPlugin",
    "MaterialsPlugin",
    "McpContribution",
    "McpGatewayPlugin",
    "PluginContribution",
    "ToolAssembly",
    "ToolPlugin",
    "assemble_tools",
    "builtin_plugins",
]
