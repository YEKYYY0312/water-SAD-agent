"""P2.5 治理契约：运行时边界的唯一定义（Step 11）。

设计约束（`docs/design/12-p2.5-production-governance.md` §3）：

1. **纯契约**：本模块不发起远程调用、不导入 langchain/langgraph/provider SDK，
   可在纯离线测试中构造。
2. **extra="forbid"**：所有模型禁止额外字段——治理字段拼错必须响亮失败，
   而不是被静默丢弃后按"未配置"放行（fail closed）。
3. **来源不可降级**：``SourceTag`` 的信任级别是**代码定义的偏序**，不是模型/用户
   可自由声称的字符串。``ContentEnvelope.relabel`` 只允许降低信任度，
   任何提升信任度的重标记抛 ``SourceTagDowngradeError``。
4. **主体不可伪造**：``Principal`` 只能由 ``Principal.local()``（CLI 本地主体）或
   未来 P4 认证层的 ``Principal.authenticated()`` 构造；两者都要求 subject 由调用方
   的可信来源提供，禁止从用户文本解析 subject/user_id。
5. **序列化不泄漏**：``Principal.subject`` 与内容原文都不进 trace/日志。对外暴露的
   是 ``principal_hash`` / ``content_hash``（sha256 前 16 位，与
   ``domain.models.content_hash_of`` 同口径）。

错误码是**稳定契约**：trace、评测 scorer、CLI 退出语义都依赖它，改名等同破坏兼容。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from water_agent.domain.models import content_hash_of, utc_now

# ---------------------------------------------------------------------------
# 稳定错误码（改名 = 破坏兼容；新增需同步 threat model 与 governance_p0）
# ---------------------------------------------------------------------------

#: 主体/请求速率超限（Step 12）。附带安全的 retry_after_seconds，不含 bucket 内部细节。
CODE_RATE_LIMITED = "RATE_LIMITED"
#: 预算（token/成本）耗尽，拒绝新的远程调用（Step 12）。
CODE_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
#: provider 未返回 usage：账本必须标记，禁止把估算写成真实账单（Step 12）。
CODE_USAGE_UNKNOWN = "USAGE_UNKNOWN"
#: 瞬时 provider 失败（网络/5xx/408），有界重试后仍失败（Step 12）。
CODE_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
#: 超过本次请求 deadline（Step 12）。
CODE_PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
#: provider 认证/授权失败——永久错误，零重试（Step 12）。
CODE_PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
#: 熔断器打开，不发送请求（Step 12）。
CODE_CIRCUIT_OPEN = "CIRCUIT_OPEN"
#: 护栏阻断（注入/PII/secret/越权工具参数等，Step 14）。
CODE_GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
#: 内容来源标签缺失或不可判定 → fail closed（Step 14）。
CODE_SOURCE_TAG_MISSING = "SOURCE_TAG_MISSING"
#: 工具能力未授权（capability 不在 Principal 授予集合内，Step 14）。
CODE_CAPABILITY_DENIED = "CAPABILITY_DENIED"
#: 输入超过字节/token 上限（Step 14）。
CODE_INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
#: 缓存条目失效或不可用于本请求（Step 13）。
CODE_CACHE_INVALIDATED = "CACHE_INVALIDATED"
#: 治理策略/配置不可判定 → fail closed（通用兜底）。
CODE_POLICY_UNAVAILABLE = "POLICY_UNAVAILABLE"

#: 全部稳定错误码。`tests/test_governance_contracts.py` 断言此集合不缩小。
GOVERNANCE_ERROR_CODES: frozenset[str] = frozenset(
    {
        CODE_RATE_LIMITED,
        CODE_BUDGET_EXCEEDED,
        CODE_USAGE_UNKNOWN,
        CODE_PROVIDER_UNAVAILABLE,
        CODE_PROVIDER_TIMEOUT,
        CODE_PROVIDER_AUTH_FAILED,
        CODE_CIRCUIT_OPEN,
        CODE_GUARDRAIL_BLOCKED,
        CODE_SOURCE_TAG_MISSING,
        CODE_CAPABILITY_DENIED,
        CODE_INPUT_TOO_LARGE,
        CODE_CACHE_INVALIDATED,
        CODE_POLICY_UNAVAILABLE,
    }
)


class GovernanceError(RuntimeError):
    """治理层结构化失败。

    ``code`` 必须来自 :data:`GOVERNANCE_ERROR_CODES`；``message`` 是给用户/调用方的
    可行动说明，**不得含** API Key、原始 PII、完整系统提示或内部 bucket 细节。
    """

    def __init__(self, code: str, message: str, *, retry_after_seconds: float | None = None) -> None:
        if code not in GOVERNANCE_ERROR_CODES:
            raise ValueError(f"未知治理错误码 {code!r}；合法值：{sorted(GOVERNANCE_ERROR_CODES)}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds

    def to_dict(self) -> dict[str, Any]:
        """结构化输出（供 CLI/trace 消费）；retry_after 仅在有值时出现。"""
        payload: dict[str, Any] = {"error": True, "code": self.code, "message": self.message}
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        return payload


class SourceTagDowngradeError(GovernanceError):
    """试图提升内容信任级别（例如把检索条文重标为 user_message）。"""

    def __init__(self, message: str) -> None:
        super().__init__(CODE_SOURCE_TAG_MISSING, message)


# ---------------------------------------------------------------------------
# 来源标签：信任级别是代码定义的偏序，不是可声称的字符串
# ---------------------------------------------------------------------------


class SourceTag(str, Enum):
    """内容来源标签（Step 11 契约，Step 14 由 GuardrailPipeline 强制）。

    覆盖规格 §3 要求的五类，外加 ``system_policy``（本项目自有的系统指令，
    唯一可信来源）以便测试"不可信文本不得拼接为系统指令"。
    """

    SYSTEM_POLICY = "system_policy"
    USER_MESSAGE = "user_message"
    RETRIEVED_CLAUSE = "retrieved_clause"
    RETRIEVED_FILE = "retrieved_file"
    TOOL_OUTPUT = "tool_output"
    #: P7-S11：联网获取的外部内容（搜索摘录等）。信任低于一切本地来源，**不可承载指令**，也不是规范依据。
    WEB_CONTENT = "web_content"
    SESSION_MEMORY = "session_memory"
    GENERATED_OUTPUT = "generated_output"


#: 信任级别：数值越大越可信。**只有 ``SYSTEM_POLICY`` 可作为指令**，其余一律是数据。
#: 该表是 ``ContentEnvelope.relabel`` 单调性检查的唯一依据。
SOURCE_TRUST_LEVEL: dict[SourceTag, int] = {
    SourceTag.SYSTEM_POLICY: 100,
    SourceTag.USER_MESSAGE: 50,
    SourceTag.SESSION_MEMORY: 30,
    SourceTag.RETRIEVED_CLAUSE: 20,
    SourceTag.TOOL_OUTPUT: 10,
    SourceTag.RETRIEVED_FILE: 10,
    SourceTag.WEB_CONTENT: 5,
    SourceTag.GENERATED_OUTPUT: 0,
}

#: 唯一可被当作指令的来源。其他来源中的"指令"必须按数据处理（间接注入防线）。
INSTRUCTION_BEARING_SOURCES: frozenset[SourceTag] = frozenset({SourceTag.SYSTEM_POLICY})


def trust_level(tag: SourceTag) -> int:
    """返回来源标签的信任级别；未登记的标签 fail closed（视为最低信任）。"""
    return SOURCE_TRUST_LEVEL.get(tag, 0)


def is_instruction_bearing(tag: SourceTag) -> bool:
    """该来源的文本是否可以作为指令（而非数据）参与提示装配。"""
    return tag in INSTRUCTION_BEARING_SOURCES


# ---------------------------------------------------------------------------
# Capability：工具/远程调用的最小授权单元（P4 不得删除，不得用 prompt 替代）
# ---------------------------------------------------------------------------


class Capability(str, Enum):
    """能力枚举：主体被授予的最小权限单元。

    P2.5 当前系统只需要只读检索、纯计算、用户显式目录导出与远程模型推理。
    可写/网络/shell/邮件类能力**在 P4 授权与人工确认之前默认拒绝**——这里先登记
    为显式枚举值，好让"默认拒绝"是可测试的断言而不是"尚未实现"。
    """

    RETRIEVE_CLAUSES = "retrieve_clauses"
    CITE_CLAUSES = "cite_clauses"
    CALCULATE = "calculate"
    MATERIAL_STATS = "material_stats"
    EXPORT_TO_ALLOWED_DIR = "export_to_allowed_dir"
    #: P3：在 `ToolPolicy.allowed_read_dirs` 内**只读**图纸文件。
    #: 只授予读，不含写入/网络/shell —— 与 `EXPORT_TO_ALLOWED_DIR` 语义分离
    #: （可读 ≠ 可写，见 `docs/design/p3-step-17-decision.md` D4）。
    READ_DRAWING = "read_drawing"
    #: P7-S9：按名加载 skill 库里的操作规程（只读：读随包分发的内置规程或运维只读目录）。
    #: 本地只读能力，与检索、计算同档；规程正文生效的约束在 ``skills/`` 与提示装配器里，不在这里。
    LOAD_SKILL = "load_skill"
    LLM_INFERENCE = "llm_inference"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    #: P4：调用 MCP server 上的**只读**远程工具（经 MCP 网关桥接为 ToolSpec）。
    #: **不在** DEFAULT_LOCAL_CAPABILITIES 内 —— 默认没有主体持有它；只有装配期
    #: 依据人工配置（mcp_servers.json）显式授予时才生效，这就是 P4「权限可配置」
    #: 的落点。危险操作（写/破坏性）不走本能力，见 `requires_human_confirmation`。
    MCP_INVOKE = "mcp_invoke"
    #: P7-S12：联网搜索（**只读外发**，目的地由运维写死的基址决定，模型只控制查询字符串）。
    #: **不在** DEFAULT_LOCAL_CAPABILITIES 与 Web 成员能力集内；只有部署开关已开且用户对**这一条消息**
    #: 勾选了"联网"时，装配期才由插件追加给主体（``GRANTABLE_CAPABILITIES``）。
    #: 与下面的 ``NETWORK_FETCH`` 语义不同：后者是"目的地由模型决定"，仍在危险集里、仍被构造期拒绝。
    WEB_SEARCH = "web_search"
    #: P7-S13：从运维配置的 skill 注册表取回候选 skill（**只读外发**，目的地由运维写死的基址决定）。
    #: 它只由运维 CLI（``skills/cli.py``）构造的主体持有；**不在** DEFAULT_LOCAL_CAPABILITIES、Web 成员能力集与
    #: ``GRANTABLE_CAPABILITIES`` 内，插件、模型与面板都拿不到——取回与批准是主机 shell 上的人工动作（P7-I12）。
    #: 它不对应任何 ``ToolSpec``（不是模型工具），所以 ``inventory`` 的"能力 → 工具"登记表不变。
    SKILL_FETCH = "skill_fetch"
    # 以下能力当前**没有任何主体被授予**（见 DEFAULT_LOCAL_CAPABILITIES），
    # 存在的意义是让 Step 14 的"默认拒绝"可被正例/反例测试覆盖。
    WRITE_FILE = "write_file"
    NETWORK_FETCH = "network_fetch"
    EXECUTE_SHELL = "execute_shell"
    SEND_MESSAGE = "send_message"


#: 需 P4 认证授权 + 人工确认后才可启用的危险能力。P2.5 一律拒绝。
DANGEROUS_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.WRITE_FILE,
        Capability.NETWORK_FETCH,
        Capability.EXECUTE_SHELL,
        Capability.SEND_MESSAGE,
    }
)

#: CLI 本地主体的默认能力集：只读检索 / 纯计算 / 只读图纸 / 显式目录导出 / 远程模型推理。
DEFAULT_LOCAL_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.RETRIEVE_CLAUSES,
        Capability.CITE_CLAUSES,
        Capability.CALCULATE,
        Capability.MATERIAL_STATS,
        Capability.READ_DRAWING,
        Capability.LOAD_SKILL,
        Capability.EXPORT_TO_ALLOWED_DIR,
        Capability.LLM_INFERENCE,
        Capability.EMBEDDING,
        Capability.RERANK,
    }
)


# ---------------------------------------------------------------------------
# Principal：不可伪造的主体
# ---------------------------------------------------------------------------

#: CLI 本地主体的 subject 形态：``local:<thread_id>``（规格 §3 强制）。
_LOCAL_SUBJECT_RE = re.compile(r"^local:[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")


class PrincipalKind(str, Enum):
    """主体类型。``LOCAL_CLI`` 是 P2.5 唯一可用类型；``AUTHENTICATED`` 留给 P4。"""

    LOCAL_CLI = "local_cli"
    AUTHENTICATED = "authenticated"


class Principal(BaseModel):
    """请求主体。

    **不可伪造约束**：
    - 只能经 :meth:`local` / :meth:`authenticated` 构造（直接构造需显式 ``_trusted=True``，
      该字段是私有约定，序列化时不出现）；
    - ``subject`` 永不进 trace/日志/缓存键原文，对外只暴露 :attr:`principal_hash`；
    - ``capabilities`` 是**授予集合**，不是模型或用户可声称的字段；危险能力被
      构造期直接拒绝（P2.5 无人可获得）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PrincipalKind
    subject: str = Field(min_length=1, repr=False)
    capabilities: frozenset[Capability] = Field(default=frozenset())
    #: 授权域：缓存隔离与限流键的组成部分（P4 填租户/组织，P2.5 固定 "local"）。
    auth_domain: str = Field(default="local", min_length=1)

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.kind is PrincipalKind.LOCAL_CLI and not _LOCAL_SUBJECT_RE.match(self.subject):
            raise ValueError(
                f"本地主体 subject 必须匹配 {_LOCAL_SUBJECT_RE.pattern!r}（形如 local:<thread_id>）"
            )
        granted_dangerous = self.capabilities & DANGEROUS_CAPABILITIES
        if granted_dangerous:
            raise ValueError(
                "P2.5 不得授予危险能力（需 P4 认证授权 + 人工确认）："
                f"{sorted(c.value for c in granted_dangerous)}"
            )
        return self

    @classmethod
    def local(
        cls, thread_id: str, *, capabilities: frozenset[Capability] | None = None
    ) -> Principal:
        """构造 CLI 本地主体。``thread_id`` 由 CLI/会话层提供，**不得**来自用户文本解析。"""
        return cls(
            kind=PrincipalKind.LOCAL_CLI,
            subject=f"local:{thread_id}",
            capabilities=DEFAULT_LOCAL_CAPABILITIES if capabilities is None else capabilities,
            auth_domain="local",
        )

    @classmethod
    def authenticated(
        cls, *, subject: str, auth_domain: str, capabilities: frozenset[Capability]
    ) -> Principal:
        """P4 认证层入口：由 MCP/HTTP 认证结果填充，**当前无调用方**。

        保留此构造器是为了让 P4 继承契约而不是另起一套；P2.5 的测试断言
        CLI 路径永不走这里。
        """
        return cls(
            kind=PrincipalKind.AUTHENTICATED,
            subject=subject,
            capabilities=capabilities,
            auth_domain=auth_domain,
        )

    @property
    def principal_hash(self) -> str:
        """主体的稳定哈希：trace / 账本 / 限流键 / 缓存键统一用它，不用 subject 原文。"""
        return hashlib.sha256(f"{self.kind.value}|{self.subject}".encode()).hexdigest()[:16]

    def has(self, capability: Capability) -> bool:
        """是否被授予某能力。未授予即拒绝（fail closed），不存在"默认允许"。"""
        return capability in self.capabilities

    def require(self, capability: Capability) -> None:
        """能力检查；未授予抛 :class:`GovernanceError`（``CAPABILITY_DENIED``）。

        错误信息只含能力名，不含 subject —— 避免把主体标识写进用户可见错误。
        """
        if not self.has(capability):
            raise GovernanceError(
                CODE_CAPABILITY_DENIED,
                f"当前主体未被授予能力 {capability.value!r}；"
                "该能力需 P4 认证授权与人工确认后才可启用",
            )

    def safe_dict(self) -> dict[str, Any]:
        """脱敏视图：只含 hash / kind / auth_domain / 能力名，**不含 subject**。"""
        return {
            "principal_hash": self.principal_hash,
            "kind": self.kind.value,
            "auth_domain": self.auth_domain,
            "capabilities": sorted(c.value for c in self.capabilities),
        }


