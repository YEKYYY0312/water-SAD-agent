"""精确回答缓存的接线层（P2.5 Step 13）。

把 `cache.py` 的资格判定与键构造收敛为**单一入口** :class:`AnswerCacheService`，
供 CLI / harness 在 RAG 路径上使用。分层理由与 `binding.py` 一致：
`cache.py` 是纯机制（不知道本项目的 state 形状），本模块负责把
`WaterAgentState` 风格的结果映射成缓存契约。

**不存在"不传 cache 即绕过"的问题**：本服务是可选增强（缓存缺失只是每次都
真调用），但**资格判定不可绕过** —— `store` 必须传 `StoreEligibility`，
而它只能由 :meth:`AnswerCacheService.eligibility_for` 或
`evaluate_store_eligibility` 产出。
"""
from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

from water_agent.prompting.assembler import PROMPT_TEMPLATE_VERSION
from water_agent.runtime.governance.cache import (
    AnswerCache,
    CachedAnswer,
    CacheKeyFactors,
    InMemoryAnswerCache,
    StoreEligibility,
    all_versions_known,
    evaluate_store_eligibility,
    normalize_query,
    retrieval_params_digest,
    spec_version_digest,
)
from water_agent.runtime.governance.contracts import (
    CacheDisposition,
    CacheStatus,
    CallCategory,
    RequestContext,
)
from water_agent.runtime.governance.corpus_version import (
    combined_corpus_version,
    corpus_version_known,
)
from water_agent.runtime.governance.inventory import TOOL_POLICY_VERSION

_lock = threading.Lock()
_default_cache: AnswerCache | None = None


def default_answer_cache() -> AnswerCache:
    """进程级默认缓存（懒构造单例）。TTL 1h、容量 512 条。"""
    global _default_cache
    with _lock:
        if _default_cache is None:
            _default_cache = InMemoryAnswerCache()
        return _default_cache


def set_default_answer_cache(cache: AnswerCache | None) -> None:
    """替换默认缓存（测试与 CLI 装配用）。传 ``None`` 恢复懒构造。"""
    global _default_cache
    with _lock:
        _default_cache = cache


def spec_statuses_for(source_ids: Sequence[str], version_registry: Any) -> list[tuple[str, str]]:
    """查出引用涉及的规范版本状态。

    未登记的规范返回 ``unknown``（fail closed）。注册表不可用时全部记 ``unknown``。
    """
    statuses: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source_id in source_ids:
        spec = source_id.split()[0] if source_id.split() else source_id
        if spec in seen:
            continue
        seen.add(spec)
        status = "unknown"
        if version_registry is not None:
            getter = getattr(version_registry, "get_status_for_source_id", None)
            if callable(getter):
                try:
                    record = getter(source_id)
                    if isinstance(record, dict):
                        status = str(record.get("status") or "unknown")
                except Exception:  # noqa: BLE001 — 注册表异常按 unknown 处理（fail closed）
                    status = "unknown"
        statuses.append((spec, status))
    return statuses


