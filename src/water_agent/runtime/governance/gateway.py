"""P2.5 Provider Gateway（Step 12）：所有远程模型调用的唯一入口。

职责顺序（规格 §4）：

```
能力校验 → 熔断检查 → 限流（预发送）→ 预算预留 → 有界重试执行（受 deadline 约束）
        → provider usage 对账 / rollback → UsageRecord 落账 + trace 事件
```

硬约束：

1. **禁止 silent fallback**：本层永不换模型、永不改用本地检索/fixture、永不伪造 usage。
   失败一律抛 :class:`GovernanceError`，由调用方决定 blocked 语义。
2. **只对瞬时错误重试**：认证/授权/参数/内容策略错误零重试（`_classify_error`）。
   429 遵守 ``Retry-After``；无该头时指数退避 + 抖动。
3. **deadline 约束总等待**：重试的累计等待不得越过 ``RequestContext.deadline_seconds``，
   越界立即 ``PROVIDER_TIMEOUT``。
4. **usage 不可得必须标记**：``usage_known=False`` + ``USAGE_UNKNOWN``，
   token/成本字段留空（契约层已强制）。
5. **不导入 provider SDK**：真实请求由调用方注入的 ``executor`` 完成，
   gateway 只负责治理。这样 fake transport 可完整覆盖所有分支。

流式通道 :meth:`ProviderGateway.stream_call` 与 :meth:`ProviderGateway.call` 共用发送前
四步与失败落账（同一组私有方法），只在执行段不同：**重试只发生在第一个 chunk 之前**
（已输出的内容撤不回），deadline 在每取一个 chunk 前检查，usage 取流里最后一个非空值，
调用方提前关闭时预留按 usage 未知结清且不计熔断失败。上游迭代器同样由调用方注入
（``opener``），本层不碰任何 SDK 的流式接口。
"""
from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import islice

from water_agent.runtime.governance.config import (
    DEFAULT_GOVERNANCE_CONFIG,
    CircuitBreakerPolicy,
    GovernanceConfig,
    ProviderProfile,
)
from water_agent.runtime.governance.contracts import (
    CODE_BUDGET_EXCEEDED,
    CODE_CIRCUIT_OPEN,
    CODE_PROVIDER_AUTH_FAILED,
    CODE_PROVIDER_TIMEOUT,
    CODE_PROVIDER_UNAVAILABLE,
    CODE_RATE_LIMITED,
    CacheStatus,
    GovernanceError,
    ProviderCall,
    ProviderKind,
    RequestContext,
    UsageRecord,
)
from water_agent.runtime.governance.stores import (
    BudgetLedger,
    InMemoryBudgetLedger,
    InMemoryRateLimitStore,
    RateLimitStore,
)

#: 明确的瞬时 HTTP 状态码。其余 4xx 一律视为永久错误（零重试）。
TRANSIENT_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})
#: 认证/授权失败（永久）。403 是 rerank 的已知阻塞码，必须零重试。
AUTH_HTTP_CODES = frozenset({401, 403})

