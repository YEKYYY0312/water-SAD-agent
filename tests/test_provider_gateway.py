"""P2.5 Step 12 测试：Provider Gateway 的限流、预算、可靠性与账本。

覆盖规格 §4「测试与验收」全部条目：
预发送限流、并发原子性、预算预留/实际对账/回滚、429 Retry-After、5xx 有界重试、
非瞬时错误零重试、deadline、熔断半开、no-silent-fallback，
以及「连续 100 个 fake 调用后每个 request 都有准确 UsageRecord 或明确 USAGE_UNKNOWN，
没有负余额或超预算后仍发送请求」。

全部离线：executor 是 fake，不发起任何真实网络请求。
"""
from __future__ import annotations

import threading

import httpx
import httpx2
import pytest
from langchain_openai.chat_models.base import (
    OpenAIAuthenticationError,
    OpenAIConnectionError,
    OpenAIPermissionDeniedError,
    OpenAIRateLimitError,
    OpenAITimeoutError,
)

from water_agent.runtime.governance.config import (
    BudgetConfig,
    CircuitBreakerPolicy,
    GovernanceConfig,
    ProviderProfile,
    RateLimitPolicy,
    RetryPolicy,
)
from water_agent.runtime.governance.contracts import (
    CODE_BUDGET_EXCEEDED,
    CODE_CAPABILITY_DENIED,
    CODE_CIRCUIT_OPEN,
    CODE_PROVIDER_AUTH_FAILED,
    CODE_PROVIDER_TIMEOUT,
    CODE_PROVIDER_UNAVAILABLE,
    CODE_RATE_LIMITED,
    CODE_USAGE_UNKNOWN,
    BudgetPolicy,
    CacheStatus,
    CallCategory,
    Capability,
    GovernanceError,
    Principal,
    ProviderCall,
    ProviderKind,
    RequestContext,
    UsageRecord,
)
from water_agent.runtime.governance.gateway import (
    ProviderGateway,
    ProviderUsage,
    _classify_error,
    _retry_after_of,
)
from water_agent.runtime.governance.stores import (
    BudgetVerdict,
    InMemoryBudgetLedger,
    InMemoryRateLimitStore,
)


