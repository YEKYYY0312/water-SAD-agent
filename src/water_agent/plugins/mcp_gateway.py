"""MCP 网关插件（P7-S2，规格 §3.4）。

两种来源模式，生命周期归来源模式管，``assemble_tools`` 本身不碰资源：

- :meth:`McpGatewayPlugin.owning`：按配置现开（``open_gateway_sessions``），打开后**立即**把
  ``assembly.close`` 登记到调用方的 ``ExitStack``——之后任何一步抛错，子进程都会被收掉。
  只能 ``contribute`` 一次（同一批子进程不能交给两次装配）。
- :meth:`McpGatewayPlugin.borrowing`：使用调用方已打开的装配，从不打开也不关闭。

配置仍由调用方用 ``load_mcp_gateway`` 加载：坏配置（``MCP_CONFIG_INVALID``）的抛出点不变，
发生在任何子进程被拉起之前。
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass

from water_agent.mcp.config import MCPGatewayConfig
from water_agent.mcp.wiring import MCPGatewayAssembly, open_gateway_sessions
from water_agent.plugins.base import AssemblyContext, PluginContribution
from water_agent.runtime.governance.contracts import Capability

_EMPTY_ASSEMBLY = MCPGatewayAssembly(sessions=(), skipped_servers=(), manifest_hash="")


@dataclass(frozen=True, slots=True, kw_only=True)
class McpContribution(PluginContribution):
    """MCP 插件的贡献，带回装配产物（暴露 / 跳过记录、manifest hash，供调用方观测）。"""

    assembly: MCPGatewayAssembly


class McpGatewayPlugin:
    plugin_id = "mcp_gateway"

    def __init__(
        self,
        *,
        config: MCPGatewayConfig | None = None,
        stack: ExitStack | None = None,
        assembly: MCPGatewayAssembly | None = None,
        owns: bool,
    ) -> None:
        self._config = config
        self._stack = stack
        self._assembly = assembly
        self._owns = owns
        self._used = False

    @classmethod
    def owning(cls, config: MCPGatewayConfig | None, *, stack: ExitStack) -> McpGatewayPlugin:
        """按配置现开；``config=None``（功能关闭）时得到空装配，照样登记关闭。"""

        return cls(config=config, stack=stack, owns=True)

    @classmethod
    def borrowing(cls, assembly: MCPGatewayAssembly | None) -> McpGatewayPlugin:
        return cls(assembly=assembly, owns=False)

    def contribute(self, ctx: AssemblyContext) -> McpContribution:
        if self._owns:
            if self._used:
                raise RuntimeError("McpGatewayPlugin.owning 只能装配一次：同一批 MCP 子进程不能交给两次装配")
            self._used = True
            assert self._stack is not None
            assembly = open_gateway_sessions(self._config)
            self._stack.callback(assembly.close)
        else:
            assembly = self._assembly if self._assembly is not None else _EMPTY_ASSEMBLY
        tools = tuple(tool for session in assembly.sessions for tool in session.result.tools)
        return McpContribution(
            plugin_id=self.plugin_id,
            tools=tools,
            grants=frozenset({Capability.MCP_INVOKE}) if tools else frozenset(),
            assembly=assembly,
        )


__all__ = ["McpContribution", "McpGatewayPlugin"]