class AnswerCacheService:
    """RAG 回答的精确缓存服务。

    Args:
        cache: 底层缓存；``None`` 时用进程级默认实例。
        embedding_model: 索引嵌入模型（缓存键的语料/索引版本组成部分）。
        dimensions: 索引维度。
        top_k / recall_top_k / reranker: 检索参数（换参数必须失效）。
        version_registry: 规范版本注册表，用于判定版本是否已核实。
    """

    def __init__(
        self,
        *,
        cache: AnswerCache | None = None,
        embedding_model: str,
        dimensions: int,
        top_k: int,
        recall_top_k: int,
        reranker: str | None = None,
        version_registry: Any = None,
        mcp_server_manifest: str = "",
    ) -> None:
        self._cache = cache if cache is not None else default_answer_cache()
        self._corpus_version = combined_corpus_version(
            embedding_model=embedding_model, dimensions=dimensions
        )
        self._retrieval_params = retrieval_params_digest(
            top_k=top_k, recall_top_k=recall_top_k, reranker=reranker
        )
        self._version_registry = version_registry
        self._mcp_server_manifest = mcp_server_manifest

    @property
    def corpus_version(self) -> str:
        return self._corpus_version

    @property
    def corpus_version_known(self) -> bool:
        """语料版本是否可信。不可信时一切不可缓存（不知道语料就不复用旧答案）。"""
        return corpus_version_known(self._corpus_version)

    def key_factors(
        self,
        *,
        query: str,
        ctx: RequestContext,
        model: str,
        temperature: float,
        category: CallCategory = CallCategory.RAG_GENERATION,
    ) -> CacheKeyFactors:
        """构造缓存键因子。

        规范版本状态用**注册表全量快照**而非本次引用（见 :meth:`_global_spec_digest`）：
        lookup 发生在检索之前，那时还不知道会引用哪些条款；若用"本次引用"，
        查与写的键必然不同，缓存永远命中不了。
        """
        return CacheKeyFactors(
            query_normalized=normalize_query(query),
            principal_scope=f"{ctx.principal.principal_hash}|{ctx.principal.auth_domain}",
            category=category,
            model=model,
            temperature=temperature,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            tool_policy_version=TOOL_POLICY_VERSION,
            corpus_version=self._corpus_version,
            spec_version_digest=self._global_spec_digest(),
            retrieval_params=self._retrieval_params,
            mcp_server_manifest=self._mcp_server_manifest,
        )

    def _global_spec_digest(self) -> str:
        """注册表全量版本状态摘要。

        **用全量而非本次引用**：lookup 发生在检索之前，那时还不知道会引用哪些条款。
        若用"本次引用"，查与写的键必然不同，缓存永远命中不了。
        全量快照同样满足"版本状态变化即失效"的要求，且查写两侧一致。
        """
        if self._version_registry is None:
            return spec_version_digest([])
        lister = getattr(self._version_registry, "list_all", None)
        if not callable(lister):
            return spec_version_digest([])
        try:
            records = lister()
        except Exception:  # noqa: BLE001 — 注册表异常按空摘要处理
            return spec_version_digest([])
        pairs = [
            (str(r.get("spec_id") or ""), str(r.get("status") or "unknown"))
            for r in records
            if isinstance(r, dict)
        ]
        return spec_version_digest(pairs)

    def lookup(
        self,
        *,
        query: str,
        ctx: RequestContext,
        model: str,
        temperature: float,
        category: CallCategory = CallCategory.RAG_GENERATION,
    ) -> tuple[CacheDisposition, CachedAnswer | None]:
        """查缓存。语料版本不可信时直接 ``INELIGIBLE``（不读不写）。"""
        if not self.corpus_version_known:
            return (
                CacheDisposition(
                    status=CacheStatus.INELIGIBLE,
                    cache_key_hash="",
                    reason="corpus_version_unknown",
                ),
                None,
            )
        factors = self.key_factors(
            query=query, ctx=ctx, model=model, temperature=temperature, category=category
        )
        return self._cache.lookup(factors, ctx)

    def eligibility_for(
        self,
        *,
        category: CallCategory,
        temperature: float,
        citation_verified: bool,
        output_guardrails_passed: bool,
        answer: str,
        source_ids: Sequence[str],
        ctx: RequestContext,
        calc_result_present: bool = False,
        contains_redacted_or_blocked: bool = False,
    ) -> StoreEligibility:
        """判定写入资格。规范版本状态由注册表查得，**不由调用方声称**。"""
        statuses = spec_statuses_for(source_ids, self._version_registry)
        return evaluate_store_eligibility(
            category=category,
            temperature=temperature,
            citation_verified=citation_verified,
            output_guardrails_passed=output_guardrails_passed,
            spec_versions_known=all_versions_known(statuses),
            answer=answer,
            contains_redacted_or_blocked=contains_redacted_or_blocked,
            calc_result_present=calc_result_present,
            no_store=ctx.no_store,
        )

    def store(
        self,
        *,
        query: str,
        ctx: RequestContext,
        model: str,
        temperature: float,
        answer: str,
        source_ids: Sequence[str],
        eligibility: StoreEligibility,
        category: CallCategory = CallCategory.RAG_GENERATION,
    ) -> CacheDisposition:
        """按资格写入。语料版本不可信时拒绝写入。"""
        if not self.corpus_version_known:
            return CacheDisposition(
                status=CacheStatus.INELIGIBLE,
                cache_key_hash="",
                reason="corpus_version_unknown",
            )
        factors = self.key_factors(
            query=query, ctx=ctx, model=model, temperature=temperature, category=category
        )
        return self._cache.store(
            factors,
            answer=answer,
            citation_source_ids=source_ids,
            eligibility=eligibility,
            model=model,
        )

    def invalidate(
        self,
        *,
        query: str,
        ctx: RequestContext,
        model: str,
        temperature: float,
        category: CallCategory = CallCategory.RAG_GENERATION,
    ) -> bool:
        """显式失效一条（管理接口）。"""
        if not self.corpus_version_known:
            return False
        return self._cache.invalidate(
            self.key_factors(
                query=query, ctx=ctx, model=model, temperature=temperature, category=category
            )
        )

    def purge(self) -> int:
        """清空缓存（管理接口）。"""
        return self._cache.purge()

    def stats(self) -> Any:
        return self._cache.stats()


__all__ = [
    "AnswerCacheService",
    "default_answer_cache",
    "set_default_answer_cache",
    "spec_statuses_for",
]