# ---------------------------------------------------------------------------
# ContentEnvelope：带来源标签的内容载体
# ---------------------------------------------------------------------------


class ContentEnvelope(BaseModel):
    """带来源标签的内容单元（Step 14 由护栏强制，Step 11 定契约）。

    规格 §3：消息历史与 trace **只传引用 ID / 哈希**，不复写完整外部内容。
    因此 :meth:`safe_dict` 输出 ``content_hash`` + 长度 + 标签，不输出 ``text``。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_tag: SourceTag
    text: str = Field(repr=False)
    #: 外部内容的引用标识（条款 source_id / 文件路径 / 工具调用 id），供 trace 关联。
    reference_id: str = ""
    #: 已由护栏脱敏 → 可写缓存/trace；未脱敏内容不得落盘。
    redacted: bool = False

    @property
    def content_hash(self) -> str:
        """内容哈希，与 ``domain.models.content_hash_of`` 同口径（sha256 前 16 位）。"""
        return content_hash_of(self.text)

    @property
    def trust_level(self) -> int:
        return trust_level(self.source_tag)

    @property
    def is_instruction_bearing(self) -> bool:
        """本内容能否作为指令参与提示装配（只有 system_policy 可以）。"""
        return is_instruction_bearing(self.source_tag)

    def relabel(self, new_tag: SourceTag) -> ContentEnvelope:
        """重标记来源：**只允许降低信任级别**（如 tool_output → generated_output）。

        任何提升信任度的重标记抛 :class:`SourceTagDowngradeError` ——
        这是"来源不可降级"（不可被提权）的强制点：模型或用户无法把检索到的
        条文/工具输出洗白成 user_message 或 system_policy。
        """
        if trust_level(new_tag) > self.trust_level:
            raise SourceTagDowngradeError(
                f"禁止提升内容信任级别：{self.source_tag.value}"
                f"（{self.trust_level}）→ {new_tag.value}（{trust_level(new_tag)}）"
            )
        return self.model_copy(update={"source_tag": new_tag})

    def safe_dict(self) -> dict[str, Any]:
        """脱敏视图：**不含 text 原文**，只有标签、哈希、长度与引用 ID。"""
        return {
            "source_tag": self.source_tag.value,
            "content_hash": self.content_hash,
            "content_length": len(self.text),
            "reference_id": self.reference_id,
            "redacted": self.redacted,
            "trust_level": self.trust_level,
        }


# ---------------------------------------------------------------------------
# RequestContext：一次用户请求的治理上下文
# ---------------------------------------------------------------------------


class PiiMode(str, Enum):
    """PII 处理模式。默认 ``REDACT``；``ALLOW`` 需显式可审计的本地策略开关。"""

    REDACT = "redact"
    BLOCK = "block"
    ALLOW = "allow"


class RequestContext(BaseModel):
    """一次请求的治理上下文：主体、请求标识、deadline、缓存与 PII 策略。

    ``request_id`` 用于串联 trace / usage 账本 / 护栏事件。``deadline_seconds`` 是
    **整个请求**的预算上限（Step 12 用它约束单请求内的重试总等待时间）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: Principal
    request_id: str = Field(min_length=1)
    #: 会话线程（与 checkpointer thread_id 同源）；缓存键与限流键都不直接用它原文。
    thread_id: str = ""
    #: 请求整体 deadline（秒）；``None`` = 由 gateway 配置默认值裁定，不是"无限期"。
    deadline_seconds: float | None = Field(default=None, gt=0)
    pii_mode: PiiMode = PiiMode.REDACT
    #: 不读缓存（仍可写）。
    no_cache: bool = False
    #: 不写缓存（也不落持久化条目）。
    no_store: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("request_id")
    @classmethod
    def _validate_request_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request_id 不得为空白")
        return value

    def safe_dict(self) -> dict[str, Any]:
        """脱敏视图：主体走 hash，不含 subject / thread_id 原文。"""
        return {
            "request_id": self.request_id,
            "principal": self.principal.safe_dict(),
            "thread_hash": content_hash_of(self.thread_id) if self.thread_id else "",
            "deadline_seconds": self.deadline_seconds,
            "pii_mode": self.pii_mode.value,
            "no_cache": self.no_cache,
            "no_store": self.no_store,
        }


