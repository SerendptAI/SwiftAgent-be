"""
Simple async TTL cache for frequently accessed data.
"""

import asyncio
import time
from typing import Any, Optional


class TTLCache:
    """In-memory cache with per-entry TTL."""

    def __init__(self, default_ttl: float = 300):
        self.default_ttl = default_ttl
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            if key in self._store:
                value, expires_at = self._store[key]
                if time.monotonic() < expires_at:
                    return value
                del self._store[key]
        return None

    async def set(self, key: str, value: Any, ttl: Optional[float] = None):
        expires_at = time.monotonic() + (ttl or self.default_ttl)
        async with self._lock:
            self._store[key] = (value, expires_at)

    async def delete(self, key: str):
        async with self._lock:
            self._store.pop(key, None)

    async def clear(self):
        async with self._lock:
            self._store.clear()

    async def cleanup(self):
        now = time.monotonic()
        async with self._lock:
            stale = [k for k, (_, exp) in self._store.items() if now >= exp]
            for k in stale:
                del self._store[k]


company_cache = TTLCache(default_ttl=300)
