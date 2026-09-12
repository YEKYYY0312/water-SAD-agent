"""P5 Step 37: login rate limiting (in-memory, per-username bucket).

Counts failed login attempts per username within a sliding window.
Step 38 moves counters to PostgreSQL for multi-instance correctness.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


# How many failures in WINDOW_SECONDS before the bucket is limited.
MAX_FAILURES = 5
WINDOW_SECONDS = 300  # 5 minutes
LOCKOUT_SECONDS = 600  # 10 minutes


@dataclass
class _Bucket:
    failure_times: deque[float] = field(default_factory=deque)


class LoginRateLimiter:
    """Sliding-window failure counter keyed by normalised username.

    Username is lower-cased for bucket lookup so case variants collapse.
    The actual username value is never stored after normalisation.
    """

    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES,
        window_seconds: float = WINDOW_SECONDS,
        lockout_seconds: float = LOCKOUT_SECONDS,
    ) -> None:
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._lockout_seconds = lockout_seconds
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _key(self, username: str) -> str:
        # Use only lowercase normalisation; do not log the real username.
        return username.lower()

    def _purge_expired(self, bucket: _Bucket, now: float) -> None:
        cutoff = now - self._window_seconds
        while bucket.failure_times and bucket.failure_times[0] < cutoff:
            bucket.failure_times.popleft()

    def is_limited(self, username: str) -> bool:
        """Return True if this username is currently rate-limited."""
        key = self._key(username)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return False
            now = time.monotonic()
            self._purge_expired(bucket, now)
            return len(bucket.failure_times) >= self._max_failures

    def record_failure(self, username: str) -> None:
        """Record one failed login attempt (call after auth failure is confirmed)."""
        key = self._key(username)
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = _Bucket()
            now = time.monotonic()
            self._purge_expired(self._buckets[key], now)
            self._buckets[key].failure_times.append(now)

    def reset(self, username: str) -> None:
        """Clear the failure counter on successful login."""
        key = self._key(username)
        with self._lock:
            self._buckets.pop(key, None)

    def failure_count(self, username: str) -> int:
        """Return current unexpired failure count (for audit logging)."""
        key = self._key(username)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return 0
            self._purge_expired(bucket, time.monotonic())
            return len(bucket.failure_times)


_limiter = LoginRateLimiter()


def get_rate_limiter() -> LoginRateLimiter:
    """Return the active rate limiter (FastAPI dependency-injection target)."""
    return _limiter
