"""插件契约（P7-S2，规格 §3.1）。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from water_agent.plugins.exposure import AMBIENT, ExposureRule, ToolGroup
from water_agent.retrieval.base import Retriever
from water_agent.runtime.governance.contracts import Capability
from water_agent.tools.base import ToolSpec

#: 插件能追加给主体的能力（规格 P7-I06）。插件不是权限主体：除了"这台 MCP server 的工具
#: 确实暴露了、所以主体需要 ``MCP_INVOKE`` 才能调用"，以及（P7-S12）"联网搜索工具确实暴露了——部署开关已开、
#: 用户对这一条消息勾选了联网——所以主体需要 ``WEB_SEARCH`` 才能调用"，插件不得替主体申请任何能力。
#: ``NETWORK_FETCH``（目的地由模型决定）仍在危险集里，不在这里，也不被任何主体持有。
GRANTABLE_CAPABILITIES: frozenset[Capability] = frozenset({Capability.MCP_INVOKE, Capability.WEB_SEARCH})


@dataclass(frozen=True, slots=True, kw_only=True)
class AssemblyContext:
    """一次装配的输入，由调用方（会话 / runner）准备。

    ``drawing_root`` **原样透传**：``""`` 表示不注册图纸工具，``None`` 表示读
    ``DRAWING_ROOT`` 环境变量——两者语义不同，插件不得互相转换。
    """

    retriever: Retriever
    cite_context_source: Callable[[], set[str] | None] | None = None
    drawing_root: str | Path | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginContribution:
    """一个插件对本次装配的贡献。

    ``status="skipped"`` 表示能力不可用（例如没有配置图纸目录）；跳过的插件不得贡献工具。

    ``exposure`` 决定这组工具何时发给模型（P7-S4）；不声明就是 ``ambient``：不看意图，预算
    不够时最先让位。
    """

    plugin_id: str
    tools: tuple[ToolSpec, ...] = ()
    status: Literal["active", "skipped"] = "active"
    detail: str = ""
    grants: frozenset[Capability] = frozenset()
    exposure: ExposureRule = AMBIENT

    def __post_init__(self) -> None:
        if self.status == "skipped" and self.tools:
            raise ValueError(f"插件 {self.plugin_id!r} 标为 skipped 却贡献了工具")

    def exposure_groups(self) -> tuple[ToolGroup, ...]:
        """这份贡献在暴露策略里的分组：缺省整份贡献一组，组 id 即 ``plugin_id``。"""

        if not self.tools:
            return ()
        return (ToolGroup(self.plugin_id, tuple(spec.name for spec in self.tools), self.exposure),)


class ToolPlugin(Protocol):
    """插件协议：一个稳定 id + 一次装配的贡献。异常一律原样上抛，不得自行吞掉。"""

    @property
    def plugin_id(self) -> str: ...

    def contribute(self, ctx: AssemblyContext) -> PluginContribution: ...


__all__ = ["GRANTABLE_CAPABILITIES", "AssemblyContext", "PluginContribution", "ToolPlugin"]
