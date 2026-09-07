"""P2.5 精确回答缓存（Step 13）。

**只做精确缓存，不做语义缓存**（规格 §1.2）：规范问答引用要求严格，
语义相似 ≠ 同一设计条件；只有具备独立 gold、误命中率证据和人工批准后，
才能另开 P2.6 变更。

## 缓存资格（全部满足才可写入）

规格 §5 允许缓存的**唯一**对象：`temperature=0`、引用校验通过、所有输出 guardrail
通过的**只读 RAG 最终答案**。以下一律不缓存：

- 计算结果（铁律①：数值必须每次程序化产出）
- docgen 草稿/终稿、审批结果
- 工具错误、被脱敏/阻断的原始内容
- **实时或规范版本未知的结果**

> ⚠️ **当前语料的实际后果**：`data/versions.json` 的 4 本规范状态全为 `unknown`，
> 因此按规格 §5「不缓存版本未知的结果」，**当前任何回答都不满足写入资格**
> （disposition 原因为 `spec_version_unknown`）。这是刻意的 fail closed：
> 人工核实版本状态后缓存自动生效，而不是为提高命中率放宽版本校验。

## 缓存键

必须由下列因子**共同**组成（规格 §5），任一变化即失效：

规范化 query、主体/授权域、模型与温度、prompt/template 版本、工具策略版本、
语料 manifest/索引版本、规范版本状态、检索参数。

**禁止仅用 query 文本跨用户命中** —— `principal_scope` 是键的必需组成部分。

本模块不导入 langchain/langgraph（治理层纯度约束）。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import unicodedata
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from water_agent.runtime.governance.contracts import (
    CacheDisposition,
    CacheStatus,
    CallCategory,
    RequestContext,
)

#: 缓存条目格式版本。**改变条目结构必须改它**（旧条目随即全部失效）。
CACHE_ENTRY_VERSION = "p2.5-step13-v1"

#: 允许进入精确缓存的调用分类。与 `inventory.py` 的 `cache_eligible` 一致。
#: `AGENTIC_LOOP`（多轮工具调用，非幂等）与 `DOCGEN_POLISH`（规格明令禁止）不在其中。
CACHEABLE_CATEGORIES: frozenset[CallCategory] = frozenset(
    {
        CallCategory.RAG_GENERATION,
        CallCategory.JUDGE,
    }
)

# ---------------------------------------------------------------------------
# 不可缓存原因（稳定标识：进 trace 与 governance_p0 断言）
# ---------------------------------------------------------------------------

REASON_OK = "eligible"
REASON_CATEGORY = "category_not_cacheable"
REASON_TEMPERATURE = "temperature_not_zero"
REASON_CITATION = "citation_not_verified"
REASON_GUARDRAIL = "output_guardrail_not_passed"
REASON_SPEC_VERSION_UNKNOWN = "spec_version_unknown"
REASON_EMPTY_ANSWER = "empty_answer"
REASON_REDACTED_OR_BLOCKED = "redacted_or_blocked_content"
REASON_NO_STORE = "request_no_store"
REASON_CALC_PRESENT = "calc_result_present"


@dataclass(frozen=True, slots=True)
class StoreEligibility:
    """写入资格裁定。``reason`` 是稳定标识，非自由文本。"""

    eligible: bool
    reason: str

    @classmethod
    def ok(cls) -> StoreEligibility:
        return cls(eligible=True, reason=REASON_OK)

    @classmethod
    def no(cls, reason: str) -> StoreEligibility:
        return cls(eligible=False, reason=reason)


def evaluate_store_eligibility(
    *,
    category: CallCategory,
    temperature: float,
    citation_verified: bool,
    output_guardrails_passed: bool,
    spec_versions_known: bool,
    answer: str,
    contains_redacted_or_blocked: bool = False,
    calc_result_present: bool = False,
    no_store: bool = False,
) -> StoreEligibility:
    """判定一条回答能否写入精确缓存。

    检查顺序刻意从"最结构性"到"最内容相关"，让 trace 里的原因最具信息量。
    **任一条不满足即拒绝写入**（fail closed）。

    Args:
        category: 调用分类；仅 :data:`CACHEABLE_CATEGORIES` 可缓存。
        temperature: 只有 0 才可缓存（非 0 输出不确定，缓存会抹掉多样性且不可复现）。
        citation_verified: 引用校验是否通过（铁律②）。
        output_guardrails_passed: 输出护栏是否全通过。Step 14 前由调用方按现有
            确定性检查填入；**没有默认 True** —— 忘记传就是不可缓存。
        spec_versions_known: 本次引用涉及的规范版本状态是否全部已核实。
            当前语料全为 unknown → 恒为 False（见模块 docstring）。
        answer: 最终回答；空回答无缓存价值。
        contains_redacted_or_blocked: 内容是否含被脱敏/阻断的部分。
        calc_result_present: 是否携带计算结果（铁律①：计算不进缓存）。
        no_store: 请求是否显式 ``no_store``。
    """
    if no_store:
        return StoreEligibility.no(REASON_NO_STORE)
    if category not in CACHEABLE_CATEGORIES:
        return StoreEligibility.no(REASON_CATEGORY)
    if temperature != 0.0:
        return StoreEligibility.no(REASON_TEMPERATURE)
    if calc_result_present:
        return StoreEligibility.no(REASON_CALC_PRESENT)
    if not citation_verified:
        return StoreEligibility.no(REASON_CITATION)
    if not output_guardrails_passed:
        return StoreEligibility.no(REASON_GUARDRAIL)
    if contains_redacted_or_blocked:
        return StoreEligibility.no(REASON_REDACTED_OR_BLOCKED)
    if not spec_versions_known:
        return StoreEligibility.no(REASON_SPEC_VERSION_UNKNOWN)
    if not answer.strip():
        return StoreEligibility.no(REASON_EMPTY_ANSWER)
    return StoreEligibility.ok()


# ---------------------------------------------------------------------------
# 缓存键
# ---------------------------------------------------------------------------


def normalize_query(query: str) -> str:
    """保守规范化：NFKC + 去首尾空白 + 折叠内部空白。

    **刻意不做大小写折叠、不做同义词/近义处理** —— 那会把不同设计条件合并
    （例如把不同规范号或参数写法视为同一问题），属于语义缓存的滑坡。
    NFKC 处理全角/半角差异（中文输入法常见），折叠空白处理排版差异。
    """
    return " ".join(unicodedata.normalize("NFKC", query).split())


class CacheKeyFactors(BaseModel):
    """缓存键的全部失效因子（规格 §5 逐项对应）。

    任一字段变化 → 键变化 → 旧条目自然失效。因此**新增失效维度就是新增字段**，
    不需要手动清缓存。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 规范化后的 query（`normalize_query`）。
    query_normalized: str = Field(min_length=1)
    #: 主体 + 授权域，形如 ``<principal_hash>|<auth_domain>``。
    #: **必需**：禁止仅用 query 文本跨用户命中。
    principal_scope: str = Field(min_length=1)
    #: 调用分类。legacy RAG（``RAG_GENERATION``）与 agentic（``AGENTIC_LOOP``）的最终
    #: 回答语义不同（单次结构化 JSON vs 多轮工具调用），**不得跨分类互相命中**；
    #: 这也是"新增失效维度必须改契约"的一次实例——分类隔离是缓存键的新因子。
    category: CallCategory = CallCategory.RAG_GENERATION
    model: str = Field(min_length=1)
    temperature: float = Field(ge=0)
    #: 系统提示模板版本（`prompting/assembler.py::PROMPT_TEMPLATE_VERSION`）。
    prompt_template_version: str = Field(min_length=1)
    #: 工具策略版本（`inventory.py::TOOL_POLICY_VERSION`）。
    tool_policy_version: str = Field(min_length=1)
    #: 语料 manifest / 索引版本摘要。条款更新后旧键必须失效。
    corpus_version: str = Field(min_length=1)
    #: 规范版本状态摘要（版本状态变化 → 失效）。
    spec_version_digest: str = Field(min_length=1)
    #: 检索参数摘要（top_k / recall_top_k / reranker 等）。
    retrieval_params: str = Field(min_length=1)
    #: 条目格式版本。
    entry_version: str = Field(default=CACHE_ENTRY_VERSION, min_length=1)

    def cache_key(self) -> str:
        """稳定缓存键（sha256 全长）。

        **键原文永不落盘/入 trace** —— 只暴露 :meth:`key_hash`
        （规格 §5：trace 不含 cache key 原文）。
        """
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def key_hash(self) -> str:
        """键的短哈希，供 trace / 报告引用。"""
        return self.cache_key()[:16]


