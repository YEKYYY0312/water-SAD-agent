"""内置插件（P7-S2，规格 §3.3）。

``ToolSpec`` 一律由 ``tools/`` 里现成的构建函数产生（规格 P7-I05）：治理清单的静态扫描
只扫描 ``tools/``，在这里另外构造 ``ToolSpec`` 会让新工具绕过"必须登记能力"的检查。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from water_agent.drawing.drawing_root import DrawingRootResult
from water_agent.plugins.base import AssemblyContext, PluginContribution, ToolPlugin
from water_agent.plugins.exposure import CORE, REQUEST, ExposureRule
from water_agent.plugins.mcp_gateway import McpGatewayPlugin
from water_agent.runtime.governance.contracts import Capability
from water_agent.skills.library import BUILTIN_SKILLS_DIR, SkillLibrary, build_skill_library
from water_agent.skills.models import Skill
from water_agent.tools.calc_velocity import calc_velocity_spec
from water_agent.tools.cite import build_cite_clauses_tool_with_context_check
from water_agent.tools.load_skill import build_load_skill_tool, fit_catalog
from water_agent.tools.material_stats import material_stats_spec
from water_agent.tools.read_drawing import maybe_build_read_drawing_tool
from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool
from water_agent.web_search.config import WebSearchConfig, resolve_web_search_config
from water_agent.web_search.runtime import WebSearchRuntime
from water_agent.web_search.tool import build_web_search_tool

#: 内置插件的注册顺序（规格 P7-I04）。顺序就是工具的注册顺序，决定提示词工具指引的排列
#: 与截断位置（800 字预算；MCP 工具排在最后，总是最先被截掉）。
BUILTIN_PLUGIN_ORDER: tuple[str, ...] = (
    "clauses",
    "hydraulics",
    "materials",
    "drawing",
    "mcp_gateway",
    "web_search",
    "skills",
)


#: 材料统计何时开放（P7-S4）：要数量、长度合计、清单、表格导出的问法。"管径""管段"这类词
#: 在规范问答里也常见，不单独作为触发词；漏判时模型仍能从指引的"本轮未开放"行看到工具名。
MATERIALS_EXPOSURE = ExposureRule(
    mode="intent",
    keywords=(
        "材料", "统计", "工程量", "清单", "用量", "汇总", "合计", "总长", "多少米", "要买",
        "管材", "管段表", "excel", "xlsx", "material_stats",
    ),
)

#: 读图何时开放（P7-S4）：提到图纸 / 图种 / 文件格式，或出现形如 ``P-01`` 的图纸编号
#: （字母 1–4 位 + 连字符 + 数字 1–3 位，与 ``dxf_reader`` 的登记号习惯一致；只在图纸目录
#: 可用、工具确实注册时才参与判定）。
DRAWING_EXPOSURE = ExposureRule(
    mode="intent",
    keywords=(
        "图纸", "图号", "这张图", "图里", "图上", "平面图", "系统图", "轴测图", "大样图",
        "读图", "看图", "dxf", "dwg", "read_drawing",
    ),
    patterns=(r"(?<![a-z0-9])[a-z]{1,4}-\d{1,3}(?![0-9])",),
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
        return PluginContribution(
            plugin_id=self.plugin_id, tools=(retrieve_spec, cite_spec), exposure=CORE
        )


class HydraulicsPlugin:
    """流速计算。core：没暴露计算工具，模型只能心算（铁律①），漏判的代价远大于 953 字符。"""

    plugin_id = "hydraulics"

    def contribute(self, ctx: AssemblyContext) -> PluginContribution:
        return PluginContribution(
            plugin_id=self.plugin_id, tools=(calc_velocity_spec,), exposure=CORE
        )


class MaterialsPlugin:
    plugin_id = "materials"

    def contribute(self, ctx: AssemblyContext) -> PluginContribution:
        return PluginContribution(
            plugin_id=self.plugin_id, tools=(material_stats_spec,), exposure=MATERIALS_EXPOSURE
        )


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
        return DrawingContribution(
            plugin_id=self.plugin_id,
            tools=(tool,),
            resolution=resolution,
            exposure=DRAWING_EXPOSURE,
        )


#: Skill 目录何时开放（P7-S9）：问题点名规程 / 流程，或命中任一已启用 skill 的 ``triggers``（装配时并入）。
#: "流程"单列：问法常是"按计算书流程来"——"按流程"不是它的子串，只靠被停用的那份 skill 的 triggers 才命中。
SKILL_FIXED_KEYWORDS: tuple[str, ...] = (
    "按规程", "按流程", "按步骤", "规程", "流程", "操作规程", "load_skill", "skill",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillsContribution(PluginContribution):
    """Skill 插件的贡献，带回本次装配的 skill 库（提示装配器从同一个实例取正文）。"""

    library: SkillLibrary = field(default_factory=SkillLibrary)


class SkillsPlugin:
    """按名加载操作规程（P7-S9）。排在最后：指引里列在最末、预算不够时与 MCP 一样整组让位。

    开关 ``WATER_AGENT_SKILLS`` 为 off 或没有任何已启用 skill 时 skipped、不注册工具。``requires_tools``
    按**先于它的插件已贡献的工具**核对，所以必须排在全部工具插件之后。
    """

    plugin_id = "skills"

    def __init__(
        self,
        *,
        earlier: Sequence[ToolPlugin] = (),
        disabled: Sequence[str] = (),
        library: SkillLibrary | None = None,
        builtin_dir: Path = BUILTIN_SKILLS_DIR,
    ) -> None:
        self._earlier = tuple(earlier)
        self._disabled = tuple(disabled)
        self._library = library
        self._builtin_dir = builtin_dir
        self._loaded: list[Skill] = []

    def contribute(self, ctx: AssemblyContext) -> SkillsContribution:
        library = self._library
        if library is None:
            available = sorted(
                {
                    spec.name
                    for plugin in self._earlier
                    for spec in getattr(plugin, "contributed_tools", ())
                }
            )
            library = build_skill_library(
                available_tools=available, disabled=self._disabled, builtin_dir=self._builtin_dir
            )
        library = fit_catalog(library)
        tool = build_load_skill_tool(library)
        if tool is None:
            return SkillsContribution(
                plugin_id=self.plugin_id,
                status="skipped",
                detail="disabled" if not library.active else "no_enabled_skills",
                library=library,
            )
        keywords = tuple(
            dict.fromkeys(
                [*SKILL_FIXED_KEYWORDS, *(word for skill in library.enabled for word in skill.manifest.triggers)]
            )
        )
        return SkillsContribution(
            plugin_id=self.plugin_id,
            tools=(tool,),
            library=library,
            exposure=ExposureRule(mode="intent", keywords=keywords),
        )


#: ``web_search`` 组 id（= 插件 id）。入口按它向 ``ToolExposurePolicy.decide(request_enabled=…)`` 声明"本条消息勾选了联网"。
WEB_SEARCH_GROUP = "web_search"


@dataclass(frozen=True, slots=True, kw_only=True)
class WebSearchContribution(PluginContribution):
    """联网搜索插件的贡献，带回解析后的部署配置（``status`` / ``provider`` / 主机名；**不含密钥与基址**的 repr）。"""

    config: WebSearchConfig = field(default_factory=lambda: WebSearchConfig(status="off"))


class WebSearchPlugin:
    """联网搜索（P7-S12）。两道门缺一不可：

    1. **部署**（运维）：开关 + provider + 基址 [+ 密钥] 缺一即 ``skipped``、不注册工具；
    2. **本条消息**（用户勾选，``requested``）：没勾选同样 ``skipped``（``detail="not_requested"``）——不注册工具、不授予能力，
       装配面与部署关闭时逐字相同。

    两道都过才贡献工具、``REQUEST`` 暴露规则与 ``Capability.WEB_SEARCH``（``GRANTABLE_CAPABILITIES`` 允许，与 MCP 插件追加
    ``MCP_INVOKE`` 同一机制）。本轮是否真的开放，还要看暴露预算：最紧组合下整组让位，由外层图向用户发
    ``WEB_SEARCH_NOT_EXPOSED``。

    非法配置（开关写错、provider 拼错、基址不是合法 URL）抛 ``WebSearchConfigError``：装配期响亮失败，不静默当成关闭。
    """

    plugin_id = "web_search"

    def __init__(
        self,
        runtime: WebSearchRuntime | None = None,
        *,
        config: WebSearchConfig | None = None,
        requested: bool = False,
    ) -> None:
        self._runtime = runtime if runtime is not None else WebSearchRuntime()
        self._config = config
        self._requested = requested

    @property
    def runtime(self) -> WebSearchRuntime:
        return self._runtime

    def contribute(self, ctx: AssemblyContext) -> WebSearchContribution:
        # 配置总是解析（没勾选也解析）：写错的部署配置必须装配期响亮失败，而不是等到第一次有人勾选联网才发现
        config = self._config if self._config is not None else resolve_web_search_config()
        if not config.ready:
            return WebSearchContribution(
                plugin_id=self.plugin_id, status="skipped", detail=config.detail or config.status, config=config
            )
        if not self._requested:
            # 部署已就绪，但这条消息没勾选联网：不注册工具、不授予能力——与部署关闭时的装配面逐字相同
            return WebSearchContribution(
                plugin_id=self.plugin_id, status="skipped", detail="not_requested", config=config
            )
        return WebSearchContribution(
            plugin_id=self.plugin_id,
            tools=(build_web_search_tool(config, self._runtime),),
            grants=frozenset({Capability.WEB_SEARCH}),
            config=config,
            exposure=REQUEST,
        )


class _Remembering:
    """包一层插件：记下它贡献的工具，供 :class:`SkillsPlugin` 核对 ``requires_tools``。"""

    def __init__(self, inner: ToolPlugin) -> None:
        self._inner = inner
        self.contributed_tools: tuple[object, ...] = ()

    @property
    def plugin_id(self) -> str:
        return self._inner.plugin_id

    def contribute(self, ctx: AssemblyContext) -> PluginContribution:
        contribution = self._inner.contribute(ctx)
        self.contributed_tools = contribution.tools
        return contribution


def builtin_plugins(
    *,
    mcp: McpGatewayPlugin,
    skills_disabled: Sequence[str] = (),
    skills_builtin_dir: Path = BUILTIN_SKILLS_DIR,
    web_search_runtime: WebSearchRuntime | None = None,
    web_search_config: WebSearchConfig | None = None,
    web_search_requested: bool = False,
) -> tuple[ToolPlugin, ...]:
    """按 :data:`BUILTIN_PLUGIN_ORDER` 排好的全部内置插件。MCP 插件由调用方给出来源模式。

    ``skills_disabled`` 是 Web 管理层里停用的 skill 名（P7-S9）；开关与目录由环境变量决定。
    ``web_search_*``（P7-S12）：联网搜索工具的会话级依赖（本轮私有上下文、请求上下文、网关、trace）、调用方已解析的部署配置
    （不给就在装配时读环境）与"这条消息是否勾选了联网"（缺省否：CLI / runner 没有勾选入口）。
    """

    tool_plugins = tuple(
        _Remembering(plugin)
        for plugin in (ClausesPlugin(), HydraulicsPlugin(), MaterialsPlugin(), DrawingPlugin(), mcp)
    )
    plugins: tuple[ToolPlugin, ...] = (
        *tool_plugins,
        # 不包 _Remembering：skill 的 requires_tools 不能依赖一个"逐条消息才可能出现"的工具
        WebSearchPlugin(web_search_runtime, config=web_search_config, requested=web_search_requested),
        SkillsPlugin(earlier=tool_plugins, disabled=skills_disabled, builtin_dir=skills_builtin_dir),
    )
    assert tuple(plugin.plugin_id for plugin in plugins) == BUILTIN_PLUGIN_ORDER
    return plugins


__all__ = [
    "BUILTIN_PLUGIN_ORDER",
    "DRAWING_EXPOSURE",
    "MATERIALS_EXPOSURE",
    "SKILL_FIXED_KEYWORDS",
    "WEB_SEARCH_GROUP",
    "ClausesPlugin",
    "DrawingContribution",
    "DrawingPlugin",
    "HydraulicsPlugin",
    "MaterialsPlugin",
    "SkillsContribution",
    "SkillsPlugin",
    "WebSearchContribution",
    "WebSearchPlugin",
    "builtin_plugins",
]
