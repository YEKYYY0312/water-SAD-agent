"""P2.5 治理配置（Step 12）：版本化的限流、重试、熔断、预算与价格。

设计约束（`docs/design/12-p2.5-production-governance.md` §4）：

1. **版本化**：``GovernanceConfig.version`` 进 trace 与 ``UsageRecord`` 的关联字段，
   便于回溯"哪一版配置做出的拒绝"。改阈值必须改版本号。
2. **价格未确认不做硬拒绝**：知识库示例价格**不得**复制为项目事实。
   本项目沿用 `generation/cost.py` 的 DeepSeek 公开单价作为**估算口径**，
   但 ``ModelPrice.confirmed=False`` —— 因此**成本预算默认不启用硬拒绝**，
   只有可直接测量的 token 额度才做硬限制。
3. **预算三态默认 WARN**：``DEGRADE`` 需显式配置 + 已评测的模型路由，P2.5 不启用。
4. **不虚构阈值**：真实 DAU / 月账单 / 命中率均为 unknown，因此额度取
   "单机单人使用下明显宽松、只挡住失控循环"的量级，并在字段注释里说明依据。
   这不是"生产调优值"，是**失控保护**。

本模块不导入 langchain/langgraph/provider SDK（治理层纯度约束）。
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from water_agent.runtime.governance.contracts import (
    BudgetPolicy,
    CallCategory,
    ProviderKind,
)

#: 配置版本。**改任何阈值都要改它**（trace 用它回溯裁定依据）。
#: P7-S8 新增 ``MCP_REMOTE`` 画像（工具调用零重试），递进。
#: P7-S10 ``DEEPSEEK_CHAT`` 画像的语义扩为"OpenAI 兼容对话端点"，超时与价格可由运维覆盖，递进。
#: P7-S12 新增 ``WEB_SEARCH`` 画像（联网搜索：15 s、瞬时错误只重试一次、每主体每分钟 ≤ 20），递进。
#: P7-S13 新增 ``SKILL_REGISTRY`` 画像（运维 CLI 取 skill：20 s、瞬时错误只重试一次、每主体每分钟 ≤ 30），递进。
#: P7-S14 与 S13 整合：保留两种注册表画像，出口总数为 11。
GOVERNANCE_CONFIG_VERSION = "p7-s14-2026-11-02"

#: P7-S10：对话端点（``ProviderKind.DEEPSEEK_CHAT``——名字保留，语义是"OpenAI 兼容对话端点"，见
#: ``generation/endpoints.py``）的运维覆盖。缺省 = 现状（60 s、DeepSeek 公开单价）。
CHAT_TIMEOUT_ENV = "WATER_AGENT_LLM_TIMEOUT_SECONDS"
CHAT_PRICE_ENV = "WATER_AGENT_LLM_PRICE"
DEFAULT_CHAT_TIMEOUT_SECONDS = 60.0
#: 超时上限：再长就不是"慢"而是"挂起"，失控保护要能兜住。
MAX_CHAT_TIMEOUT_SECONDS = 600.0


class RetryPolicy(BaseModel):
    """有界重试策略。

    ``max_attempts`` 含首次尝试（=1 表示不重试）。退避为指数 + 抖动；
    **总等待时间另受 ``RequestContext.deadline_seconds`` 约束**，两者取先到者。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=3, ge=1, le=10)
    base_backoff_seconds: float = Field(default=0.5, ge=0)
    max_backoff_seconds: float = Field(default=8.0, ge=0)
    #: 抖动比例：实际延迟 ∈ [d*(1-jitter), d]，避免多进程重试对齐。
    jitter_ratio: float = Field(default=0.25, ge=0, le=1)

    @model_validator(mode="after")
    def _validate_backoff(self) -> Self:
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds 不得小于 base_backoff_seconds")
        return self

    def backoff_for(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的退避基准（未加抖动，秒）。attempt 从 0 起。"""
        return float(min(self.base_backoff_seconds * (2**attempt), self.max_backoff_seconds))


class CircuitBreakerPolicy(BaseModel):
    """熔断策略：连续可重试失败达阈值即开路；``recovery_seconds`` 后半开探测。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_threshold: int = Field(default=5, ge=1)
    recovery_seconds: float = Field(default=30.0, gt=0)
    #: 半开期允许的探测请求数上限（超出直接拒绝，防半开期打满）。
    half_open_max_calls: int = Field(default=1, ge=1)


class RateLimitPolicy(BaseModel):
    """token bucket 限流策略（按 ``principal_hash + capability/model tier``）。

    额度依据：本项目当前是**单机单人 CLI**（DAU=unknown 但显然是 1）。
    这里的数值不是"生产调优值"，而是**失控保护**——挡住 agentic 循环失控、
    重试风暴、批量 embedding 打满，而不影响正常人工使用。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 桶容量（token）。P5-S50 起附件预算 50,000 字：带两份文件的一次提问实测
    #: 4 次模型调用共 ~7–8 万 input token（agentic 每轮重送系统提示），旧的 200k
    #: 在第 2–3 次提问就被打空（实测 "请求频率超过本地配额限制"）。1M ≈ 连续十几次
    #: 带大附件的提问，仍远低于 daily_token_cap，失控循环照样撞限。
    bucket_capacity_tokens: int = Field(default=1_000_000, ge=1)
    #: 补充速率（token/秒）。5,000/s ≈ 18M token/小时；按人工节奏（一分钟一问）每问可回补
    #: ~30 万，只挡机器失控。
    refill_tokens_per_second: float = Field(default=5_000.0, gt=0)
    #: 每分钟请求数上限。人工提问不可能超过；agentic 循环失控会立刻撞到。
    max_requests_per_minute: int = Field(default=120, ge=1)


#: 批量嵌入（入库）专用限流：`scripts/build_index.py` 会对整个语料库逐块嵌入，
#: 属**合法批量作业**。若沿用 chat 的 120 RPM，正常入库会被自己的限流打断。
#: 这里放宽到分钟级数千请求 —— 仍能挡住无限循环，但不干扰入库。
INGEST_RATE_LIMIT = RateLimitPolicy(
    bucket_capacity_tokens=5_000_000,
    refill_tokens_per_second=50_000.0,
    max_requests_per_minute=6_000,
)


class BudgetConfig(BaseModel):
    """预算配置。

    **成本上限默认 None（不启用）**：价格未经人工确认（见 :class:`ModelPrice`），
    用未确认价格做硬拒绝等于"用估算当账单"。可直接测量的 token 额度才做硬限制。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: BudgetPolicy = BudgetPolicy.WARN
    #: 每日 token 额度（输入+输出）。None = 不启用。
    daily_token_cap: int | None = Field(default=5_000_000, gt=0)
    #: 每日成本上限（USD）。**默认 None：价格未确认，不做成本硬拒绝。**
    daily_cost_cap_usd: float | None = Field(default=None, gt=0)
    #: 告警比例（达到额度的该比例时记 warn 事件）。
    warn_ratio: float = Field(default=0.8, gt=0, le=1)

    @model_validator(mode="after")
    def _validate_degrade_not_enabled(self) -> Self:
        if self.policy is BudgetPolicy.DEGRADE:
            raise ValueError(
                "P2.5 不启用模型降级（BudgetPolicy.DEGRADE）："
                "需先有独立配置、质量评测与审批记录（规格 §4）"
            )
        return self


class ModelPrice(BaseModel):
    """模型单价（USD / 1M token）。

    ``confirmed=False`` 表示**该价格未经人工与账单核对**，只能用于估算记账，
    不得作为硬拒绝依据。本项目沿用 `generation/cost.py` 既有 DeepSeek 公开单价口径。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)
    cached_input_per_1m: float | None = Field(default=None, ge=0)
    #: 是否已与真实账单核对。**False 时成本只作估算**。
    confirmed: bool = False

    def estimate_usd(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
        """估算成本（USD，8 位）。缓存 token 按 cached 单价计（无 cached 单价时按输入价）。"""
        billable_input = max(0, input_tokens - max(0, cached_input_tokens))
        cached_rate = self.cached_input_per_1m if self.cached_input_per_1m is not None else self.input_per_1m
        total = (
            billable_input * self.input_per_1m
            + max(0, cached_input_tokens) * cached_rate
            + max(0, output_tokens) * self.output_per_1m
        ) / 1_000_000
        return round(total, 8)


class ProviderProfile(BaseModel):
    """单个 provider 的可靠性画像：超时 + 重试 + 熔断 + 限流。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = Field(gt=0)
    retry: RetryPolicy = RetryPolicy()
    breaker: CircuitBreakerPolicy = CircuitBreakerPolicy()
    rate_limit: RateLimitPolicy = RateLimitPolicy()


#: DeepSeek 公开单价（沿用 `generation/cost.py` 口径）。**未与账单核对 → confirmed=False**。
_DEEPSEEK_CHAT_PRICE = ModelPrice(
    input_per_1m=0.27,
    output_per_1m=1.10,
    cached_input_per_1m=0.07,
    confirmed=False,
)


def chat_timeout_seconds(getenv: Callable[[str], str | None] = os.getenv) -> float:
    """对话端点超时：``WATER_AGENT_LLM_TIMEOUT_SECONDS``（缺省 60 s，(0, 600]）。写错即响亮失败。"""

    raw = (getenv(CHAT_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_CHAT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{CHAT_TIMEOUT_ENV} 必须是秒数（如 90）") from exc
    if not 0 < value <= MAX_CHAT_TIMEOUT_SECONDS:
        raise ValueError(f"{CHAT_TIMEOUT_ENV} 必须在 (0, {MAX_CHAT_TIMEOUT_SECONDS:g}] 秒之间")
    return value


def chat_price_override(
    getenv: Callable[[str], str | None] = os.getenv,
) -> tuple[str, ModelPrice] | None:
    """``WATER_AGENT_LLM_PRICE=input_per_1m,output_per_1m[,cached_input_per_1m]``（USD / 1M token）。

    价格挂在**当前对话模型名**上（与 ``resolve_chat_endpoint`` 同一解析），``confirmed=False``：只用于
    成本估算显示与软预警，不做硬拒绝（与"未核对价格不做硬上限"同一口径；``daily_cost_cap_usd`` 缺省不启用）。
    未设时返回 ``None``——未登记的模型成本记 ``None``，不猜价。
    """

    raw = (getenv(CHAT_PRICE_ENV) or "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",")]
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"{CHAT_PRICE_ENV} 的格式是 input_per_1m,output_per_1m[,cached_input_per_1m]") from exc
    if len(numbers) not in (2, 3) or any(number < 0 for number in numbers):
        raise ValueError(f"{CHAT_PRICE_ENV} 的格式是 input_per_1m,output_per_1m[,cached_input_per_1m]（非负）")
    from water_agent.generation.endpoints import resolve_chat_endpoint

    model = resolve_chat_endpoint(getenv=getenv).model
    return model, ModelPrice(
        input_per_1m=numbers[0],
        output_per_1m=numbers[1],
        cached_input_per_1m=numbers[2] if len(numbers) == 3 else None,
        confirmed=False,
    )


def _default_prices() -> dict[str, ModelPrice]:
    prices = {
        "deepseek-chat": _DEEPSEEK_CHAT_PRICE,
        "deepseek-reasoner": _DEEPSEEK_CHAT_PRICE,
    }
    override = chat_price_override()
    if override is not None:
        prices[override[0]] = override[1]
    return prices


class GovernanceConfig(BaseModel):
    """治理配置根。按 provider 取画像，按模型名取价格。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default=GOVERNANCE_CONFIG_VERSION, min_length=1)
    budget: BudgetConfig = BudgetConfig()
    profiles: dict[ProviderKind, ProviderProfile] = Field(
        default_factory=lambda: {
            # 60s：沿用 generation/llm.py 既有超时口径；P7-S10 起运维可用
            # WATER_AGENT_LLM_TIMEOUT_SECONDS 覆盖（换了服务商，超时口径未必还合适）。
            ProviderKind.DEEPSEEK_CHAT: ProviderProfile(timeout_seconds=chat_timeout_seconds()),
            # 60s + 6 次重试：沿用 embedding.py 既有口径（当前最健康的一路）。
            # 限流用 INGEST_RATE_LIMIT：入库要对整个语料库逐块嵌入，属合法批量作业。
            ProviderKind.BAILIAN_EMBEDDING: ProviderProfile(
                timeout_seconds=60.0,
                retry=RetryPolicy(max_attempts=7, base_backoff_seconds=1.0, max_backoff_seconds=32.0),
                rate_limit=INGEST_RATE_LIMIT,
            ),
            # rerank 已知 403 AccessDenied（永久错误）；重试次数保持最小。
            ProviderKind.BAILIAN_RERANK: ProviderProfile(
                timeout_seconds=60.0,
                retry=RetryPolicy(max_attempts=2),
            ),
            # P5-S52 视觉审阅：单页实测 plus 39.7 s / flash 18.6 s，120 s 留足余量；
            # 一次上传 ≤4 页、每页一次调用。重试 2 次：视觉调用贵，不做重试风暴。
            ProviderKind.BAILIAN_VISION: ProviderProfile(
                timeout_seconds=120.0,
                retry=RetryPolicy(max_attempts=2),
            ),
            # P7-S8 远程 MCP：**max_attempts=1——工具调用不重试**（MCP 工具可能有副作用，重放即重复执行；
            # 超时 / 连接中断后远端状态未知，交给模型和人判断）。超时由 server 配置的
            # ``call_timeout_seconds`` / ``startup_timeout_seconds`` 逐次决定，这里的值只是画像默认。
            # 熔断按单台 server 计（``ProviderCall.model`` = ``mcp:<server_id>``）；限流额度是失控保护：
            # 一个 run 的 initialize / 通知 / tools/list / DELETE 都算请求，放宽到每分钟 1200。
            ProviderKind.MCP_REMOTE: ProviderProfile(
                timeout_seconds=30.0,
                retry=RetryPolicy(max_attempts=1),
                rate_limit=RateLimitPolicy(max_requests_per_minute=1200),
            ),
            # P7-S12 联网搜索：单次 15 s；**只对瞬时错误（408/429/5xx/超时/连接错误）重试一次**，401/403 零重试且不计熔断
            # （沿用 gateway 既有分类，换密钥才能解决）；熔断按 provider 主机分桶（``ProviderCall.model`` =
            # ``web_search:<host>``，一台服务连续失败开路不影响另一台）。限流是**失控保护**量级——每主体每分钟 ≤ 20 次
            # 搜索，足以挡住模型循环或被投毒文档驱动的搜索风暴；**不是生产调优值**（DAU / 搜索服务配额均未知）。
            # 搜索不返回 token 用量，usage 记未知，不估算成本。
            ProviderKind.WEB_SEARCH: ProviderProfile(
                timeout_seconds=15.0,
                retry=RetryPolicy(max_attempts=2),
                rate_limit=RateLimitPolicy(max_requests_per_minute=20),
            ),
            # P7-S13 skill 注册表：只有运维 CLI 在主机 shell 上手动触发（一次 fetch = index + SKILL.md 两个 GET）。单次 20 s
            # （SKILL.md ≤ 64 KiB，注册表可能是慢速内网静态站）；**只对瞬时错误重试一次**，401/403 零重试且不计熔断；
            # 熔断按注册表主机分桶（``ProviderCall.model`` = ``skill_registry:<host>``）。限流是失控保护——每主体每分钟 ≤ 30
            # 次，足以挡住脚本循环，不是生产调优值。注册表不返回 token 用量，usage 记未知，不估算成本。
            ProviderKind.SKILL_REGISTRY: ProviderProfile(
                timeout_seconds=20.0,
                retry=RetryPolicy(max_attempts=2),
                rate_limit=RateLimitPolicy(max_requests_per_minute=30),
            ),
            # P7-S14 MCP 目录：与联网搜索同构——单次 15 s，只对瞬时错误重试一次，401/403 零重试且不计熔断；熔断按目录主机
            # 分桶（``ProviderCall.model`` = ``mcp_registry:<host>``）。限流是**失控保护**量级：每个管理员主体每分钟 ≤ 20 次
            # 目录读取（一次搜索最多 5 页），不是生产调优值。目录不返回 token 用量，usage 记未知。
            ProviderKind.MCP_REGISTRY: ProviderProfile(
                timeout_seconds=15.0,
                retry=RetryPolicy(max_attempts=2),
                rate_limit=RateLimitPolicy(max_requests_per_minute=20),
            ),
        }
    )
    prices: dict[str, ModelPrice] = Field(default_factory=_default_prices)
    #: 各调用分类的默认输出上限（token）。None = 不设限（由 provider 默认裁定）。
    max_output_tokens: dict[CallCategory, int | None] = Field(default_factory=dict)

    def profile_for(self, provider: ProviderKind) -> ProviderProfile:
        """取 provider 画像；未配置 fail closed（抛 ``KeyError``，不给"默认宽松"）。"""
        try:
            return self.profiles[provider]
        except KeyError:
            raise KeyError(
                f"provider {provider.value!r} 未配置 ProviderProfile；"
                "新增 provider 必须先配置超时/重试/熔断/限流（fail closed）"
            ) from None

    def price_for(self, model: str) -> ModelPrice | None:
        """取模型价格；未登记返回 ``None`` → 成本记为 ``None``（不猜价）。"""
        return self.prices.get(model)


#: 默认配置单例。测试与调用方可构造自己的实例覆盖。
DEFAULT_GOVERNANCE_CONFIG = GovernanceConfig()

__all__ = [
    "CHAT_PRICE_ENV",
    "CHAT_TIMEOUT_ENV",
    "DEFAULT_GOVERNANCE_CONFIG",
    "GOVERNANCE_CONFIG_VERSION",
    "BudgetConfig",
    "CircuitBreakerPolicy",
    "GovernanceConfig",
    "ModelPrice",
    "ProviderProfile",
    "RateLimitPolicy",
    "RetryPolicy",
    "chat_price_override",
    "chat_timeout_seconds",
]