#: 无状态码时按异常类名识别瞬时错误。**沿 MRO 匹配**，不只看 ``type(exc).__name__``：
#: langchain-openai 1.6 把 openai SDK 的 ``APITimeoutError`` / ``APIConnectionError``
#: 重包装成子类 ``OpenAITimeoutError`` / ``OpenAIConnectionError`` 再抛，只比对类名时
#: ChatOpenAI 的超时与连接失败被判成永久错误、零重试、不计熔断（实测）。
#: httpx 传输层基类也在此：流式输出中途断连时 openai SDK 不再包装，原始的
#: ``RemoteProtocolError`` / ``ReadError`` 直接冒出来（openai 3.x 用的是 ``httpx2``
#: 包，类名与 httpx 相同，按名匹配两者都覆盖）。
_TRANSIENT_ERROR_NAMES = frozenset(
    {
        "URLError",
        "APIConnectionError",
        "APITimeoutError",
        "ConnectTimeout",
        "ReadTimeout",
        "TimeoutException",  # httpx：Connect/Read/Write/PoolTimeout 的基类
        "NetworkError",  # httpx：Connect/Read/Write/CloseError 的基类
        "RemoteProtocolError",  # httpx：对端没发完响应就断开（流式中途断流的典型形态）
    }
)
_AUTH_ERROR_NAMES = frozenset({"AuthenticationError", "PermissionDeniedError"})


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """provider 返回的真实用量。

    ``known=False`` 表示 provider 未返回 usage —— 此时 gateway 记
    ``USAGE_UNKNOWN``，**不估算成本**。
    """

    known: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @classmethod
    def unknown(cls) -> ProviderUsage:
        return cls(known=False)

    @classmethod
    def of(cls, *, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> ProviderUsage:
        return cls(
            known=True,
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            cached_input_tokens=max(0, cached_input_tokens),
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class GatewayResult[T]:
    """gateway 调用结果：provider 产物 + 账本条目。"""

    value: T
    usage_record: UsageRecord


class _ErrorClass:
    """错误分类结果。"""

    TRANSIENT = "transient"
    AUTH = "auth"
    PERMANENT = "permanent"


def _http_status_of(exc: BaseException) -> int | None:
    """尽力提取 HTTP 状态码，不导入任何 provider SDK。

    覆盖 ``urllib.error.HTTPError.code``、openai/httpx 的 ``status_code``、
    以及部分 SDK 的 ``response.status_code``。
    """
    for attr in ("code", "status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value < 600:
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and 100 <= value < 600:
            return value
    return None


def _classify_error(exc: BaseException) -> str:
    """区分瞬时 / 认证 / 永久错误。

    分类依据优先级：HTTP 状态码 → 超时类型 → 异常类名特征（含父类，见
    :data:`_TRANSIENT_ERROR_NAMES`）。
    **默认永久**（fail closed 的另一面：不确定就不重试，避免放大故障与成本）。
    """
    status = _http_status_of(exc)
    if status is not None:
        if status in AUTH_HTTP_CODES:
            return _ErrorClass.AUTH
        if status in TRANSIENT_HTTP_CODES:
            return _ErrorClass.TRANSIENT
        return _ErrorClass.PERMANENT

    if isinstance(exc, TimeoutError):
        return _ErrorClass.TRANSIENT
    if isinstance(exc, ConnectionError):
        return _ErrorClass.TRANSIENT
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, ConnectionError)):
        return _ErrorClass.TRANSIENT

    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _TRANSIENT_ERROR_NAMES:
        return _ErrorClass.TRANSIENT
    if names & _AUTH_ERROR_NAMES:
        return _ErrorClass.AUTH
    return _ErrorClass.PERMANENT


def _retry_after_of(exc: BaseException) -> float | None:
    """提取 ``Retry-After``（秒）。支持 urllib 的 ``headers`` 与 SDK 的 ``response.headers``。"""
    for holder in (exc, getattr(exc, "response", None)):
        headers = getattr(holder, "headers", None)
        if headers is None:
            continue
        getter = getattr(headers, "get", None)
        if getter is None:
            continue
        raw = getter("Retry-After") or getter("retry-after")
        if raw is None:
            continue
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


@dataclass
class _BreakerState:
    consecutive_failures: int = 0
    opened_at: float | None = None
    half_open_calls: int = 0


@dataclass(frozen=True, slots=True)
class _Admission:
    """发送前四步（能力 → 熔断 → 限流 → 预算预留）全部通过后的放行凭据。

    预算预留句柄在这里：拿到它之后，每条退出路径都必须恰好结清一次
    （``_settle`` 对账或 ``_fail`` 回滚），否则失败的调用会永久占额度。
    """

    profile: ProviderProfile
    started: float
    deadline: float | None
    breaker_key: str
    budget_key: str
    reservation_id: str


def _close_quietly(iterator: object) -> None:
    """关闭上游迭代器（生成器 / SDK 流）以释放连接；清理本身出错不得掩盖治理结论。"""
    close = getattr(iterator, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001, S110 — 上游清理失败不影响主流程（无 logger 可用）
        pass


class ProviderGateway:
    """远程 provider 调用的统一治理入口。

    Args:
        config: 治理配置（限流/重试/熔断/预算/价格）。
        rate_limit_store: 限流存储；默认进程内（**不支持多实例**）。
        budget_ledger: 预算账本；默认进程内。
        on_usage: 账本回调（trace/metric 落盘）。异常不影响主流程。
        clock: 单调时钟注入（测试用）。
        sleep: 睡眠注入（测试用，避免真实等待）。
    """

    def __init__(
        self,
        *,
        config: GovernanceConfig | None = None,
        rate_limit_store: RateLimitStore | None = None,
        budget_ledger: BudgetLedger | None = None,
        on_usage: Callable[[UsageRecord], None] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config or DEFAULT_GOVERNANCE_CONFIG
        self.rate_limit_store = rate_limit_store or InMemoryRateLimitStore(clock=clock)
        self.budget_ledger = budget_ledger or InMemoryBudgetLedger()
        self._on_usage = on_usage
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._breakers: dict[str, _BreakerState] = {}
        self._lock = threading.Lock()
        #: 实际发往 provider 的请求次数（含重试）。测试断言缓存/限流是否真的省掉了调用。
        self.provider_call_count = 0

    # -- 键构造：主体走 hash，绝不用 subject 原文 ---------------------------------

    @staticmethod
    def rate_limit_key(ctx: RequestContext, call: ProviderCall) -> str:
        return f"{ctx.principal.principal_hash}|{call.required_capability.value}|{call.model}"

    @staticmethod
    def budget_key(ctx: RequestContext) -> str:
        """预算按主体 + 授权域聚合。周期切换由调用方在 key 里带日期段。"""
        return f"{ctx.principal.principal_hash}|{ctx.principal.auth_domain}"

    @staticmethod
    def breaker_key(call: ProviderCall) -> str:
        return f"{call.provider.value}|{call.model}"

    # -- 熔断 ------------------------------------------------------------------

    def _breaker(self, key: str) -> _BreakerState:
        state = self._breakers.get(key)
        if state is None:
            state = _BreakerState()
            self._breakers[key] = state
        return state

    def _check_breaker(self, key: str, policy: CircuitBreakerPolicy) -> None:
        """开路则抛 ``CIRCUIT_OPEN``；半开期超过探测上限也拒绝。"""
        now = self._clock()
        with self._lock:
            state = self._breaker(key)
            if state.opened_at is None:
                return
            elapsed = now - state.opened_at
            if elapsed < policy.recovery_seconds:
                raise GovernanceError(
                    CODE_CIRCUIT_OPEN,
                    "上游服务连续失败，已暂时停止请求以避免放大故障",
                    retry_after_seconds=round(policy.recovery_seconds - elapsed, 3),
                )
            # 半开：限量探测
            if state.half_open_calls >= policy.half_open_max_calls:
                raise GovernanceError(
                    CODE_CIRCUIT_OPEN,
                    "上游服务恢复探测中，请稍后重试",
                    retry_after_seconds=round(policy.recovery_seconds, 3),
                )
            state.half_open_calls += 1

    def _record_breaker_success(self, key: str) -> None:
        with self._lock:
            state = self._breaker(key)
            state.consecutive_failures = 0
            state.opened_at = None
            state.half_open_calls = 0

    def _record_breaker_failure(self, key: str, policy: CircuitBreakerPolicy) -> None:
        with self._lock:
            state = self._breaker(key)
            state.consecutive_failures += 1
            if state.consecutive_failures >= policy.failure_threshold:
                state.opened_at = self._clock()
                state.half_open_calls = 0

    def breaker_open(self, call: ProviderCall) -> bool:
        """当前是否开路（供观测与测试）。"""
        with self._lock:
            return self._breaker(self.breaker_key(call)).opened_at is not None

    # -- 主入口 ----------------------------------------------------------------

    def call[T](
        self,
        ctx: RequestContext,
        call: ProviderCall,
        executor: Callable[[], tuple[T, ProviderUsage]],
        *,
        cache_status: CacheStatus = CacheStatus.INELIGIBLE,
    ) -> GatewayResult[T]:
        """执行一次受治理的远程调用。

        Args:
            ctx: 请求上下文（主体 / deadline / 缓存与 PII 策略）。
            call: 调用意图（provider / 分类 / 模型 / 预估 token）。
            executor: 真实发送逻辑，返回 ``(产物, ProviderUsage)``。
                **必须**自行带上 provider 侧超时（gateway 传 profile 超时给调用方装配）。
            cache_status: 若调用方已判定缓存状态，透传进账本。

        Returns:
            :class:`GatewayResult`，含 provider 产物与 :class:`UsageRecord`。

        Raises:
            GovernanceError: 能力/限流/预算/熔断/超时/provider 失败，均为结构化错误。
        """
        # 1–4. 能力 → 熔断 → 限流 → 预算预留
        admission = self._admit(ctx, call, cache_status)

        # 5. 有界重试执行
        try:
            value, usage, retries = self._execute_with_retry(executor, admission)
        except GovernanceError as exc:
            self._fail(ctx, call, admission, exc.code, cache_status,
                       retry_after=exc.retry_after_seconds)
            raise

        # 6. usage 对账
        record = self._settle(ctx, call, admission, usage, retries, cache_status)
        return GatewayResult(value=value, usage_record=record)

    def stream_call[T](
        self,
        ctx: RequestContext,
        call: ProviderCall,
        opener: Callable[[], Iterator[T]],
        *,
        usage_of: Callable[[T], ProviderUsage | None],
        cache_status: CacheStatus = CacheStatus.INELIGIBLE,
    ) -> Iterator[T]:
        """执行一次受治理的**流式**远程调用，逐块产出上游 chunk（顺序与内容原样透传）。

        发送前四步与失败落账和 :meth:`call` 共用同一组私有方法，差别只在执行段：

        - **重试只发生在第一个 chunk 之前**：一次尝试 = ``opener()`` + 取首个 chunk，出错按
          :meth:`call` 的同一分类处理（瞬时错误退避重试、受 deadline 约束；认证与永久错误
          立即失败）。首个 chunk 交给调用方之后任何错误都不再重试——已输出的内容撤不回，
          重放只会重复输出：回滚预留、瞬时错误计一次熔断失败、落 ``PROVIDER_UNAVAILABLE``
          失败账并抛 :class:`GovernanceError`。
        - **deadline**：每次尝试前、以及每取下一个 chunk 前检查，越界抛 ``PROVIDER_TIMEOUT``
          （回滚 + 失败账；deadline 是调用方的时间预算，不计熔断）。已取到的 chunk 总会交出。
        - **熔断**：首个 chunk 到达即记成功（与 :meth:`call` 中 executor 返回是同一时点），
          半开期的探测流因此在首个 chunk 后就放行后续请求，不会占着探测名额直到流结束。
          代价：总在首块之后断流的上游每次都先记成功、再记一次失败，连续失败计数到不了
          开路阈值——首块之后不重试，所以不存在重试放大。
        - ``provider_call_count`` 与 :meth:`call` 同口径：每次尝试加 1。
        - **usage**：取流里最后一个非 ``None`` 的 ``usage_of(chunk)``（OpenAI 兼容接口开启
          ``include_usage`` 后 usage 在最后一个 chunk）。正常结束的对账与 :meth:`call` 一致：
          有 usage 按真实用量结清，没有就记 ``USAGE_UNKNOWN`` 并释放预留。空流（首个 chunk
          就是 ``StopIteration``）按正常结束、usage 未知处理。
        - **调用方提前关闭**（``GeneratorExit``：取消、上层预算超限）：关闭上游迭代器，预留按
          usage 未知结清（``actual_tokens=0``，不估算），落一条 ``USAGE_UNKNOWN`` 账，
          **不计熔断失败**——不是上游的错。
        - ``opener()`` 或首个 chunk 抛 ``NotImplementedError``（上游不支持流式）：不是 provider
          失败——不重试、不计熔断、不落账，回滚预留后原样抛出，调用方据此回退到 :meth:`call`。

        本方法是生成器：发送前检查与预算预留都在第一次 ``next()`` 时才发生，创建后未迭代即
        丢弃不占任何额度；拿到预留之后由 ``try/finally`` 保证每条退出路径恰好结清一次。

        Args:
            ctx: 同 :meth:`call`。
            call: 同 :meth:`call`。
            opener: 打开一次上游流（每次尝试调用一次）；**必须**自行带上 provider 侧超时。
            usage_of: 从单个 chunk 取 usage；该 chunk 不带 usage 时返回 ``None``。
            cache_status: 同 :meth:`call`。

        Raises:
            GovernanceError: 能力/限流/预算/熔断/超时/provider 失败，均为结构化错误。
            NotImplementedError: 上游不支持流式（见上），原样抛出。
        """
        admission = self._admit(ctx, call, cache_status)
        # 预留已存在：``resolved`` 记录是否已结清（settle 或 rollback 恰好一次），
        # finally 兜底没走到显式结清的退出路径（KeyboardInterrupt、调用方 throw() 等）。
        resolved = False
        upstream: Iterator[T] | None = None
        retries = 0

        def open_first() -> tuple[tuple[Iterator[T], list[T]], ProviderUsage]:
            # 一次"尝试"= 打开上游流 + 取首个 chunk。失败时先关掉本次打开的流，再交给重试内核。
            stream = opener()
            try:
                head = list(islice(stream, 1))  # 空流 → []
            except BaseException:
                _close_quietly(stream)
                raise
            return (stream, head), ProviderUsage.unknown()

        try:
            try:
                (upstream, head), _, retries = self._execute_with_retry(
                    open_first, admission, passthrough=(NotImplementedError,)
                )
            except GovernanceError as exc:
                resolved = True
                self._fail(ctx, call, admission, exc.code, cache_status,
                           retry_after=exc.retry_after_seconds)
                raise
            except NotImplementedError:
                resolved = True
                self.budget_ledger.rollback(admission.budget_key, admission.reservation_id)
                raise

            usage: ProviderUsage | None = None
            if head:
                chunk = head[0]
                while True:
                    observed = usage_of(chunk)
                    if observed is not None:
                        usage = observed
                    yield chunk
                    if admission.deadline is not None and self._clock() >= admission.deadline:
                        resolved = True
                        self._fail(ctx, call, admission, CODE_PROVIDER_TIMEOUT, cache_status)
                        raise GovernanceError(
                            CODE_PROVIDER_TIMEOUT, "流式输出超过时间预算（deadline），已中止"
                        )
                    try:
                        chunk = next(upstream)
                    except StopIteration:
                        break
                    except Exception as exc:
                        if _classify_error(exc) is _ErrorClass.TRANSIENT:
                            self._record_breaker_failure(
                                admission.breaker_key, admission.profile.breaker
                            )
                        resolved = True
                        self._fail(ctx, call, admission, CODE_PROVIDER_UNAVAILABLE, cache_status)
                        raise GovernanceError(
                            CODE_PROVIDER_UNAVAILABLE,
                            "上游服务在流式输出中途失败（已输出部分内容，不重试）："
                            f"{type(exc).__name__}",
                        ) from exc

            resolved = True
            self._settle(
                ctx, call, admission,
                usage if usage is not None else ProviderUsage.unknown(),
                retries, cache_status,
            )
        except GeneratorExit:
            if upstream is not None:
                _close_quietly(upstream)  # 先释放上游连接
            if not resolved:
                resolved = True
                self._settle(ctx, call, admission, ProviderUsage.unknown(), retries, cache_status)
            raise
        finally:
            if upstream is not None:
                _close_quietly(upstream)
            if not resolved:
                self.budget_ledger.rollback(admission.budget_key, admission.reservation_id)
                self._emit(
                    self._unknown_usage_record(
                        ctx, call, self._elapsed_ms(admission), retries, cache_status
                    )
                )

    # -- 共享步骤：call 与 stream_call 的发送前检查与结清 --------------------------

    def _admit(
        self, ctx: RequestContext, call: ProviderCall, cache_status: CacheStatus
    ) -> _Admission:
        """发送前四步：能力校验 → 熔断 → 限流 → 预算预留。

        任一步拒绝都发生在**发送前**（provider_call_count 不变）；限流与预算拒绝各落一条
        失败账，否则在观测里完全不可见。全部通过返回放行凭据（含预算预留句柄）。
        """
        profile = self.config.profile_for(call.provider)
        started = self._clock()
        deadline = None if ctx.deadline_seconds is None else started + ctx.deadline_seconds

        # 1. 能力校验（fail closed）
        ctx.principal.require(call.required_capability)

        # 2. 熔断
        breaker_key = self.breaker_key(call)
        self._check_breaker(breaker_key, profile.breaker)

        # 3. 限流（发送前）
        estimated = call.estimated_input_tokens + (call.max_output_tokens or 0)
        verdict = self.rate_limit_store.acquire(
            self.rate_limit_key(ctx, call), estimated, profile.rate_limit
        )
        if not verdict.allowed:
            self._emit(
                self._failure_record(ctx, call, CODE_RATE_LIMITED, 0.0, cache_status,
                                     retry_after=verdict.retry_after_seconds)
            )
            raise GovernanceError(
                CODE_RATE_LIMITED,
                "请求频率超过本地配额限制，请稍后重试",
                retry_after_seconds=verdict.retry_after_seconds,
            )

        # 4. 预算预留
        budget_key = self.budget_key(ctx)
        budget = self.budget_ledger.reserve(budget_key, estimated, self.config.budget)
        if not budget.allowed:
            self._emit(
                self._failure_record(ctx, call, CODE_BUDGET_EXCEEDED, 0.0, cache_status)
            )
            raise GovernanceError(
                CODE_BUDGET_EXCEEDED,
                f"已达本地预算上限（{budget.reason}），拒绝新的远程调用",
            )
        reservation_id = budget.reservation_id
        assert reservation_id is not None  # allowed=True 时契约保证
        return _Admission(
            profile=profile,
            started=started,
            deadline=deadline,
            breaker_key=breaker_key,
            budget_key=budget_key,
            reservation_id=reservation_id,
        )

    def _elapsed_ms(self, admission: _Admission) -> float:
        return (self._clock() - admission.started) * 1000.0

    def _fail(
        self,
        ctx: RequestContext,
        call: ProviderCall,
        admission: _Admission,
        code: str,
        cache_status: CacheStatus,
        *,
        retry_after: float | None = None,
    ) -> None:
        """执行段失败：回滚预留（失败的调用不得永久占额度）并落一条失败账。"""
        self.budget_ledger.rollback(admission.budget_key, admission.reservation_id)
        self._emit(
            self._failure_record(
                ctx, call, code, self._elapsed_ms(admission), cache_status,
                retry_after=retry_after,
            )
        )

    def _settle(
        self,
        ctx: RequestContext,
        call: ProviderCall,
        admission: _Admission,
        usage: ProviderUsage,
        retries: int,
        cache_status: CacheStatus,
    ) -> UsageRecord:
        """按 provider 返回的真实 usage 对账并落账；usage 不可得只释放预留、不估算。"""
        latency_ms = self._elapsed_ms(admission)
        if usage.known:
            price = self.config.price_for(call.model)
            cost = (
                price.estimate_usd(usage.input_tokens, usage.output_tokens, usage.cached_input_tokens)
                if price is not None
                else None  # 价格未登记 → 不猜
            )
            self.budget_ledger.settle(
                admission.budget_key, admission.reservation_id,
                actual_tokens=usage.total_tokens, actual_cost_usd=cost,
            )
            record = UsageRecord(
                request_id=ctx.request_id,
                principal_hash=ctx.principal.principal_hash,
                provider=call.provider,
                model=call.model,
                category=call.category,
                usage_known=True,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
                cache_status=cache_status,
                result_status="ok",
                retry_count=retries,
            )
        else:
            # provider 未返回 usage：只释放预留，不记 token（无从得知），不估成本。
            self.budget_ledger.settle(
                admission.budget_key, admission.reservation_id,
                actual_tokens=0, actual_cost_usd=None,
            )
            record = self._unknown_usage_record(ctx, call, latency_ms, retries, cache_status)

        self._emit(record)
        return record

    # -- 重试内核 --------------------------------------------------------------

    def _execute_with_retry[T](
        self,
        executor: Callable[[], tuple[T, ProviderUsage]],
        admission: _Admission,
        *,
        passthrough: tuple[type[BaseException], ...] = (),
    ) -> tuple[T, ProviderUsage, int]:
        """有界重试（受 deadline 约束）。``passthrough`` 中的异常不分类、不计熔断、原样抛出。"""
        profile = admission.profile
        deadline = admission.deadline
        breaker_key = admission.breaker_key
        retry = profile.retry
        last_exc: BaseException | None = None
        for attempt in range(retry.max_attempts):
            if deadline is not None and self._clock() >= deadline:
                raise GovernanceError(CODE_PROVIDER_TIMEOUT, "请求已超过时间预算（deadline）")
            self.provider_call_count += 1
            try:
                value, usage = executor()
            except passthrough:
                raise
            except BaseException as exc:
                last_exc = exc
                kind = _classify_error(exc)

                if kind is _ErrorClass.AUTH:
                    # 认证/授权：永久错误，零重试，且不计入熔断（换 Key 才能解决）
                    raise GovernanceError(
                        CODE_PROVIDER_AUTH_FAILED,
                        "上游服务拒绝访问（认证或权限失败），请检查 API Key 与服务开通状态",
                    ) from exc
                if kind is _ErrorClass.PERMANENT:
                    raise GovernanceError(
                        CODE_PROVIDER_UNAVAILABLE,
                        f"上游服务返回不可重试的错误：{type(exc).__name__}",
                    ) from exc

                # 瞬时错误
                self._record_breaker_failure(breaker_key, profile.breaker)
                if attempt == retry.max_attempts - 1:
                    raise GovernanceError(
                        CODE_PROVIDER_UNAVAILABLE,
                        f"上游服务连续失败且已达重试上限（{retry.max_attempts} 次）",
                    ) from exc

                delay = _retry_after_of(exc)
                if delay is None:
                    base = retry.backoff_for(attempt)
                    delay = base * (1.0 - random.random() * retry.jitter_ratio)
                if deadline is not None and self._clock() + delay >= deadline:
                    raise GovernanceError(
                        CODE_PROVIDER_TIMEOUT,
                        "重试等待将超过时间预算（deadline），停止重试",
                    ) from exc
                if delay > 0:
                    self._sleep(delay)
                continue

            self._record_breaker_success(breaker_key)
            return value, usage, attempt

        # 循环必然以 return 或 raise 结束；此处仅为类型完备
        raise GovernanceError(  # pragma: no cover
            CODE_PROVIDER_UNAVAILABLE, f"重试耗尽：{last_exc!r}"
        )

    # -- 账本落盘 --------------------------------------------------------------

    def _unknown_usage_record(
        self,
        ctx: RequestContext,
        call: ProviderCall,
        latency_ms: float,
        retries: int,
        cache_status: CacheStatus,
    ) -> UsageRecord:
        """usage 不可得的账本条目（``USAGE_UNKNOWN``，token/成本留空）。"""
        return UsageRecord.unknown_usage(
            request_id=ctx.request_id,
            principal_hash=ctx.principal.principal_hash,
            provider=call.provider,
            model=call.model,
            category=call.category,
            latency_ms=latency_ms,
        ).model_copy(update={"cache_status": cache_status, "retry_count": retries})

    def _failure_record(
        self,
        ctx: RequestContext,
        call: ProviderCall,
        code: str,
        latency_ms: float,
        cache_status: CacheStatus,
        *,
        retry_after: float | None = None,
    ) -> UsageRecord:
        """失败也要落账：否则限流/预算拒绝在观测里完全不可见。"""
        return UsageRecord(
            request_id=ctx.request_id,
            principal_hash=ctx.principal.principal_hash,
            provider=call.provider,
            model=call.model,
            category=call.category,
            usage_known=False,
            latency_ms=latency_ms,
            cache_status=cache_status,
            result_status=code,
            retry_after_seconds=retry_after,
        )

    def _emit(self, record: UsageRecord) -> None:
        if self._on_usage is None:
            return
        try:
            self._on_usage(record)
        except Exception:  # noqa: BLE001, S110 — 观测失败绝不影响主流程（无 logger 可用）
            pass

    def timeout_for(self, provider: ProviderKind) -> float:
        """该 provider 的配置超时，供调用方装配 SDK/urllib 时使用。"""
        return float(self.config.profile_for(provider).timeout_seconds)


__all__ = [
    "AUTH_HTTP_CODES",
    "TRANSIENT_HTTP_CODES",
    "GatewayResult",
    "ProviderGateway",
    "ProviderUsage",
]