def retrieval_params_digest(
    *, top_k: int, recall_top_k: int, reranker: str | None = None
) -> str:
    """检索参数摘要。换 rerank / 改召回数必须让缓存失效。"""
    return f"top_k={top_k};recall={recall_top_k};rerank={reranker or 'none'}"


def spec_version_digest(statuses: Sequence[tuple[str, str]]) -> str:
    """规范版本状态摘要。

    Args:
        statuses: ``(spec_id, status)`` 序列。排序后摘要，保证顺序无关。
    """
    if not statuses:
        return "none"
    joined = ";".join(f"{spec}={status}" for spec, status in sorted(statuses))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def all_versions_known(statuses: Sequence[tuple[str, str]]) -> bool:
    """本次引用涉及的规范版本是否**全部**已核实。

    空序列返回 ``False``：无引用的回答不满足"引用校验通过"前提，
    也不该因为"没有未知项"而被判定可缓存（fail closed）。
    """
    if not statuses:
        return False
    return all(status not in ("unknown", "", None) for _spec, status in statuses)


# ---------------------------------------------------------------------------
# 缓存条目
# ---------------------------------------------------------------------------


class CachedAnswer(BaseModel):
    """缓存条目。

    **只存已脱敏的最终回答、已验证引用与必要元数据**（规格 §5）。
    刻意**不存**：原始检索上下文全文、系统提示、用户原始输入、trace 原文。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str = Field(min_length=1)
    #: 已验证的引用 source_id 列表（不存条文全文，避免二次泄漏面）。
    citation_source_ids: tuple[str, ...] = ()
    #: 键的短哈希（自证条目归属；不含键原文）。
    key_hash: str = Field(min_length=1)
    entry_version: str = Field(default=CACHE_ENTRY_VERSION, min_length=1)
    #: 单调时钟写入时刻（TTL 判定用）。
    stored_at_monotonic: float = Field(ge=0)
    #: 写入时的模型（观测用）。
    model: str = ""
    #: 是否已脱敏（Step 14 接入后由护栏置位）。
    redacted: bool = True


@dataclass(frozen=True, slots=True)
class CacheStats:
    """缓存统计（供报告与 Step 15 聚合指标）。"""

    hits: int = 0
    misses: int = 0
    stores: int = 0
    rejected_stores: int = 0
    evictions: int = 0
    expirations: int = 0
    entries: int = 0


# ---------------------------------------------------------------------------
# 缓存抽象与内存实现
# ---------------------------------------------------------------------------


class AnswerCache(ABC):
    """精确回答缓存抽象。实现必须支持 TTL、容量上限与显式失效。"""

    @abstractmethod
    def lookup(self, factors: CacheKeyFactors, ctx: RequestContext) -> tuple[CacheDisposition, CachedAnswer | None]:
        """查缓存。``ctx.no_cache`` 时返回 ``BYPASS`` 且不读。"""

    @abstractmethod
    def store(
        self,
        factors: CacheKeyFactors,
        *,
        answer: str,
        citation_source_ids: Sequence[str],
        eligibility: StoreEligibility,
        model: str = "",
        redacted: bool = True,
    ) -> CacheDisposition:
        """按资格写入。``eligibility.eligible=False`` 时**不写**并回报原因。"""

    @abstractmethod
    def invalidate(self, factors: CacheKeyFactors) -> bool:
        """按键显式失效；返回是否命中并删除。"""

    @abstractmethod
    def purge(self) -> int:
        """清空全部条目（运维/测试）。返回删除条目数。"""

    @abstractmethod
    def stats(self) -> CacheStats:
        """当前统计。"""


class InMemoryAnswerCache(AnswerCache):
    """进程内 LRU + TTL 精确缓存。

    - **TTL**：过期条目在 lookup 时删除并记 MISS（惰性过期，不起后台线程）。
    - **容量**：LRU 淘汰（`OrderedDict` 尾部为最近使用）。
    - **`no_cache`**：不读但仍可写（用于强制刷新）。
    - **`no_store`**：不写（由 :func:`evaluate_store_eligibility` 判定）。
    - **无持久化**：本实现不落盘，因此不存在"运行时缓存目录需删除路径"的问题；
      未来若加持久化 adapter，必须实现 :meth:`purge` 的磁盘清理。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        max_entries: int = 512,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须为正（缓存必须可过期）")
        if max_entries <= 0:
            raise ValueError("max_entries 必须为正（缓存必须有容量上限）")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock or time.monotonic
        self._entries: OrderedDict[str, CachedAnswer] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._stores = 0
        self._rejected = 0
        self._evictions = 0
        self._expirations = 0

    def lookup(
        self, factors: CacheKeyFactors, ctx: RequestContext
    ) -> tuple[CacheDisposition, CachedAnswer | None]:
        key_hash = factors.key_hash()
        if ctx.no_cache:
            # 显式绕过：不读缓存，也不计 miss（不是"未命中"，是"没查"）
            return (
                CacheDisposition(
                    status=CacheStatus.BYPASS, cache_key_hash=key_hash, reason="request_no_cache"
                ),
                None,
            )

        key = factors.cache_key()
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return (
                    CacheDisposition(
                        status=CacheStatus.MISS, cache_key_hash=key_hash, reason="not_found"
                    ),
                    None,
                )
            age = now - entry.stored_at_monotonic
            if age >= self.ttl_seconds:
                del self._entries[key]
                self._expirations += 1
                self._misses += 1
                return (
                    CacheDisposition(
                        status=CacheStatus.MISS, cache_key_hash=key_hash, reason="expired"
                    ),
                    None,
                )
            self._entries.move_to_end(key)  # LRU：命中即刷新
            self._hits += 1
            return (
                CacheDisposition(
                    status=CacheStatus.HIT,
                    cache_key_hash=key_hash,
                    entry_age_seconds=round(age, 6),
                    reason="hit",
                ),
                entry,
            )

    def store(
        self,
        factors: CacheKeyFactors,
        *,
        answer: str,
        citation_source_ids: Sequence[str],
        eligibility: StoreEligibility,
        model: str = "",
        redacted: bool = True,
    ) -> CacheDisposition:
        key_hash = factors.key_hash()
        if not eligibility.eligible:
            with self._lock:
                self._rejected += 1
            return CacheDisposition(
                status=CacheStatus.INELIGIBLE,
                eligible_for_store=False,
                cache_key_hash=key_hash,
                reason=eligibility.reason,
            )

        entry = CachedAnswer(
            answer=answer,
            citation_source_ids=tuple(citation_source_ids),
            key_hash=key_hash,
            stored_at_monotonic=self._clock(),
            model=model,
            redacted=redacted,
        )
        key = factors.cache_key()
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            self._stores += 1
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)  # 淘汰最久未使用
                self._evictions += 1
        return CacheDisposition(
            status=CacheStatus.MISS,  # 本次是 miss 后写入，不是命中
            eligible_for_store=True,
            cache_key_hash=key_hash,
            reason="stored",
        )

    def invalidate(self, factors: CacheKeyFactors) -> bool:
        with self._lock:
            return self._entries.pop(factors.cache_key(), None) is not None

    def purge(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                stores=self._stores,
                rejected_stores=self._rejected,
                evictions=self._evictions,
                expirations=self._expirations,
                entries=len(self._entries),
            )


