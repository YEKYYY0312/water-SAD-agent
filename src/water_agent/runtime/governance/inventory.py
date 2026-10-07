"""P2.5 治理接入清单：远程调用点与工具能力的唯一真源（Step 11）。

**为什么需要清单**：缓存、限流、预算与护栏一旦散落在 CLI、图节点和 adapter 中，
新增的调用路径就能绕过治理层（`skill-guardrail-patterns` 的
"Guardrails as a library import, not a middleware" 反模式）。本模块把"系统里有哪些
远程出口、哪些工具、各自需要什么能力"变成**数据**，再由
`tests/test_governance_contracts.py` 用静态扫描断言"代码里的出口 == 清单里的出口"。

**接入状态语义**：
- :attr:`ProviderCallSite.governed` = 该出口是否已经过 ``ProviderGateway``。
  Step 11 全部为 ``False``（Gateway 属 Step 12）；Step 12 逐个翻转为 ``True``。
- :attr:`ToolCapabilityEntry.guardrail_validated` = 该工具是否已过 ``GuardrailPipeline``
  前置校验。Step 14 后全部为 ``True``：``ToolRegistry.invoke`` 在
  ``args_model`` 校验**之前**调用 ``validate_tool_call``，未过护栏则工具体永不执行。
  注意这条标记的含义是"该工具的调用路径已被护栏覆盖"，**不是**"该工具无风险"。

**纪律**：新增远程 provider 调用或新增 ToolSpec，必须先更新本清单，否则覆盖测试失败。
详见 `docs/design/p2.5-provider-inventory.md`。
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from water_agent.runtime.governance.contracts import (
    CallCategory,
    Capability,
    ProviderKind,
)

#: 包根目录（`src/water_agent/`），静态扫描的范围。
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

#: 数据分类：决定 PII 处理与缓存资格。
DATA_CLASS_PUBLIC_CLAUSE = "public_clause"
DATA_CLASS_USER_QUERY = "user_query"
DATA_CLASS_PROJECT_DOC = "project_doc"


class ProviderCallSite(BaseModel):
    """一个远程 provider 调用点的登记项。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 稳定标识（trace/文档引用用它，改名等同破坏兼容）。
    site_id: str = Field(min_length=1)
    #: 相对 `src/` 的模块路径，例如 ``water_agent/generation/llm.py``。
    #: 这是**逻辑调用点**所在模块（谁发起这类调用）。
    module: str = Field(min_length=1)
    #: 实际打开网络连接的模块（相对 `src/`）。
    #: P2.5 Step 12 后三个 chat 出口收敛到 ``generation/chat_model.py``，
    #: 因此 ``module`` 与 ``egress_module`` 可以不同 —— 静态扫描断言的是
    #: **egress_module 集合 == 代码里真实开连接的模块集合**。
    egress_module: str = Field(min_length=1)
    #: 发起调用的限定名，例如 ``LLMGenerator._completion``。
    symbol: str = Field(min_length=1)
    provider: ProviderKind
    category: CallCategory
    required_capability: Capability
    #: 送往该 provider 的数据分类。
    data_class: str = Field(min_length=1)
    #: 主体来源说明（P2.5 一律是 CLI/harness 传入的本地主体）。
    principal_source: str = Field(min_length=1)
    #: 现有超时（秒）；``None`` = 代码里没有显式超时（Step 12 必须补）。
    current_timeout_seconds: float | None = None
    #: 现有重试实现描述（Step 12 前多为"无"或"仅瞬时错误单层重试"）。
    current_retry: str = "无"
    #: 是否具备缓存资格（Step 13 判定的先验；计算/docgen/审批一律 False）。
    cache_eligible: bool = False
    #: 未来应接入的 gateway 方法名（Step 12 实现时对齐）。
    target_gateway_method: str = Field(min_length=1)
    #: 是否已接入 ProviderGateway。Step 11 全 False。
    governed: bool = False


