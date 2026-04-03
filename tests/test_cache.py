"""Tests for the TTL cache."""

import asyncio
import pytest
from app.core.cache import TTLCache


class TestTTLCache:
    @pytest.fixture
    def cache(self):
        return TTLCache(default_ttl=1.0)

    @pytest.mark.asyncio
    async def test_set_and_get(self, cache):
        await cache.set("key1", "value1")
        assert await cache.get("key1") == "value1"

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_key(self, cache):
        assert await cache.get("missing") is None

    @pytest.mark.asyncio
    async def test_expires_after_ttl(self, cache):
        await cache.set("key1", "value1", ttl=0.1)
        assert await cache.get("key1") == "value1"
        await asyncio.sleep(0.15)
        assert await cache.get("key1") is None

    @pytest.mark.asyncio
    async def test_delete(self, cache):
        await cache.set("key1", "value1")
        await cache.delete("key1")
        assert await cache.get("key1") is None

    @pytest.mark.asyncio
    async def test_clear(self, cache):
        await cache.set("key1", "value1")
        await cache.set("key2", "value2")
        await cache.clear()
        assert await cache.get("key1") is None
        assert await cache.get("key2") is None