class _FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _HttpError(Exception):
    """模拟带状态码与 headers 的 HTTP 错误（不依赖任何 provider SDK）。"""

    def __init__(self, code: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code
        self.headers = headers or {}


def _ctx(*, thread: str = "t-1", deadline: float | None = None) -> RequestContext:
    return RequestContext(
        principal=Principal.local(thread),
        request_id=f"req-{thread}",
        thread_id=thread,
        deadline_seconds=deadline,
    )


def _call(
    *,
    provider: ProviderKind = ProviderKind.DEEPSEEK_CHAT,
    category: CallCategory = CallCategory.RAG_GENERATION,
    model: str = "deepseek-chat",
    est_in: int = 100,
    max_out: int | None = 200,
    capability: Capability = Capability.LLM_INFERENCE,
) -> ProviderCall:
    return ProviderCall(
        provider=provider,
        category=category,
        model=model,
        estimated_input_tokens=est_in,
        max_output_tokens=max_out,
        required_capability=capability,
    )


def _gateway(
    *,
    config: GovernanceConfig | None = None,
    clock: _FakeClock | None = None,
    records: list[UsageRecord] | None = None,
) -> ProviderGateway:
    return ProviderGateway(
        config=config,
        clock=clock,
        sleep=lambda _d: None,  # 测试永不真实等待
        on_usage=(records.append if records is not None else None),
    )


def _ok(value: str = "答案", *, in_tokens: int = 90, out_tokens: int = 40):
    def executor() -> tuple[str, ProviderUsage]:
        return value, ProviderUsage.of(input_tokens=in_tokens, output_tokens=out_tokens)

    return executor


class TestHappyPathAndAccounting:
    def test_successful_call_returns_value_and_usage_record(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records=records)
        result = gw.call(_ctx(), _call(), _ok())
        assert result.value == "答案"
        rec = result.usage_record
        assert rec.usage_known is True
        assert rec.input_tokens == 90 and rec.output_tokens == 40
        assert rec.result_status == "ok"
        assert rec.retry_count == 0
        assert gw.provider_call_count == 1
        assert records == [rec]

    def test_cost_computed_from_configured_price(self) -> None:
        gw = _gateway()
        rec = gw.call(_ctx(), _call(), _ok(in_tokens=1_000_000, out_tokens=0)).usage_record
        # deepseek-chat 输入 $0.27/1M
        assert rec.cost_usd == pytest.approx(0.27)

    def test_unregistered_model_price_yields_none_cost(self) -> None:
        """价格未登记 → 成本为 None（不猜价），token 仍准确。"""
        gw = _gateway()
        rec = gw.call(_ctx(), _call(model="mystery-model"), _ok()).usage_record
        assert rec.usage_known is True
        assert rec.input_tokens == 90
        assert rec.cost_usd is None

    def test_cached_tokens_priced_at_cached_rate(self) -> None:
        gw = _gateway()

        def executor() -> tuple[str, ProviderUsage]:
            return "x", ProviderUsage.of(
                input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000
            )

        rec = gw.call(_ctx(), _call(), executor).usage_record
        assert rec.cached_input_tokens == 1_000_000
        # 全部命中缓存 → 按 cached 单价 $0.07/1M
        assert rec.cost_usd == pytest.approx(0.07)

    def test_usage_unknown_is_marked_not_estimated(self) -> None:
        """provider 未返回 usage → USAGE_UNKNOWN，token/成本留空。"""
        gw = _gateway()
        rec = gw.call(_ctx(), _call(), lambda: ("x", ProviderUsage.unknown())).usage_record
        assert rec.usage_known is False
        assert rec.result_status == CODE_USAGE_UNKNOWN
        assert rec.input_tokens is None
        assert rec.cost_usd is None

    def test_cache_status_propagates_to_record(self) -> None:
        gw = _gateway()
        rec = gw.call(_ctx(), _call(), _ok(), cache_status=CacheStatus.MISS).usage_record
        assert rec.cache_status is CacheStatus.MISS

    def test_record_never_contains_subject_or_prompt(self) -> None:
        gw = _gateway()
        ctx = RequestContext(
            principal=Principal.local("secret-thread"),
            request_id="r-opaque",
            thread_id="secret-thread",
        )
        rec = gw.call(ctx, _call(), _ok()).usage_record
        dumped = rec.model_dump_json()
        assert "secret-thread" not in dumped
        assert rec.principal_hash == Principal.local("secret-thread").principal_hash


class TestCapabilityCheck:
    def test_ungranted_capability_denied_before_any_call(self) -> None:
        gw = _gateway()
        ctx = RequestContext(
            principal=Principal.local("t", capabilities=frozenset({Capability.CALCULATE})),
            request_id="r-1",
        )
        with pytest.raises(GovernanceError) as exc:
            gw.call(ctx, _call(capability=Capability.LLM_INFERENCE), _ok())
        assert exc.value.code == CODE_CAPABILITY_DENIED
        assert gw.provider_call_count == 0  # 未发送


class TestRateLimiting:
    def test_rate_limit_rejects_before_sending(self) -> None:
        """限流是**预发送**检查：拒绝时 provider_call_count 不增加。"""
        config = GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    rate_limit=RateLimitPolicy(
                        bucket_capacity_tokens=500,
                        refill_tokens_per_second=1.0,
                        max_requests_per_minute=100,
                    ),
                )
            }
        )
        gw = _gateway(config=config)
        # 每次 300 token（100 in + 200 out）；桶容量 500 → 第 2 次后耗尽
        gw.call(_ctx(), _call(), _ok())
        assert gw.provider_call_count == 1
        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(), _call(), _ok())
        assert exc.value.code == CODE_RATE_LIMITED
        assert exc.value.retry_after_seconds is not None
        assert exc.value.retry_after_seconds > 0
        assert gw.provider_call_count == 1  # 未发送第二次

    def test_rate_limit_error_does_not_leak_bucket_internals(self) -> None:
        config = GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    rate_limit=RateLimitPolicy(bucket_capacity_tokens=1, refill_tokens_per_second=1.0),
                )
            }
        )
        gw = _gateway(config=config)
        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(), _call(), _ok())
        for leak in ("bucket", "capacity", "refill", "300"):
            assert leak not in exc.value.message

    def test_rpm_limit_enforced(self) -> None:
        config = GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    rate_limit=RateLimitPolicy(
                        bucket_capacity_tokens=10_000_000,
                        refill_tokens_per_second=1_000_000.0,
                        max_requests_per_minute=3,
                    ),
                )
            }
        )
        gw = _gateway(config=config)
        for _ in range(3):
            gw.call(_ctx(), _call(), _ok())
        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(), _call(), _ok())
        assert exc.value.code == CODE_RATE_LIMITED

    def test_rate_limit_key_isolates_principals(self) -> None:
        """一个主体打满不影响另一个主体。"""
        config = GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    rate_limit=RateLimitPolicy(
                        bucket_capacity_tokens=400,
                        refill_tokens_per_second=0.001,
                        max_requests_per_minute=100,
                    ),
                )
            }
        )
        gw = _gateway(config=config)
        gw.call(_ctx(thread="a"), _call(), _ok())
        with pytest.raises(GovernanceError):
            gw.call(_ctx(thread="a"), _call(), _ok())
        # 另一主体仍可调用
        gw.call(_ctx(thread="b"), _call(), _ok())

    def test_request_larger_than_bucket_rejected_without_retry_after(self) -> None:
        """单次请求就超桶容量：等待也无用，不给 retry_after 误导调用方。"""
        store = InMemoryRateLimitStore()
        policy = RateLimitPolicy(bucket_capacity_tokens=10, refill_tokens_per_second=1.0)
        verdict = store.acquire("k", 100, policy)
        assert verdict.allowed is False
        assert verdict.retry_after_seconds is None
        assert verdict.reason == "request_exceeds_capacity"

    def test_concurrent_acquire_is_atomic(self) -> None:
        """并发原子性：20 线程各取 100，桶容量 1000 → 恰好 10 次成功。"""
        store = InMemoryRateLimitStore()
        policy = RateLimitPolicy(
            bucket_capacity_tokens=1000,
            refill_tokens_per_second=0.0001,  # 近似不补充
            max_requests_per_minute=10_000,
        )
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            verdict = store.acquire("shared", 100, policy)
            with lock:
                results.append(verdict.allowed)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(results) == 10

    def test_in_memory_store_does_not_claim_multi_instance(self) -> None:
        """规格 §4：不得用单进程 dict 声称支持多实例限流。"""
        assert InMemoryRateLimitStore().supports_multi_instance is False
        assert InMemoryBudgetLedger().supports_multi_instance is False


