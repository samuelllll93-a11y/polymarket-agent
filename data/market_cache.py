"""
Local market data caching layer.

Provides a lightweight in-process (and optionally on-disk) cache for
Polymarket market data, order book snapshots, and agent probability
estimates. Reduces redundant API calls and allows agents to share state
without tight coupling.

Cache entries are keyed by market_id and carry a configurable TTL.
Supports both synchronous reads and async refresh callbacks.
"""

import asyncio
import time
from typing import Any, Callable, Optional


class MarketCache:
    """In-process TTL cache for market data shared across agents."""

    def __init__(self, default_ttl_seconds: int = 60):
        self.default_ttl = default_ttl_seconds
        self._store: dict[str, dict] = {}  # key -> {value, expires_at}

    def get(self, key: str) -> Optional[Any]:
        """Return cached value if present and not expired, else None."""
        entry = self._store.get(key)
        if entry and entry["expires_at"] > time.monotonic():
            return entry["value"]
        return None

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        """Store a value with the given TTL (defaults to self.default_ttl)."""
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        self._store[key] = {
            "value": value,
            "expires_at": time.monotonic() + ttl,
        }

    def invalidate(self, key: str) -> None:
        """Immediately expire a specific cache entry."""
        self._store.pop(key, None)

    def clear(self) -> None:
        """Flush the entire cache."""
        self._store.clear()

    async def get_or_fetch(
        self,
        key: str,
        fetch_fn: Callable,
        ttl_seconds: Optional[int] = None,
    ) -> Any:
        """
        Return cached value, or call fetch_fn() to populate and cache it.

        Args:
            key:         Cache key.
            fetch_fn:    Async callable that returns the value to cache.
            ttl_seconds: Override TTL for this entry.
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        value = await fetch_fn()
        self.set(key, value, ttl_seconds)
        return value
