"""EventTransport ABC + supporting Protocols and enums.

See spec section "Public surface".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Protocol, runtime_checkable

from proctor.core.models import Event

Handler = Callable[[Event], Awaitable[None]]


class ConnectionState(Enum):
    """Enum for transport connection state."""

    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"


@runtime_checkable
class SubscriptionHandle(Protocol):
    """Subscription returned by EventTransport.subscribe.

    LocalEventTransport returns its own impl; NATSEventTransport
    wraps nats.aio.subscription.Subscription.
    """

    @property
    def subject(self) -> str:
        """Return the subscribed subject pattern."""
        ...

    async def unsubscribe(self) -> None:
        """Unsubscribe from the subject."""
        ...


@runtime_checkable
class ListenerHandle(Protocol):
    """Handle returned by add_{disconnect,reconnect}_listener.

    Call .remove() to deregister.
    """

    def remove(self) -> None:
        """Remove the listener."""
        ...


DisconnectCallback = Callable[[], Awaitable[None]] | Callable[[], None]


class EventTransport(ABC):
    """Transport for broadcast event delivery (fan-out, at-most-once).

    Subscribe accepts NATS-subject syntax (tokens separated by '.',
    '*' = single-token wildcard, '>' = multi-token trailing wildcard —
    only allowed as the last token). Both backends enforce identical
    wildcard and delivery semantics so tests are portable.
    """

    @abstractmethod
    async def start(self) -> None:
        """Connect backend and register buffered subscriptions.

        Not idempotent — double-call raises TransportLifecycleError.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Unsubscribe all and disconnect from backend.

        Must be preceded by drain() for graceful behaviour.
        """

    @abstractmethod
    async def drain(self, timeout: float = 60.0) -> None:
        """Reject new publishes (TransportDrainingError) and wait for
        in-flight handler tasks to complete within timeout.
        """

    @abstractmethod
    async def flush(self, timeout: float = 5.0) -> None:
        """Wait until buffered subscribe/publish commands are ACKed by broker.
        No-op for LocalEventTransport; nc.flush(timeout) for
        NATSEventTransport.
        """

    @abstractmethod
    async def publish(self, event: Event) -> None:
        """Serialize event, enforce max_payload, dispatch.

        Raises:
            EventTooLargeError: if serialized size > events.max_payload.
            TransportUnavailableError: if not CONNECTED.
            TransportDrainingError: during drain phase.
        """

    @abstractmethod
    def subscribe(self, subject: str, handler: Handler) -> SubscriptionHandle:
        """Register handler for subject pattern.

        Sync call — returns SubscriptionHandle immediately.

        Before start(): subscription is buffered, registered at start()
        with flush. After start(): registration in background; caller
        should await flush() before publishing matching events if
        first-delivery semantics matter.

        Raises:
            InvalidSubjectError: if subject violates charset/wildcard rules.
        """

    @property
    @abstractmethod
    def connection_state(self) -> ConnectionState:
        """Return current connection state."""

    @abstractmethod
    def add_disconnect_listener(self, cb: DisconnectCallback) -> ListenerHandle:
        """Add listener to be called when transport disconnects.

        Returns a handle to remove the listener.
        """

    @abstractmethod
    def add_reconnect_listener(self, cb: DisconnectCallback) -> ListenerHandle:
        """Add listener to be called when transport reconnects.

        Returns a handle to remove the listener.
        """
