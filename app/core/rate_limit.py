"""A small in-process sliding-window rate limiter.

Deliberately dependency-free and deliberately *per process*. Behind more than
one worker it limits per worker, which is the right trade for a single-node
deployment and the wrong one for a cluster - swap in Redis at that point and
keep this interface.

It protects one thing: plan generation, which costs a model call. Reads are
cheap and are not limited.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    """Allow at most ``limit`` events per ``window_seconds`` per key.

    Thread-safe, and bounded: idle keys are purged so a long-running process
    cannot accumulate one deque per IP that ever visited.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 4096) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._window = float(window_seconds)
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Register an attempt for ``key``.

        Returns ``(allowed, retry_after_seconds)``. ``retry_after`` is ``0`` when
        the call is allowed.
        """
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self._max_keys:
                    self._purge(cutoff)
                hits = self._hits.setdefault(key, deque())

            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self._limit:
                retry_after = max(1, int(self._window - (now - hits[0])) + 1)
                return False, retry_after

            hits.append(now)
            return True, 0

    def reset(self, key: str | None = None) -> None:
        """Clear one key, or everything when ``key`` is ``None``. Used by tests."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def _purge(self, cutoff: float) -> None:
        """Drop keys with no activity inside the window. Caller holds the lock."""
        stale = [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]
        for key in stale:
            del self._hits[key]
        if not stale:
            # Everything is active: drop the oldest half rather than grow without
            # bound. Under this much load the limiter is already doing its job.
            ordered = sorted(self._hits.items(), key=lambda kv: kv[1][-1])
            for key, _ in ordered[: len(ordered) // 2]:
                del self._hits[key]
