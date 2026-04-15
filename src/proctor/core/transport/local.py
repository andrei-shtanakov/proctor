"""LocalEventTransport — in-process event bus with NATS-wildcard semantics.

Also houses shared helpers (_RateLimitedLogger, _DedupCache, wildcard
matcher) reused by NATSEventTransport.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

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
    EventTooLargeError,
    InvalidSubjectError,
    TransportDrainingError,
    TransportLifecycleError,
    TransportUnavailableError,
)

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


_LITERAL_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]*")


def _validate_subject(s: str, *, allow_wildcards: bool) -> None:
    """Validate NATS subject / pattern. Raises InvalidSubjectError.

    allow_wildcards=True: subscribe patterns (accept *, >).
    allow_wildcards=False: publish subjects (reject any wildcard).
    """
    if not s:
        raise InvalidSubjectError("subject must not be empty")
    tokens = s.split(".")
    for i, tok in enumerate(tokens):
        if not tok:
            raise InvalidSubjectError(f"subject {s!r} has empty token")
        if tok == ">":
            if not allow_wildcards:
                raise InvalidSubjectError(f"wildcard > not allowed in subject {s!r}")
            if i != len(tokens) - 1:
                raise InvalidSubjectError(f"wildcard > must be the last token in {s!r}")
            continue
        if tok == "*":
            if not allow_wildcards:
                raise InvalidSubjectError(f"wildcard * not allowed in subject {s!r}")
            continue
        # Literal token: same charset as event.type segment
        if not _LITERAL_TOKEN_RE.fullmatch(tok):
            raise InvalidSubjectError(
                f"token {tok!r} in {s!r} must match [a-z][a-z0-9_]*"
            )


def _match_subject(subject: str, pattern: str) -> bool:
    """Match subject against NATS-syntax pattern.

    Both subject and pattern are validated first; subject disallows
    wildcards (concrete), pattern allows them.
    """
    _validate_subject(pattern, allow_wildcards=True)
    _validate_subject(subject, allow_wildcards=False)
    return _match_tokens(subject.split("."), pattern.split("."))


def _match_tokens(sub: list[str], pat: list[str]) -> bool:
    """Recursively match token lists."""
    if not pat:
        return not sub
    if pat[0] == ">":
        # > at end — must have ≥1 remaining subject token
        return len(sub) >= 1
    if not sub:
        return False
    if pat[0] == "*" or pat[0] == sub[0]:
        return _match_tokens(sub[1:], pat[1:])
    return False


@dataclass(eq=False)  # eq=False preserves id-based __hash__ for set storage
class _LocalSubscription:
    subject: str
    handler: Handler
    transport: LocalEventTransport
    _removed: bool = field(default=False)

    async def unsubscribe(self) -> None:
        if self._removed:
            return
        self._removed = True
        self.transport._subscriptions.discard(self)


@dataclass
class _LocalListenerHandle:
    callback: DisconnectCallback
    transport: LocalEventTransport
    disconnect: bool  # True = disconnect listener, False = reconnect

    def remove(self) -> None:
        bucket = (
            self.transport._disconnect_listeners
            if self.disconnect
            else self.transport._reconnect_listeners
        )
        if self.callback in bucket:
            bucket.remove(self.callback)


class _SubHandleAdapter:
    """Adapter wrapping _LocalSubscription to satisfy SubscriptionHandle."""

    def __init__(self, sub: _LocalSubscription) -> None:
        self._sub = sub

    @property
    def subject(self) -> str:
        return self._sub.subject

    async def unsubscribe(self) -> None:
        await self._sub.unsubscribe()


class LocalEventTransport(EventTransport):
    """In-process EventTransport. No network; identical observable
    behaviour to NATSEventTransport for the contract surface.
    """

    def __init__(
        self,
        *,
        strict_size_check: bool = True,
        max_payload: int = 65_536,
    ) -> None:
        self._strict_size_check = strict_size_check
        self._max_payload = max_payload
        self._state: ConnectionState = ConnectionState.DISCONNECTED
        self._started = False
        self._draining = False
        self._subscriptions: set[_LocalSubscription] = set()
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._dedup = _DedupCache()
        self._disconnect_listeners: list[DisconnectCallback] = []
        self._reconnect_listeners: list[DisconnectCallback] = []
        self._rl = _RateLimitedLogger(logger)

    # --- lifecycle ---

    async def start(self) -> None:
        if self._started:
            raise TransportLifecycleError("LocalEventTransport already started")
        self._started = True
        self._state = ConnectionState.CONNECTED
        logger.info(
            "LocalEventTransport started; %d buffered subscriptions active",
            len(self._subscriptions),
        )

    async def stop(self) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._started = False
        logger.info("LocalEventTransport stopped")

    async def drain(self, timeout: float = 60.0) -> None:
        self._draining = True
        if self._handler_tasks:
            gather_fut: asyncio.Future[Any] = asyncio.gather(
                *self._handler_tasks, return_exceptions=True
            )
            try:
                await asyncio.wait_for(gather_fut, timeout=timeout)
            except TimeoutError:
                remaining = sum(1 for t in self._handler_tasks if not t.done())
                logger.warning(
                    "LocalEventTransport drain timed out with %d tasks",
                    remaining,
                )
                for t in list(self._handler_tasks):
                    if not t.done():
                        t.cancel()

    async def flush(self, timeout: float = 5.0) -> None:
        """Wait for all pending handler tasks to complete.

        For parity with NATSEventTransport.flush() (which blocks until
        the wire has drained), the local variant blocks until scheduled
        handler callbacks finish — including any events they publish,
        transitively. Timeout bounds the total wait.
        """
        deadline = time.monotonic() + timeout
        await self._drain_handler_tasks(deadline)

    # --- publish / subscribe ---

    async def publish(self, event: Event) -> None:
        if self._draining:
            raise TransportDrainingError("LocalEventTransport is draining")
        if self._state != ConnectionState.CONNECTED:
            await self._rl.warn(
                "publish_unavailable",
                "Publish while not connected: event.type=%s",
                event.type,
            )
            raise TransportUnavailableError(
                f"LocalEventTransport is {self._state.value}"
            )
        if self._strict_size_check:
            data = event.model_dump_json().encode("utf-8")
            if len(data) > self._max_payload:
                raise EventTooLargeError(
                    f"Event {event.type!r} serialized {len(data)} bytes "
                    f"exceeds max_payload {self._max_payload}"
                )
        msg_id = str(uuid4())
        self._dispatch(event, msg_id)

    def subscribe(self, subject: str, handler: Handler) -> SubscriptionHandle:
        _validate_subject(subject, allow_wildcards=True)
        sub = _LocalSubscription(subject=subject, handler=handler, transport=self)
        self._subscriptions.add(sub)
        return _SubHandleAdapter(sub)

    # --- listeners ---

    def add_disconnect_listener(self, cb: DisconnectCallback) -> ListenerHandle:
        self._disconnect_listeners.append(cb)
        return _LocalListenerHandle(cb, self, disconnect=True)

    def add_reconnect_listener(self, cb: DisconnectCallback) -> ListenerHandle:
        self._reconnect_listeners.append(cb)
        return _LocalListenerHandle(cb, self, disconnect=False)

    @property
    def connection_state(self) -> ConnectionState:
        return self._state

    # --- internal ---

    def _dispatch(self, event: Event, msg_id: str) -> None:
        for sub in list(self._subscriptions):
            if not _match_subject(event.type, sub.subject):
                continue
            if self._dedup.seen(sub.handler, msg_id):
                continue
            task = asyncio.create_task(self._safe_invoke(sub.handler, event))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

    async def _drain_handler_tasks(self, deadline: float | None = None) -> None:
        """Await completion of all pending handler tasks until deadline.

        Re-iterates because handlers may chain new publishes that
        schedule fresh tasks. Bounded by the deadline (None = no
        bound).
        """
        while True:
            pending = [t for t in self._handler_tasks if not t.done()]
            if not pending:
                return
            if deadline is None:
                await asyncio.gather(*pending, return_exceptions=True)
                continue
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
