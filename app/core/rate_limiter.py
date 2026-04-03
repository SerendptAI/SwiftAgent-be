"""
In-memory token bucket rate limiter.

Provides per-key rate limiting with configurable capacity and refill rate.
Uses a sliding window approach to avoid burst issues.
"""

import asyncio
import time
from collections import defaultdict


class RateLimiter:
    """Token bucket rate limiter for per-key throttling."""

    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self._buckets: dict[str, tuple[float, float]] = defaultdict(
            lambda: (float(capacity), time.monotonic())
        )
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, tokens: int = 1) -> bool:
        async with self._lock:
            current_tokens, last_time = self._buckets[key]
            now = time.monotonic()
            elapsed = now - last_time

            current_tokens = min(self.capacity, current_tokens + elapsed * self.rate)
            current_tokens -= tokens

            if current_tokens < 0:
                self._buckets[key] = (current_tokens + tokens, last_time)
                return False

            self._buckets[key] = (current_tokens, now)
            return True

    def cleanup(self, max_age: float = 3600):
        now = time.monotonic()
        stale_keys = [
            k
            for k, (_, last_time) in self._buckets.items()
            if now - last_time > max_age
        ]
        for k in stale_keys:
            del self._buckets[k]


def _make_general_limiter():
    from app.core.config import settings

    rate = settings.RATE_LIMIT_PER_MINUTE / 60.0
    return RateLimiter(rate=rate, capacity=settings.RATE_LIMIT_PER_MINUTE)


def _make_auth_limiter():
    from app.core.config import settings

    rate = settings.RATE_LIMIT_AUTH_PER_MINUTE / 60.0
    return RateLimiter(rate=rate, capacity=settings.RATE_LIMIT_AUTH_PER_MINUTE)


general_limiter = None
auth_limiter = None


def get_general_limiter() -> RateLimiter:
    global general_limiter
    if general_limiter is None:
        general_limiter = _make_general_limiter()
    return general_limiter


def get_auth_limiter() -> RateLimiter:
    global auth_limiter
    if auth_limiter is None:
        auth_limiter = _make_auth_limiter()
    return auth_limiter