# ---------------------------------------------------------------------------
# provider prompt cache 观测（只记事实，不推断）
# ---------------------------------------------------------------------------


class PromptCacheSupport(str, Enum):
    """provider prompt cache 的支持状态。

    规格 §5：**不把"静态段在前"误报为缓存命中**。只有 provider 在 usage 中
    真实返回 cached-token 字段才算证据。
    """

    #: provider 返回了 cached-token 字段，且本项目已用真实响应证实。
    VERIFIED = "verified"
    #: 官方文档声明支持，但本项目**尚无真实 usage 证据**（无 Key 环境）。
    DOCUMENTED_UNVERIFIED = "documented_unverified"
    #: provider 明确不支持。
    UNSUPPORTED = "unsupported"
    #: 无从判断。
    UNKNOWN = "unknown"


class PromptCacheObservation(BaseModel):
    """一次调用的 provider prompt cache 观测。

    ``cached_tokens=None`` 表示 provider 未返回该字段 —— 与"命中 0 token"
    是不同的事实，不得混淆。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    support: PromptCacheSupport
    cached_tokens: int | None = Field(default=None, ge=0)
    #: 观测来源字段名（便于回溯 provider 契约变化）。
    source_field: str = ""

    @property
    def has_evidence(self) -> bool:
        """是否有真实命中证据（字段存在且 > 0）。"""
        return self.cached_tokens is not None and self.cached_tokens > 0


def observe_prompt_cache(usage_payload: dict[str, Any]) -> PromptCacheObservation:
    """从 provider usage 提取 prompt cache 观测。

    覆盖两种已文档化的字段形态：
    - OpenAI 兼容：``prompt_tokens_details.cached_tokens``
    - DeepSeek：``prompt_cache_hit_tokens``

    字段缺失 → ``UNKNOWN`` + ``cached_tokens=None``（**不推断、不估算**）。

    ⚠️ 返回 ``DOCUMENTED_UNVERIFIED`` 而非 ``VERIFIED``：两种字段名依官方文档编写，
    但本项目在无 Key 环境下**未用真实响应证实**。有 Key 后抽查确认，再改为 VERIFIED。
    """
    details = usage_payload.get("prompt_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return PromptCacheObservation(
            support=PromptCacheSupport.DOCUMENTED_UNVERIFIED,
            cached_tokens=int(details["cached_tokens"]),
            source_field="prompt_tokens_details.cached_tokens",
        )
    hit = usage_payload.get("prompt_cache_hit_tokens")
    if isinstance(hit, int):
        return PromptCacheObservation(
            support=PromptCacheSupport.DOCUMENTED_UNVERIFIED,
            cached_tokens=hit,
            source_field="prompt_cache_hit_tokens",
        )
    return PromptCacheObservation(support=PromptCacheSupport.UNKNOWN, cached_tokens=None)


__all__ = [
    "CACHEABLE_CATEGORIES",
    "CACHE_ENTRY_VERSION",
    "REASON_CALC_PRESENT",
    "REASON_CATEGORY",
    "REASON_CITATION",
    "REASON_EMPTY_ANSWER",
    "REASON_GUARDRAIL",
    "REASON_NO_STORE",
    "REASON_OK",
    "REASON_REDACTED_OR_BLOCKED",
    "REASON_SPEC_VERSION_UNKNOWN",
    "REASON_TEMPERATURE",
    "AnswerCache",
    "CacheKeyFactors",
    "CacheStats",
    "CachedAnswer",
    "InMemoryAnswerCache",
    "PromptCacheObservation",
    "PromptCacheSupport",
    "StoreEligibility",
    "all_versions_known",
    "evaluate_store_eligibility",
    "normalize_query",
    "observe_prompt_cache",
    "retrieval_params_digest",
    "spec_version_digest",
]
