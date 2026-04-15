"""WebhookTrigger — aiohttp-based HTTP endpoint that publishes
trigger.webhook.<source_name> events on the bus.

Fire-and-forget semantics (202 Accepted), per-path auth
(HMAC / Bearer / none), in-flight admission cap, graceful drain on
stop(). See docs/superpowers/specs/2026-04-15-webhook-trigger-design.md
for the full design.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class InflightLimiter:
    """Counter-based in-flight cap with event-driven idle signalling.

    Uses asyncio primitives (not anyio) because Proctor de facto runs
    on asyncio (aiosqlite, litellm are asyncio-only) and asyncio.Event
    has clear(), which anyio.Event lacks — yielding a simpler,
    race-free reusable idle signal.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._count = 0
        self._lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def in_flight(self) -> int:
        return self._count

    @property
    def limit(self) -> int:
        return self._limit

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self._count >= self._limit:
                return False
            self._count += 1
            self._idle.clear()
            return True

    async def release(self) -> None:
        async with self._lock:
            self._count -= 1
            if self._count == 0:
                self._idle.set()

    async def wait_idle(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False