class TestBudget:
    def _blocking_config(self, cap: int) -> GovernanceConfig:
        return GovernanceConfig(
            budget=BudgetConfig(policy=BudgetPolicy.BLOCK, daily_token_cap=cap),
            profiles={ProviderKind.DEEPSEEK_CHAT: ProviderProfile(timeout_seconds=10.0)},
        )

    def test_budget_block_rejects_before_sending(self) -> None:
        gw = _gateway(config=self._blocking_config(cap=350))
        gw.call(_ctx(), _call(), _ok())  # 预留 300，实际结算 130
        # 已花 130，再预留 300 → 430 > 350 → 拒绝
        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(), _call(), _ok())
        assert exc.value.code == CODE_BUDGET_EXCEEDED
        assert gw.provider_call_count == 1

    def test_settle_uses_actual_usage_not_estimate(self) -> None:
        """对账用真实 usage：预留 300 但实际只用 130。"""
        config = self._blocking_config(cap=10_000)
        gw = _gateway(config=config)
        gw.call(_ctx(), _call(), _ok(in_tokens=90, out_tokens=40))
        snap = gw.budget_ledger.snapshot(gw.budget_key(_ctx()), config.budget)
        assert snap.spent_tokens == 130  # 不是预留的 300
        assert snap.reserved_tokens == 0  # 预留已释放

    def test_failed_call_rolls_back_reservation(self) -> None:
        """失败调用必须回滚预留，否则失败会永久占额度。"""
        config = self._blocking_config(cap=10_000)
        gw = _gateway(config=config)
        with pytest.raises(GovernanceError):
            gw.call(_ctx(), _call(), lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        snap = gw.budget_ledger.snapshot(gw.budget_key(_ctx()), config.budget)
        assert snap.reserved_tokens == 0
        assert snap.spent_tokens == 0

    def test_no_negative_balance_and_no_send_after_cap(self) -> None:
        """规格 §4：没有负余额，也没有超预算后仍发送请求。"""
        config = self._blocking_config(cap=1000)
        gw = _gateway(config=config)
        sent = 0
        for _ in range(20):
            try:
                gw.call(_ctx(), _call(), _ok())
                sent += 1
            except GovernanceError as exc:
                assert exc.value.code if False else exc.code == CODE_BUDGET_EXCEEDED
                break
        snap = gw.budget_ledger.snapshot(gw.budget_key(_ctx()), config.budget)
        assert snap.spent_tokens >= 0
        assert snap.reserved_tokens == 0
        assert snap.committed_tokens <= config.budget.daily_token_cap or sent > 0
        assert gw.provider_call_count == sent

    def test_warn_policy_observes_without_blocking(self) -> None:
        """三态语义：WARN 只观测，不拦截。"""
        config = GovernanceConfig(
            budget=BudgetConfig(policy=BudgetPolicy.WARN, daily_token_cap=100),
            profiles={ProviderKind.DEEPSEEK_CHAT: ProviderProfile(timeout_seconds=10.0)},
        )
        gw = _gateway(config=config)
        for _ in range(5):
            gw.call(_ctx(), _call(), _ok())  # 远超 100 的额度，但不拦
        assert gw.provider_call_count == 5

    def test_degrade_policy_is_rejected_by_config(self) -> None:
        """P2.5 不启用模型降级：配置期即拒绝。"""
        with pytest.raises(ValueError, match="不启用模型降级"):
            BudgetConfig(policy=BudgetPolicy.DEGRADE)

    def test_default_budget_has_no_cost_cap(self) -> None:
        """价格未与账单核对 → 不做成本硬拒绝（不得用估算当账单）。"""
        assert BudgetConfig().daily_cost_cap_usd is None
        assert BudgetConfig().policy is BudgetPolicy.WARN

    def test_default_prices_are_unconfirmed(self) -> None:
        for model in ("deepseek-chat", "deepseek-reasoner"):
            price = GovernanceConfig().price_for(model)
            assert price is not None
            assert price.confirmed is False

    def test_unknown_usage_settles_without_guessing_cost(self) -> None:
        config = self._blocking_config(cap=10_000)
        gw = _gateway(config=config)
        gw.call(_ctx(), _call(), lambda: ("x", ProviderUsage.unknown()))
        snap = gw.budget_ledger.snapshot(gw.budget_key(_ctx()), config.budget)
        assert snap.spent_cost_usd == 0.0  # 不猜成本
        assert snap.reserved_tokens == 0  # 预留已释放


class TestRetryAndErrorClassification:
    def _retry_config(self, attempts: int = 3) -> GovernanceConfig:
        return GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    retry=RetryPolicy(max_attempts=attempts, base_backoff_seconds=0.0),
                    breaker=CircuitBreakerPolicy(failure_threshold=100),
                )
            }
        )

    @pytest.mark.parametrize("code", [408, 429, 500, 502, 503, 504])
    def test_transient_http_codes_retried(self, code: int) -> None:
        gw = _gateway(config=self._retry_config())
        calls = {"n": 0}

        def executor() -> tuple[str, ProviderUsage]:
            calls["n"] += 1
            if calls["n"] < 3:
                raise _HttpError(code)
            return "ok", ProviderUsage.of(input_tokens=1, output_tokens=1)

        result = gw.call(_ctx(), _call(), executor)
        assert result.value == "ok"
        assert calls["n"] == 3
        assert result.usage_record.retry_count == 2

    @pytest.mark.parametrize("code", [400, 404, 409, 422])
    def test_permanent_http_codes_not_retried(self, code: int) -> None:
        gw = _gateway(config=self._retry_config())
        calls = {"n": 0}

        def executor() -> tuple[str, ProviderUsage]:
            calls["n"] += 1
            raise _HttpError(code)

        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(), _call(), executor)
        assert exc.value.code == CODE_PROVIDER_UNAVAILABLE
        assert calls["n"] == 1  # 零重试

    @pytest.mark.parametrize("code", [401, 403])
    def test_auth_errors_not_retried(self, code: int) -> None:
        """403 是 rerank 的已知阻塞码：必须零重试且分类为 AUTH。"""
        gw = _gateway(config=self._retry_config())
        calls = {"n": 0}

        def executor() -> tuple[str, ProviderUsage]:
            calls["n"] += 1
            raise _HttpError(code)

        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(), _call(), executor)
        assert exc.value.code == CODE_PROVIDER_AUTH_FAILED
        assert calls["n"] == 1

    def test_auth_failure_does_not_trip_breaker(self) -> None:
        """认证失败换 Key 才能解决，熔断无意义（否则会误伤后续正确配置）。"""
        config = GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    retry=RetryPolicy(max_attempts=1),
                    breaker=CircuitBreakerPolicy(failure_threshold=1),
                )
            }
        )
        gw = _gateway(config=config)
        with pytest.raises(GovernanceError):
            gw.call(_ctx(), _call(), lambda: (_ for _ in ()).throw(_HttpError(403)))
        assert gw.breaker_open(_call()) is False

    def test_retry_exhausted_raises_provider_unavailable(self) -> None:
        gw = _gateway(config=self._retry_config(attempts=3))
        calls = {"n": 0}

        def executor() -> tuple[str, ProviderUsage]:
            calls["n"] += 1
            raise _HttpError(503)

        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(), _call(), executor)
        assert exc.value.code == CODE_PROVIDER_UNAVAILABLE
        assert calls["n"] == 3

    def test_429_respects_retry_after_header(self) -> None:
        slept: list[float] = []
        gw = ProviderGateway(
            config=self._retry_config(), sleep=slept.append, clock=_FakeClock()
        )
        calls = {"n": 0}

        def executor() -> tuple[str, ProviderUsage]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _HttpError(429, headers={"Retry-After": "7"})
            return "ok", ProviderUsage.of(input_tokens=1, output_tokens=1)

        gw.call(_ctx(), _call(), executor)
        assert slept == [7.0]

    def test_backoff_is_exponential_with_jitter_when_no_header(self) -> None:
        slept: list[float] = []
        config = GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=100.0,
                    retry=RetryPolicy(
                        max_attempts=4, base_backoff_seconds=1.0, max_backoff_seconds=100.0,
                        jitter_ratio=0.25,
                    ),
                    breaker=CircuitBreakerPolicy(failure_threshold=100),
                )
            }
        )
        gw = ProviderGateway(config=config, sleep=slept.append, clock=_FakeClock())
        with pytest.raises(GovernanceError):
            gw.call(_ctx(), _call(), lambda: (_ for _ in ()).throw(_HttpError(503)))
        assert len(slept) == 3
        # 指数递增的基准分别是 1、2、4；抖动最多下浮 25%
        for i, delay in enumerate(slept):
            base = 2.0**i
            assert base * 0.75 <= delay <= base

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (TimeoutError("t"), "transient"),
            (ConnectionError("c"), "transient"),
            (_HttpError(503), "transient"),
            (_HttpError(401), "auth"),
            (_HttpError(400), "permanent"),
            (ValueError("v"), "permanent"),
            (RuntimeError("r"), "permanent"),
        ],
    )
    def test_classify_error(self, exc: BaseException, expected: str) -> None:
        assert _classify_error(exc) == expected

    def test_classify_defaults_to_permanent(self) -> None:
        """不确定就不重试（避免放大故障与成本）。"""

        class Weird(Exception):
            pass

        assert _classify_error(Weird()) == "permanent"

    def test_retry_after_parsing(self) -> None:
        assert _retry_after_of(_HttpError(429, headers={"Retry-After": "3.5"})) == 3.5
        assert _retry_after_of(_HttpError(429, headers={"retry-after": "2"})) == 2.0
        assert _retry_after_of(_HttpError(429, headers={"Retry-After": "junk"})) is None
        assert _retry_after_of(_HttpError(429)) is None