class ToolCapabilityEntry(BaseModel):
    """一个 ToolSpec 的能力与护栏登记项。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1)
    module: str = Field(min_length=1)
    required_capability: Capability
    #: 只读 / 纯计算 / 写入。P2.5 只允许前两类与"用户显式目录导出"。
    effect: str = Field(min_length=1)
    #: 是否已过 GuardrailPipeline 前置校验。
    #: **默认仍为 False**：新增工具默认"未覆盖"，必须显式声明并配套测试才能翻 True，
    #: 否则新工具会因为字段默认值而白得一个"已治理"的名声。
    guardrail_validated: bool = False


#: 远程 provider 调用点清单。**新增远程调用必须先加到这里**。
#:
#: P2.5 Step 12 后 ``governed=True``：每个出口都经 ``ProviderGateway``。
#: ``current_timeout_seconds`` / ``current_retry`` 记录的是**接入后**的真实口径
#: （统一取自 ``GovernanceConfig`` 的 provider 画像，不再各处硬编码）。
PROVIDER_CALL_SITES: tuple[ProviderCallSite, ...] = (
    ProviderCallSite(
        site_id="deepseek_chat_legacy_rag",
        module="water_agent/generation/llm.py",
        egress_module="water_agent/generation/llm.py",
        symbol="LLMGenerator.generate → _completion",
        provider=ProviderKind.DEEPSEEK_CHAT,
        category=CallCategory.RAG_GENERATION,
        required_capability=Capability.LLM_INFERENCE,
        data_class=DATA_CLASS_USER_QUERY,
        principal_source="CLI 本地主体（local:<thread_id>）",
        current_timeout_seconds=60.0,
        current_retry="ProviderGateway：仅瞬时错误，max_attempts=3，429 遵守 Retry-After",
        cache_eligible=True,
        target_gateway_method="ProviderGateway.call",
        governed=True,
    ),
    ProviderCallSite(
        site_id="deepseek_chat_agentic_loop",
        module="water_agent/generation/chat_model.py",
        egress_module="water_agent/generation/chat_model.py",
        symbol="build_chat_model → GovernedChatModel._generate / _stream",
        provider=ProviderKind.DEEPSEEK_CHAT,
        category=CallCategory.AGENTIC_LOOP,
        required_capability=Capability.LLM_INFERENCE,
        data_class=DATA_CLASS_USER_QUERY,
        principal_source="AgenticSession thread_id → 本地主体",
        current_timeout_seconds=60.0,
        current_retry=(
            "ProviderGateway：max_attempts=3（流式只在首个 chunk 之前重试）；"
            "ChatOpenAI 自带重试已关（max_retries=0，避免叠乘）"
        ),
        cache_eligible=False,
        target_gateway_method="ProviderGateway.call / ProviderGateway.stream_call",
        governed=True,
    ),
    ProviderCallSite(
        site_id="deepseek_chat_docgen_polish",
        module="water_agent/docgen/quality.py",
        # Step 12 收敛：不再内联构造 ChatOpenAI，改走 build_chat_model
        egress_module="water_agent/generation/chat_model.py",
        symbol="RealPolishTransport.__call__ → build_chat_model",
        provider=ProviderKind.DEEPSEEK_CHAT,
        category=CallCategory.DOCGEN_POLISH,
        required_capability=Capability.LLM_INFERENCE,
        data_class=DATA_CLASS_PROJECT_DOC,
        principal_source="docgen flow 的 CLI 本地主体",
        current_timeout_seconds=60.0,
        current_retry="ProviderGateway：max_attempts=3；失败仍结构化为 blocked（不回退原文）",
        cache_eligible=False,
        target_gateway_method="ProviderGateway.call",
        governed=True,
    ),
    ProviderCallSite(
        site_id="deepseek_chat_judge",
        module="water_agent/harness/llm_judge.py",
        # Step 12 收敛：不再内联构造 ChatOpenAI，改走 build_chat_model
        egress_module="water_agent/generation/chat_model.py",
        symbol="LLMJudge._call_llm → build_chat_model",
        provider=ProviderKind.DEEPSEEK_CHAT,
        category=CallCategory.JUDGE,
        required_capability=Capability.LLM_INFERENCE,
        data_class=DATA_CLASS_USER_QUERY,
        principal_source="harness runner 本地主体（评测进程）",
        current_timeout_seconds=60.0,
        current_retry="ProviderGateway：max_attempts=3",
        cache_eligible=True,
        target_gateway_method="ProviderGateway.call",
        governed=True,
    ),
    ProviderCallSite(
        site_id="bailian_embedding_query",
        module="water_agent/embedding.py",
        egress_module="water_agent/embedding.py",
        symbol="BailianEmbeddingProvider.embed → _request_embedding",
        provider=ProviderKind.BAILIAN_EMBEDDING,
        category=CallCategory.EMBEDDING_QUERY,
        required_capability=Capability.EMBEDDING,
        data_class=DATA_CLASS_USER_QUERY,
        # Step 11 记为 30s 有误：代码实际是 60s（BailianEmbeddingConfig 默认）
        principal_source="检索装配阶段的本地主体",
        current_timeout_seconds=60.0,
        current_retry="ProviderGateway：max_attempts=7（沿用 config.max_retries=6 口径）",
        cache_eligible=False,
        target_gateway_method="ProviderGateway.call",
        governed=True,
    ),
    ProviderCallSite(
        site_id="bailian_rerank",
        module="water_agent/retrieval/rerank.py",
        egress_module="water_agent/retrieval/rerank.py",
        symbol="BailianReranker.rerank → _call",
        provider=ProviderKind.BAILIAN_RERANK,
        category=CallCategory.RERANK,
        required_capability=Capability.RERANK,
        data_class=DATA_CLASS_PUBLIC_CLAUSE,
        # Step 11 记为 30s 有误：代码实际是 60s（构造参数默认）
        principal_source="检索装配阶段的本地主体",
        current_timeout_seconds=60.0,
        current_retry="ProviderGateway：max_attempts=2；403 AccessDenied 属永久错误 → 零重试",
        cache_eligible=False,
        target_gateway_method="ProviderGateway.call",
        governed=True,
    ),
    ProviderCallSite(
        site_id="bailian_vision_pdf_review",
        module="water_agent/vision.py",
        egress_module="water_agent/vision.py",
        symbol="QwenVisionReviewer.describe_page → _call",
        provider=ProviderKind.BAILIAN_VISION,
        category=CallCategory.VISION_REVIEW,
        required_capability=Capability.LLM_INFERENCE,
        # 上传的工程图纸页面图像：项目资料，与 docgen 同级。
        data_class=DATA_CLASS_PROJECT_DOC,
        principal_source="上传入库时的 Web 登录主体（上传者本人）",
        current_timeout_seconds=120.0,
        current_retry="ProviderGateway：max_attempts=2；401/403 零重试",
        cache_eligible=False,
        target_gateway_method="ProviderGateway.call",
        governed=True,
    ),
)

#: 工具策略版本。**新增/删除工具或改变 capability 映射都必须改它** ——
#: 它是 `cache.py` 缓存键的失效因子：可用工具集变了，旧回答不该再命中。
#: 命名格式 `p<阶段>-step<步号>-<YYYY-MM-DD>`（有测试锁死结构，不锁死阶段号）。
#: P3 Step 18：新增 `Capability.READ_DRAWING` 与 `ToolPolicy.allowed_read_dirs`，
#: capability 映射面已变，故递进版本号。
#: P3 Step 20：新增 `read_drawing` 工具，可用工具集已变，再次递进。
#: P7 S1：`confirmation_handler_ready=true` 配置期拒绝 + 需确认的 MCP 工具执行期拒绝，
#: 可用工具集已变（这类 server 不再能被暴露），递进。
#: P7 S4：按意图最小暴露——每轮发给模型的工具集随问题变化（注册集与 capability 映射不变），
#: 递进。
#: P7 S7：确认门接线——Web 入口下 human_confirmation=true 的 server 可以暴露（以延迟工具出现），
#: 新增凭证放行路径，可用工具集已变，递进。
TOOL_POLICY_VERSION = "p7-step7-2026-10-07"

#: 工具能力清单。**新增 ToolSpec 必须先加到这里**。
#:
#: P2.5 Step 14 后 ``guardrail_validated=True``：工具的调用都经
#: ``ToolRegistry.invoke`` → ``GuardrailPipeline.validate_tool_call``
#: （主体授权、危险能力拒绝、参数注入/大小、路径与域名 allowlist），
#: 由 ``tests/test_tool_policy.py`` 与 ``tests/test_injection_defense.py`` 断言。
#:
#: P3 Step 20 加入 ``read_drawing``（第 5 个工具）。它是**第一个碰文件系统**的
#: 模型可调用工具，因此路径判定不依赖护栏的 ``_looks_like_path``
#: （已实测不识别相对路径），而由工具层三层纵深防御承担，见
#: ``drawing/dxf_reader.py`` 与规格 §4.3。
TOOL_CAPABILITIES: tuple[ToolCapabilityEntry, ...] = (
    ToolCapabilityEntry(
        tool_name="retrieve_clauses",
        module="water_agent/tools/retrieve_clauses.py",
        required_capability=Capability.RETRIEVE_CLAUSES,
        effect="只读（向量/文本检索，无写入）",
        guardrail_validated=True,
    ),
    ToolCapabilityEntry(
        tool_name="cite_clauses",
        module="water_agent/tools/cite.py",
        required_capability=Capability.CITE_CLAUSES,
        effect="只读（声明引用，校验由 validate_citations 承担）",
        guardrail_validated=True,
    ),
    ToolCapabilityEntry(
        tool_name="calc_velocity",
        module="water_agent/tools/calc_velocity.py",
        required_capability=Capability.CALCULATE,
        effect="纯计算（程序化，LLM 不参与数值运算）",
        guardrail_validated=True,
    ),
    ToolCapabilityEntry(
        tool_name="material_stats",
        module="water_agent/tools/material_stats.py",
        required_capability=Capability.MATERIAL_STATS,
        effect="纯计算（聚合管段表；导出到磁盘由 CLI 另行显式触发）",
        guardrail_validated=True,
    ),
    ToolCapabilityEntry(
        tool_name="read_drawing",
        module="water_agent/tools/read_drawing.py",
        required_capability=Capability.READ_DRAWING,
        effect=(
            "只读（在 ToolPolicy.allowed_read_dirs 内读 DXF；无写入、无网络、无 shell。"
            "路径由装配期注入的 DRAWING_ROOT 决定，模型只能表达 drawing_id）"
        ),
        guardrail_validated=True,
    ),
)


# ---------------------------------------------------------------------------
# 运行时 MCP 绑定登记（P4 Step 23）
# ---------------------------------------------------------------------------


class McpToolBinding(BaseModel):
    """一个 MCP 动态工具的治理登记项。

    MCP 工具是**运行时**从 server 发现的，无法静态录入 ``TOOL_CAPABILITIES``
    （那份清单 + 静态扫描的契约是"代码里声明的 ToolSpec == 清单"，MCP 工具不在
    代码里声明）。但它们必须进**同一个** ``capability_for_tool`` 查询口 ——
    否则护栏对 MCP 工具 fail closed 失效，或反过来出现"未登记即放行"的后门。

    写入走 :func:`register_mcp_tool_binding`，只由 ``mcp/bridge.py`` 在装配期调用：
    仅当 server 已配置、工具被 allowlist 收录、且无危险标记时才登记 ——
    "工具出现在 server 上"本身不构成登记资格。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 桥接后的工具名（``mcp_<server>_<tool>``，桥接层已保证唯一与 snake_case）。
    tool_name: str = Field(min_length=1)
    #: 所属 server 的稳定 id（配置里的 key）。
    server_id: str = Field(min_length=1)
    #: server 上的原始工具名。
    remote_tool: str = Field(min_length=1)
    capability: Capability
    #: server 配置是否显式声明了该工具的写/危险操作需人工确认。
    #: 为 ``True`` 的绑定，``GuardrailPipeline.validate_tool_call`` 没有执行凭证时一律以
    #: ``tool.confirmation_required`` 拒绝调用（P7-S1）；唯一的放行路径是外层确认门在人工批准后
    #: 签发的一次性凭证（P7-S7，``ToolRegistry.invoke_confirmed``）。装配时这类工具在 ToolNode 里
    #: 是延迟工具（``plugins/assembly.py``）。
    requires_human_confirmation: bool = False