# ---------------------------------------------------------------------------
# PolicyDecision：护栏/限流/预算的统一裁定结果
# ---------------------------------------------------------------------------


class PolicyAction(str, Enum):
    """裁定动作。``UNKNOWN`` 不是"放行"——调用方必须按 fail closed 处理。"""

    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"
    UNKNOWN = "unknown"


class PolicyDecision(BaseModel):
    """一次策略裁定的结构化结果。

    ``rule_id`` 是版本化规则标识（Step 14 每条规则都要有正反例），
    ``detail`` 只放脱敏摘要，**禁止**原始命中文本。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: PolicyAction
    rule_id: str = Field(min_length=1)
    #: 规则集版本；便于回溯"哪一版规则做出的裁定"。
    ruleset_version: str = Field(default="unset", min_length=1)
    #: 对应的稳定错误码（BLOCK/UNKNOWN 时必填，ALLOW/REDACT 时必须为 None）。
    code: str | None = None
    #: 脱敏摘要（命中类型、位置计数等），不含原始命中文本。
    detail: str = ""

    @model_validator(mode="after")
    def _validate_code(self) -> Self:
        if self.action in (PolicyAction.BLOCK, PolicyAction.UNKNOWN):
            if self.code is None:
                raise ValueError(f"action={self.action.value} 必须携带稳定错误码")
            if self.code not in GOVERNANCE_ERROR_CODES:
                raise ValueError(f"未知治理错误码 {self.code!r}")
        elif self.code is not None:
            raise ValueError(f"action={self.action.value} 不得携带错误码")
        return self

    @property
    def allowed(self) -> bool:
        """是否放行。``UNKNOWN`` 视为不放行（fail closed）。"""
        return self.action in (PolicyAction.ALLOW, PolicyAction.REDACT)

    def raise_if_blocked(self) -> None:
        """非放行则抛结构化治理错误。"""
        if not self.allowed:
            assert self.code is not None  # _validate_code 已保证
            raise GovernanceError(self.code, f"策略 {self.rule_id} 拒绝本次请求：{self.detail}")


# ---------------------------------------------------------------------------
# ProviderCall / UsageRecord：远程调用与账本
# ---------------------------------------------------------------------------


class ProviderKind(str, Enum):
    """远程 provider 类别。与 `inventory` 的调用点登记一一对应。"""

    DEEPSEEK_CHAT = "deepseek_chat"
    BAILIAN_EMBEDDING = "bailian_embedding"
    BAILIAN_RERANK = "bailian_rerank"
    #: P5-S52：CAD 导出 PDF 的视觉审阅（百炼 Qwen-VL，OpenAI 兼容端点）。
    BAILIAN_VISION = "bailian_vision"
    #: P7-S8：远程 MCP server（Streamable HTTP）与其 OAuth 授权服务器。出口只由运维配置的 URL 决定。
    MCP_REMOTE = "mcp_remote"
    #: P7-S12：联网搜索服务（运维配置的单一基址；searxng 自托管或 brave 公网 API）。
    WEB_SEARCH = "web_search"
    #: P7-S13：skill 注册表（运维配置的单一基址；静态文件，只有运维 CLI 会取）。
    SKILL_REGISTRY = "skill_registry"


class CallCategory(str, Enum):
    """调用分类：限流/预算/缓存资格都按分类区分，而不是按代码位置。"""

    RAG_GENERATION = "rag_generation"
    AGENTIC_LOOP = "agentic_loop"
    DOCGEN_POLISH = "docgen_polish"
    JUDGE = "judge"
    EMBEDDING_QUERY = "embedding_query"
    EMBEDDING_INGEST = "embedding_ingest"
    RERANK = "rerank"
    VISION_REVIEW = "vision_review"
    #: P7-S8：对远程 MCP server 的一次 HTTP 交换（initialize / tools/list / tools/call / 授权端点……）。
    MCP_TOOL_CALL = "mcp_tool_call"
    #: 设置面板「获取模型」：对用户填写的对话端点发一次 ``GET {base_url}/models``（管理员触发，不含用户问题）。
    MODEL_LIST = "model_list"
    #: P7-S12：一次联网搜索（``web_search`` 工具发往运维配置的搜索服务的 GET）。
    WEB_SEARCH = "web_search"
    #: P7-S13：运维 CLI 向 skill 注册表取 ``index.json`` 或某个 ``SKILL.md`` 的一次 GET。
    SKILL_REGISTRY = "skill_registry"


class ProviderCall(BaseModel):
    """一次远程调用的意图声明（发送前构造，供限流/预算/缓存判定）。

    ``estimated_input_tokens`` / ``max_output_tokens`` 是**预留额度**依据；
    真实用量由 :class:`UsageRecord` 在返回后对账（规格 §4）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderKind
    category: CallCategory
    model: str = Field(min_length=1)
    #: 温度：缓存资格的组成部分（只有 0 才可能进精确缓存，Step 13）。
    temperature: float = Field(default=0.0, ge=0.0)
    estimated_input_tokens: int = Field(default=0, ge=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    #: 需要的能力；gateway 发送前对 Principal 做 require()。
    required_capability: Capability = Capability.LLM_INFERENCE

    def safe_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "category": self.category.value,
            "model": self.model,
            "temperature": self.temperature,
            "estimated_input_tokens": self.estimated_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "required_capability": self.required_capability.value,
        }


