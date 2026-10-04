"""内置插件（P7-S2，规格 §3.3）。

``ToolSpec`` 一律由 ``tools/`` 里现成的构建函数产生（规格 P7-I05）：治理清单的静态扫描
只扫描 ``tools/``，在这里另外构造 ``ToolSpec`` 会让新工具绕过"必须登记能力"的检查。
"""
from __future__ import annotations

from dataclasses import dataclass

from water_agent.drawing.drawing_root import DrawingRootResult
from water_agent.plugins.base import AssemblyContext, PluginContribution, ToolPlugin
from water_agent.plugins.mcp_gateway import McpGatewayPlugin
from water_agent.tools.calc_velocity import calc_velocity_spec
from water_agent.tools.cite import build_cite_clauses_tool_with_context_check
from water_agent.tools.material_stats import material_stats_spec
from water_agent.tools.read_drawing import maybe_build_read_drawing_tool
from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool

#: 内置插件的注册顺序（规格 P7-I04）。顺序就是工具的注册顺序，决定提示词工具指引的排列
#: 与截断位置（800 字预算；MCP 工具排在最后，总是最先被截掉）。
BUILTIN_PLUGIN_ORDER: tuple[str, ...] = (
    "clauses",
    "hydraulics",
    "materials",
    "drawing",
    "mcp_gateway",
)


class ClausesPlugin:
    """检索 + 引用声明。两者必须在同一个插件里：cite 的可追溯检查靠 retrieve 回填
    ``context_updater``，拆开就会出现"检索到了却被判不可追溯"。"""

    plugin_id = "clauses"

    def contribute(self, ctx: AssemblyContext) -> PluginContribution:
        cite_spec, context_updater = build_cite_clauses_tool_with_context_check(
            context_source=ctx.cite_context_source
        )
        retrieve_spec = build_retrieve_clauses_tool(ctx.retriever, context_updater=context_updater)
        return PluginContribution(plugin_id=self.plugin_id, tools=(retrieve_spec, cite_spec))


class HydraulicsPlugin:
    plugin_id = "hydraulics"

    def contribute(self, ctx: AssemblyContext) -> PluginContribution:
        return PluginContribution(plugin_id=self.plugin_id, tools=(calc_velocity_spec,))


class MaterialsPlugin:
    plugin_id = "materials"

    def contribute(self, ctx: AssemblyContext) -> PluginContribution:
        return PluginContribution(plugin_id=self.plugin_id, tools=(material_stats_spec,))


@dataclass(frozen=True, slots=True, kw_only=True)
class DrawingContribution(PluginContribution):
    """图纸插件的贡献，带回目录解析结果（"未配置"与"目录里没有 DXF"要如实区分）。"""

    resolution: DrawingRootResult


class DrawingPlugin:
    """图纸读取。能力不可用就不暴露：注册一个必然失败的工具会诱导模型反复重试。"""

    plugin_id = "drawing"

    def contribute(self, ctx: AssemblyContext) -> DrawingContribution:
        tool, resolution = maybe_build_read_drawing_tool(ctx.drawing_root)
        if tool is None:
            return DrawingContribution(
                plugin_id=self.plugin_id,
                status="skipped",
                detail=resolution.status,
                resolution=resolution,
            )
        return DrawingContribution(plugin_id=self.plugin_id, tools=(tool,), resolution=resolution)


def builtin_plugins(*, mcp: McpGatewayPlugin) -> tuple[ToolPlugin, ...]:
    """按 :data:`BUILTIN_PLUGIN_ORDER` 排好的全部内置插件。MCP 插件由调用方给出来源模式。"""

    plugins: tuple[ToolPlugin, ...] = (
        ClausesPlugin(),
        HydraulicsPlugin(),
        MaterialsPlugin(),
        DrawingPlugin(),
        mcp,
    )
    assert tuple(plugin.plugin_id for plugin in plugins) == BUILTIN_PLUGIN_ORDER
    return plugins


__all__ = [
    "BUILTIN_PLUGIN_ORDER",
    "ClausesPlugin",
    "DrawingContribution",
    "DrawingPlugin",
    "HydraulicsPlugin",
    "MaterialsPlugin",
    "builtin_plugins",
]