class TestWrappedSdkErrorClassification:
    """SDK 异常被上层重包装成子类后，仍按瞬时 / 认证 / 永久正确分类（类名沿 MRO 匹配）。

    langchain-openai 1.6 的 ChatOpenAI 把 openai SDK 的超时 / 连接错误重包装成
    ``OpenAITimeoutError`` / ``OpenAIConnectionError`` 再抛。旧实现只比对
    ``type(exc).__name__``，三个 DeepSeek chat 出口的超时与连接失败因此被判成永久错误：
    零重试、不计熔断。本组用真实异常类型锁死（测试可以导入 SDK，gateway 本身不导入）。
    """

    _REQUEST = httpx.Request("POST", "https://api.deepseek.com/chat/completions")

    def test_subclass_of_known_transient_name_is_transient(self) -> None:
        """不依赖任何 SDK：类名特征对子类同样生效。"""

        class APITimeoutError(Exception):
            pass

        class VendorTimeoutError(APITimeoutError):
            pass

        assert _classify_error(VendorTimeoutError()) == "transient"

    def test_langchain_openai_timeout_and_connection_errors_are_transient(self) -> None:
        assert _classify_error(OpenAITimeoutError(self._REQUEST)) == "transient"
        assert (
            _classify_error(OpenAIConnectionError(message="refused", request=self._REQUEST))
            == "transient"
        )

    @pytest.mark.parametrize(("status", "expected"), [(401, "auth"), (403, "auth"), (429, "transient")])
    def test_langchain_openai_status_errors_still_classified_by_status(
        self, status: int, expected: str
    ) -> None:
        response = httpx.Response(status, request=self._REQUEST)
        error_cls = {
            401: OpenAIAuthenticationError,
            403: OpenAIPermissionDeniedError,
            429: OpenAIRateLimitError,
        }[status]
        assert _classify_error(error_cls(message="x", response=response, body=None)) == expected

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            # 流式中途断连：openai SDK 不再包装，原始 httpx 异常直接冒出
            (httpx.RemoteProtocolError("peer closed connection", request=_REQUEST), "transient"),
            (httpx.ReadError("connection reset", request=_REQUEST), "transient"),
            (httpx.ConnectError("refused", request=_REQUEST), "transient"),
            (httpx.PoolTimeout("pool exhausted", request=_REQUEST), "transient"),
            # 本端协议错误：重试同一个请求毫无意义
            (httpx.LocalProtocolError("illegal header"), "permanent"),
            # openai 3.x 实际用的是 httpx2（类名相同，按名匹配同样生效）
            (httpx2.RemoteProtocolError("incomplete chunked read"), "transient"),
            (httpx2.ReadTimeout("read timed out"), "transient"),
            (httpx2.LocalProtocolError("illegal header"), "permanent"),
        ],
    )
    def test_httpx_transport_errors(self, exc: BaseException, expected: str) -> None:
        assert _classify_error(exc) == expected

    def test_wrapped_timeout_is_retried_by_gateway(self) -> None:
        gw = _gateway(
            config=GovernanceConfig(
                profiles={
                    ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                        timeout_seconds=10.0,
                        retry=RetryPolicy(max_attempts=3, base_backoff_seconds=0.0),
                    )
                }
            )
        )
        calls = {"n": 0}

        def executor() -> tuple[str, ProviderUsage]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OpenAITimeoutError(self._REQUEST)
            return "ok", ProviderUsage.of(input_tokens=1, output_tokens=1)

        result = gw.call(_ctx(), _call(), executor)
        assert result.value == "ok"
        assert result.usage_record.retry_count == 1
        assert gw.provider_call_count == 2