#: 进程内 MCP 绑定登记。**只增不删**（进程生命周期内注册表不可被部分撤销，
#: 避免出现"登记了桥接工具又摘掉登记"的窗口期）。测试用独立进程或
#: ``clear_mcp_tool_bindings()``（后者仅供测试，生产代码不得调用）。
_MCP_RUNTIME_BINDINGS: dict[str, McpToolBinding] = {}


def register_mcp_tool_binding(binding: McpToolBinding) -> None:
    """登记一个 MCP 动态工具绑定；**不同的**绑定抢同一个名字时拒绝。

    同一份绑定（server / 远端工具 / 能力 / 确认标记全部相同）再次登记是幂等的：
    每个 Web run 都会装配一次 ``AgenticSession``、重新打开 MCP 桥接，此前第二次
    登记即 ``ValueError`` → 桥接层记为 ``MCP_NAME_COLLISION`` 并跳过，于是**进程里
    只有第一个会话拿得到 MCP 工具**（review 09-28 #3，实测）。
    重名拒绝要防的是"另一个 server 的工具静默顶替已登记的名字"，那种情况照旧拒绝；
    幂等不削弱它，因为 ``McpToolBinding`` 是 frozen 的，相等即每个治理字段都相等。
    """
    existing = _MCP_RUNTIME_BINDINGS.get(binding.tool_name)
    if existing == binding:
        return
    if existing is not None:
        raise ValueError(
            f"MCP 工具绑定 {binding.tool_name!r} 已登记（server={existing.server_id}）；"
            "桥接层必须保证工具名全局唯一"
        )
    _MCP_RUNTIME_BINDINGS[binding.tool_name] = binding