class CacheStatus(str, Enum):
    """缓存状态。``BYPASS`` = 请求显式 no_cache；``INELIGIBLE`` = 不满足缓存资格。"""

    HIT = "hit"
    MISS = "miss"
    BYPASS = "bypass"
    INELIGIBLE = "ineligible"


class CacheDisposition(BaseModel):
    """缓存裁定结果（Step 13 由 AnswerCache 产出，Step 11 定契约）。

    ``eligible_for_store`` 与 ``status`` 是**独立**的两件事：MISS 且不可存
    （例如引用校验未通过）必须能被表达，否则"未验证引用进缓存"就无法被测试排除。
    ``reason`` 是脱敏摘要，``cache_key_hash`` 是键的哈希——**cache key 原文永不落盘/入 trace**。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CacheStatus
    eligible_for_store: bool = False
    cache_key_hash: str = ""
    #: 命中条目的年龄（秒）；仅 HIT 时有意义。
    entry_age_seconds: float | None = Field(default=None, ge=0)
    reason: str = ""

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.status is CacheStatus.HIT and self.entry_age_seconds is None:
            raise ValueError("status=hit 必须给出 entry_age_seconds（用于 TTL 观测）")
        if self.status is not CacheStatus.HIT and self.entry_age_seconds is not None:
            raise ValueError("只有 status=hit 才有 entry_age_seconds")
        if self.status in (CacheStatus.BYPASS, CacheStatus.INELIGIBLE) and self.eligible_for_store:
            raise ValueError(f"status={self.status.value} 不得同时声明 eligible_for_store")
        return self


class UsageRecord(BaseModel):
    """逐请求用量账本条目（Step 12 产出）。

    规格 §4 的硬约束：
    - provider 未返回 usage → ``usage_known=False`` 且 token 字段留空，
      ``cost_usd`` 必须为 ``None``——**估算不得写成真实账单**；
    - 不记录 API Key、原始输入或 PII：主体只有 hash。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    principal_hash: str = Field(min_length=1)
    provider: ProviderKind
    model: str = Field(min_length=1)
    category: CallCategory
    #: provider 是否返回了可信 usage。False 时 token/cost 一律为空。
    usage_known: bool = True
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: float = Field(default=0.0, ge=0)
    cache_status: CacheStatus = CacheStatus.INELIGIBLE
    #: 结果状态：``ok`` 或某个稳定错误码。
    result_status: str = "ok"
    #: 429 的 Retry-After（秒），仅限流场景有值。
    retry_after_seconds: float | None = Field(default=None, ge=0)
    retry_count: int = Field(default=0, ge=0)
    recorded_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_usage(self) -> Self:
        if not self.usage_known:
            leaked = {
                name: getattr(self, name)
                for name in ("input_tokens", "output_tokens", "cached_input_tokens", "cost_usd")
                if getattr(self, name) is not None
            }
            if leaked:
                raise ValueError(
                    f"usage_known=False 时禁止填充用量/成本（估算不得当账单）：{sorted(leaked)}"
                )
        if self.result_status != "ok" and self.result_status not in GOVERNANCE_ERROR_CODES:
            raise ValueError(f"result_status 必须是 'ok' 或稳定错误码，得到 {self.result_status!r}")
        return self

    @classmethod
    def unknown_usage(
        cls,
        *,
        request_id: str,
        principal_hash: str,
        provider: ProviderKind,
        model: str,
        category: CallCategory,
        latency_ms: float = 0.0,
    ) -> UsageRecord:
        """provider 未返回 usage 时的账本条目（``result_status=USAGE_UNKNOWN``）。"""
        return cls(
            request_id=request_id,
            principal_hash=principal_hash,
            provider=provider,
            model=model,
            category=category,
            usage_known=False,
            latency_ms=latency_ms,
            result_status=CODE_USAGE_UNKNOWN,
        )