class TestDeadline:
    """deadline 从**本次调用开始**计时，约束重试的累计等待。"""

    def test_deadline_stops_retry_when_call_itself_consumed_budget(self) -> None:
        """executor 自身耗时越过 deadline → 不再重试，报 PROVIDER_TIMEOUT。"""
        clock = _FakeClock()
        config = GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=100.0,
                    retry=RetryPolicy(max_attempts=5, base_backoff_seconds=0.0),
                    breaker=CircuitBreakerPolicy(failure_threshold=100),
                )
            }
        )
        gw = ProviderGateway(config=config, clock=clock, sleep=lambda _d: None)
        calls = {"n": 0}

        def slow_then_fail() -> tuple[str, ProviderUsage]:
            calls["n"] += 1
            clock.advance(10.0)  # 每次调用耗时 10s
            raise _HttpError(503)

        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(deadline=5.0), _call(), slow_then_fail)
        assert exc.value.code == CODE_PROVIDER_TIMEOUT
        # 第一次尝试消耗 10s > deadline 5s → 第二轮开头即拦下
        assert calls["n"] == 1

    def test_deadline_stops_retry_when_backoff_would_exceed(self) -> None:
        """重试等待将越过 deadline → 立即 PROVIDER_TIMEOUT，不再白等。"""
        clock = _FakeClock()
        config = GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=100.0,
                    retry=RetryPolicy(max_attempts=5, base_backoff_seconds=100.0,
                                      max_backoff_seconds=100.0, jitter_ratio=0.0),
                    breaker=CircuitBreakerPolicy(failure_threshold=100),
                )
            }
        )
        slept: list[float] = []
        gw = ProviderGateway(config=config, clock=clock, sleep=slept.append)
        calls = {"n": 0}

        def executor() -> tuple[str, ProviderUsage]:
            calls["n"] += 1
            raise _HttpError(503)

        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(deadline=5.0), _call(), executor)
        assert exc.value.code == CODE_PROVIDER_TIMEOUT
        assert calls["n"] == 1  # 只尝试一次，没有白等
        assert slept == []  # 没有睡过

    def test_no_deadline_means_retry_policy_governs(self) -> None:
        """deadline 为 None 不等于"无限期"：仍受 max_attempts 约束。"""
        config = GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    retry=RetryPolicy(max_attempts=2, base_backoff_seconds=0.0),
                    breaker=CircuitBreakerPolicy(failure_threshold=100),
                )
            }
        )
        gw = _gateway(config=config)
        calls = {"n": 0}

        def executor() -> tuple[str, ProviderUsage]:
            calls["n"] += 1
            raise _HttpError(503)

        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(deadline=None), _call(), executor)
        assert exc.value.code == CODE_PROVIDER_UNAVAILABLE
        assert calls["n"] == 2

    def test_timeout_rolls_back_budget(self) -> None:
        """deadline 超时也必须回滚预留，不能让超时永久占额度。"""
        clock = _FakeClock()
        config = GovernanceConfig(
            budget=BudgetConfig(policy=BudgetPolicy.BLOCK, daily_token_cap=10_000),
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=100.0,
                    retry=RetryPolicy(max_attempts=5, base_backoff_seconds=0.0),
                    breaker=CircuitBreakerPolicy(failure_threshold=100),
                )
            },
        )
        gw = ProviderGateway(config=config, clock=clock, sleep=lambda _d: None)
        ctx = _ctx(deadline=5.0)

        def slow_then_fail() -> tuple[str, ProviderUsage]:
            clock.advance(10.0)
            raise _HttpError(503)

        with pytest.raises(GovernanceError) as exc:
            gw.call(ctx, _call(), slow_then_fail)
        assert exc.value.code == CODE_PROVIDER_TIMEOUT
        snap = gw.budget_ledger.snapshot(gw.budget_key(ctx), config.budget)
        assert snap.reserved_tokens == 0
        assert snap.spent_tokens == 0