def mcp_tool_bindings() -> frozenset[McpToolBinding]:
    """当前全部 MCP 绑定（只读视图，供测试与 governance_coverage 消费）。"""
    return frozenset(_MCP_RUNTIME_BINDINGS.values())


def mcp_tool_binding(tool_name: str) -> McpToolBinding | None:
    """按工具名查运行时 MCP 绑定；不是 MCP 工具（或未登记）返回 ``None``。"""
    return _MCP_RUNTIME_BINDINGS.get(tool_name)


def clear_mcp_tool_bindings() -> None:
    """清空 MCP 绑定登记。**仅供测试**（隔离进程级状态）；生产代码不得调用。"""
    _MCP_RUNTIME_BINDINGS.clear()


# ---------------------------------------------------------------------------
# 静态扫描：让"新增未登记的出口/工具"成为可失败的测试，而不是靠人记得改文档
# ---------------------------------------------------------------------------

#: 远程出口的代码特征。任何新的远程 SDK 入口都必须在此登记，否则扫描形同虚设。
_EGRESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\burlopen\s*\("),
    re.compile(r"\bChatOpenAI\s*\("),
    re.compile(r"\bOpenAIEmbeddings\s*\("),
    re.compile(r"\bhttpx\.(?:get|post|request|Client)\s*\("),
    re.compile(r"\brequests\.(?:get|post|request|Session)\s*\("),
)

