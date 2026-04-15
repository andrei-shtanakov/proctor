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
import json
import logging
import os
from collections.abc import Mapping

from aiohttp import web

from proctor.core.bus import EventBus
from proctor.core.config import WebhookConfig
from proctor.core.models import Event  # noqa: F401 — used in Task 7's handler
from proctor.triggers.base import Trigger

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


class WebhookTrigger(Trigger):
    """aiohttp-based HTTP server that publishes trigger.webhook.<source_name>
    events on the bus. Per-path auth (HMAC/Bearer/none), fire-and-forget
    semantics (202 Accepted), graceful drain on stop().
    """

    def __init__(self, config: WebhookConfig) -> None:
        self._config = config
        self._paths = config.paths
        self._limiter = InflightLimiter(config.max_in_flight)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._bus: EventBus | None = None
        self._stopped = False

    @property
    def bound_port(self) -> int | None:
        """Actual bound port (useful when config.port=0). None until
        started, also None after stop() cleanup.
        """
        if self._site is None or self._site._server is None:
            return None
        sockets = self._site._server.sockets  # type: ignore[attr-defined]
        return sockets[0].getsockname()[1] if sockets else None

    async def start(self, bus: EventBus) -> None:
        # Fail fast: every required env secret must be present.
        missing = sorted(
            {
                cfg.auth.secret_env
                for cfg in self._paths.values()
                if cfg.auth.type != "none" and cfg.auth.secret_env not in os.environ
            }
        )
        if missing:
            raise RuntimeError(f"Missing required webhook secrets in env: {missing}")

        # Loud WARNING for any unauthenticated path.
        for path, cfg in self._paths.items():
            if cfg.auth.type == "none":
                logger.warning(
                    "Webhook path %r has NO AUTHENTICATION "
                    "(auth.type: none). Do not use in production.",
                    path,
                )

        self._bus = bus
        app = web.Application(client_max_size=self._config.max_body_bytes)
        for path in self._paths:
            app.router.add_post(path, self._handle_webhook)
        self._runner = web.AppRunner(
            app,
            keepalive_timeout=self._config.keepalive_timeout,
        )
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await self._site.start()
        logger.info(
            "WebhookTrigger started on %s:%d with %d path(s)",
            self._config.host,
            self._config.port,
            len(self._paths),
        )

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True

        if self._site is not None:
            await self._site.stop()

        drained = await self._limiter.wait_idle(self._config.shutdown_timeout)
        if not drained:
            logger.warning(
                "Webhook shutdown timed out with %d in-flight requests",
                self._limiter.in_flight,
            )

        if self._runner is not None:
            await self._runner.cleanup()
        logger.info("WebhookTrigger stopped")

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        try:
            cfg = self._paths[request.path]

            if not await self._limiter.try_acquire():
                logger.warning(
                    "webhook overloaded: rejected %s (in_flight=%d/%d)",
                    request.path,
                    self._limiter.in_flight,
                    self._limiter.limit,
                )
                return web.json_response(
                    {"error": "overloaded"},
                    status=503,
                    headers={"Retry-After": "1"},
                )
            try:
                raw_body = await request.read()

                if not _verify_auth(cfg.auth, request, raw_body):
                    logger.info(
                        "webhook auth failed: path=%s",
                        request.path,
                    )
                    return web.json_response(
                        {"error": "unauthorized"},
                        status=401,
                    )

                try:
                    body = json.loads(raw_body) if raw_body else {}
                except json.JSONDecodeError:
                    return web.json_response(
                        {"error": "bad request"},
                        status=400,
                    )

                headers = _safe_headers(request.headers)
                assert cfg.source_name is not None  # guaranteed by validator
                event = Event(
                    type=f"trigger.webhook.{cfg.source_name}",
                    source="webhook",
                    payload={
                        "path": request.path,
                        "headers": headers,
                        "body": body,
                    },
                )
                assert self._bus is not None  # set in start()
                try:
                    await self._bus.publish(event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "webhook failed to publish event for %s",
                        request.path,
                    )
                    return web.json_response(
                        {"error": "service unavailable"},
                        status=503,
                        headers={"Retry-After": "5"},
                    )

                logger.debug(
                    "webhook accepted: path=%s source=%s event_id=%s",
                    request.path,
                    cfg.source_name,
                    event.id,
                )
                return web.json_response(
                    {"accepted": True, "correlation_id": event.id},
                    status=202,
                )
            finally:
                await self._limiter.release()
        except asyncio.CancelledError:
            raise
        except web.HTTPException:
            raise
        except Exception:
            logger.exception(
                "webhook handler unexpected error for %s",
                request.path,
            )
            return web.json_response(
                {"error": "service unavailable"},
                status=503,
                headers={"Retry-After": "5"},
            )
