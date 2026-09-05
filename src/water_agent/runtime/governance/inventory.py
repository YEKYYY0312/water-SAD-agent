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
  前置校验。Step 11 全部为 ``False``（Pipeline 属 Step 14）。

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
    #: 是否已过 GuardrailPipeline 前置校验。Step 11 全 False。
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
        symbol="build_chat_model → GovernedChatModel._generate",
        provider=ProviderKind.DEEPSEEK_CHAT,
        category=CallCategory.AGENTIC_LOOP,
        required_capability=Capability.LLM_INFERENCE,
        data_class=DATA_CLASS_USER_QUERY,
        principal_source="AgenticSession thread_id → 本地主体",
        current_timeout_seconds=60.0,
        current_retry="ProviderGateway：max_attempts=3；ChatOpenAI 自带重试已关（max_retries=0，避免叠乘）",
        cache_eligible=False,
        target_gateway_method="ProviderGateway.call",
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
)

#: 工具能力清单。**新增 ToolSpec 必须先加到这里**。
TOOL_CAPABILITIES: tuple[ToolCapabilityEntry, ...] = (
    ToolCapabilityEntry(
        tool_name="retrieve_clauses",
        module="water_agent/tools/retrieve_clauses.py",
        required_capability=Capability.RETRIEVE_CLAUSES,
        effect="只读（向量/文本检索，无写入）",
    ),
    ToolCapabilityEntry(
        tool_name="cite_clauses",
        module="water_agent/tools/cite.py",
        required_capability=Capability.CITE_CLAUSES,
        effect="只读（声明引用，校验由 validate_citations 承担）",
    ),
    ToolCapabilityEntry(
        tool_name="calc_velocity",
        module="water_agent/tools/calc_velocity.py",
        required_capability=Capability.CALCULATE,
        effect="纯计算（程序化，LLM 不参与数值运算）",
    ),
    ToolCapabilityEntry(
        tool_name="material_stats",
        module="water_agent/tools/material_stats.py",
        required_capability=Capability.MATERIAL_STATS,
        effect="纯计算（聚合管段表；导出到磁盘由 CLI 另行显式触发）",
    ),
)


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
    """查询工具所需能力；未登记的工具 fail closed（抛 ``KeyError``）。

    Step 14 的 ``GuardrailPipeline`` 用它在 ``ToolRegistry.invoke`` 前做能力校验：
    未登记 = 不可判定 = 拒绝，而不是"默认放行"。
    """
    for entry in TOOL_CAPABILITIES:
        if entry.tool_name == tool_name:
            return entry.required_capability
    raise KeyError(
        f"工具 {tool_name!r} 未在 runtime/governance/inventory.py 登记 capability；"
        "新增工具必须先登记（fail closed，不得默认放行）"
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
    "ProviderCallSite",
    "ToolCapabilityEntry",
    "call_site",
    "capability_for_tool",
    "governance_coverage",
    "registered_call_site_modules",
    "scan_remote_egress_modules",
    "scan_tool_spec_names",
    "undeclared_tool_names",
    "ungoverned_egress_modules",
]