#: 扫描豁免：治理层自身、以及只做离线确定性计算的模块。
_EGRESS_SCAN_EXEMPT: frozenset[str] = frozenset(
    {
        "water_agent/runtime/governance/inventory.py",
    }
)

_TOOLSPEC_NAME_RE = re.compile(r"""ToolSpec\s*\(\s*\n\s*name\s*=\s*["']([a-z0-9_]+)["']""")


def _iter_package_modules() -> list[Path]:
    return sorted(p for p in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _relative(path: Path) -> str:
    return path.relative_to(PACKAGE_ROOT.parent).as_posix()


def registered_call_site_modules() -> frozenset[str]:
    """清单中登记的**实际出站模块**集合（``egress_module``，不是逻辑调用点）。"""
    return frozenset(site.egress_module for site in PROVIDER_CALL_SITES)


def scan_remote_egress_modules() -> frozenset[str]:
    """静态扫描包内实际发起远程调用的模块（相对 `src/` 的 posix 路径）。

    只做代码特征匹配，不导入模块——避免扫描本身触发 provider 构造。
    """
    found: set[str] = set()
    for path in _iter_package_modules():
        rel = _relative(path)
        if rel in _EGRESS_SCAN_EXEMPT:
            continue
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in _EGRESS_PATTERNS):
            found.add(rel)
    return frozenset(found)


