"""WebhookTrigger — aiohttp-based HTTP endpoint that publishes
trigger.webhook.<source_name> events on the bus.

Fire-and-forget semantics (202 Accepted), per-path auth
(HMAC / Bearer / none), in-flight admission cap, graceful drain on
stop(). See docs/superpowers/specs/2026-04-15-webhook-trigger-design.md
for the full design.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
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


# Auth failure reason codes. Never include raw header values, signatures,
# or tokens in any log line — logs often have broader read access than
# the SQLite DBs.
_AUTH_REASONS = frozenset(
    {
        "missing_header",
        "bad_prefix",
        "bad_signature",
        "non_bearer_scheme",
        "wrong_token",
    }
)


def _verify_auth(
    auth_cfg: object,  # HMACAuthConfig | BearerAuthConfig | NoneAuthConfig
    request: object,  # aiohttp.web.Request (duck-typed for unit tests)
    raw_body: bytes,
) -> bool:
    """Per-request auth verification.

    Secrets are re-read from os.environ on every call; rotation via
    os.environ[...] = new_value takes effect immediately without
    restart.
    """
    kind = getattr(auth_cfg, "type", None)
    if kind == "none":
        return True
    if kind == "hmac":
        header_name: str = auth_cfg.header  # type: ignore[attr-defined]
        prefix: str = auth_cfg.prefix  # type: ignore[attr-defined]
        secret_env: str = auth_cfg.secret_env  # type: ignore[attr-defined]
        header_val = request.headers.get(header_name)  # type: ignore[attr-defined]
        if header_val is None or not header_val.startswith(prefix):
            return False
        sig_hex = header_val[len(prefix) :]
        secret = os.environ[secret_env].encode()
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig_hex, expected)
    if kind == "bearer":
        header_name = auth_cfg.header  # type: ignore[attr-defined]
        secret_env = auth_cfg.secret_env  # type: ignore[attr-defined]
        header_val = request.headers.get(header_name, "")  # type: ignore[attr-defined]
        parts = header_val.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        token = parts[1]
        secret = os.environ[secret_env]
        return hmac.compare_digest(token, secret)
    return False


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