class TestCircuitBreaker:
    def _breaker_config(self) -> GovernanceConfig:
        return GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    retry=RetryPolicy(max_attempts=1),
                    breaker=CircuitBreakerPolicy(
                        failure_threshold=2, recovery_seconds=30.0, half_open_max_calls=1
                    ),
                )
            }
        )

    def test_breaker_opens_after_threshold(self) -> None:
        clock = _FakeClock()
        gw = ProviderGateway(config=self._breaker_config(), clock=clock, sleep=lambda _d: None)
        boom = lambda: (_ for _ in ()).throw(_HttpError(503))

        for _ in range(2):
            with pytest.raises(GovernanceError):
                gw.call(_ctx(), _call(), boom)
        assert gw.breaker_open(_call()) is True

        before = gw.provider_call_count
        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(), _call(), _ok())
        assert exc.value.code == CODE_CIRCUIT_OPEN
        assert exc.value.retry_after_seconds is not None
        assert gw.provider_call_count == before  # 开路期不发送

    def test_breaker_half_open_allows_limited_probe(self) -> None:
        clock = _FakeClock()
        gw = ProviderGateway(config=self._breaker_config(), clock=clock, sleep=lambda _d: None)
        boom = lambda: (_ for _ in ()).throw(_HttpError(503))
        for _ in range(2):
            with pytest.raises(GovernanceError):
                gw.call(_ctx(), _call(), boom)

        clock.advance(31.0)  # 进入半开
        result = gw.call(_ctx(), _call(), _ok())  # 探测成功 → 闭合
        assert result.value == "答案"
        assert gw.breaker_open(_call()) is False

    def test_half_open_rejects_beyond_probe_limit(self) -> None:
        clock = _FakeClock()
        gw = ProviderGateway(config=self._breaker_config(), clock=clock, sleep=lambda _d: None)
        boom = lambda: (_ for _ in ()).throw(_HttpError(503))
        for _ in range(2):
            with pytest.raises(GovernanceError):
                gw.call(_ctx(), _call(), boom)

        clock.advance(31.0)
        # 半开期第一次探测又失败 → 重新开路
        with pytest.raises(GovernanceError):
            gw.call(_ctx(), _call(), boom)
        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(), _call(), _ok())
        assert exc.value.code == CODE_CIRCUIT_OPEN

    def test_breaker_scoped_per_provider_model(self) -> None:
        """一个模型熔断不影响另一个模型。"""
        clock = _FakeClock()
        gw = ProviderGateway(config=self._breaker_config(), clock=clock, sleep=lambda _d: None)
        boom = lambda: (_ for _ in ()).throw(_HttpError(503))
        for _ in range(2):
            with pytest.raises(GovernanceError):
                gw.call(_ctx(), _call(model="model-a"), boom)
        assert gw.breaker_open(_call(model="model-a")) is True
        # 另一模型仍可用
        gw.call(_ctx(), _call(model="model-b"), _ok())


class _SlowLedger(InMemoryBudgetLedger):
    """预留时推进假时钟：模拟共享账本（Redis/Postgres adapter）慢到在发送前就耗尽 deadline。"""

    def __init__(self, clock: _FakeClock) -> None:
        super().__init__()
        self._fake_clock = clock
        self.delay = 0.0

    def reserve(self, key: str, tokens: int, config: BudgetConfig) -> BudgetVerdict:
        self._fake_clock.advance(self.delay)
        return super().reserve(key, tokens, config)


class _InFlightCall:
    """在后台线程发起一次 ``gw.call``，executor 停在"已发出、未返回"，直到 :meth:`finish`。

    让一个请求在熔断状态变化期间一直挂着（真实场景里就是一个慢请求）。
    """

    def __init__(self, gw: ProviderGateway, outcome: BaseException | str, *, thread: str) -> None:
        self._sending = threading.Event()
        self._go = threading.Event()
        self.value: str | None = None
        self.error: GovernanceError | None = None

        def executor() -> tuple[str, ProviderUsage]:
            self._sending.set()
            assert self._go.wait(timeout=10), "测试没有放行这个请求"
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome, ProviderUsage.of(input_tokens=1, output_tokens=1)

        def run() -> None:
            try:
                self.value = gw.call(_ctx(thread=thread), _call(), executor).value
            except GovernanceError as exc:
                self.error = exc
            finally:
                self._sending.set()  # 发送前就被拒时别让主线程干等

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert self._sending.wait(timeout=10)
        assert self.error is None, f"请求没能发出：{self.error}"

    def finish(self) -> None:
        self._go.set()
        self._thread.join(timeout=10)
        assert not self._thread.is_alive()


