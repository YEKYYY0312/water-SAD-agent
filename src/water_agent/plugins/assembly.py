"""面向模型的工具装配单入口（P7-S2，规格 §3.2）。

两阶段：先收集全部插件的贡献，再用最终主体建**一个** ``ToolRegistry`` 并注册。
不先建注册表再"补授权"：``ToolRegistry`` 持有的主体在构造时就定了，护栏按它判定。
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from langchain_core.tools import StructuredTool

from water_agent.plugins.base import (
    GRANTABLE_CAPABILITIES,
    AssemblyContext,
    PluginContribution,
    ToolPlugin,
)
from water_agent.plugins.exposure import (
    ExposureStrategy,
    ToolExposurePolicy,
    bound_tool_definition,
    resolve_exposure_strategy,
)
from water_agent.prompting.assembler import SEGMENT_BUDGETS
from water_agent.runtime.governance.contracts import Principal
from water_agent.tools.ds_adapters import tool_spec_to_base_tool
from water_agent.tools.registry import ToolRegistry

_C = TypeVar("_C", bound=PluginContribution)


@dataclass(frozen=True, slots=True)
class ToolAssembly:
    """一次装配的产物。``tools`` 按 ``registry.list_all_names()``（字母序），全部委托给
    ``registry``；``PromptAssembler`` 也必须持有这同一个 ``registry``。``exposure`` 是本次装配的
    按意图暴露策略（P7-S4），生产路径必须把它交给 ``build_agentic_app``（规格 P7-I08）。"""

    registry: ToolRegistry
    tools: list[StructuredTool]
    principal: Principal
    contributions: tuple[PluginContribution, ...]
    exposure: ToolExposurePolicy

    def contribution(self, kind: type[_C]) -> _C:
        """取某一类插件的贡献（例如 ``DrawingContribution``、``McpContribution``）。"""

        matches = [item for item in self.contributions if isinstance(item, kind)]
        if len(matches) != 1:
            raise LookupError(f"装配里 {kind.__name__} 有 {len(matches)} 个，应恰好 1 个")
        return matches[0]

    def registered(self, plugin_id: str) -> list[str]:
        """某个插件实际注册进来的工具名（按注册顺序）。"""

        for item in self.contributions:
            if item.plugin_id == plugin_id:
                return [tool.name for tool in item.tools]
        raise LookupError(f"装配里没有插件 {plugin_id!r}")


def _grant(base: Principal, grants: frozenset[Any]) -> Principal:
    """在 ``base`` 上叠加能力；没有新能力时原样返回同一个对象。用构造器而不是
    ``model_copy``：构造器会重跑 ``Principal`` 的校验（危险能力拒绝、subject 形状）。"""

    if grants <= base.capabilities:
        return base
    return Principal(
        kind=base.kind,
        subject=base.subject,
        capabilities=frozenset(base.capabilities) | grants,
        auth_domain=base.auth_domain,
    )


def assemble_tools(
    ctx: AssemblyContext,
    *,
    base_principal: Principal,
    trace: Any | None,
    plugins: Sequence[ToolPlugin],
    exposure_strategy: ExposureStrategy | None = None,
) -> ToolAssembly:
    """按 ``plugins`` 的顺序装配工具。插件异常原样上抛（坏配置、注册冲突都必须响亮失败）。

    ``exposure_strategy`` 缺省读 ``WATER_AGENT_TOOL_EXPOSURE``（非法值在这里抛 ``ValueError``）。
    """

    strategy = (
        exposure_strategy
        if exposure_strategy is not None
        else resolve_exposure_strategy(os.environ)
    )
    seen: set[str] = set()
    contributions: list[PluginContribution] = []
    for plugin in plugins:
        if plugin.plugin_id in seen:
            raise ValueError(f"插件 id 重复：{plugin.plugin_id!r}")
        seen.add(plugin.plugin_id)
        contribution = plugin.contribute(ctx)
        if contribution.plugin_id != plugin.plugin_id:
            raise ValueError(
                f"插件 {plugin.plugin_id!r} 返回了 plugin_id={contribution.plugin_id!r} 的贡献"
            )
        illegal = contribution.grants - GRANTABLE_CAPABILITIES
        if illegal:
            raise ValueError(
                f"插件 {plugin.plugin_id!r} 申请了不可授予的能力："
                f"{sorted(cap.value for cap in illegal)}"
            )
        contributions.append(contribution)

    grants: frozenset[Any] = frozenset().union(
        *(item.grants for item in contributions if item.tools)
    )
    principal = _grant(base_principal, grants)
    registry = ToolRegistry(principal=principal, trace=trace)
    for item in contributions:
        for spec in item.tools:
            registry.register(spec)
    tools = [tool_spec_to_base_tool(registry, name) for name in registry.list_all_names()]
    exposure = ToolExposurePolicy(
        strategy=strategy,
        groups=[group for item in contributions for group in item.exposure_groups()],
        definitions={tool.name: bound_tool_definition(tool) for tool in tools},
        order=[spec.name for item in contributions for spec in item.tools],
        guide_budget=SEGMENT_BUDGETS["tool_guide"],
    )
    return ToolAssembly(
        registry=registry,
        tools=tools,
        principal=principal,
        contributions=tuple(contributions),
        exposure=exposure,
    )


__all__ = ["ToolAssembly", "assemble_tools"]
