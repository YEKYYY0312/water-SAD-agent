"""P2.5 Step 13 测试：精确回答缓存。

覆盖规格 §5「测试与验收」全部条目：
同一安全请求第二次命中且不发 provider；TTL 到期、manifest/prompt/policy/权限域变更
均 miss；不同主体不可串数据；`no_store` 不落盘；被拒绝或引用失败的结果永不进入缓存；
trace 不含 cache key 原文或 PII；对任一失效因子改动，断言调用次数增加。

**关于真实语料**：`data/versions.json` 的 4 本规范已于 2026-09-14 核实为 `current`
（提交 `b3cbf09`，来源均为 www.mohurd.gov.cn），因此真实语料**现在可以进缓存**。
这正是 `TestRealCorpusCurrentlyIneligible` 当初预留的「解锁缓存」信号，该类已随之
翻转为 `TestRealCorpusEligibility`。机制层测试仍使用版本已核实的 fake 注册表 ——
测的是机制，不是放宽校验。

全部离线，不发起任何远程调用。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from water_agent.ingestion.version_registry import get_version_registry
from water_agent.prompting.assembler import PROMPT_TEMPLATE_VERSION
from water_agent.runtime.governance.binding import local_request_context
from water_agent.runtime.governance.cache import (
    CACHEABLE_CATEGORIES,
    REASON_CALC_PRESENT,
    REASON_CATEGORY,
    REASON_CITATION,
    REASON_EMPTY_ANSWER,
    REASON_GUARDRAIL,
    REASON_NO_STORE,
    REASON_OK,
    REASON_REDACTED_OR_BLOCKED,
    REASON_SPEC_VERSION_UNKNOWN,
    REASON_TEMPERATURE,
    CachedAnswer,
    CacheKeyFactors,
    InMemoryAnswerCache,
    PromptCacheSupport,
    StoreEligibility,
    all_versions_known,
    evaluate_store_eligibility,
    normalize_query,
    observe_prompt_cache,
    retrieval_params_digest,
    spec_version_digest,
)
from water_agent.runtime.governance.cache_service import (
    AnswerCacheService,
    spec_statuses_for,
)
from water_agent.runtime.governance.contracts import (
    CacheStatus,
    CallCategory,
    Principal,
    RequestContext,
)
from water_agent.runtime.governance.corpus_version import (
    CORPUS_VERSION_UNKNOWN,
    combined_corpus_version,
    corpus_version,
    corpus_version_known,
)
from water_agent.runtime.governance.inventory import TOOL_POLICY_VERSION


class _Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeRegistry:
    """版本注册表替身。真实注册表当前全 unknown，故机制测试用它。"""

    def __init__(self, statuses: dict[str, str]) -> None:
        self._statuses = statuses

    def list_all(self) -> list[dict[str, str]]:
        return [{"spec_id": k, "status": v} for k, v in self._statuses.items()]

    def get_status_for_source_id(self, source_id: str) -> dict[str, str]:
        spec = source_id.split()[0] if source_id.split() else source_id
        return {"status": self._statuses.get(spec, "unknown")}


_KNOWN_REGISTRY = _FakeRegistry({"GB50015-2019": "current", "GB50015": "current"})
_UNKNOWN_REGISTRY = _FakeRegistry({"GB50015-2019": "unknown", "GB50015": "unknown"})

_QUERY = "给水管道设计秒流量如何确定？"
_SOURCES = ("GB50015-2019 3.1.1",)


def _ctx(thread: str = "t-1", **kwargs: object) -> RequestContext:
    return local_request_context(thread_id=thread, **kwargs)  # type: ignore[arg-type]


def _factors(**overrides: object) -> CacheKeyFactors:
    base: dict[str, object] = {
        "query_normalized": normalize_query(_QUERY),
        "principal_scope": "hash-a|local",
        "model": "deepseek-chat",
        "temperature": 0.0,
        "prompt_template_version": "pv1",
        "tool_policy_version": "tv1",
        "corpus_version": "cv1",
        "spec_version_digest": "sv1",
        "retrieval_params": "top_k=5;recall=20;rerank=none",
    }
    base.update(overrides)
    return CacheKeyFactors(**base)  # type: ignore[arg-type]


def _service(
    *,
    cache: InMemoryAnswerCache | None = None,
    registry: object = _KNOWN_REGISTRY,
    top_k: int = 5,
    recall_top_k: int = 20,
    reranker: str | None = None,
    mcp_server_manifest: str = "",
) -> AnswerCacheService:
    return AnswerCacheService(
        cache=cache if cache is not None else InMemoryAnswerCache(),
        embedding_model="qwen3.7-text-embedding",
        dimensions=512,
        top_k=top_k,
        recall_top_k=recall_top_k,
        reranker=reranker,
        version_registry=registry,
        mcp_server_manifest=mcp_server_manifest,
    )


def _eligible() -> StoreEligibility:
    return StoreEligibility.ok()


# ---------------------------------------------------------------------------


class TestNoSemanticCache:
    """规格 §1.2：不做语义缓存。"""

    def test_normalize_is_conservative(self) -> None:
        """只做 NFKC + 空白折叠，**不做大小写折叠/同义处理**。"""
        assert normalize_query("  给水  管道　设计 ") == "给水 管道 设计"
        # 大小写不折叠：GB 与 gb 是不同的规范号写法，不得合并
        assert normalize_query("GB50015") != normalize_query("gb50015")

    def test_paraphrase_does_not_hit(self) -> None:
        """近义问法不得命中 —— 语义相似 ≠ 同一设计条件。"""
        cache = InMemoryAnswerCache()
        cache.store(_factors(), answer="答案", citation_source_ids=_SOURCES,
                    eligibility=_eligible())
        other = _factors(query_normalized=normalize_query("给水管道的设计秒流量怎么算"))
        disposition, entry = cache.lookup(other, _ctx())
        assert disposition.status is CacheStatus.MISS
        assert entry is None

    def test_cache_module_has_no_embedding_dependency(self) -> None:
        """语义缓存需要 embedding；本模块不得引入它。"""
        src = (
            Path(__file__).resolve().parents[1]
            / "src/water_agent/runtime/governance/cache.py"
        ).read_text(encoding="utf-8")
        for banned in ("embed", "similarity", "cosine", "vector"):
            assert banned not in src.lower(), f"cache.py 不得涉及 {banned}（语义缓存滑坡）"


class TestHitAndProviderCallCount:
    """规格 §5：相同请求第二次命中，且第二次 provider 调用数为 0。"""

    def test_second_identical_request_hits_without_provider_call(self) -> None:
        cache = InMemoryAnswerCache()
        svc = _service(cache=cache)
        ctx = _ctx()
        provider_calls = {"n": 0}

        def ask() -> str:
            _disposition, entry = svc.lookup(
                query=_QUERY, ctx=ctx, model="deepseek-chat", temperature=0.0
            )
            if entry is not None:
                return entry.answer
            provider_calls["n"] += 1
            answer = "设计秒流量应按用水定额确定。"
            eligibility = svc.eligibility_for(
                category=CallCategory.RAG_GENERATION,
                temperature=0.0,
                citation_verified=True,
                output_guardrails_passed=True,
                answer=answer,
                source_ids=_SOURCES,
                ctx=ctx,
            )
            assert eligibility.eligible, eligibility.reason
            svc.store(
                query=_QUERY, ctx=ctx, model="deepseek-chat", temperature=0.0,
                answer=answer, source_ids=_SOURCES, eligibility=eligibility,
            )
            return answer

        first = ask()
        assert provider_calls["n"] == 1
        second = ask()
        assert second == first
        assert provider_calls["n"] == 1  # 第二次未发 provider

    def test_hit_reports_entry_age(self) -> None:
        clock = _Clock()
        cache = InMemoryAnswerCache(ttl_seconds=100.0, clock=clock)
        cache.store(_factors(), answer="A", citation_source_ids=_SOURCES,
                    eligibility=_eligible())
        clock.advance(7.5)
        disposition, entry = cache.lookup(_factors(), _ctx())
        assert disposition.status is CacheStatus.HIT
        assert disposition.entry_age_seconds == pytest.approx(7.5)
        assert entry is not None and entry.answer == "A"

    def test_stats_track_hits_and_misses(self) -> None:
        cache = InMemoryAnswerCache()
        cache.lookup(_factors(), _ctx())
        cache.store(_factors(), answer="A", citation_source_ids=_SOURCES,
                    eligibility=_eligible())
        cache.lookup(_factors(), _ctx())
        stats = cache.stats()
        assert stats.misses == 1 and stats.hits == 1 and stats.stores == 1
        assert stats.entries == 1


class TestInvalidationFactors:
    """规格 §5：任一失效因子改动 → miss（调用次数增加）。"""

    @pytest.mark.parametrize(
        "override",
        [
            {"query_normalized": "别的问题"},
            {"principal_scope": "hash-b|local"},
            {"model": "deepseek-reasoner"},
            {"temperature": 0.7},
            {"prompt_template_version": "pv2"},
            {"tool_policy_version": "tv2"},
            {"corpus_version": "cv2"},
            {"spec_version_digest": "sv2"},
            {"retrieval_params": "top_k=10;recall=20;rerank=none"},
            {"entry_version": "other"},
        ],
    )
    def test_each_factor_change_causes_miss(self, override: dict) -> None:
        cache = InMemoryAnswerCache()
        cache.store(_factors(), answer="A", citation_source_ids=_SOURCES,
                    eligibility=_eligible())
        disposition, entry = cache.lookup(_factors(**override), _ctx())
        assert disposition.status is CacheStatus.MISS
        assert entry is None

    def test_key_factors_forbid_extra_field(self) -> None:
        """新增失效维度必须改契约，不能靠额外字段偷偷混入。"""
        with pytest.raises(Exception, match="[Ee]xtra"):
            CacheKeyFactors(  # type: ignore[call-arg]
                query_normalized="q", principal_scope="p", model="m", temperature=0.0,
                prompt_template_version="a", tool_policy_version="b",
                corpus_version="c", spec_version_digest="d", retrieval_params="e",
                sneaky="x",
            )

    def test_retrieval_param_change_invalidates_via_service(self) -> None:
        """换 top_k / rerank 必须让缓存失效（服务层口径）。"""
        cache = InMemoryAnswerCache()
        ctx = _ctx()
        svc_a = _service(cache=cache, top_k=5)
        svc_a.store(query=_QUERY, ctx=ctx, model="m", temperature=0.0, answer="A",
                    source_ids=_SOURCES, eligibility=_eligible())
        svc_b = _service(cache=cache, top_k=10)
        disposition, entry = svc_b.lookup(query=_QUERY, ctx=ctx, model="m", temperature=0.0)
        assert disposition.status is CacheStatus.MISS and entry is None

    def test_reranker_change_invalidates(self) -> None:
        cache = InMemoryAnswerCache()
        ctx = _ctx()
        _service(cache=cache, reranker=None).store(
            query=_QUERY, ctx=ctx, model="m", temperature=0.0, answer="A",
            source_ids=_SOURCES, eligibility=_eligible())
        disposition, _ = _service(cache=cache, reranker="bailian").lookup(
            query=_QUERY, ctx=ctx, model="m", temperature=0.0)
        assert disposition.status is CacheStatus.MISS

    def test_spec_version_status_change_invalidates(self) -> None:
        """版本状态由 unknown 变 current → 键变 → 旧条目失效。"""
        cache = InMemoryAnswerCache()
        ctx = _ctx()
        _service(cache=cache, registry=_UNKNOWN_REGISTRY).key_factors(
            query=_QUERY, ctx=ctx, model="m", temperature=0.0)
        a = _service(cache=cache, registry=_UNKNOWN_REGISTRY)
        b = _service(cache=cache, registry=_KNOWN_REGISTRY)
        assert (
            a.key_factors(query=_QUERY, ctx=ctx, model="m", temperature=0.0).cache_key()
            != b.key_factors(query=_QUERY, ctx=ctx, model="m", temperature=0.0).cache_key()
        )

    def test_category_participates_in_key(self) -> None:
        """legacy（RAG_GENERATION）与 agentic（AGENTIC_LOOP）不得跨分类互相命中。"""
        cache = InMemoryAnswerCache()
        ctx = _ctx()
        svc = _service(cache=cache)
        # legacy 写入（分类 RAG_GENERATION）
        svc.store(
            query=_QUERY, ctx=ctx, model="m", temperature=0.0, answer="legacy 答案",
            source_ids=_SOURCES, eligibility=_eligible(),
            category=CallCategory.RAG_GENERATION,
        )
        # agentic 同 query/model/temperature/principal 也不得命中
        disposition, entry = svc.lookup(
            query=_QUERY, ctx=ctx, model="m", temperature=0.0,
            category=CallCategory.AGENTIC_LOOP,
        )
        assert disposition.status is CacheStatus.MISS
        assert entry is None
        # legacy 自身仍命中
        hit, entry = svc.lookup(
            query=_QUERY, ctx=ctx, model="m", temperature=0.0,
            category=CallCategory.RAG_GENERATION,
        )
        assert hit.status is CacheStatus.HIT
        assert entry is not None and entry.answer == "legacy 答案"


class TestCrossPrincipalIsolation:
    """规格 §5：禁止仅用 query 文本跨用户命中。"""

    def test_different_principal_cannot_read(self) -> None:
        cache = InMemoryAnswerCache()
        svc = _service(cache=cache)
        svc.store(query=_QUERY, ctx=_ctx("alice"), model="m", temperature=0.0,
                  answer="alice 的答案", source_ids=_SOURCES, eligibility=_eligible())
        disposition, entry = svc.lookup(query=_QUERY, ctx=_ctx("bob"), model="m",
                                        temperature=0.0)
        assert disposition.status is CacheStatus.MISS
        assert entry is None

    def test_same_principal_hits(self) -> None:
        cache = InMemoryAnswerCache()
        svc = _service(cache=cache)
        svc.store(query=_QUERY, ctx=_ctx("alice"), model="m", temperature=0.0,
                  answer="A", source_ids=_SOURCES, eligibility=_eligible())
        disposition, entry = svc.lookup(query=_QUERY, ctx=_ctx("alice"), model="m",
                                        temperature=0.0)
        assert disposition.status is CacheStatus.HIT
        assert entry is not None and entry.answer == "A"

    def test_auth_domain_participates_in_scope(self) -> None:
        """授权域不同不得互串（P4 多租户的前置保证）。"""
        cache = InMemoryAnswerCache()
        a = Principal.authenticated(subject="u1", auth_domain="tenant-a",
                                    capabilities=frozenset())
        b = Principal.authenticated(subject="u1", auth_domain="tenant-b",
                                    capabilities=frozenset())
        svc = _service(cache=cache)
        ctx_a = RequestContext(principal=a, request_id="r1")
        ctx_b = RequestContext(principal=b, request_id="r2")
        svc.store(query=_QUERY, ctx=ctx_a, model="m", temperature=0.0, answer="A",
                  source_ids=_SOURCES, eligibility=_eligible())
        disposition, _ = svc.lookup(query=_QUERY, ctx=ctx_b, model="m", temperature=0.0)
        assert disposition.status is CacheStatus.MISS


class TestTTLAndCapacity:
    def test_expired_entry_is_miss_and_removed(self) -> None:
        clock = _Clock()
        cache = InMemoryAnswerCache(ttl_seconds=10.0, clock=clock)
        cache.store(_factors(), answer="A", citation_source_ids=_SOURCES,
                    eligibility=_eligible())
        clock.advance(10.0)  # 恰好到期即算过期
        disposition, entry = cache.lookup(_factors(), _ctx())
        assert disposition.status is CacheStatus.MISS
        assert disposition.reason == "expired"
        assert entry is None
        assert cache.stats().entries == 0
        assert cache.stats().expirations == 1

    def test_entry_within_ttl_hits(self) -> None:
        clock = _Clock()
        cache = InMemoryAnswerCache(ttl_seconds=10.0, clock=clock)
        cache.store(_factors(), answer="A", citation_source_ids=_SOURCES,
                    eligibility=_eligible())
        clock.advance(9.999)
        assert cache.lookup(_factors(), _ctx())[0].status is CacheStatus.HIT

    def test_lru_eviction_at_capacity(self) -> None:
        cache = InMemoryAnswerCache(max_entries=2)
        for i in range(3):
            cache.store(_factors(query_normalized=f"q{i}"), answer=f"A{i}",
                        citation_source_ids=_SOURCES, eligibility=_eligible())
        assert cache.stats().entries == 2
        assert cache.stats().evictions == 1
        # q0 最久未使用，已被淘汰
        assert cache.lookup(_factors(query_normalized="q0"), _ctx())[0].status is CacheStatus.MISS
        assert cache.lookup(_factors(query_normalized="q2"), _ctx())[0].status is CacheStatus.HIT

    def test_hit_refreshes_lru_position(self) -> None:
        cache = InMemoryAnswerCache(max_entries=2)
        cache.store(_factors(query_normalized="q0"), answer="A0",
                    citation_source_ids=_SOURCES, eligibility=_eligible())
        cache.store(_factors(query_normalized="q1"), answer="A1",
                    citation_source_ids=_SOURCES, eligibility=_eligible())
        cache.lookup(_factors(query_normalized="q0"), _ctx())  # q0 变为最近使用
        cache.store(_factors(query_normalized="q2"), answer="A2",
                    citation_source_ids=_SOURCES, eligibility=_eligible())
        assert cache.lookup(_factors(query_normalized="q0"), _ctx())[0].status is CacheStatus.HIT
        assert cache.lookup(_factors(query_normalized="q1"), _ctx())[0].status is CacheStatus.MISS

    def test_ttl_and_capacity_must_be_positive(self) -> None:
        """缓存必须可过期、必须有容量上限。"""
        with pytest.raises(ValueError, match="必须为正"):
            InMemoryAnswerCache(ttl_seconds=0)
        with pytest.raises(ValueError, match="必须为正"):
            InMemoryAnswerCache(max_entries=0)

    def test_explicit_invalidate_and_purge(self) -> None:
        cache = InMemoryAnswerCache()
        cache.store(_factors(), answer="A", citation_source_ids=_SOURCES,
                    eligibility=_eligible())
        assert cache.invalidate(_factors()) is True
        assert cache.invalidate(_factors()) is False  # 幂等
        cache.store(_factors(), answer="A", citation_source_ids=_SOURCES,
                    eligibility=_eligible())
        assert cache.purge() == 1
        assert cache.stats().entries == 0


class TestNoCacheAndNoStore:
    def test_no_cache_bypasses_read_but_allows_write(self) -> None:
        cache = InMemoryAnswerCache()
        cache.store(_factors(), answer="A", citation_source_ids=_SOURCES,
                    eligibility=_eligible())
        ctx = _ctx()
        bypass_ctx = RequestContext(principal=ctx.principal, request_id="r", no_cache=True)
        disposition, entry = cache.lookup(_factors(), bypass_ctx)
        assert disposition.status is CacheStatus.BYPASS
        assert entry is None
        # BYPASS 不计 miss（是"没查"，不是"未命中"）
        assert cache.stats().misses == 0

    def test_no_store_prevents_write(self) -> None:
        cache = InMemoryAnswerCache()
        svc = _service(cache=cache)
        ctx = RequestContext(
            principal=Principal.local("t"), request_id="r", no_store=True
        )
        eligibility = svc.eligibility_for(
            category=CallCategory.RAG_GENERATION, temperature=0.0,
            citation_verified=True, output_guardrails_passed=True, answer="A",
            source_ids=_SOURCES, ctx=ctx,
        )
        assert eligibility.eligible is False
        assert eligibility.reason == REASON_NO_STORE
        disposition = svc.store(query=_QUERY, ctx=ctx, model="m", temperature=0.0,
                                answer="A", source_ids=_SOURCES, eligibility=eligibility)
        assert disposition.status is CacheStatus.INELIGIBLE
        assert cache.stats().entries == 0

    def test_bypass_disposition_cannot_claim_store_eligible(self) -> None:
        """契约层保证：BYPASS/INELIGIBLE 不得声明可写。"""
        from water_agent.runtime.governance.contracts import CacheDisposition

        with pytest.raises(Exception, match="不得同时声明"):
            CacheDisposition(status=CacheStatus.BYPASS, eligible_for_store=True)


class TestStoreEligibility:
    """规格 §5：被拒绝或引用失败的结果**永不**进入缓存。"""

    def _kwargs(self, **overrides: object) -> dict:
        base: dict = {
            "category": CallCategory.RAG_GENERATION,
            "temperature": 0.0,
            "citation_verified": True,
            "output_guardrails_passed": True,
            "spec_versions_known": True,
            "answer": "答案",
        }
        base.update(overrides)
        return base

    def test_all_conditions_met_is_eligible(self) -> None:
        result = evaluate_store_eligibility(**self._kwargs())
        assert result.eligible is True and result.reason == REASON_OK

    @pytest.mark.parametrize(
        ("override", "expected_reason"),
        [
            ({"citation_verified": False}, REASON_CITATION),
            ({"output_guardrails_passed": False}, REASON_GUARDRAIL),
            ({"temperature": 0.7}, REASON_TEMPERATURE),
            ({"spec_versions_known": False}, REASON_SPEC_VERSION_UNKNOWN),
            ({"answer": "   "}, REASON_EMPTY_ANSWER),
            ({"category": CallCategory.DOCGEN_POLISH}, REASON_CATEGORY),
            ({"category": CallCategory.AGENTIC_LOOP}, REASON_CATEGORY),
            ({"calc_result_present": True}, REASON_CALC_PRESENT),
            ({"contains_redacted_or_blocked": True}, REASON_REDACTED_OR_BLOCKED),
            ({"no_store": True}, REASON_NO_STORE),
        ],
    )
    def test_each_violation_rejected_with_stable_reason(
        self, override: dict, expected_reason: str
    ) -> None:
        result = evaluate_store_eligibility(**self._kwargs(**override))
        assert result.eligible is False
        assert result.reason == expected_reason

    def test_rejected_store_does_not_write(self) -> None:
        cache = InMemoryAnswerCache()
        disposition = cache.store(
            _factors(), answer="A", citation_source_ids=_SOURCES,
            eligibility=StoreEligibility.no(REASON_CITATION),
        )
        assert disposition.status is CacheStatus.INELIGIBLE
        assert disposition.eligible_for_store is False
        assert disposition.reason == REASON_CITATION
        assert cache.stats().entries == 0
        assert cache.stats().rejected_stores == 1
        # 且后续 lookup 确实取不到
        assert cache.lookup(_factors(), _ctx())[1] is None

    def test_docgen_and_agentic_never_cacheable(self) -> None:
        """与 inventory 的 cache_eligible 一致（规格 §5 禁止项）。"""
        assert CallCategory.DOCGEN_POLISH not in CACHEABLE_CATEGORIES
        assert CallCategory.AGENTIC_LOOP not in CACHEABLE_CATEGORIES
        assert CallCategory.RAG_GENERATION in CACHEABLE_CATEGORIES

    def test_guardrail_flag_has_no_permissive_default(self) -> None:
        """忘记传 output_guardrails_passed 就该报错，而不是默认放行。"""
        with pytest.raises(TypeError):
            evaluate_store_eligibility(  # type: ignore[call-arg]
                category=CallCategory.RAG_GENERATION, temperature=0.0,
                citation_verified=True, spec_versions_known=True, answer="A",
            )

    def test_all_versions_known_empty_is_false(self) -> None:
        """无引用不得因"没有未知项"而判可缓存（fail closed）。"""
        assert all_versions_known([]) is False
        assert all_versions_known([("A", "current")]) is True
        assert all_versions_known([("A", "current"), ("B", "unknown")]) is False


class TestNoLeakInTraceOrEntry:
    """规格 §5：trace 不含 cache key 原文或 PII。"""

    def test_disposition_carries_hash_not_key(self) -> None:
        factors = _factors()
        disposition, _ = InMemoryAnswerCache().lookup(factors, _ctx())
        assert disposition.cache_key_hash == factors.key_hash()
        assert disposition.cache_key_hash != factors.cache_key()
        assert len(disposition.cache_key_hash) == 16

    def test_disposition_json_has_no_query_or_subject(self) -> None:
        pii_query = "业主张三，手机 13800138000，问设计秒流量"
        svc = _service()
        ctx = _ctx("secret-thread")
        disposition, _ = svc.lookup(query=pii_query, ctx=ctx, model="m", temperature=0.0)
        dumped = disposition.model_dump_json()
        for leak in ("张三", "13800138000", "secret-thread", pii_query):
            assert leak not in dumped

    def test_cached_entry_stores_no_context_or_prompt(self) -> None:
        """条目只存最终回答 + 已验证引用 ID，不存条文全文/系统提示/用户原文。"""
        forbidden = {"context", "prompt", "system_prompt", "messages", "query", "raw"}
        assert not (forbidden & set(CachedAnswer.model_fields))
        assert set(CachedAnswer.model_fields) >= {"answer", "citation_source_ids", "key_hash"}

    def test_entry_records_only_source_ids(self) -> None:
        cache = InMemoryAnswerCache()
        cache.store(_factors(), answer="A", citation_source_ids=_SOURCES,
                    eligibility=_eligible())
        _, entry = cache.lookup(_factors(), _ctx())
        assert entry is not None
        assert entry.citation_source_ids == _SOURCES
        assert entry.redacted is True


class TestCorpusVersion:
    def test_real_manifest_yields_known_version(self) -> None:
        version = combined_corpus_version(
            embedding_model="qwen3.7-text-embedding", dimensions=512
        )
        assert corpus_version_known(version) is True
        assert "qwen3.7-text-embedding__512" in version

    def test_missing_manifest_is_unknown(self, tmp_path: Path) -> None:
        assert corpus_version(tmp_path / "nope.json") == CORPUS_VERSION_UNKNOWN

    def test_malformed_manifest_is_unknown(self, tmp_path: Path) -> None:
        bad = tmp_path / "manifest.json"
        bad.write_text("{not json", encoding="utf-8")
        assert corpus_version(bad) == CORPUS_VERSION_UNKNOWN

    def test_content_change_changes_version(self, tmp_path: Path) -> None:
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text(json.dumps({"included_specs": ["GB50015"], "records_included": 10}),
                     encoding="utf-8")
        b.write_text(json.dumps({"included_specs": ["GB50015"], "records_included": 11}),
                     encoding="utf-8")
        assert corpus_version(a) != corpus_version(b)

    def test_timestamp_only_change_does_not_change_version(self, tmp_path: Path) -> None:
        """重建但内容未变时不应无谓清空缓存。"""
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        payload = {"included_specs": ["GB50015"], "records_included": 10}
        a.write_text(json.dumps({**payload, "created_at": "2026-01-01"}), encoding="utf-8")
        b.write_text(json.dumps({**payload, "created_at": "2026-09-06"}), encoding="utf-8")
        assert corpus_version(a) == corpus_version(b)

    def test_index_dimension_change_changes_version(self) -> None:
        assert combined_corpus_version(
            embedding_model="m", dimensions=512
        ) != combined_corpus_version(embedding_model="m", dimensions=768)

    def test_unknown_corpus_blocks_read_and_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """语料版本不可判定 → 不读不写（不知道语料就不复用旧答案）。"""
        import water_agent.runtime.governance.cache_service as mod

        monkeypatch.setattr(
            mod, "combined_corpus_version", lambda **_kw: f"{CORPUS_VERSION_UNKNOWN}|x"
        )
        svc = _service()
        assert svc.corpus_version_known is False
        disposition, entry = svc.lookup(query=_QUERY, ctx=_ctx(), model="m", temperature=0.0)
        assert disposition.status is CacheStatus.INELIGIBLE
        assert disposition.reason == "corpus_version_unknown"
        assert entry is None
        stored = svc.store(query=_QUERY, ctx=_ctx(), model="m", temperature=0.0,
                           answer="A", source_ids=_SOURCES, eligibility=_eligible())
        assert stored.status is CacheStatus.INELIGIBLE


class TestRealCorpusEligibility:
    """真实语料的当前事实：4 本规范已核实为 ``current`` → **缓存已解锁**。

    本类原名 ``TestRealCorpusCurrentlyIneligible``，断言「4 本规范全为 unknown
    → 一条也存不进缓存」。2026-09-14 的 ``b3cbf09`` 把四本规范核实为 ``current``
    （来源均为 www.mohurd.gov.cn），该断言随即失败 —— 这**正是当初写它时预留的
    「解锁缓存」信号**（原 docstring 原话：「人工核实后本测试需相应更新 ——
    那正是"解锁缓存"的信号」），不是回归。本类随之翻转为断言「现在可以缓存」。

    翻转后仍是一道绊线：若 ``data/versions.json`` 被改回 ``unknown``、或新增规范
    未核实，下面三条会立刻失败，提醒复核缓存是否该再次关闭（fail closed）。
    """

    def test_real_registry_all_verified_current(self) -> None:
        statuses = [
            (str(r.get("spec_id")), str(r.get("status")))
            for r in get_version_registry().list_all()
        ]
        assert statuses, "注册表应有条目"
        assert all(status == "current" for _spec, status in statuses), (
            "若规范状态被改回 unknown（或新增了未核实的规范），请更新本测试与 "
            "test_real_corpus_is_eligible_for_store —— 缓存必须随之关闭"
        )

    def test_real_corpus_is_eligible_for_store(self) -> None:
        svc = _service(registry=get_version_registry())
        ctx = _ctx()
        eligibility = svc.eligibility_for(
            category=CallCategory.RAG_GENERATION, temperature=0.0,
            citation_verified=True, output_guardrails_passed=True,
            answer="答案", source_ids=_SOURCES, ctx=ctx,
        )
        assert eligibility.eligible is True
        assert eligibility.reason == REASON_OK
        # 语料版本必须可判定 —— 否则缓存仍会因 corpus_version_unknown 关闭
        assert svc.corpus_version_known is True

    def test_versions_json_claims_survive_load_guard(self) -> None:
        """双保险：直接读 `data/versions.json`，逐条比对注册表的**实际**判定。

        断言「文件里声明的 status」与「过加载期守卫之后的 status」一致。
        任何被降级的条目（缺 ``verified_at`` / ``source`` 自述未核实 /
        ``current`` 缺 ``effective_date``）都会在这里暴露 —— 而不是悄悄变成
        unknown 让缓存无声关闭。比硬编码「必须是 current」更能说明问题：
        它连降级原因一起报出来。
        """
        path = Path(__file__).resolve().parents[1] / "data" / "versions.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        records = raw if isinstance(raw, list) else raw.get("specs", [])
        assert records, "data/versions.json 不应为空"
        actual_by_id = {
            str(r.get("spec_id")): r for r in get_version_registry().list_all()
        }
        for record in records:
            spec_id = str(record.get("spec_id"))
            declared = str(record.get("status", "unknown"))
            actual = actual_by_id.get(spec_id, {})
            assert str(actual.get("status", "unknown")) == declared, (
                f"{spec_id}: versions.json 声明 {declared!r}，注册表判定 "
                f"{actual.get('status')!r}（降级原因 {actual.get('demotion_reason')!r}）"
            )


class TestServiceHelpers:
    def test_spec_statuses_dedupes_and_defaults_unknown(self) -> None:
        statuses = spec_statuses_for(
            ["GB50015-2019 3.1.1", "GB50015-2019 3.1.2", "GB99999-2099 1.1.1"],
            _KNOWN_REGISTRY,
        )
        assert ("GB50015-2019", "current") in statuses
        assert ("GB99999-2099", "unknown") in statuses
        assert len(statuses) == 2  # 去重

    def test_spec_statuses_without_registry_is_unknown(self) -> None:
        assert spec_statuses_for(["GB50015-2019 3.1.1"], None) == [
            ("GB50015-2019", "unknown")
        ]

    def test_registry_exception_degrades_to_unknown(self) -> None:
        class _Boom:
            def get_status_for_source_id(self, _source_id: str) -> dict:
                raise RuntimeError("registry down")

        assert spec_statuses_for(["GB50015-2019 3.1.1"], _Boom()) == [
            ("GB50015-2019", "unknown")
        ]

    def test_service_uses_real_version_constants(self) -> None:
        """缓存键必须绑定真实的 prompt/tool 版本常量，不是硬编码占位。"""
        factors = _service().key_factors(
            query=_QUERY, ctx=_ctx(), model="m", temperature=0.0
        )
        assert factors.prompt_template_version == PROMPT_TEMPLATE_VERSION
        assert factors.tool_policy_version == TOOL_POLICY_VERSION

    def test_spec_version_digest_is_order_independent(self) -> None:
        assert spec_version_digest([("A", "x"), ("B", "y")]) == spec_version_digest(
            [("B", "y"), ("A", "x")]
        )

    def test_retrieval_params_digest_is_explicit(self) -> None:
        assert retrieval_params_digest(top_k=5, recall_top_k=20) == (
            "top_k=5;recall=20;rerank=none"
        )


class TestPromptCacheObservation:
    """规格 §5：不把"静态段在前"误报为缓存命中。"""

    def test_openai_style_field_parsed(self) -> None:
        obs = observe_prompt_cache({"prompt_tokens_details": {"cached_tokens": 1024}})
        assert obs.cached_tokens == 1024
        assert obs.has_evidence is True
        assert obs.source_field == "prompt_tokens_details.cached_tokens"

    def test_deepseek_style_field_parsed(self) -> None:
        obs = observe_prompt_cache({"prompt_cache_hit_tokens": 512})
        assert obs.cached_tokens == 512
        assert obs.source_field == "prompt_cache_hit_tokens"

    def test_missing_field_is_unknown_not_zero(self) -> None:
        """字段缺失 ≠ 命中 0 token。"""
        obs = observe_prompt_cache({"prompt_tokens": 100})
        assert obs.support is PromptCacheSupport.UNKNOWN
        assert obs.cached_tokens is None
        assert obs.has_evidence is False

    def test_zero_cached_tokens_is_evidence_of_no_hit(self) -> None:
        obs = observe_prompt_cache({"prompt_cache_hit_tokens": 0})
        assert obs.cached_tokens == 0
        assert obs.has_evidence is False  # 有字段但未命中

    def test_support_is_not_claimed_verified_without_real_response(self) -> None:
        """无 Key 环境未证实 → 只能是 DOCUMENTED_UNVERIFIED，不得谎称 VERIFIED。"""
        obs = observe_prompt_cache({"prompt_cache_hit_tokens": 512})
        assert obs.support is PromptCacheSupport.DOCUMENTED_UNVERIFIED
        assert obs.support is not PromptCacheSupport.VERIFIED


class TestTraceCacheEvent:
    """规格 §5：trace 含 cache_status / 命中类型 / entry age / 节省量，
    且不含 cache key 原文或 PII。"""

    def _recorder(self, tmp_path: Path):
        from water_agent.observability.json_trace import JsonTraceRecorder

        recorder = JsonTraceRecorder(tmp_path)
        recorder.start(run_id="r1", trace_id="t1", query=_QUERY)
        return recorder

    def _cache_events(self, recorder) -> list[dict]:
        path = recorder.finish("succeeded")
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        return [e for e in doc["events"] if e["event_type"] == "cache"]

    def test_hit_event_records_status_and_age(self, tmp_path: Path) -> None:
        recorder = self._recorder(tmp_path)
        factors = _factors()
        recorder.record_cache_event(
            cache_status=CacheStatus.HIT.value,
            cache_key_hash=factors.key_hash(),
            reason="hit",
            entry_age_seconds=12.5,
            saved_input_tokens=900,
            saved_output_tokens=120,
            saved_cost_usd=0.000375,
        )
        events = self._cache_events(recorder)
        assert len(events) == 1
        data = events[0]["data"]
        assert data["cache_status"] == "hit"
        assert data["entry_age_seconds"] == pytest.approx(12.5)
        assert data["saved_input_tokens"] == 900
        assert data["saved_cost_usd"] == pytest.approx(0.000375)

    def test_trace_has_key_hash_not_key_plaintext(self, tmp_path: Path) -> None:
        recorder = self._recorder(tmp_path)
        factors = _factors()
        recorder.record_cache_event(
            cache_status=CacheStatus.MISS.value,
            cache_key_hash=factors.key_hash(),
            reason="not_found",
        )
        path = recorder.finish("succeeded")
        raw = Path(path).read_text(encoding="utf-8")
        assert factors.key_hash() in raw
        assert factors.cache_key() not in raw  # 键原文永不落盘

    def test_savings_omitted_when_usage_unknown(self, tmp_path: Path) -> None:
        """usage 不可得时不写 saved_* 字段（不估算，与 USAGE_UNKNOWN 同口径）。"""
        recorder = self._recorder(tmp_path)
        recorder.record_cache_event(
            cache_status=CacheStatus.HIT.value, cache_key_hash="abcd",
            reason="hit", entry_age_seconds=1.0,
        )
        data = self._cache_events(recorder)[0]["data"]
        for field in ("saved_input_tokens", "saved_output_tokens", "saved_cost_usd"):
            assert field not in data

    def test_prompt_cache_unknown_field_omitted(self, tmp_path: Path) -> None:
        """provider 未返回 cached-token 字段 → trace 不出现该键（≠ 0）。"""
        recorder = self._recorder(tmp_path)
        obs = observe_prompt_cache({"prompt_tokens": 100})
        recorder.record_cache_event(
            cache_status=CacheStatus.MISS.value, cache_key_hash="abcd",
            prompt_cache_support=obs.support.value,
            prompt_cache_cached_tokens=obs.cached_tokens,
        )
        data = self._cache_events(recorder)[0]["data"]
        assert data["prompt_cache_support"] == "unknown"
        assert "prompt_cache_cached_tokens" not in data

    def test_ineligible_event_records_stable_reason(self, tmp_path: Path) -> None:
        recorder = self._recorder(tmp_path)
        recorder.record_cache_event(
            cache_status=CacheStatus.INELIGIBLE.value, cache_key_hash="abcd",
            reason=REASON_SPEC_VERSION_UNKNOWN, eligible_for_store=False,
        )
        data = self._cache_events(recorder)[0]["data"]
        assert data["reason"] == REASON_SPEC_VERSION_UNKNOWN
        assert data["eligible_for_store"] is False


class TestContextBudgetReview:
    """规格 §5 末项：依上下文预算审查工具清单、系统提示与生成保留。

    这些断言把**实测数字**钉住，防止提示/工具无声膨胀。
    数字来源见 `docs/design/p2.5-step-13-decision.md` §6。
    """

    def _registry(self):
        from water_agent.retrieval.in_memory import InMemoryRetriever
        from water_agent.tools.calc_velocity import calc_velocity_spec
        from water_agent.tools.cite import build_cite_clauses_tool
        from water_agent.tools.material_stats import material_stats_spec
        from water_agent.tools.read_drawing import build_read_drawing_tool
        from water_agent.tools.registry import ToolRegistry
        from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool

        registry = ToolRegistry()
        registry.register(build_retrieve_clauses_tool(InMemoryRetriever([])))
        registry.register(build_cite_clauses_tool())
        registry.register(calc_velocity_spec)
        registry.register(material_stats_spec)
        # P3 Step 20：图纸工具进装配，预算断言必须测**真实装配**的开销 ——
        # 否则守的是一个不存在的配置。
        registry.register(build_read_drawing_tool(Path("/tmp/water-agent-token-probe")))
        return registry

    def test_system_prompt_within_segment_budget(self) -> None:
        from water_agent.prompting.assembler import (
            SEGMENT_BUDGETS,
            PromptAssembler,
            _estimate_tokens,
        )

        assembled = PromptAssembler(tool_registry=self._registry()).assemble_prompt({})
        used = _estimate_tokens(str(assembled.content))
        assert used <= sum(SEGMENT_BUDGETS.values())
        # 2026-09-15 重新标定（原为「实测约 1424 / 上限 1800」）。
        # 三点实测，便于下次失败时直接定位是哪一段涨的：
        #     空注册表 1458 | 旧「四本全 unknown」注册表 1587 | 现行（全 current）1827
        # +240 全部来自 b3cbf09 把四本规范核实为 current：`snapshot_lines()` 从此
        # 输出生效日期与公告号来源（铁律 2 要求引用可追溯，这部分开销是**必要**的，
        # 不是无声膨胀 —— 它对应一次有据可查的提交）。
        # 2026-09-17 再次标定：1827 → 1917 (+90)。
        # 增量来自引用可追溯性增强（cite工具描述补充检索上下文校验说明）。
        # 上限取 1950（余量 33，约 1.7%）：保持紧绷，任何进一步膨胀立刻失败。
        assert used < 1950, f"系统提示膨胀到 {used} tokens，请复核预算"

    def test_tool_definitions_token_cost_is_bounded(self) -> None:
        """5 个工具的 schema 约 4.4K tokens —— 是当前最大的静态开销。

        规格 §5 要求"工具按意图最小暴露"。当前全量暴露，
        按意图裁剪属独立改动（需先有工具选择准确率证据），见决策文档 §5 拒绝项。

        P3 Step 20 实测：4 工具 3412 → 5 工具 4370（`read_drawing` 单独 958），
        headroom 仅 130。**上限刻意保持 4500 不上调** —— 为了塞进新工具而抬高预算
        护栏，等于把这条断言变成事后追认。Step 21 若再加工具很可能撞破，
        届时必须实施按意图最小暴露，而不是再改这个数（规格 §9 第 5 条）。
        """
        from water_agent.prompting.assembler import _estimate_tokens

        cost = _estimate_tokens(json.dumps(self._registry().list_tools(), ensure_ascii=False))
        assert cost < 4500, f"工具定义膨胀到 {cost} tokens，请复核是否需按意图裁剪"

    def test_no_history_is_silently_dropped(self) -> None:
        """规格 §5：P2.5 不可静默丢失历史。

        当前实现不做历史摘要/截断——checkpointer 保留完整 state。
        本测试确认 assembler 不含任何截断历史的逻辑（若将来加入，
        必须先有独立评测，见决策文档 §5）。
        """
        src = (
            Path(__file__).resolve().parents[1]
            / "src/water_agent/prompting/assembler.py"
        ).read_text(encoding="utf-8")
        for banned in ("messages[-", "history[:", "summarize_history", "drop_oldest"):
            assert banned not in src, f"assembler 出现历史裁剪逻辑 {banned}，需先有评测"
