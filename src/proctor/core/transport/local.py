"""LocalEventTransport — in-process event bus with NATS-wildcard semantics.

Also houses shared helpers (_RateLimitedLogger, _DedupCache, wildcard
matcher) reused by NATSEventTransport.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from typing import Any

from proctor.core.transport.base import Handler

logger = logging.getLogger(__name__)


class _RateLimitedLogger:
    """Log first occurrence per key immediately, then every `interval`
    seconds with aggregated count of suppressed occurrences.

    Concurrent-safe via asyncio.Lock.
    """

    def __init__(
        self,
        logger: logging.Logger,
        interval: float = 60.0,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._logger = logger
        self._interval = interval
        self._time_fn = time_fn or time.monotonic
        self._last_logged: dict[str, float] = {}
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def warn(self, key: str, fmt: str, *args: object) -> None:
        async with self._lock:
            self._counts[key] += 1
            now = self._time_fn()
            last = self._last_logged.get(key, -float("inf"))
            if now - last >= self._interval:
                count = self._counts[key]
                self._logger.warning(
                    f"{fmt} (occurrences since last log: %d)",
                    *args,
                    count,
                )
                self._last_logged[key] = now
                self._counts[key] = 0


class _DedupCache:
    """LRU + TTL cache for (handler_id, msg_id) → seen.

    Delivers same-handler-overlapping-subscriptions exactly once
    per message. Handler identity via id(handler); bound methods
    use (id(__self__), id(__func__)) since each attribute access
    creates a new bound-method object. Strong-ref held to prevent
    id reuse masking collisions.

    Covered handler types: async def, async lambda, bound method,
    functools.partial, class with async __call__.
    """

    def __init__(self, size: int = 10_000, ttl: float = 60.0) -> None:
        self._size = size
        self._ttl = ttl
        # OrderedDict acts as LRU
        self._entries: OrderedDict[tuple[int | tuple[int, int], str], float] = (
            OrderedDict()
        )
        # Hold strong refs keyed by id so IDs don't recycle before we're done
        self._handler_refs: dict[int | tuple[int, int], Any] = {}

    def _handler_key(self, handler: Handler) -> int | tuple[int, int]:
        # Normalize bound methods to (instance, function) identity
        if hasattr(handler, "__self__") and hasattr(handler, "__func__"):
            hid: int | tuple[int, int] = (
                id(handler.__self__),
                id(handler.__func__),
            )
        else:
            hid = id(handler)
        # Strong ref prevents GC + id reuse
        self._handler_refs[hid] = handler
        return hid

    def seen(self, handler: Handler, msg_id: str) -> bool:
        key = (self._handler_key(handler), msg_id)
        now = time.monotonic()
        self._evict_expired(now)
        if key in self._entries:
            # Touch for LRU
            self._entries.move_to_end(key)
            return True
        self._entries[key] = now
        # LRU eviction at capacity
        while len(self._entries) > self._size:
            self._entries.popitem(last=False)
        return False

    def _evict_expired(self, now: float) -> None:
        expired_keys = [k for k, ts in self._entries.items() if now - ts >= self._ttl]
        for k in expired_keys:
            del self._entries[k]