# ---------------------------------------------------------------------------
# 预算策略三态（规格 §4）
# ---------------------------------------------------------------------------


class BudgetPolicy(str, Enum):
    """预算策略。``DEGRADE`` 需显式配置 + 已评测的模型路由，P2.5 默认不启用。"""

    WARN = "warn"
    DEGRADE = "degrade"
    BLOCK = "block"


#: 价格/阈值未经人工确认时的占位：**不得**把知识库示例价格复制为项目事实。
PRICE_UNKNOWN: Literal["unknown"] = "unknown"


# ---------------------------------------------------------------------------
# 人工确认凭证（P7-S7）
# ---------------------------------------------------------------------------


def confirmation_args_hash(args: Mapping[str, Any]) -> str:
    """工具参数的确认摘要：规范化 JSON（键排序、紧凑分隔符）的 sha256 全长。

    确认门在批准时用它绑定"批准的是哪一份参数"，护栏在执行时用同一函数重算——两边口径
    只有这一处。``default=str`` 让不可序列化的值也能得到稳定摘要（模型产出的参数本就是 JSON）。
    """
    payload = json.dumps(
        dict(args), sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ConfirmationGrant(BaseModel):
    """一次人工批准签发的执行凭证（P7-S7）：只能由确认门在合法的批准之后构造。

    绑定三项——工具名、参数摘要、主体 hash——并且只能用一次（护栏在进程内记下已用的
    ``confirmation_id``）。模型只能产出 JSON，传不出这个对象；``ToolRegistry.invoke`` 的
    ``**args`` 是工具参数，所以凭证只走 ``invoke_confirmed`` 的关键字参数，不会与参数撞名。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmation_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=256)
    args_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal_hash: str = Field(min_length=1, max_length=64)
    decided_at: datetime

__all__ = [
    "CODE_BUDGET_EXCEEDED",
    "CODE_CACHE_INVALIDATED",
    "CODE_CAPABILITY_DENIED",
    "CODE_CIRCUIT_OPEN",
    "CODE_GUARDRAIL_BLOCKED",
    "CODE_INPUT_TOO_LARGE",
    "CODE_POLICY_UNAVAILABLE",
    "CODE_PROVIDER_AUTH_FAILED",
    "CODE_PROVIDER_TIMEOUT",
    "CODE_PROVIDER_UNAVAILABLE",
    "CODE_RATE_LIMITED",
    "CODE_SOURCE_TAG_MISSING",
    "CODE_USAGE_UNKNOWN",
    "DANGEROUS_CAPABILITIES",
    "DEFAULT_LOCAL_CAPABILITIES",
    "GOVERNANCE_ERROR_CODES",
    "INSTRUCTION_BEARING_SOURCES",
    "PRICE_UNKNOWN",
    "SOURCE_TRUST_LEVEL",
    "BudgetPolicy",
    "CacheDisposition",
    "CacheStatus",
    "CallCategory",
    "Capability",
    "ConfirmationGrant",
    "ContentEnvelope",
    "GovernanceError",
    "PiiMode",
    "PolicyAction",
    "PolicyDecision",
    "Principal",
    "PrincipalKind",
    "ProviderCall",
    "ProviderKind",
    "RequestContext",
    "SourceTag",
    "SourceTagDowngradeError",
    "UsageRecord",
    "confirmation_args_hash",
    "is_instruction_bearing",
    "trust_level",
]
