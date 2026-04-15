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
from collections.abc import Mapping

logger = logging.getLogger(__name__)


_SAFE_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "content-type",
        "user-agent",
        "x-real-ip",
        "x-request-id",
        "x-github-event",
        "x-github-delivery",
        "x-github-hook-id",
        "x-gitlab-event",
        "x-gitlab-event-uuid",
    }
)
_SAFE_HEADER_PREFIXES: tuple[str, ...] = ("x-forwarded-",)


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Whitelist-filter request headers before publishing to the bus.

    Auth headers (Authorization, X-Hub-Signature-256, Stripe-Signature,
    Cookie, etc.) MUST be excluded: event.payload is persisted to
    episodes.db; leaking credentials there is a security incident.

    Multi-value headers: last-wins via dict conversion. Duplicate
    headers are rare in webhook traffic; if a future use case needs
    them preserved, switch to list[tuple[str, str]].

    Header casing: keys preserve whatever the client sent (HTTP
    headers are case-insensitive, but Python dicts are not).
    Downstream consumers should use case-insensitive lookup if
    portability across clients matters.
    """
    result: dict[str, str] = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in _SAFE_HEADER_NAMES or kl.startswith(_SAFE_HEADER_PREFIXES):
            result[k] = v
    return result


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
