"""Tests for EventBus thin wrapper."""

import asyncio

import pytest

from proctor.core.bus import EventBus
from proctor.core.models import Event
from proctor.core.transport import ConnectionState, LocalEventTransport

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestEventBus:
    async def test_no_default_transport(self) -> None:
        with pytest.raises(TypeError):
            EventBus()  # type: ignore[call-arg]

    async def test_start_stop_delegation(self) -> None:
        t = LocalEventTransport()
        bus = EventBus(t)
        await bus.start()
        assert bus.connection_state == ConnectionState.CONNECTED
        await bus.stop()

    async def test_publish_subscribe(self) -> None:
        received: list[Event] = []

        async def h(e: Event) -> None:
            received.append(e)

        bus = EventBus(LocalEventTransport())
        bus.subscribe("test.ok", h)
        await bus.start()
        await bus.publish(Event(type="test.ok", source="x", payload={}))
        await bus.flush()
        assert len(received) == 1
        await bus.stop()
