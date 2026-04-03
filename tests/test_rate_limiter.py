"""Tests for the rate limiter."""

import asyncio
import pytest
from app.core.rate_limiter import RateLimiter


class TestRateLimiter:
    @pytest.fixture
    def limiter(self):
        return RateLimiter(rate=10.0, capacity=10)

    @pytest.mark.asyncio
    async def test_allows_requests_within_capacity(self, limiter):
        for _ in range(10):
            assert await limiter.acquire("test_key") is True

    @pytest.mark.asyncio
    async def test_blocks_when_capacity_exhausted(self, limiter):
        for _ in range(10):
            await limiter.acquire("test_key")
        assert await limiter.acquire("test_key") is False

    @pytest.mark.asyncio
    async def test_refills_over_time(self, limiter):
        for _ in range(10):
            await limiter.acquire("test_key")
        assert await limiter.acquire("test_key") is False

        await asyncio.sleep(0.15)
        assert await limiter.acquire("test_key") is True

    @pytest.mark.asyncio
    async def test_separate_keys_have_separate_buckets(self, limiter):
        for _ in range(10):
            await limiter.acquire("key_a")
        assert await limiter.acquire("key_a") is False
        assert await limiter.acquire("key_b") is True

    @pytest.mark.asyncio
    async def test_cleanup_removes_stale_buckets(self, limiter):
        await limiter.acquire("stale_key")
        limiter._buckets["stale_key"] = (5.0, 0.0)
        limiter.cleanup(max_age=1.0)
        assert "stale_key" not in limiter._buckets