def ungoverned_egress_modules() -> frozenset[str]:
    """有远程出口但**未登记**到清单的模块——必须为空，否则治理层可被绕过。"""
    return scan_remote_egress_modules() - registered_call_site_modules()


def scan_tool_spec_names() -> frozenset[str]:
    """静态扫描 `tools/` 下声明的 ToolSpec 名称。"""
    found: set[str] = set()
    tools_dir = PACKAGE_ROOT / "tools"
    for path in sorted(tools_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        found.update(_TOOLSPEC_NAME_RE.findall(path.read_text(encoding="utf-8")))
    return frozenset(found)


def undeclared_tool_names() -> frozenset[str]:
    """代码里存在但**未登记** capability 的工具——必须为空。"""
    return scan_tool_spec_names() - frozenset(entry.tool_name for entry in TOOL_CAPABILITIES)


def capability_for_tool(tool_name: str) -> Capability:
    """查询工具所需能力；未登记 fail closed（抛 ``KeyError``）。

    查询顺序：静态清单 ``TOOL_CAPABILITIES`` → 运行时 MCP 绑定登记
    （``register_mcp_tool_binding``，P4 Step 23）。仍未命中 = 不可判定 = 拒绝。

    Step 14 的 ``GuardrailPipeline`` 用它在 ``ToolRegistry.invoke`` 前做能力校验：
    未登记 = 不可判定 = 拒绝，而不是"默认放行"。
    """
    for entry in TOOL_CAPABILITIES:
        if entry.tool_name == tool_name:
            return entry.required_capability
    binding = _MCP_RUNTIME_BINDINGS.get(tool_name)
    if binding is not None:
        return binding.capability
    raise KeyError(
        f"工具 {tool_name!r} 未在 runtime/governance/inventory.py 登记；"
        "静态工具必须在 TOOL_CAPABILITIES 声明，MCP 动态工具必须经 "
        "register_mcp_tool_binding 登记（fail closed，不得默认放行）"
    )


def call_site(site_id: str) -> ProviderCallSite:
    """按 site_id 查询调用点；未登记抛 ``KeyError``。"""
    for site in PROVIDER_CALL_SITES:
        if site.site_id == site_id:
            return site
    raise KeyError(f"未登记的远程调用点 {site_id!r}")


def governance_coverage() -> dict[str, int]:
    """接入进度摘要（供文档与 Step 15 报告引用，不作为通过条件）。"""
    return {
        "provider_call_sites": len(PROVIDER_CALL_SITES),
        "provider_governed": sum(1 for s in PROVIDER_CALL_SITES if s.governed),
        "tools": len(TOOL_CAPABILITIES),
        "tools_guardrail_validated": sum(1 for t in TOOL_CAPABILITIES if t.guardrail_validated),
    }


__all__ = [
    "DATA_CLASS_PROJECT_DOC",
    "DATA_CLASS_PUBLIC_CLAUSE",
    "DATA_CLASS_USER_QUERY",
    "PACKAGE_ROOT",
    "PROVIDER_CALL_SITES",
    "TOOL_CAPABILITIES",
    "TOOL_POLICY_VERSION",
    "McpToolBinding",
    "ProviderCallSite",
    "ToolCapabilityEntry",
    "call_site",
    "capability_for_tool",
    "clear_mcp_tool_bindings",
    "governance_coverage",
    "mcp_tool_binding",
    "mcp_tool_bindings",
    "register_mcp_tool_binding",
    "registered_call_site_modules",
    "scan_remote_egress_modules",
    "scan_tool_spec_names",
    "undeclared_tool_names",
    "ungoverned_egress_modules",
]