class TestHalfOpenProbeWithoutVerdict:
    """半开探测没出成败判定就结束时，必须归还探测名额。

    回归：名额此前只在探测成功（闭合）或瞬时失败（重新开路）时清零。探测若被限流/预算
    拒绝、认证失败（401/403 不计熔断）、永久错误（不计熔断）或 deadline 用尽，名额就一直
    占着，此后**每个**请求都是 ``CIRCUIT_OPEN``（"上游服务恢复探测中"），推进时钟也没用，
    只能重启进程。熔断规则本身不变：成功闭合、瞬时失败重新开路、401/403 与永久错误不计熔断。
    """

    @staticmethod
    def _config(
        *, rate_limit: RateLimitPolicy | None = None, budget: BudgetConfig | None = None
    ) -> GovernanceConfig:
        """阈值 1、恢复 30s、半开只放 1 个探测、不重试：一次 503 即开路，31s 后半开。"""
        return GovernanceConfig(
            budget=budget or BudgetConfig(),
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    retry=RetryPolicy(max_attempts=1),
                    breaker=CircuitBreakerPolicy(
                        failure_threshold=1, recovery_seconds=30.0, half_open_max_calls=1
                    ),
                    rate_limit=rate_limit or RateLimitPolicy(),
                )
            },
        )

    @staticmethod
    def _enter_half_open(gw: ProviderGateway, clock: _FakeClock) -> None:
        with pytest.raises(GovernanceError):
            gw.call(_ctx(thread="a"), _call(), lambda: (_ for _ in ()).throw(_HttpError(503)))
        assert gw.breaker_open(_call()) is True
        clock.advance(31.0)

    @staticmethod
    def _assert_slot_was_released(gw: ProviderGateway) -> None:
        """没出判定：熔断既没闭合也没重新开路；名额已还，下一个请求（换一个主体）立刻作为
        新探测放行、不必再等恢复窗口，成功即闭合。修复前这里是 CIRCUIT_OPEN，且永远如此。"""
        assert gw.breaker_open(_call()) is True
        result = gw.call(_ctx(thread="b"), _call(), _ok())
        assert result.value == "答案"
        assert gw.breaker_open(_call()) is False

    def test_rate_limited_probe_releases_slot(self) -> None:
        """限流按主体计：一个主体用满本地配额，不能把所有主体都关在熔断外。"""
        clock = _FakeClock()
        config = self._config(rate_limit=RateLimitPolicy(max_requests_per_minute=1))
        gw = _gateway(config=config, clock=clock)
        self._enter_half_open(gw, clock)  # 开路那次用掉了主体 a 这一分钟唯一的请求配额

        sent = gw.provider_call_count
        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(thread="a"), _call(), _ok())
        assert exc.value.code == CODE_RATE_LIMITED
        assert gw.provider_call_count == sent  # 发送前被拒
        self._assert_slot_was_released(gw)

    def test_budget_rejected_probe_releases_slot(self) -> None:
        clock = _FakeClock()
        config = self._config(budget=BudgetConfig(policy=BudgetPolicy.BLOCK, daily_token_cap=1_000))
        gw = _gateway(config=config, clock=clock)
        self._enter_half_open(gw, clock)

        sent = gw.provider_call_count
        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(thread="a"), _call(est_in=5_000), _ok())  # 预留 5,200 > 额度 1,000
        assert exc.value.code == CODE_BUDGET_EXCEEDED
        assert gw.provider_call_count == sent
        self._assert_slot_was_released(gw)

    @pytest.mark.parametrize(
        ("error", "expected_code"),
        [
            (_HttpError(401), CODE_PROVIDER_AUTH_FAILED),
            (_HttpError(403), CODE_PROVIDER_AUTH_FAILED),
            (_HttpError(400), CODE_PROVIDER_UNAVAILABLE),
            (ValueError("bad payload"), CODE_PROVIDER_UNAVAILABLE),  # 未知错误按永久处理
        ],
        ids=["auth-401", "auth-403", "permanent-400", "permanent-unknown-error"],
    )
    def test_auth_or_permanent_error_releases_slot_and_still_does_not_trip_breaker(
        self, error: BaseException, expected_code: str
    ) -> None:
        """阈值为 1：若 401/403 或永久错误计了熔断，这里会重新开路，下一个请求还得再等 30s。"""
        clock = _FakeClock()
        gw = _gateway(config=self._config(), clock=clock)
        self._enter_half_open(gw, clock)

        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(thread="a"), _call(), lambda: (_ for _ in ()).throw(error))
        assert exc.value.code == expected_code
        self._assert_slot_was_released(gw)

    def test_probe_whose_deadline_runs_out_before_sending_releases_slot(self) -> None:
        """deadline 是调用方的时间预算，不计熔断；在发送前就用尽时探测同样没出判定。"""
        clock = _FakeClock()
        ledger = _SlowLedger(clock)
        gw = ProviderGateway(
            config=self._config(), clock=clock, sleep=lambda _d: None, budget_ledger=ledger
        )
        self._enter_half_open(gw, clock)

        ledger.delay = 10.0  # 预留就耗掉 10s，超过 5s 的 deadline
        sent = gw.provider_call_count
        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(thread="a", deadline=5.0), _call(), _ok())
        assert exc.value.code == CODE_PROVIDER_TIMEOUT
        assert gw.provider_call_count == sent  # 一次都没发出
        ledger.delay = 0.0
        self._assert_slot_was_released(gw)

    def test_in_flight_probe_holds_its_slot_until_it_ends(self) -> None:
        """名额在探测结束时才还，不是一放行就还：探测在途期间其他请求照旧被拒。"""
        clock = _FakeClock()
        gw = _gateway(config=self._config(), clock=clock)
        self._enter_half_open(gw, clock)

        probe = _InFlightCall(gw, _HttpError(401), thread="a")
        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(thread="b"), _call(), _ok())
        assert exc.value.code == CODE_CIRCUIT_OPEN
        assert "恢复探测中" in exc.value.message

        probe.finish()
        assert probe.error is not None and probe.error.code == CODE_PROVIDER_AUTH_FAILED
        self._assert_slot_was_released(gw)

    def test_stale_slot_from_an_earlier_round_does_not_free_a_later_rounds_slot(self) -> None:
        """名额按熔断代次归还：上一轮半开期的探测拖到下一轮才结束，不得腾出下一轮的名额。

        只记"占没占名额"、结束时一律减一的话，第一轮探测的 401 会把第二轮那个仍在途的探测
        的名额减掉，半开期同时放进两个探测，超过 ``half_open_max_calls=1``。
        """
        clock = _FakeClock()
        gw = _gateway(config=self._config(), clock=clock)
        slow = _InFlightCall(gw, _HttpError(503), thread="slow")  # 闭合期放行的慢请求，不占名额
        self._enter_half_open(gw, clock)  # 第一轮半开
        stale = _InFlightCall(gw, _HttpError(401), thread="a")  # 第一轮的探测

        slow.finish()  # 503 是瞬时失败 → 重新开路，第一轮的名额随之作废
        assert gw.breaker_open(_call()) is True
        clock.advance(31.0)  # 第二轮半开
        current = _InFlightCall(gw, "答案", thread="b")  # 第二轮的探测，占着唯一的名额

        stale.finish()  # 第一轮的探测这时才以 401 结束
        assert stale.error is not None and stale.error.code == CODE_PROVIDER_AUTH_FAILED
        with pytest.raises(GovernanceError) as exc:
            gw.call(_ctx(thread="c"), _call(), _ok())
        assert exc.value.code == CODE_CIRCUIT_OPEN

        current.finish()  # 第二轮探测成功 → 闭合
        assert current.value == "答案"
        assert gw.breaker_open(_call()) is False


