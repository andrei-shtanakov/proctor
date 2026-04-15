"""NATSEventTransport — NATS-backed event transport for multi-node.

nats-py is a lazy import — module parse succeeds without the package
installed. Construction raises friendly ImportError if not available.

Wire format per spec §466-480 (headers: content-type, schema-version,
event-type, Nats-Msg-Id, published-at; body: Event.model_dump_json).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from proctor.core.config import EventsConfig, NATSConfig
from proctor.core.models import Event
from proctor.core.transport.base import (
    ConnectionState,
    DisconnectCallback,
    EventTransport,
    Handler,
    ListenerHandle,
    SubscriptionHandle,
)
from proctor.core.transport.errors import (
    EventSchemaError,
    EventTooLargeError,
    TransportConnectionError,
    TransportDrainingError,
    TransportLifecycleError,
    TransportUnavailableError,
)
from proctor.core.transport.local import (
    _DedupCache,
    _RateLimitedLogger,
    _validate_subject,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

Decoder = Callable[[bytes], Event]
_DECODERS: dict[tuple[str, int], Decoder] = {}


def _default_decoder_v1(data: bytes) -> Event:
    return Event.model_validate_json(data)


_DECODERS[("*", 1)] = _default_decoder_v1


def register_decoder(event_type: str, version: int, decoder: Decoder) -> None:
    """Register custom decoder for a ``(event_type, schema_version)`` pair.

    Lookup falls back to ``("*", version)`` when no exact match exists.
    Unknown ``schema_version`` → messages dropped with rate-limited WARN.
    """
    _DECODERS[(event_type, version)] = decoder


@dataclass
class _PendingSub:
    """A subscribe() call recorded before start() or during reconnect."""

    subject: str
    handler: Handler
    real_sub: Any = None  # nats.aio.subscription.Subscription when attached


@dataclass
class _NatsListenerHandle:
    callback: DisconnectCallback
    bucket: list[DisconnectCallback]

    def remove(self) -> None:
        if self.callback in self.bucket:
            self.bucket.remove(self.callback)


@dataclass
class _NatsSubHandle:
    subject: str
    pending: _PendingSub
    transport: NATSEventTransport
    _removed: bool = field(default=False)

    async def unsubscribe(self) -> None:
        if self._removed:
            return
        self._removed = True
        with contextlib.suppress(ValueError):
            self.transport._pending_subs.remove(self.pending)
        real = self.pending.real_sub
        if real is not None:
            try:
                await real.unsubscribe()
            except Exception:
                logger.exception("NATS unsubscribe failed for %s", self.subject)


class NATSEventTransport(EventTransport):
    """NATS-backed EventTransport. Lazy-imports nats-py.

    Install with: ``pip install proctor[nats]``
    """

    def __init__(
        self,
        nats_config: NATSConfig,
        events_config: EventsConfig | None = None,
    ) -> None:
        try:
            import nats  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "NATSEventTransport requires 'nats-py'. "
                "Install with: pip install proctor[nats]"
            ) from e
        self._config = nats_config
        self._events_config = events_config or EventsConfig()
        self._nc: Any = None  # nats.aio.client.Client
        self._state: ConnectionState = ConnectionState.DISCONNECTED
        self._started = False
        self._draining = False
        self._pending_subs: list[_PendingSub] = []
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._dedup = _DedupCache()
        self._disconnect_listeners: list[DisconnectCallback] = []
        self._reconnect_listeners: list[DisconnectCallback] = []
        self._rl = _RateLimitedLogger(logger)
        self._subject_prefix = self._config.subject_prefix

    @property
    def connection_state(self) -> ConnectionState:
        return self._state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            raise TransportLifecycleError("NATSEventTransport already started")
        import os

        import nats

        user = self._config.user
        if self._config.user_env:
            user = os.environ.get(self._config.user_env)
        password: str | None = None
        if self._config.password_env:
            password = os.environ.get(self._config.password_env)

        connect_kwargs: dict[str, Any] = {
            "servers": self._config.servers,
            "name": self._config.name,
            "connect_timeout": self._config.connect_timeout,
            "reconnect_time_wait": self._config.reconnect_time_wait,
            "max_reconnect_attempts": self._config.max_reconnect_attempts,
            "disconnected_cb": self._on_disconnect,
            "reconnected_cb": self._on_reconnect,
            "closed_cb": self._on_closed,
            "error_cb": self._on_error,
        }
        if user:
            connect_kwargs["user"] = user
        if password:
            connect_kwargs["password"] = password
        if self._config.tls_ca:
            import ssl

            ctx = ssl.create_default_context(cafile=str(self._config.tls_ca))
            if self._config.tls_client_cert and self._config.tls_client_key:
                ctx.load_cert_chain(
                    certfile=str(self._config.tls_client_cert),
                    keyfile=str(self._config.tls_client_key),
                )
            connect_kwargs["tls"] = ctx

        try:
            self._nc = await nats.connect(**connect_kwargs)
        except Exception as e:
            raise TransportConnectionError(
                f"Failed to connect to NATS {self._config.servers}: {e}"
            ) from e

        self._state = ConnectionState.CONNECTED
        self._started = True

        # Register buffered subscriptions
        for pending in list(self._pending_subs):
            await self._register_pending(pending)
        await self._nc.flush(timeout=int(self._config.connect_timeout))
        logger.info(
            "NATSEventTransport started; registered %d buffered subscriptions",
            len(self._pending_subs),
        )

    async def stop(self) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._started = False
        if self._nc is not None:
            try:
                await self._nc.close()
            except Exception:
                logger.exception("NATS close failed")
            self._nc = None
        logger.info("NATSEventTransport stopped")

    async def drain(self, timeout: float = 60.0) -> None:
        self._draining = True
        deadline = time.monotonic() + timeout
        # Drain NATS client subscriptions first so no new messages arrive
        if self._nc is not None:
            try:
                await asyncio.wait_for(self._nc.drain(), timeout=timeout)
            except TimeoutError:
                logger.warning("NATS drain() timed out after %.1fs", timeout)
            except Exception:
                logger.exception("NATS drain failed")
        # Then await in-flight handler tasks within remaining budget
        remaining = max(0.0, deadline - time.monotonic())
        if self._handler_tasks and remaining > 0:
            pending = [t for t in self._handler_tasks if not t.done()]
            if pending:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),  # type: ignore[arg-type]
                        timeout=remaining,
                    )
                except TimeoutError:
                    still_pending = sum(1 for t in self._handler_tasks if not t.done())
                    logger.warning(
                        "NATSEventTransport drain timed out with %d tasks",
                        still_pending,
                    )
                    for t in list(self._handler_tasks):
                        if not t.done():
                            t.cancel()

    async def flush(self, timeout: float = 5.0) -> None:
        """Wait until buffered wire traffic is ACKed and pending handler
        tasks finish — symmetric with LocalEventTransport.flush().
        """
        if self._nc is not None and self._state == ConnectionState.CONNECTED:
            try:
                await self._nc.flush(timeout=int(timeout))
            except Exception:
                logger.exception("NATS flush failed")
        deadline = time.monotonic() + timeout
        while True:
            pending = [t for t in self._handler_tasks if not t.done()]
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),  # type: ignore[arg-type]
                    timeout=remaining,
                )
            except TimeoutError:
                return

    # ------------------------------------------------------------------
    # Publish / subscribe
    # ------------------------------------------------------------------

    async def publish(self, event: Event) -> None:
        if self._draining:
            raise TransportDrainingError("NATSEventTransport is draining")
        if self._state != ConnectionState.CONNECTED or self._nc is None:
            await self._rl.warn(
                "publish_unavailable",
                "Publish while not connected: event.type=%s",
                event.type,
            )
            raise TransportUnavailableError(
                f"NATSEventTransport is {self._state.value}"
            )
        data = event.model_dump_json().encode("utf-8")
        if len(data) > self._events_config.max_payload:
            raise EventTooLargeError(
                f"Event {event.type!r} serialized {len(data)} bytes "
                f"exceeds max_payload {self._events_config.max_payload}"
            )
        headers = {
            "content-type": "application/json",
            "schema-version": str(SCHEMA_VERSION),
            "event-type": event.type,
            "Nats-Msg-Id": str(uuid4()),
            "published-at": datetime.now(UTC).isoformat(timespec="microseconds"),
        }
        subject = f"{self._subject_prefix}.events.{event.type}"
        try:
            await self._nc.publish(subject, data, headers=headers)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise TransportUnavailableError(f"NATS publish failed: {e}") from e

    def subscribe(self, subject: str, handler: Handler) -> SubscriptionHandle:
        _validate_subject(subject, allow_wildcards=True)
        pending = _PendingSub(subject=subject, handler=handler)
        self._pending_subs.append(pending)
        # If already started, schedule async registration immediately
        if self._started and self._state == ConnectionState.CONNECTED:
            task = asyncio.create_task(self._register_pending(pending))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)
        return _NatsSubHandle(subject=subject, pending=pending, transport=self)

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    def add_disconnect_listener(self, cb: DisconnectCallback) -> ListenerHandle:
        self._disconnect_listeners.append(cb)
        return _NatsListenerHandle(cb, self._disconnect_listeners)

    def add_reconnect_listener(self, cb: DisconnectCallback) -> ListenerHandle:
        self._reconnect_listeners.append(cb)
        return _NatsListenerHandle(cb, self._reconnect_listeners)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _pattern_to_nats_subject(self, pattern: str) -> str:
        """`trigger.>` → `{prefix}.events.trigger.>` (NATS subject space)."""
        return f"{self._subject_prefix}.events.{pattern}"

    async def _register_pending(self, pending: _PendingSub) -> None:
        nats_subject = self._pattern_to_nats_subject(pending.subject)
        try:
            real = await self._nc.subscribe(
                nats_subject,
                cb=self._make_cb(pending),
            )
            pending.real_sub = real
        except Exception:
            logger.exception("NATS subscribe failed for %s", nats_subject)

    def _make_cb(self, pending: _PendingSub) -> Callable[[Any], Any]:
        async def _cb(msg: Any) -> None:
            await self._on_message(msg, pending.handler)

        return _cb

    async def _on_message(self, msg: Any, handler: Handler) -> None:
        """Decode, validate, dedup, and dispatch a single NATS message."""
        try:
            event = await self._decode(msg)
        except EventSchemaError as e:
            await self._rl.warn(
                "schema_error",
                "Dropping NATS message: %s subject=%s",
                str(e),
                getattr(msg, "subject", "?"),
            )
            return
        msg_id = (msg.headers or {}).get("Nats-Msg-Id", "")
        if self._dedup.seen(handler, msg_id):
            return
        task = asyncio.create_task(self._safe_invoke(handler, event))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _decode(self, msg: Any) -> Event:
        headers = msg.headers or {}
        try:
            event_type = headers["event-type"]
            version = int(headers["schema-version"])
            content_type = headers["content-type"]
            msg_id = headers["Nats-Msg-Id"]
            published_at_raw = headers["published-at"]
        except (KeyError, ValueError) as e:
            raise EventSchemaError(f"malformed headers: {e}") from e

        expected_subject = f"{self._subject_prefix}.events.{event_type}"
        if msg.subject != expected_subject:
            raise EventSchemaError(
                f"subject/header mismatch: subject={msg.subject!r} "
                f"event-type header={event_type!r}"
            )
        if content_type != "application/json":
            raise EventSchemaError(f"unsupported content-type: {content_type!r}")

        decoder = _DECODERS.get((event_type, version)) or _DECODERS.get(("*", version))
        if decoder is None:
            raise EventSchemaError(f"no decoder for ({event_type}, v{version})")

        # Clock skew check — rate-limited WARN only
        try:
            published_at = datetime.fromisoformat(published_at_raw)
            skew = abs((datetime.now(UTC) - published_at).total_seconds())
            if skew > 3600:
                await self._rl.warn(
                    "clock_skew",
                    "Clock skew >1h on received event: skew=%.1fs msg_id=%s",
                    skew,
                    msg_id,
                )
        except ValueError:
            raise EventSchemaError(
                f"invalid published-at: {published_at_raw!r}"
            ) from None

        try:
            return decoder(msg.data)
        except Exception as e:
            raise EventSchemaError(f"decode failed: {e}") from e

    async def _safe_invoke(self, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Event handler error: type=%s handler=%s event_id=%s",
                event.type,
                handler,
                event.id,
            )

    # ------------------------------------------------------------------
    # NATS client callbacks
    # ------------------------------------------------------------------

    async def _on_disconnect(self) -> None:
        if self._state == ConnectionState.CONNECTED:
            logger.warning(
                "NATS disconnected; entering RECONNECTING (name=%s)",
                self._config.name,
            )
        self._state = ConnectionState.RECONNECTING
        for cb in list(self._disconnect_listeners):
            await _invoke_listener(cb)

    async def _on_reconnect(self) -> None:
        if self._state != ConnectionState.CONNECTED:
            logger.info(
                "NATS reconnected; back to CONNECTED (name=%s)",
                self._config.name,
            )
        self._state = ConnectionState.CONNECTED
        for cb in list(self._reconnect_listeners):
            await _invoke_listener(cb)

    async def _on_closed(self) -> None:
        logger.info("NATS connection closed (name=%s)", self._config.name)
        self._state = ConnectionState.DISCONNECTED

    async def _on_error(self, e: Exception) -> None:
        await self._rl.warn(
            "nats_error",
            "NATS client error: %s",
            str(e),
        )


async def _invoke_listener(cb: DisconnectCallback) -> None:
    try:
        result = cb()
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.exception("Listener callback error")
