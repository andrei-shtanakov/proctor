"""EventBus — thin wrapper over EventTransport.

Transport is plumbing; EventBus is the stable caller-facing contract.
Future observability hooks (metrics, tracing, event enrichment) wire
at this level, not in Transport.
"""

from __future__ import annotations

from proctor.core.models import Event
from proctor.core.transport import (
    ConnectionState,
    DisconnectCallback,
    EventTransport,
    Handler,
    ListenerHandle,
    SubscriptionHandle,
)


class EventBus:
    """Application-facing event bus. Requires explicit transport."""

    def __init__(self, transport: EventTransport) -> None:
        self._transport = transport

    async def start(self) -> None:
        await self._transport.start()

    async def stop(self) -> None:
        await self._transport.stop()

    async def drain(self, timeout: float = 60.0) -> None:
        await self._transport.drain(timeout)

    async def flush(self, timeout: float = 5.0) -> None:
        await self._transport.flush(timeout)

    async def publish(self, event: Event) -> None:
        await self._transport.publish(event)

    def subscribe(
        self, subject: str, handler: Handler
    ) -> SubscriptionHandle:
        return self._transport.subscribe(subject, handler)

    @property
    def connection_state(self) -> ConnectionState:
        return self._transport.connection_state

    def add_disconnect_listener(
        self, cb: DisconnectCallback
    ) -> ListenerHandle:
        return self._transport.add_disconnect_listener(cb)

    def add_reconnect_listener(
        self, cb: DisconnectCallback
    ) -> ListenerHandle:
        return self._transport.add_reconnect_listener(cb)
