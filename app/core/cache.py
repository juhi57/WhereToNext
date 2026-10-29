"""A tiny TTL + LRU cache for generated plans.

Identical briefs are common - a user tweaks nothing and hits "Plan" again, or a
demo is refreshed during a review. Serving those from memory turns a multi-second
paid model call into a sub-millisecond lookup.

Keyed on a hash of the normalised brief, so two requests that differ only in
field order or whitespace share an entry.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Thread-safe cache with per-entry expiry and least-recently-used eviction."""

    def __init__(self, ttl_seconds: float, max_entries: int = 256) -> None:
        self._ttl = float(ttl_seconds)
        self._max = max(1, int(max_entries))
        self._store: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, key: str) -> T | None:
        """Return the cached value, or ``None`` when absent or expired."""
        if not self.enabled:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, value = entry
            if expires_at <= now:
                del self._store[key]
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value: T) -> None:
        """Store ``value``, evicting the least recently used entry if needed."""
        if not self.enabled:
            return
        with self._lock:
            self._store[key] = (time.monotonic() + self._ttl, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> dict[str, int | float]:
        """Observability snapshot, surfaced on the health endpoint."""
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._store),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
                "ttl_seconds": self._ttl,
            }
