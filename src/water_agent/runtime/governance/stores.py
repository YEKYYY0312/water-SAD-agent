"""P2.5 限流与预算存储（Step 12）。

规格 §4 的硬约束：

- ``RateLimitStore`` / ``BudgetLedger`` **可替换**：开发/测试用内存或 SQLite；
  生产多实例只允许明确配置的原子共享存储 adapter。
- **不得用单进程 dict 声称支持多实例限流** —— 因此 :class:`InMemoryRateLimitStore`
  的 :attr:`supports_multi_instance` 恒为 ``False``，并有测试断言这一点。
  真正的多实例支持需要另写 adapter（Redis/Postgres），P2.5 不提供。
- 预算需要 **预留 → 对账 → 回滚** 三段语义：发送前按预估值预留额度，
  provider 返回后用真实 usage 对账，调用失败则回滚预留 —— 否则失败的调用会
  永久占用额度，或超预算后仍能发请求。

线程安全：两个内存实现都用 ``threading.Lock`` 保护，因为 harness 与 CLI 可能并发
调用（`test_provider_gateway.py` 有并发原子性测试）。
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from water_agent.runtime.governance.config import BudgetConfig, RateLimitPolicy


@dataclass(frozen=True, slots=True)
class RateLimitVerdict:
    """限流裁定。``allowed=False`` 时 ``retry_after_seconds`` 必有值。"""

    allowed: bool
    retry_after_seconds: float | None = None
    #: 拒绝原因分类（``tokens`` / ``requests``）；**不含 bucket 内部数值**，
    #: 因为它会进用户可见错误（规格 §4：不含内部 bucket 细节）。
    reason: str = ""


class RateLimitStore(ABC):
    """限流存储抽象。实现必须保证 ``acquire`` 的检查+扣减是原子的。"""

    #: 是否支持多实例部署。内存实现恒为 False（不得谎称）。
    supports_multi_instance: bool = False

    @abstractmethod
    def acquire(self, key: str, tokens: int, policy: RateLimitPolicy) -> RateLimitVerdict:
        """尝试获取 ``tokens`` 个额度与 1 个请求配额；不足则拒绝并给出重试等待。"""

    @abstractmethod
    def reset(self) -> None:
        """清空所有桶（仅测试与运维清理用）。"""


@dataclass
class _Bucket:
    tokens: float
    last_refill: float
    request_times: list[float] = field(default_factory=list)


class InMemoryRateLimitStore(RateLimitStore):
    """进程内 token bucket。

    **仅适用于单进程**（CLI / harness / 测试）。:attr:`supports_multi_instance` 恒 False：
    多实例部署必须换成原子共享存储 adapter，否则每个实例各自持有一份配额。
    """

    supports_multi_instance = False

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        import time

        self._clock = clock or time.monotonic
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str, tokens: int, policy: RateLimitPolicy) -> RateLimitVerdict:
        need = max(0, tokens)
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(policy.bucket_capacity_tokens), last_refill=now)
                self._buckets[key] = bucket

            # 按经过时间补充（上限为容量）
            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(
                float(policy.bucket_capacity_tokens),
                bucket.tokens + elapsed * policy.refill_tokens_per_second,
            )
            bucket.last_refill = now

            # RPM 窗口：滑动 60s
            bucket.request_times = [t for t in bucket.request_times if now - t < 60.0]
            if len(bucket.request_times) >= policy.max_requests_per_minute:
                oldest = min(bucket.request_times)
                wait = max(0.0, 60.0 - (now - oldest))
                return RateLimitVerdict(
                    allowed=False, retry_after_seconds=round(wait, 3), reason="requests"
                )

            if need > policy.bucket_capacity_tokens:
                # 单次请求就超过桶容量：等待也不可能通过，直接拒绝并说明。
                return RateLimitVerdict(
                    allowed=False, retry_after_seconds=None, reason="request_exceeds_capacity"
                )
            if bucket.tokens < need:
                deficit = need - bucket.tokens
                wait = deficit / policy.refill_tokens_per_second
                return RateLimitVerdict(
                    allowed=False, retry_after_seconds=round(wait, 3), reason="tokens"
                )

            bucket.tokens -= need
            bucket.request_times.append(now)
            return RateLimitVerdict(allowed=True)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """预算余量快照（供观测与告警，不含主体原文）。"""

    spent_tokens: int
    reserved_tokens: int
    spent_cost_usd: float
    token_cap: int | None
    cost_cap_usd: float | None
    #: 是否已达告警比例。
    warn_triggered: bool = False

    @property
    def committed_tokens(self) -> int:
        """已花 + 已预留（判定是否还能发新请求用它，而不是只看已花）。"""
        return self.spent_tokens + self.reserved_tokens


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """预算裁定。``allowed=False`` 时 ``reason`` 说明是 token 还是成本触顶。"""

    allowed: bool
    reason: str = ""
    warn: bool = False
    #: 预留句柄；``allowed=True`` 时必有，用于后续 settle/rollback。
    reservation_id: str | None = None


class BudgetLedger(ABC):
    """预算账本抽象：预留 → 对账 → 回滚。"""

    supports_multi_instance: bool = False

    @abstractmethod
    def reserve(self, key: str, tokens: int, config: BudgetConfig) -> BudgetVerdict:
        """按预估 token 预留额度。超额时按 ``config.policy`` 决定 warn 还是拒绝。"""

    @abstractmethod
    def settle(
        self, key: str, reservation_id: str, *, actual_tokens: int, actual_cost_usd: float | None
    ) -> None:
        """用真实 usage 对账：释放预留，记入实际消耗。

        ``actual_cost_usd=None``（usage 不可得或价格未登记）时只记 token，
        **不猜成本** —— 与 ``UsageRecord.usage_known=False`` 语义一致。
        """

    @abstractmethod
    def rollback(self, key: str, reservation_id: str) -> None:
        """调用失败：释放预留，不记消耗（否则失败调用会永久占额度）。"""

    @abstractmethod
    def snapshot(self, key: str, config: BudgetConfig) -> BudgetSnapshot:
        """当前余量快照。"""

    @abstractmethod
    def reset(self) -> None:
        """清空账本（仅测试与周期切换用）。"""


@dataclass
class _Account:
    spent_tokens: int = 0
    spent_cost_usd: float = 0.0
    reservations: dict[str, int] = field(default_factory=dict)

    @property
    def reserved_tokens(self) -> int:
        return sum(self.reservations.values())


class InMemoryBudgetLedger(BudgetLedger):
    """进程内预算账本。与限流存储同样**不支持多实例**。

    周期（每日/每月）切换由调用方通过 ``key`` 里的日期段实现，
    本实现不自行判断日历——避免把时区假设埋进账本。
    """

    supports_multi_instance = False

    def __init__(self) -> None:
        self._accounts: dict[str, _Account] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def _account(self, key: str) -> _Account:
        account = self._accounts.get(key)
        if account is None:
            account = _Account()
            self._accounts[key] = account
        return account

    def reserve(self, key: str, tokens: int, config: BudgetConfig) -> BudgetVerdict:
        from water_agent.runtime.governance.config import BudgetPolicy

        need = max(0, tokens)
        with self._lock:
            account = self._account(key)
            projected = account.spent_tokens + account.reserved_tokens + need
            cap = config.daily_token_cap
            cost_cap = config.daily_cost_cap_usd

            over_tokens = cap is not None and projected > cap
            over_cost = cost_cap is not None and account.spent_cost_usd >= cost_cap
            warn = False
            if cap is not None and projected >= cap * config.warn_ratio:
                warn = True
            if cost_cap is not None and account.spent_cost_usd >= cost_cap * config.warn_ratio:
                warn = True

            if over_tokens or over_cost:
                reason = "token_cap" if over_tokens else "cost_cap"
                if config.policy is BudgetPolicy.BLOCK:
                    return BudgetVerdict(allowed=False, reason=reason, warn=True)
                # WARN 策略：只观测，不拦截（规格 §4 三态语义）。
                return BudgetVerdict(
                    allowed=True, reason=reason, warn=True, reservation_id=self._new_reservation(account, need)
                )

            return BudgetVerdict(
                allowed=True, warn=warn, reservation_id=self._new_reservation(account, need)
            )

    def _new_reservation(self, account: _Account, tokens: int) -> str:
        """在持锁状态下创建预留（调用方必须已持 ``self._lock``）。"""
        self._seq += 1
        reservation_id = f"res-{self._seq}"
        account.reservations[reservation_id] = tokens
        return reservation_id

    def settle(
        self, key: str, reservation_id: str, *, actual_tokens: int, actual_cost_usd: float | None
    ) -> None:
        with self._lock:
            account = self._account(key)
            account.reservations.pop(reservation_id, None)
            account.spent_tokens += max(0, actual_tokens)
            if actual_cost_usd is not None:
                account.spent_cost_usd += max(0.0, actual_cost_usd)

    def rollback(self, key: str, reservation_id: str) -> None:
        with self._lock:
            self._account(key).reservations.pop(reservation_id, None)

    def snapshot(self, key: str, config: BudgetConfig) -> BudgetSnapshot:
        with self._lock:
            account = self._account(key)
            cap = config.daily_token_cap
            warn = False
            if cap is not None:
                warn = (account.spent_tokens + account.reserved_tokens) >= cap * config.warn_ratio
            return BudgetSnapshot(
                spent_tokens=account.spent_tokens,
                reserved_tokens=account.reserved_tokens,
                spent_cost_usd=round(account.spent_cost_usd, 8),
                token_cap=cap,
                cost_cap_usd=config.daily_cost_cap_usd,
                warn_triggered=warn,
            )

    def reset(self) -> None:
        with self._lock:
            self._accounts.clear()
            self._seq = 0


__all__ = [
    "BudgetLedger",
    "BudgetSnapshot",
    "BudgetVerdict",
    "InMemoryBudgetLedger",
    "InMemoryRateLimitStore",
    "RateLimitStore",
    "RateLimitVerdict",
]
