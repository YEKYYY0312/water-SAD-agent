"""P5 Step 37: login rate limiting (in-memory, per-username bucket).

Counts failed login attempts per username within a sliding window.
Step 38 keeps the Web app single-instance; a future multi-instance deployment
must replace these counters with a shared implementation.

Memory is bounded (review 2026-09-30): buckets used to be created for every
failed username and never removed, so anonymous requests with ever-new (and,
before the request model capped it, megabyte-long) usernames grew the process
without limit.  Now idle buckets are dropped on access, each bucket keeps at
most ``max_failures`` timestamps, and the table holds at most ``max_buckets``
entries -- evicting unlocked buckets first, so a flood of junk usernames cannot
push a locked account's counter out.

Per-username limiting does not stop an attacker spraying one password across
many accounts; that needs a per-client limit in front of the app.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

# How many failures in WINDOW_SECONDS before the bucket is limited.
MAX_FAILURES = 5
WINDOW_SECONDS = 300  # 5 minutes
LOCKOUT_SECONDS = 600  # 10 minutes
# Upper bound on tracked usernames; each bucket is a key (<=128 chars, capped by
# the login request model) plus at most MAX_FAILURES timestamps.
MAX_BUCKETS = 10_000


@dataclass
class _Bucket:
    failure_times: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0


class LoginRateLimiter:
    """Sliding-window failure counter keyed by normalised username.

    Username is lower-cased for bucket lookup so case variants collapse.
    The actual username value is never stored after normalisation.

    Reaching ``max_failures`` inside ``window_seconds`` locks the bucket for
    ``lockout_seconds`` (the documented lockout; it used to end as soon as the
    oldest failure slid out of the window).
    """

    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES,
        window_seconds: float = WINDOW_SECONDS,
        lockout_seconds: float = LOCKOUT_SECONDS,
        max_buckets: int = MAX_BUCKETS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._lockout_seconds = lockout_seconds
        self._max_buckets = max_buckets
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, username: str) -> str:
        # Use only lowercase normalisation; do not log the real username.
        return username.lower()

    def _purge_expired(self, bucket: _Bucket, now: float) -> None:
        cutoff = now - self._window_seconds
        while bucket.failure_times and bucket.failure_times[0] < cutoff:
            bucket.failure_times.popleft()

    def _limited(self, bucket: _Bucket, now: float) -> bool:
        return bucket.locked_until > now or len(bucket.failure_times) >= self._max_failures

    def _idle(self, bucket: _Bucket, now: float) -> bool:
        self._purge_expired(bucket, now)
        return not bucket.failure_times and bucket.locked_until <= now

    def _lookup(self, key: str, now: float) -> _Bucket | None:
        """Return the live bucket for *key*, dropping it if it has gone idle."""
        bucket = self._buckets.get(key)
        if bucket is not None and self._idle(bucket, now):
            del self._buckets[key]
            return None
        return bucket

    def _make_room(self, now: float) -> None:
        """Keep the table under ``max_buckets``: idle first, then oldest unlocked."""
        if len(self._buckets) < self._max_buckets:
            return
        for key in [k for k, b in self._buckets.items() if self._idle(b, now)]:
            del self._buckets[key]
        if len(self._buckets) < self._max_buckets:
            return
        for key, bucket in self._buckets.items():  # oldest-updated first
            if not self._limited(bucket, now):
                del self._buckets[key]
                return
        self._buckets.popitem(last=False)

    def is_limited(self, username: str) -> bool:
        """Return True if this username is currently rate-limited."""
        key = self._key(username)
        with self._lock:
            now = self._clock()
            bucket = self._lookup(key, now)
            return bucket is not None and self._limited(bucket, now)

    def record_failure(self, username: str) -> None:
        """Record one failed login attempt (call after auth failure is confirmed)."""
        key = self._key(username)
        with self._lock:
            now = self._clock()
            bucket = self._lookup(key, now)
            if bucket is None:
                self._make_room(now)
                bucket = _Bucket(failure_times=deque(maxlen=self._max_failures))
                self._buckets[key] = bucket
            else:
                self._buckets.move_to_end(key)
            bucket.failure_times.append(now)
            if len(bucket.failure_times) >= self._max_failures:
                bucket.locked_until = max(bucket.locked_until, now + self._lockout_seconds)

    def reset(self, username: str) -> None:
        """Clear the failure counter on successful login."""
        key = self._key(username)
        with self._lock:
            self._buckets.pop(key, None)

    def failure_count(self, username: str) -> int:
        """Return current unexpired failure count (for audit logging)."""
        key = self._key(username)
        with self._lock:
            bucket = self._lookup(key, self._clock())
            return 0 if bucket is None else len(bucket.failure_times)

    def bucket_count(self) -> int:
        """Number of tracked usernames (bounded by ``max_buckets``)."""
        with self._lock:
            return len(self._buckets)


_limiter = LoginRateLimiter()


def get_rate_limiter() -> LoginRateLimiter:
    """Return the active rate limiter (FastAPI dependency-injection target)."""
    return _limiter