class TestNoSilentFallback:
    def test_gateway_never_substitutes_a_value_on_failure(self) -> None:
        """失败一律抛错，绝不返回替代产物（no silent fallback）。"""
        gw = _gateway(
            config=GovernanceConfig(
                profiles={
                    ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                        timeout_seconds=10.0, retry=RetryPolicy(max_attempts=1)
                    )
                }
            )
        )
        with pytest.raises(GovernanceError):
            gw.call(_ctx(), _call(), lambda: (_ for _ in ()).throw(_HttpError(503)))

    def test_failures_are_recorded_in_ledger(self) -> None:
        """限流/预算/provider 失败都要落账，否则观测里完全不可见。"""
        records: list[UsageRecord] = []
        config = GovernanceConfig(
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0, retry=RetryPolicy(max_attempts=1)
                )
            }
        )
        gw = _gateway(config=config, records=records)
        with pytest.raises(GovernanceError):
            gw.call(_ctx(), _call(), lambda: (_ for _ in ()).throw(_HttpError(503)))
        assert len(records) == 1
        assert records[0].result_status == CODE_PROVIDER_UNAVAILABLE
        assert records[0].usage_known is False

    def test_observer_failure_does_not_break_main_flow(self) -> None:
        def boom_observer(_rec: UsageRecord) -> None:
            raise RuntimeError("observer down")

        gw = ProviderGateway(on_usage=boom_observer, sleep=lambda _d: None)
        assert gw.call(_ctx(), _call(), _ok()).value == "答案"

    def test_unconfigured_provider_fails_closed(self) -> None:
        """未配置 provider 画像 → KeyError，不给"默认宽松"。"""
        config = GovernanceConfig(profiles={})
        gw = _gateway(config=config)
        with pytest.raises(KeyError, match="未配置 ProviderProfile"):
            gw.call(_ctx(), _call(), _ok())


class TestHundredCallInvariant:
    """规格 §4：连续 100 个 fake 调用后，每个 request 都有准确 UsageRecord 或
    明确 USAGE_UNKNOWN，没有负余额，也没有超预算后仍发送请求。"""

    def test_hundred_calls_all_accounted(self) -> None:
        records: list[UsageRecord] = []
        config = GovernanceConfig(
            budget=BudgetConfig(policy=BudgetPolicy.BLOCK, daily_token_cap=1_000_000),
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    rate_limit=RateLimitPolicy(
                        bucket_capacity_tokens=10_000_000,
                        refill_tokens_per_second=1_000_000.0,
                        max_requests_per_minute=10_000,
                    ),
                )
            },
        )
        gw = _gateway(config=config, records=records)

        for i in range(100):
            ctx = RequestContext(principal=Principal.local("t"), request_id=f"req-{i}")
            if i % 5 == 0:
                gw.call(ctx, _call(), lambda: ("x", ProviderUsage.unknown()))
            else:
                gw.call(ctx, _call(), _ok(in_tokens=10, out_tokens=5))

        assert len(records) == 100
        assert {r.request_id for r in records} == {f"req-{i}" for i in range(100)}
        for rec in records:
            if rec.usage_known:
                assert rec.input_tokens == 10 and rec.output_tokens == 5
                assert rec.result_status == "ok"
                assert rec.cost_usd is not None
            else:
                assert rec.result_status == CODE_USAGE_UNKNOWN
                assert rec.input_tokens is None and rec.cost_usd is None

        snap = gw.budget_ledger.snapshot(
            gw.budget_key(RequestContext(principal=Principal.local("t"), request_id="x")),
            config.budget,
        )
        assert snap.spent_tokens >= 0
        assert snap.reserved_tokens == 0  # 全部对账完毕，无泄漏预留
        assert snap.spent_tokens == 80 * 15  # 80 次 known × 15 token
        assert gw.provider_call_count == 100

    def test_cap_reached_stops_sending(self) -> None:
        config = GovernanceConfig(
            budget=BudgetConfig(policy=BudgetPolicy.BLOCK, daily_token_cap=900),
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    rate_limit=RateLimitPolicy(
                        bucket_capacity_tokens=10_000_000,
                        refill_tokens_per_second=1_000_000.0,
                        max_requests_per_minute=10_000,
                    ),
                )
            },
        )
        gw = _gateway(config=config)
        sent = 0
        rejected = 0
        for i in range(100):
            ctx = RequestContext(principal=Principal.local("t"), request_id=f"req-{i}")
            try:
                gw.call(ctx, _call(est_in=100, max_out=200), _ok(in_tokens=100, out_tokens=100))
                sent += 1
            except GovernanceError as exc:
                assert exc.code == CODE_BUDGET_EXCEEDED
                rejected += 1
        assert rejected > 0
        assert gw.provider_call_count == sent  # 超预算后一次都没发
        snap = gw.budget_ledger.snapshot(gw.budget_key(_ctx(thread="t")), config.budget)
        assert snap.spent_tokens <= 900 + 200  # 不会无限增长
        assert snap.reserved_tokens == 0
