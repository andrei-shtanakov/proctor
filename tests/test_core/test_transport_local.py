"""Tests for LocalEventTransport."""

import asyncio

import pytest

from proctor.core.models import Event
from proctor.core.transport import (
    ConnectionState,
    EventTooLargeError,
    TransportDrainingError,
    TransportLifecycleError,
    TransportUnavailableError,
)
from proctor.core.transport.local import LocalEventTransport

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestLifecycle:
    async def test_initial_state(self) -> None:
        t = LocalEventTransport()
        assert t.connection_state == ConnectionState.DISCONNECTED

    async def test_start_transitions_to_connected(self) -> None:
        t = LocalEventTransport()
        await t.start()
        assert t.connection_state == ConnectionState.CONNECTED
        await t.stop()

    async def test_double_start_raises(self) -> None:
        t = LocalEventTransport()
        await t.start()
        with pytest.raises(TransportLifecycleError):
            await t.start()
        await t.stop()

    async def test_stop_transitions_to_disconnected(self) -> None:
        t = LocalEventTransport()
        await t.start()
        await t.stop()
        assert t.connection_state == ConnectionState.DISCONNECTED

    async def test_subscribe_before_start_buffered(self) -> None:
        t = LocalEventTransport()
        received: list[Event] = []

        async def h(e: Event) -> None:
            received.append(e)

        handle = t.subscribe("test.ok", h)
        assert handle.subject == "test.ok"
        await t.start()
        await t.publish(Event(type="test.ok", source="x", payload={}))
        await asyncio.sleep(0.05)
        assert len(received) == 1
        await t.stop()


class TestPublish:
    async def test_publish_before_start_raises(self) -> None:
        t = LocalEventTransport()
        with pytest.raises(TransportUnavailableError):
            await t.publish(Event(type="test.ok", source="x", payload={}))

    async def test_publish_after_stop_raises(self) -> None:
        t = LocalEventTransport()
        await t.start()
        await t.stop()
        with pytest.raises(TransportUnavailableError):
            await t.publish(Event(type="test.ok", source="x", payload={}))

    async def test_publish_during_drain_raises(self) -> None:
        t = LocalEventTransport()
        await t.start()
        drain_task = asyncio.create_task(t.drain(timeout=0.5))
        await asyncio.sleep(0.01)
        with pytest.raises(TransportDrainingError):
            await t.publish(Event(type="test.ok", source="x", payload={}))
        await drain_task
        await t.stop()

    async def test_size_limit_enforced(self) -> None:
        t = LocalEventTransport(max_payload=200)
        await t.start()
        huge_payload = {"x": "a" * 500}
        with pytest.raises(EventTooLargeError):
            await t.publish(Event(type="test.ok", source="x", payload=huge_payload))
        await t.stop()


class TestWildcardDelivery:
    async def test_wildcard_match(self) -> None:
        t = LocalEventTransport()
        received: list[Event] = []

        async def h(e: Event) -> None:
            received.append(e)

        t.subscribe("trigger.>", h)
        await t.start()
        await t.publish(Event(type="trigger.webhook.github", source="x", payload={}))
        await t.publish(Event(type="trigger.terminal", source="x", payload={}))
        await asyncio.sleep(0.05)
        assert len(received) == 2
        await t.stop()

    async def test_overlapping_subscribe_dedups(self) -> None:
        t = LocalEventTransport()
        received: list[Event] = []

        async def h(e: Event) -> None:
            received.append(e)

        t.subscribe("trigger.>", h)
        t.subscribe("trigger.webhook.*", h)  # same handler, overlap
        await t.start()
        await t.publish(Event(type="trigger.webhook.github", source="x", payload={}))
        await asyncio.sleep(0.05)
        assert len(received) == 1  # dedup
        await t.stop()


class TestDrainAndCancel:
    async def test_drain_waits_for_in_flight(self) -> None:
        t = LocalEventTransport()
        gate = asyncio.Event()
        completed: list[int] = []

        async def slow_handler(e: Event) -> None:
            await gate.wait()
            completed.append(1)

        t.subscribe("test.slow", slow_handler)
        await t.start()
        await t.publish(Event(type="test.slow", source="x", payload={}))
        await asyncio.sleep(0.01)
        drain_task = asyncio.create_task(t.drain(timeout=2.0))
        await asyncio.sleep(0.05)
        assert not drain_task.done()
        gate.set()
        await drain_task
        assert completed == [1]
        await t.stop()

    async def test_handler_exception_isolated(self) -> None:
        t = LocalEventTransport()
        received_by_ok: list[Event] = []

        async def bad(e: Event) -> None:
            raise RuntimeError("bad handler")

        async def ok(e: Event) -> None:
            received_by_ok.append(e)

        t.subscribe("test.x", bad)
        t.subscribe("test.x", ok)
        await t.start()
        await t.publish(Event(type="test.x", source="s", payload={}))
        await asyncio.sleep(0.05)
        assert len(received_by_ok) == 1
        await t.stop()
