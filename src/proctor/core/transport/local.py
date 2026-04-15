"""LocalEventTransport — in-process event bus with NATS-wildcard semantics.

Also houses shared helpers (_RateLimitedLogger, _DedupCache, wildcard
matcher) reused by NATSEventTransport.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable

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
