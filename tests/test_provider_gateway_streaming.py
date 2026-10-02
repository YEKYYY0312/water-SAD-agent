"""ProviderGateway.stream_call 测试：流式通道的重试边界、deadline、usage、取消与账本。

与 ``tests/test_provider_gateway.py`` 同一写法：opener 是 fake，时钟可手动推进，sleep
不真实等待，全程离线。重点锁死四条流式特有的语义：

1. 重试只发生在第一个 chunk 之前；首个 chunk 交出后任何错误都不重试；
2. deadline 每次尝试前、每取下一个 chunk 前检查；
3. usage 取流里最后一个非 None 的值，正常结束的对账与 ``call`` 一致；
4. 调用方提前关闭：关上游、预留按 usage 未知结清、不计熔断失败——预留绝不泄漏。
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

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
from water_agent.runtime.governance.gateway import ProviderGateway, ProviderUsage
from water_agent.runtime.governance.stores import BudgetVerdict, InMemoryBudgetLedger


class _SpyLedger(InMemoryBudgetLedger):
    """记录每次结清是 settle 还是 rollback（两者在内存账本上效果相同，语义不同）。"""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple] = []

    def settle(self, key, reservation_id, *, actual_tokens, actual_cost_usd):  # type: ignore[override]
        self.events.append(("settle", actual_tokens, actual_cost_usd))
        super().settle(key, reservation_id, actual_tokens=actual_tokens,
                       actual_cost_usd=actual_cost_usd)

    def rollback(self, key, reservation_id):  # type: ignore[override]
        self.events.append(("rollback",))
        super().rollback(key, reservation_id)

    def settle_unknown(self, key, reservation_id):  # type: ignore[override]
        self.events.append(("settle_unknown",))
        super().settle_unknown(key, reservation_id)


class _FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _HttpError(Exception):
    """带状态码与 headers 的 HTTP 错误（不依赖任何 provider SDK）。"""

    def __init__(self, code: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code
        self.headers = headers or {}


class _Chunk:
    """fake 流的一个 chunk：文本 + 可选 usage（模拟 OpenAI 兼容接口最后一块带 usage）。"""

    def __init__(self, text: str, usage: ProviderUsage | None = None) -> None:
        self.text = text
        self.usage = usage

    def __repr__(self) -> str:  # pragma: no cover — 仅断言失败时可读
        return f"_Chunk({self.text!r})"


def _usage_of(chunk: _Chunk) -> ProviderUsage | None:
    return chunk.usage


class _FakeStream:
    """可观测的上游流：记录产出了几个 chunk、是否被关闭；可在任意位置抛错或推进时钟。

    行为脚本 ``script`` 的每一项：``_Chunk`` → 产出；``BaseException`` → 抛出；
    ``float`` → 把时钟推进这么多秒（模拟上游产出下一块很慢）。
    """

    def __init__(self, script: list[object], clock: _FakeClock | None = None) -> None:
        self._script = list(script)
        self._clock = clock
        self.produced = 0
        self.closed = False

    def __iter__(self) -> _FakeStream:
        return self

    def __next__(self) -> _Chunk:
        while self._script:
            step = self._script.pop(0)
            if isinstance(step, float):
                assert self._clock is not None
                self._clock.advance(step)
                continue
            if isinstance(step, BaseException):
                raise step
            assert isinstance(step, _Chunk)
            self.produced += 1
            return step
        raise StopIteration

    def close(self) -> None:
        self.closed = True


class _Opener:
    """按尝试次序返回不同的上游流；``streams[i]`` 是 BaseException 时 opener 本身抛错。"""

    def __init__(self, *streams: object) -> None:
        self._streams = list(streams)
        self.calls = 0
        self.opened: list[_FakeStream] = []

    def __call__(self) -> Iterator[_Chunk]:
        self.calls += 1
        item = self._streams.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, _FakeStream)
        self.opened.append(item)
        return item


def _usage(in_tokens: int = 90, out_tokens: int = 40) -> ProviderUsage:
    return ProviderUsage.of(input_tokens=in_tokens, output_tokens=out_tokens)


def _ctx(*, thread: str = "t-1", deadline: float | None = None, request_id: str | None = None,
         capabilities: frozenset[Capability] | None = None) -> RequestContext:
    return RequestContext(
        principal=Principal.local(thread, capabilities=capabilities),
        request_id=request_id or f"req-{thread}",
        thread_id=thread,
        deadline_seconds=deadline,
    )


def _call(*, model: str = "deepseek-chat", est_in: int = 100, max_out: int | None = 200) -> ProviderCall:
    return ProviderCall(
        provider=ProviderKind.DEEPSEEK_CHAT,
        category=CallCategory.AGENTIC_LOOP,
        model=model,
        estimated_input_tokens=est_in,
        max_output_tokens=max_out,
        required_capability=Capability.LLM_INFERENCE,
    )


def _config(
    *,
    attempts: int = 3,
    backoff: float = 0.0,
    threshold: int = 100,
    budget: BudgetConfig | None = None,
    rate_limit: RateLimitPolicy | None = None,
) -> GovernanceConfig:
    profile = ProviderProfile(
        timeout_seconds=10.0,
        retry=RetryPolicy(max_attempts=attempts, base_backoff_seconds=backoff,
                          max_backoff_seconds=max(backoff, 8.0)),
        breaker=CircuitBreakerPolicy(failure_threshold=threshold),
        rate_limit=rate_limit or RateLimitPolicy(),
    )
    return GovernanceConfig(
        budget=budget or BudgetConfig(policy=BudgetPolicy.BLOCK, daily_token_cap=1_000_000),
        profiles={ProviderKind.DEEPSEEK_CHAT: profile},
    )


def _gateway(
    *,
    config: GovernanceConfig | None = None,
    clock: _FakeClock | None = None,
    records: list[UsageRecord] | None = None,
    slept: list[float] | None = None,
    ledger: InMemoryBudgetLedger | None = None,
) -> ProviderGateway:
    return ProviderGateway(
        config=config or _config(),
        clock=clock or _FakeClock(),
        sleep=(slept.append if slept is not None else (lambda _d: None)),
        on_usage=(records.append if records is not None else None),
        budget_ledger=ledger,
    )


def _snapshot(gw: ProviderGateway, ctx: RequestContext | None = None):
    return gw.budget_ledger.snapshot(gw.budget_key(ctx or _ctx()), gw.config.budget)


def _texts(chunks: list[_Chunk]) -> list[str]:
    return [c.text for c in chunks]


class TestNormalStream:
    def test_chunks_pass_through_in_order_and_last_usage_is_settled(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records=records)
        stream = _FakeStream([_Chunk("流"), _Chunk("式"), _Chunk("", usage=_usage(90, 40))])

        out = list(gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of))

        assert _texts(out) == ["流", "式", ""]
        assert [r.result_status for r in records] == ["ok"]
        rec = records[0]
        assert rec.usage_known is True
        assert (rec.input_tokens, rec.output_tokens) == (90, 40)
        assert rec.retry_count == 0
        assert rec.cost_usd is not None  # deepseek-chat 有登记价格
        snap = _snapshot(gw)
        assert snap.spent_tokens == 130  # 真实 usage，不是预留的 300
        assert snap.reserved_tokens == 0
        assert gw.provider_call_count == 1

    def test_chunks_are_forwarded_one_by_one_not_buffered(self) -> None:
        """每交出一块，上游恰好只多产出一块：真流式，不是攒齐再吐。"""
        gw = _gateway()
        stream = _FakeStream([_Chunk("a"), _Chunk("b"), _Chunk("c")])
        it = gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of)
        for expected_produced, expected_text in ((1, "a"), (2, "b"), (3, "c")):
            assert next(it).text == expected_text
            assert stream.produced == expected_produced
        with pytest.raises(StopIteration):
            next(it)

    def test_last_non_none_usage_wins(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records=records)
        stream = _FakeStream([
            _Chunk("a", usage=_usage(1, 1)),  # 中途出现的 usage 不是最终值
            _Chunk("b"),
            _Chunk("c", usage=_usage(50, 7)),
            _Chunk("d"),  # 之后不带 usage 的块不得把它抹掉
        ])
        list(gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of))
        assert (records[0].input_tokens, records[0].output_tokens) == (50, 7)
        assert _snapshot(gw).spent_tokens == 57

    def test_stream_without_usage_is_recorded_unknown_not_estimated(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records=records)
        stream = _FakeStream([_Chunk("a"), _Chunk("b")])
        list(gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of))
        rec = records[0]
        assert rec.result_status == CODE_USAGE_UNKNOWN
        assert rec.usage_known is False
        assert rec.input_tokens is None and rec.cost_usd is None
        snap = _snapshot(gw)
        assert snap.spent_tokens == 0 and snap.reserved_tokens == 0

    def test_empty_stream_ends_normally_with_unknown_usage(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records=records)
        out = list(gw.stream_call(_ctx(), _call(), _Opener(_FakeStream([])), usage_of=_usage_of))
        assert out == []
        assert [r.result_status for r in records] == [CODE_USAGE_UNKNOWN]
        assert _snapshot(gw).reserved_tokens == 0
        assert gw.provider_call_count == 1

    def test_cache_status_propagates_to_record(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records=records)
        stream = _FakeStream([_Chunk("a", usage=_usage())])
        list(gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of,
                            cache_status=CacheStatus.MISS))
        assert records[0].cache_status is CacheStatus.MISS

    def test_generator_is_lazy_nothing_happens_before_first_next(self) -> None:
        """创建后未迭代即丢弃：不限流、不预留、不发请求、不落账。"""
        records: list[UsageRecord] = []
        gw = _gateway(records=records)
        opener = _Opener(_FakeStream([_Chunk("a")]))
        it = gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of)
        it.close()
        assert opener.calls == 0
        assert gw.provider_call_count == 0
        assert records == []
        assert _snapshot(gw).reserved_tokens == 0


class TestRetryBeforeFirstChunk:
    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_transient_error_opening_stream_is_retried(self, code: int) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records=records)
        opener = _Opener(_HttpError(code), _FakeStream([_Chunk("ok", usage=_usage())]))
        out = list(gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of))
        assert _texts(out) == ["ok"]
        assert opener.calls == 2
        assert gw.provider_call_count == 2
        assert records[0].result_status == "ok"
        assert records[0].retry_count == 1

    def test_transient_error_on_first_chunk_is_retried_and_failed_stream_closed(self) -> None:
        gw = _gateway()
        broken = _FakeStream([_HttpError(503)])
        healthy = _FakeStream([_Chunk("a"), _Chunk("b")])
        opener = _Opener(broken, healthy)
        out = list(gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of))
        assert _texts(out) == ["a", "b"]
        assert broken.closed is True  # 失败尝试打开的流先关掉，再重试
        assert opener.calls == 2

    def test_429_retry_after_is_respected(self) -> None:
        slept: list[float] = []
        gw = _gateway(config=_config(backoff=1.0), slept=slept)
        opener = _Opener(_HttpError(429, headers={"Retry-After": "7"}), _FakeStream([_Chunk("a")]))
        list(gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of))
        assert slept == [7.0]

    def test_retry_exhausted_raises_without_any_chunk(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(config=_config(attempts=3), records=records)
        opener = _Opener(_HttpError(503), _HttpError(503), _HttpError(503))
        received: list[str] = []
        with pytest.raises(GovernanceError) as exc:
            for chunk in gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of):
                received.append(chunk.text)
        assert exc.value.code == CODE_PROVIDER_UNAVAILABLE
        assert received == []
        assert opener.calls == 3
        assert [r.result_status for r in records] == [CODE_PROVIDER_UNAVAILABLE]
        assert _snapshot(gw).reserved_tokens == 0

    @pytest.mark.parametrize("code", [401, 403])
    def test_auth_error_is_not_retried_and_does_not_trip_breaker(self, code: int) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(config=_config(attempts=3, threshold=1), records=records)
        opener = _Opener(_HttpError(code), _FakeStream([_Chunk("never")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of))
        assert exc.value.code == CODE_PROVIDER_AUTH_FAILED
        assert opener.calls == 1  # 零重试
        assert gw.provider_call_count == 1
        assert gw.breaker_open(_call()) is False  # 阈值为 1 也不开路：换 Key 才能解决
        assert [r.result_status for r in records] == [CODE_PROVIDER_AUTH_FAILED]
        assert _snapshot(gw).reserved_tokens == 0

    def test_auth_error_on_first_chunk_is_not_retried(self) -> None:
        gw = _gateway(config=_config(attempts=3, threshold=1))
        opener = _Opener(_FakeStream([_HttpError(401)]), _FakeStream([_Chunk("never")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of))
        assert exc.value.code == CODE_PROVIDER_AUTH_FAILED
        assert opener.calls == 1
        assert gw.breaker_open(_call()) is False

    @pytest.mark.parametrize("error", [_HttpError(400), ValueError("bad payload")])
    def test_permanent_error_is_not_retried(self, error: BaseException) -> None:
        gw = _gateway(config=_config(attempts=3))
        opener = _Opener(error, _FakeStream([_Chunk("never")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of))
        assert exc.value.code == CODE_PROVIDER_UNAVAILABLE
        assert opener.calls == 1

    def test_attempt_that_consumes_deadline_is_not_retried(self) -> None:
        """第一次尝试自身就耗掉 deadline：不再发起第二次尝试。"""
        clock = _FakeClock()
        records: list[UsageRecord] = []
        gw = _gateway(config=_config(attempts=5), clock=clock, records=records)
        opener = _Opener(_FakeStream([10.0, _HttpError(503)], clock), _FakeStream([_Chunk("x")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(deadline=5.0), _call(), opener, usage_of=_usage_of))
        assert exc.value.code == CODE_PROVIDER_TIMEOUT
        assert opener.calls == 1
        assert [r.result_status for r in records] == [CODE_PROVIDER_TIMEOUT]
        assert _snapshot(gw, _ctx(deadline=5.0)).reserved_tokens == 0

    def test_deadline_checked_again_right_before_retry_attempt(self) -> None:
        """退避本身越界（例如睡过头）时，下一次尝试开始前再拦一次，不发请求。"""
        clock = _FakeClock()
        gw = ProviderGateway(
            config=_config(attempts=5, backoff=1.0),
            clock=clock,
            sleep=lambda delay: clock.advance(delay + 10.0),  # 模拟睡过头
        )
        opener = _Opener(_HttpError(503), _FakeStream([_Chunk("never")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(deadline=5.0), _call(), opener, usage_of=_usage_of))
        assert exc.value.code == CODE_PROVIDER_TIMEOUT
        assert opener.calls == 1
        assert gw.provider_call_count == 1

    def test_backoff_that_would_cross_deadline_is_not_slept(self) -> None:
        slept: list[float] = []
        gw = _gateway(config=_config(attempts=5, backoff=100.0), slept=slept)
        opener = _Opener(_HttpError(503), _FakeStream([_Chunk("x")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(deadline=5.0), _call(), opener, usage_of=_usage_of))
        assert exc.value.code == CODE_PROVIDER_TIMEOUT
        assert slept == []
        assert opener.calls == 1


class TestErrorAfterFirstChunk:
    def test_error_after_first_chunk_is_not_retried_and_rolls_back(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(config=_config(attempts=3), records=records)
        stream = _FakeStream([_Chunk("已"), _Chunk("输出"), _HttpError(503)])
        opener = _Opener(stream, _FakeStream([_Chunk("重放")]))
        received: list[str] = []
        with pytest.raises(GovernanceError) as exc:
            for chunk in gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of):
                received.append(chunk.text)
        assert exc.value.code == CODE_PROVIDER_UNAVAILABLE
        assert isinstance(exc.value.__cause__, _HttpError)  # 原始错误保留在因果链上
        assert received == ["已", "输出"]  # 已交出的内容不重放
        assert opener.calls == 1
        assert gw.provider_call_count == 1
        assert [r.result_status for r in records] == [CODE_PROVIDER_UNAVAILABLE]
        snap = _snapshot(gw)
        assert snap.reserved_tokens == 0 and snap.spent_tokens == 0

    def test_transient_error_after_first_chunk_counts_as_breaker_failure(self) -> None:
        gw = _gateway(config=_config(threshold=1))
        stream = _FakeStream([_Chunk("a"), ConnectionError("reset by peer")])
        with pytest.raises(GovernanceError):
            list(gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of))
        assert gw.breaker_open(_call()) is True

    @pytest.mark.parametrize("error", [_HttpError(401), ValueError("decode error")])
    def test_non_transient_error_after_first_chunk_does_not_trip_breaker(
        self, error: BaseException
    ) -> None:
        gw = _gateway(config=_config(threshold=1))
        stream = _FakeStream([_Chunk("a"), error])
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of))
        assert exc.value.code == CODE_PROVIDER_UNAVAILABLE
        assert gw.breaker_open(_call()) is False

    def test_first_chunk_records_breaker_success(self) -> None:
        """首个 chunk 到达即记熔断成功（与 call 中 executor 返回同一时点），不等流结束。

        第二个流交出首块后被调用方关闭——若成功只在流结束时记，此前那次失败不会清零，
        第三次失败就凑够阈值 2 而开路。
        """
        gw = _gateway(config=_config(attempts=1, threshold=2))
        with pytest.raises(GovernanceError):
            list(gw.stream_call(_ctx(), _call(), _Opener(_HttpError(503)), usage_of=_usage_of))
        it = gw.stream_call(_ctx(), _call(), _Opener(_FakeStream([_Chunk("a"), _Chunk("b")])),
                            usage_of=_usage_of)
        next(it)
        it.close()
        with pytest.raises(GovernanceError):
            list(gw.stream_call(_ctx(), _call(), _Opener(_HttpError(503)), usage_of=_usage_of))
        assert gw.breaker_open(_call()) is False  # 不是连续两次失败

    def test_half_open_probe_stream_releases_breaker_at_first_chunk(self) -> None:
        """半开期的探测流交出首块即闭合熔断：其他请求不必等这条流结束。"""
        clock = _FakeClock()
        config = GovernanceConfig(
            budget=BudgetConfig(policy=BudgetPolicy.BLOCK, daily_token_cap=1_000_000),
            profiles={
                ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                    timeout_seconds=10.0,
                    retry=RetryPolicy(max_attempts=1),
                    breaker=CircuitBreakerPolicy(
                        failure_threshold=1, recovery_seconds=30.0, half_open_max_calls=1
                    ),
                )
            },
        )
        gw = _gateway(config=config, clock=clock)
        with pytest.raises(GovernanceError):
            list(gw.stream_call(_ctx(), _call(), _Opener(_HttpError(503)), usage_of=_usage_of))
        assert gw.breaker_open(_call()) is True

        clock.advance(31.0)  # 进入半开
        probe = gw.stream_call(_ctx(), _call(), _Opener(_FakeStream([_Chunk("a"), _Chunk("b")])),
                               usage_of=_usage_of)
        next(probe)  # 探测流仍在进行中
        assert gw.breaker_open(_call()) is False
        other = list(gw.stream_call(_ctx(), _call(), _Opener(_FakeStream([_Chunk("x")])),
                                    usage_of=_usage_of))
        assert _texts(other) == ["x"]
        assert _texts(list(probe)) == ["b"]


class TestDeadlineBetweenChunks:
    def test_consumer_side_delay_past_deadline_aborts_before_next_pull(self) -> None:
        clock = _FakeClock()
        records: list[UsageRecord] = []
        gw = _gateway(clock=clock, records=records)
        stream = _FakeStream([_Chunk("a"), _Chunk("b")])
        ctx = _ctx(deadline=5.0)
        it = gw.stream_call(ctx, _call(), _Opener(stream), usage_of=_usage_of)
        assert next(it).text == "a"
        clock.advance(6.0)  # 调用方处理第一块耗时越过 deadline
        with pytest.raises(GovernanceError) as exc:
            next(it)
        assert exc.value.code == CODE_PROVIDER_TIMEOUT
        assert stream.produced == 1  # 没再向上游要下一块
        assert stream.closed is True
        assert [r.result_status for r in records] == [CODE_PROVIDER_TIMEOUT]
        assert _snapshot(gw, ctx).reserved_tokens == 0
        assert gw.breaker_open(_call()) is False

    def test_slow_upstream_chunk_is_delivered_then_deadline_aborts(self) -> None:
        """已取到的 chunk 总会交出；越界后不再拉下一块。"""
        clock = _FakeClock()
        gw = _gateway(clock=clock, config=_config(threshold=1))
        stream = _FakeStream([_Chunk("a"), 6.0, _Chunk("b"), _Chunk("c")], clock)
        received: list[str] = []
        with pytest.raises(GovernanceError) as exc:
            for chunk in gw.stream_call(_ctx(deadline=5.0), _call(), _Opener(stream),
                                        usage_of=_usage_of):
                received.append(chunk.text)
        assert exc.value.code == CODE_PROVIDER_TIMEOUT
        assert received == ["a", "b"]
        assert stream.produced == 2
        assert gw.breaker_open(_call()) is False  # deadline 是调用方的时间预算，不计熔断


class TestCallerClose:
    def test_close_mid_stream_releases_reservation_and_closes_upstream(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(config=_config(threshold=1), records=records)
        stream = _FakeStream([_Chunk("a"), _Chunk("b"), _Chunk("c", usage=_usage())])
        it = gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of)
        assert next(it).text == "a"
        it.close()
        assert stream.closed is True
        assert stream.produced == 1
        assert [r.result_status for r in records] == [CODE_USAGE_UNKNOWN]
        assert records[0].usage_known is False and records[0].input_tokens is None
        snap = _snapshot(gw)
        assert snap.reserved_tokens == 0 and snap.spent_tokens == 0
        assert gw.breaker_open(_call()) is False  # 阈值为 1：不计熔断失败

    def test_generator_upstream_receives_generator_exit(self) -> None:
        """上游是生成器（如 ChatOpenAI._stream）时，关闭会传到它的 finally 释放连接。"""
        released: list[bool] = []

        def upstream() -> Iterator[_Chunk]:
            try:
                yield _Chunk("a")
                yield _Chunk("b")
            finally:
                released.append(True)

        gw = _gateway()
        it = gw.stream_call(_ctx(), _call(), upstream, usage_of=_usage_of)
        next(it)
        it.close()
        assert released == [True]

    def test_exception_thrown_in_by_caller_does_not_leak_reservation(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records=records)
        stream = _FakeStream([_Chunk("a"), _Chunk("b")])
        it = gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of)
        next(it)
        with pytest.raises(KeyError):
            it.throw(KeyError("上层逻辑错误"))
        assert stream.closed is True
        assert _snapshot(gw).reserved_tokens == 0
        assert [r.result_status for r in records] == [CODE_USAGE_UNKNOWN]

    def test_keyboard_interrupt_mid_stream_does_not_leak_reservation(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records=records)
        stream = _FakeStream([_Chunk("a"), KeyboardInterrupt()])
        it = gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of)
        next(it)
        with pytest.raises(KeyboardInterrupt):
            next(it)
        assert _snapshot(gw).reserved_tokens == 0
        assert [r.result_status for r in records] == [CODE_USAGE_UNKNOWN]


#: 非 ``Exception`` 的 ``BaseException``：进程或任务要停下的信号，不是 provider 的失败。
_INTERRUPTS = [
    pytest.param(KeyboardInterrupt, id="KeyboardInterrupt"),
    pytest.param(SystemExit, id="SystemExit"),
    pytest.param(asyncio.CancelledError, id="CancelledError"),
]


class TestInterruptPassthrough:
    """中断信号首块之前与之后同一处理：原样上抛，不分类、不计熔断、不重试，预留恰好回滚一次。

    回归：首块之前的尝试走重试内核，那里此前 ``except BaseException`` 之后一律分类，
    ``KeyboardInterrupt`` 被判成永久错误、包装成 ``PROVIDER_UNAVAILABLE``；首块之后只接
    ``Exception``，中断原样上抛——同一个 Ctrl-C，落在首块前后结果不一样。
    """

    @pytest.mark.parametrize("interrupt", _INTERRUPTS)
    @pytest.mark.parametrize("where", ["opener", "first_chunk", "mid_stream"])
    def test_interrupt_propagates_unchanged_before_and_after_first_chunk(
        self, where: str, interrupt: type[BaseException]
    ) -> None:
        records: list[UsageRecord] = []
        ledger = _SpyLedger()
        gw = _gateway(config=_config(attempts=3, threshold=1), records=records, ledger=ledger)
        error = interrupt()
        source: object = {
            "opener": error,
            "first_chunk": _FakeStream([error]),
            "mid_stream": _FakeStream([_Chunk("a"), error]),
        }[where]
        opener = _Opener(source, _FakeStream([_Chunk("never")]))
        received: list[str] = []
        with pytest.raises(interrupt) as exc:
            for chunk in gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of):
                received.append(chunk.text)
        assert exc.value is error  # 原样上抛，没有包装成 GovernanceError
        assert received == (["a"] if where == "mid_stream" else [])
        assert opener.calls == 1  # max_attempts=3 也不重试
        assert gw.provider_call_count == 1
        assert gw.breaker_open(_call()) is False  # 阈值为 1：不计熔断失败
        assert all(stream.closed for stream in opener.opened)  # 打开过的上游流都已关闭
        assert ledger.events == [("rollback",)]  # 恰好结清一次
        assert _snapshot(gw).reserved_tokens == 0
        assert [r.result_status for r in records] == [CODE_USAGE_UNKNOWN]


class TestAdmissionBeforeSending:
    def test_rate_limited_stream_yields_nothing_and_records_failure(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(
            config=_config(rate_limit=RateLimitPolicy(bucket_capacity_tokens=1,
                                                      refill_tokens_per_second=1.0)),
            records=records,
        )
        opener = _Opener(_FakeStream([_Chunk("never")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of))
        assert exc.value.code == CODE_RATE_LIMITED
        assert opener.calls == 0
        assert gw.provider_call_count == 0
        assert [r.result_status for r in records] == [CODE_RATE_LIMITED]

    def test_budget_blocked_stream_yields_nothing_and_records_failure(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(
            config=_config(budget=BudgetConfig(policy=BudgetPolicy.BLOCK, daily_token_cap=100)),
            records=records,
        )
        opener = _Opener(_FakeStream([_Chunk("never")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(), _call(est_in=100, max_out=200), opener,
                                usage_of=_usage_of))
        assert exc.value.code == CODE_BUDGET_EXCEEDED
        assert opener.calls == 0
        assert gw.provider_call_count == 0
        assert [r.result_status for r in records] == [CODE_BUDGET_EXCEEDED]

    def test_open_breaker_rejects_before_opening_stream(self) -> None:
        gw = _gateway(config=_config(attempts=1, threshold=1))
        with pytest.raises(GovernanceError):
            list(gw.stream_call(_ctx(), _call(), _Opener(_HttpError(503)), usage_of=_usage_of))
        assert gw.breaker_open(_call()) is True

        opener = _Opener(_FakeStream([_Chunk("never")]))
        before = gw.provider_call_count
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of))
        assert exc.value.code == CODE_CIRCUIT_OPEN
        assert exc.value.retry_after_seconds is not None
        assert opener.calls == 0
        assert gw.provider_call_count == before

    def test_breaker_opened_by_call_also_blocks_stream_call(self) -> None:
        """两条通道共用同一个熔断器（同 provider + 模型）。"""
        gw = _gateway(config=_config(attempts=1, threshold=1))
        with pytest.raises(GovernanceError):
            gw.call(_ctx(), _call(), lambda: (_ for _ in ()).throw(_HttpError(503)))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(), _call(), _Opener(_FakeStream([])), usage_of=_usage_of))
        assert exc.value.code == CODE_CIRCUIT_OPEN

    def test_ungranted_capability_denied_before_any_call(self) -> None:
        gw = _gateway()
        ctx = _ctx(capabilities=frozenset({Capability.CALCULATE}))
        opener = _Opener(_FakeStream([_Chunk("never")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(ctx, _call(), opener, usage_of=_usage_of))
        assert exc.value.code == CODE_CAPABILITY_DENIED
        assert opener.calls == 0


class TestNotImplementedPassthrough:
    @pytest.mark.parametrize("where", ["opener", "first_chunk"])
    def test_not_implemented_is_reraised_as_is(self, where: str) -> None:
        """上游声明不支持流式：不是 provider 失败——不重试、不计熔断、不落账、预留回滚。"""
        records: list[UsageRecord] = []
        gw = _gateway(config=_config(attempts=3, threshold=1), records=records)
        source = (
            NotImplementedError() if where == "opener"
            else _FakeStream([NotImplementedError()])
        )
        opener = _Opener(source, _FakeStream([_Chunk("never")]))
        with pytest.raises(NotImplementedError):
            list(gw.stream_call(_ctx(), _call(), opener, usage_of=_usage_of))
        assert opener.calls == 1
        assert records == []
        assert gw.breaker_open(_call()) is False
        assert _snapshot(gw).reserved_tokens == 0


class _SlowLedger(InMemoryBudgetLedger):
    """预留时推进假时钟：模拟共享账本（Redis/Postgres adapter）慢到在发送前就耗尽 deadline。"""

    def __init__(self, clock: _FakeClock) -> None:
        super().__init__()
        self._fake_clock = clock
        self.delay = 0.0

    def reserve(self, key: str, tokens: int, config: BudgetConfig) -> BudgetVerdict:
        self._fake_clock.advance(self.delay)
        return super().reserve(key, tokens, config)


class TestHalfOpenProbeWithoutVerdict:
    """流式探测在首块之前没出成败判定就结束，同样归还半开探测名额（与 ``call`` 同一处理）。

    回归：名额此前只在成功（首块到达）或瞬时失败时清零；以限流/预算拒绝、401/403、永久错误、
    发送前用尽 deadline 或 ``NotImplementedError`` 结束的探测流把名额永久占住，此后每个请求
    （流式与非流式共用熔断器）都是 ``CIRCUIT_OPEN``，推进时钟也没用。
    """

    @staticmethod
    def _enter_half_open(gw: ProviderGateway, clock: _FakeClock) -> None:
        with pytest.raises(GovernanceError):
            list(gw.stream_call(_ctx(thread="a"), _call(), _Opener(_HttpError(503)),
                                usage_of=_usage_of))
        assert gw.breaker_open(_call()) is True
        clock.advance(31.0)  # 默认恢复窗口 30s，半开只放 1 个探测

    @staticmethod
    def _assert_slot_was_released(gw: ProviderGateway) -> None:
        """没出判定：熔断既没闭合也没重新开路；名额已还，下一个探测流立刻放行，首块到达即闭合。"""
        assert gw.breaker_open(_call()) is True
        out = list(gw.stream_call(_ctx(thread="b"), _call(), _Opener(_FakeStream([_Chunk("ok")])),
                                  usage_of=_usage_of))
        assert _texts(out) == ["ok"]
        assert gw.breaker_open(_call()) is False

    def test_rate_limited_probe_releases_slot(self) -> None:
        clock = _FakeClock()
        config = _config(attempts=1, threshold=1,
                         rate_limit=RateLimitPolicy(max_requests_per_minute=1))
        gw = _gateway(config=config, clock=clock)
        self._enter_half_open(gw, clock)  # 开路那次用掉了主体 a 这一分钟唯一的请求配额

        opener = _Opener(_FakeStream([_Chunk("never")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(thread="a"), _call(), opener, usage_of=_usage_of))
        assert exc.value.code == CODE_RATE_LIMITED
        assert opener.calls == 0
        self._assert_slot_was_released(gw)

    def test_budget_rejected_probe_releases_slot(self) -> None:
        clock = _FakeClock()
        config = _config(attempts=1, threshold=1,
                         budget=BudgetConfig(policy=BudgetPolicy.BLOCK, daily_token_cap=1_000))
        gw = _gateway(config=config, clock=clock)
        self._enter_half_open(gw, clock)

        opener = _Opener(_FakeStream([_Chunk("never")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(thread="a"), _call(est_in=5_000), opener,  # 预留 5,200
                                usage_of=_usage_of))
        assert exc.value.code == CODE_BUDGET_EXCEEDED
        assert opener.calls == 0
        self._assert_slot_was_released(gw)

    @pytest.mark.parametrize(
        ("where", "error", "expected_code"),
        [
            ("opener", _HttpError(401), CODE_PROVIDER_AUTH_FAILED),
            ("first_chunk", _HttpError(403), CODE_PROVIDER_AUTH_FAILED),
            ("opener", _HttpError(400), CODE_PROVIDER_UNAVAILABLE),
            ("first_chunk", ValueError("bad payload"), CODE_PROVIDER_UNAVAILABLE),  # 未知 → 永久
        ],
        ids=["opener-401", "first-chunk-403", "opener-400", "first-chunk-unknown-error"],
    )
    def test_auth_or_permanent_error_before_first_chunk_releases_slot(
        self, where: str, error: BaseException, expected_code: str
    ) -> None:
        """阈值为 1：若 401/403 或永久错误计了熔断，这里会重新开路，下一个请求还得再等 30s。"""
        clock = _FakeClock()
        gw = _gateway(config=_config(attempts=1, threshold=1), clock=clock)
        self._enter_half_open(gw, clock)

        source = error if where == "opener" else _FakeStream([error])
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(thread="a"), _call(), _Opener(source), usage_of=_usage_of))
        assert exc.value.code == expected_code
        self._assert_slot_was_released(gw)

    def test_probe_whose_deadline_runs_out_before_sending_releases_slot(self) -> None:
        clock = _FakeClock()
        ledger = _SlowLedger(clock)
        gw = _gateway(config=_config(attempts=1, threshold=1), clock=clock, ledger=ledger)
        self._enter_half_open(gw, clock)

        ledger.delay = 10.0  # 预留就耗掉 10s，超过 5s 的 deadline
        opener = _Opener(_FakeStream([_Chunk("never")]))
        with pytest.raises(GovernanceError) as exc:
            list(gw.stream_call(_ctx(thread="a", deadline=5.0), _call(), opener,
                                usage_of=_usage_of))
        assert exc.value.code == CODE_PROVIDER_TIMEOUT
        assert opener.calls == 0  # 一次都没发出
        ledger.delay = 0.0
        self._assert_slot_was_released(gw)

    @pytest.mark.parametrize("where", ["opener", "first_chunk"])
    def test_not_implemented_probe_releases_slot_for_the_fallback_call(self, where: str) -> None:
        """上游不支持流式时调用方回退到 ``call``：名额不还，回退的这次 ``call`` 就被这条流
        自己挡在熔断外，而且此后永远如此。"""
        clock = _FakeClock()
        gw = _gateway(config=_config(attempts=1, threshold=1), clock=clock)
        self._enter_half_open(gw, clock)

        source = (
            NotImplementedError() if where == "opener"
            else _FakeStream([NotImplementedError()])
        )
        with pytest.raises(NotImplementedError):
            list(gw.stream_call(_ctx(thread="a"), _call(), _Opener(source), usage_of=_usage_of))
        assert gw.breaker_open(_call()) is True  # 不计熔断：没闭合，也没重新开路

        fallback = gw.call(_ctx(thread="a"), _call(), lambda: ("整块回答", _usage()))
        assert fallback.value == "整块回答"
        assert gw.breaker_open(_call()) is False

    @pytest.mark.parametrize("interrupt", _INTERRUPTS)
    @pytest.mark.parametrize("where", ["opener", "first_chunk"])
    def test_interrupted_probe_releases_slot(
        self, where: str, interrupt: type[BaseException]
    ) -> None:
        """中断原样上抛、不经错误分类，名额照样在重试内核的 finally 里归还。"""
        clock = _FakeClock()
        gw = _gateway(config=_config(attempts=1, threshold=1), clock=clock)
        self._enter_half_open(gw, clock)

        error = interrupt()
        source = error if where == "opener" else _FakeStream([error])
        with pytest.raises(interrupt):
            list(gw.stream_call(_ctx(thread="a"), _call(), _Opener(source), usage_of=_usage_of))
        self._assert_slot_was_released(gw)


def _drive(scenario: str, gw: ProviderGateway, clock: _FakeClock) -> None:
    """按场景跑一条流，吞掉预期内的异常（结清方式由调用方断言）。"""
    ctx = _ctx(deadline=5.0)
    if scenario == "normal_with_usage":
        stream = _FakeStream([_Chunk("a"), _Chunk("", usage=_usage(90, 40))])
        list(gw.stream_call(ctx, _call(), _Opener(stream), usage_of=_usage_of))
    elif scenario == "normal_without_usage":
        list(gw.stream_call(ctx, _call(), _Opener(_FakeStream([_Chunk("a")])), usage_of=_usage_of))
    elif scenario == "caller_close":
        it = gw.stream_call(ctx, _call(), _Opener(_FakeStream([_Chunk("a"), _Chunk("b")])),
                            usage_of=_usage_of)
        next(it)
        it.close()
    elif scenario in ("error_before_first_chunk", "error_after_first_chunk", "deadline_mid_stream"):
        script: list[object] = {
            "error_before_first_chunk": [_HttpError(400)],
            "error_after_first_chunk": [_Chunk("a"), _HttpError(503)],
            "deadline_mid_stream": [_Chunk("a"), 6.0, _Chunk("b"), _Chunk("c")],
        }[scenario]
        with pytest.raises(GovernanceError):
            list(gw.stream_call(ctx, _call(), _Opener(_FakeStream(script, clock)),
                                usage_of=_usage_of))
    elif scenario == "not_implemented":
        with pytest.raises(NotImplementedError):
            list(gw.stream_call(ctx, _call(), _Opener(NotImplementedError()), usage_of=_usage_of))
    elif scenario in ("interrupt_before_first_chunk", "interrupt_mid_stream"):
        interrupted: list[object] = {
            "interrupt_before_first_chunk": [KeyboardInterrupt()],
            "interrupt_mid_stream": [_Chunk("a"), KeyboardInterrupt()],
        }[scenario]
        with pytest.raises(KeyboardInterrupt):
            list(gw.stream_call(ctx, _call(), _Opener(_FakeStream(interrupted)),
                                usage_of=_usage_of))
    elif scenario == "thrown_in":
        it = gw.stream_call(ctx, _call(), _Opener(_FakeStream([_Chunk("a"), _Chunk("b")])),
                            usage_of=_usage_of)
        next(it)
        with pytest.raises(RuntimeError):
            it.throw(RuntimeError("上层逻辑错误"))
    else:  # pragma: no cover
        raise AssertionError(scenario)


class TestReservationResolvedExactlyOnce:
    """每条退出路径恰好结清一次预留；settle 与 rollback 按规格区分（内存账本上效果相同）。"""

    @pytest.mark.parametrize(
        ("scenario", "expected"),
        [
            ("normal_with_usage", ("settle", 130)),
            ("normal_without_usage", ("settle_unknown",)),  # usage 未知：不估算账单，额度按预留计入
            ("caller_close", ("settle_unknown",)),  # 提前关闭：按 usage 未知结清
            ("error_before_first_chunk", ("rollback",)),
            ("error_after_first_chunk", ("rollback",)),
            ("deadline_mid_stream", ("rollback",)),
            ("not_implemented", ("rollback",)),
            ("interrupt_before_first_chunk", ("rollback",)),  # 中断原样上抛，finally 兜底
            ("interrupt_mid_stream", ("rollback",)),
            ("thrown_in", ("rollback",)),  # finally 兜底
        ],
    )
    def test_each_exit_path_resolves_once(self, scenario: str, expected: tuple) -> None:
        clock = _FakeClock()
        ledger = _SpyLedger()
        gw = _gateway(clock=clock, ledger=ledger)
        _drive(scenario, gw, clock)
        assert len(ledger.events) == 1, ledger.events
        assert ledger.events[0][: len(expected)] == expected
        assert _snapshot(gw, _ctx(deadline=5.0)).reserved_tokens == 0


class TestHundredStreamInvariant:
    """与 ``call`` 的 100 次不变式对应：每个请求恰好一条账，预留全部结清，无负余额。"""

    def test_mixed_outcomes_all_accounted(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(config=_config(attempts=1), records=records)
        expected_spent = 0
        for i in range(100):
            ctx = _ctx(thread="t", request_id=f"req-{i}")
            kind = i % 4
            if kind == 0:  # 正常结束、带 usage
                stream = _FakeStream([_Chunk("a"), _Chunk("", usage=_usage(10, 5))])
                list(gw.stream_call(ctx, _call(), _Opener(stream), usage_of=_usage_of))
                expected_spent += 15
            elif kind == 1:  # 正常结束、无 usage
                stream = _FakeStream([_Chunk("a")])
                list(gw.stream_call(ctx, _call(), _Opener(stream), usage_of=_usage_of))
            elif kind == 2:  # 调用方提前关闭
                stream = _FakeStream([_Chunk("a"), _Chunk("b")])
                it = gw.stream_call(ctx, _call(), _Opener(stream), usage_of=_usage_of)
                next(it)
                it.close()
            else:  # 中途失败
                stream = _FakeStream([_Chunk("a"), ValueError("boom")])
                with pytest.raises(GovernanceError):
                    list(gw.stream_call(ctx, _call(), _Opener(stream), usage_of=_usage_of))

        assert len(records) == 100
        assert sorted(r.request_id for r in records) == sorted(f"req-{i}" for i in range(100))
        snap = _snapshot(gw, _ctx(thread="t"))
        assert snap.reserved_tokens == 0
        assert snap.spent_tokens == expected_spent == 25 * 15
        assert gw.provider_call_count == 100


class TestEarlyCloseCannotDodgeTheTokenCap:
    """review 2026-09-30：提前关闭的流按 0 token 结清，反复提前关闭就能绕过每日 token 额度。

    额度是失控保护，拿不到账单（provider 不回 usage、调用方提前关闭）时也必须成立：请求已经
    发出、上游可能已经计费，预留额度计入"未确认用量"；账单侧照旧不估算。
    """

    def test_repeated_early_close_hits_the_daily_cap(self) -> None:
        config = _config(budget=BudgetConfig(policy=BudgetPolicy.BLOCK, daily_token_cap=1_000))
        gw = _gateway(config=config)
        accepted = 0
        for _ in range(10):  # 每次预留 100 + 200 = 300：第 4 次起超额
            stream = _FakeStream([_Chunk("a"), _Chunk("b")])
            it = gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of)
            try:
                next(it)
            except GovernanceError as exc:
                assert exc.code == "BUDGET_EXCEEDED"
                break
            accepted += 1
            it.close()
        assert accepted == 3, "提前关闭不得让额度永远不涨"
        snap = _snapshot(gw)
        assert snap.unconfirmed_tokens == 900 and snap.spent_tokens == 0, "未确认用量与真实账单分开记"
        assert snap.reserved_tokens == 0

    def test_unknown_usage_is_not_reported_as_spend(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records=records)
        stream = _FakeStream([_Chunk("a")])
        list(gw.stream_call(_ctx(), _call(), _Opener(stream), usage_of=_usage_of))
        snap = _snapshot(gw)
        assert snap.spent_tokens == 0 and snap.spent_cost_usd == 0.0
        assert snap.unconfirmed_tokens == 300
        assert records[0].usage_known is False and records[0].input_tokens is None
