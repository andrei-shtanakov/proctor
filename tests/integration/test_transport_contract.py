"""Shared contract tests for EventTransport implementations.

Parametrized across `local` and `nats` — the observable behaviour MUST
be identical. NATS-backed runs are gated by the `nats` marker and a
running NATS server (testcontainers or CI `services`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from proctor.core.models import Event
from proctor.core.transport import (
    EventTooLargeError,
    EventTransport,
    TransportUnavailableError,
)
from proctor.core.transport.local import LocalEventTransport

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(
    params=[
        pytest.param("local", id="local"),
        pytest.param("nats", id="nats", marks=pytest.mark.nats),
    ]
)
async def transport(
    request: pytest.FixtureRequest,
) -> AsyncIterator[EventTransport]:
    """Parametrized transport fixture — runs each test against both.

    The `nats` parameter carries the `nats` marker so default `-m 'not nats'`
    skips it; `-m nats` runs only those. Local runs in both cases.
    """
    kind = request.param
    if kind == "local":
        t: EventTransport = LocalEventTransport(max_payload=65_536)
        await t.start()
        try:
            yield t
        finally:
            await t.stop()
    elif kind == "nats":
        pytest.importorskip("nats")
        nats_url = request.getfixturevalue("nats_url")
        from proctor.core.config import EventsConfig, NATSConfig
        from proctor.core.transport.nats import NATSEventTransport

        prefix = f"proctor_test_{uuid4().hex[:8]}"
        t = NATSEventTransport(
            NATSConfig(servers=[nats_url], subject_prefix=prefix),
            events_config=EventsConfig(max_payload=65_536),
        )
        await t.start()
        try:
            yield t
        finally:
            await t.stop()
    else:  # pragma: no cover
        raise ValueError(f"unknown transport: {kind}")


async def _settle(transport: EventTransport, timeout: float = 1.0) -> None:
    """Wait for in-flight handler tasks — flush + brief network settle.

    LocalEventTransport dispatches synchronously via asyncio.create_task
    at publish time; flush waits for those tasks. NATSEventTransport
    dispatches only after the reader task reads the message off the wire,
    so additional short sleeps are required for bounded delivery.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await transport.flush(timeout=0.1)
        await asyncio.sleep(0.05)


async def _wait_for_count(
    received: list[Event], expected: int, timeout: float = 2.0
) -> None:
    """Poll until `received` reaches `expected` count or timeout.

    Fails fast when count goes over expected (duplicate delivery).
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if len(received) >= expected:
            return
        await asyncio.sleep(0.02)


# --------------------------------------------------------------------- #
# Basic delivery contract
# --------------------------------------------------------------------- #


class TestContract:
    async def test_literal_subject_delivery(self, transport: EventTransport) -> None:
        received: list[Event] = []

        async def h(e: Event) -> None:
            received.append(e)

        transport.subscribe("test.ok", h)
        await transport.flush(timeout=1.0)
        await transport.publish(Event(type="test.ok", source="x", payload={}))
        await _wait_for_count(received, 1)
        await _settle(transport, timeout=0.2)
        assert len(received) == 1
        assert received[0].type == "test.ok"

    async def test_single_token_wildcard(self, transport: EventTransport) -> None:
        received: list[Event] = []

        async def h(e: Event) -> None:
            received.append(e)

        transport.subscribe("trigger.*", h)
        await transport.flush(timeout=1.0)
        await transport.publish(Event(type="trigger.terminal", source="x", payload={}))
        await transport.publish(Event(type="trigger.webhook", source="x", payload={}))
        await transport.publish(
            Event(type="trigger.webhook.github", source="x", payload={})
        )
        await _wait_for_count(received, 2)
        await _settle(transport, timeout=0.2)
        # * matches exactly one segment → 2 of 3 delivered
        assert len(received) == 2

    async def test_trailing_wildcard(self, transport: EventTransport) -> None:
        received: list[Event] = []

        async def h(e: Event) -> None:
            received.append(e)

        transport.subscribe("trigger.>", h)
        await transport.flush(timeout=1.0)
        for t in [
            "trigger.terminal",
            "trigger.webhook.github",
            "trigger.webhook.ci.build",
        ]:
            await transport.publish(Event(type=t, source="x", payload={}))
        await _wait_for_count(received, 3)
        assert len(received) == 3

    async def test_unmatched_event_not_delivered(
        self, transport: EventTransport
    ) -> None:
        received: list[Event] = []

        async def h(e: Event) -> None:
            received.append(e)

        transport.subscribe("foo.>", h)
        await transport.flush(timeout=1.0)
        await transport.publish(Event(type="bar.baz", source="x", payload={}))
        await _settle(transport, timeout=0.3)
        assert received == []

    async def test_overlapping_subscribe_dedups(
        self, transport: EventTransport
    ) -> None:
        received: list[Event] = []

        async def h(e: Event) -> None:
            received.append(e)

        transport.subscribe("trigger.>", h)
        transport.subscribe("trigger.webhook.*", h)
        await transport.flush(timeout=1.0)
        await transport.publish(
            Event(type="trigger.webhook.github", source="x", payload={})
        )
        await _wait_for_count(received, 1)
        await _settle(transport, timeout=0.3)
        assert len(received) == 1

    async def test_distinct_handlers_each_receive(
        self, transport: EventTransport
    ) -> None:
        a_seen: list[Event] = []
        b_seen: list[Event] = []

        async def a(e: Event) -> None:
            a_seen.append(e)

        async def b(e: Event) -> None:
            b_seen.append(e)

        transport.subscribe("trigger.>", a)
        transport.subscribe("trigger.>", b)
        await transport.flush(timeout=1.0)
        await transport.publish(Event(type="trigger.terminal", source="x", payload={}))
        await _wait_for_count(a_seen, 1)
        await _wait_for_count(b_seen, 1)
        assert len(a_seen) == 1 and len(b_seen) == 1

    async def test_handler_exception_isolated(self, transport: EventTransport) -> None:
        ok_seen: list[Event] = []

        async def bad(e: Event) -> None:
            raise RuntimeError("boom")

        async def ok(e: Event) -> None:
            ok_seen.append(e)

        transport.subscribe("test.x", bad)
        transport.subscribe("test.x", ok)
        await transport.flush(timeout=1.0)
        await transport.publish(Event(type="test.x", source="x", payload={}))
        await _wait_for_count(ok_seen, 1)
        assert len(ok_seen) == 1

    async def test_size_limit_enforced(self, transport: EventTransport) -> None:
        huge = {"x": "a" * 100_000}
        with pytest.raises(EventTooLargeError):
            await transport.publish(Event(type="test.huge", source="x", payload=huge))

    async def test_publish_during_drain_raises(self, transport: EventTransport) -> None:
        drain_task = asyncio.create_task(transport.drain(timeout=0.3))
        await asyncio.sleep(0.01)
        # TransportDrainingError subclasses TransportUnavailableError;
        # catching the parent matches both
        with pytest.raises(TransportUnavailableError):
            await transport.publish(Event(type="test.ok", source="x", payload={}))
        await drain_task

    async def test_unsubscribe_stops_delivery(self, transport: EventTransport) -> None:
        received: list[Event] = []

        async def h(e: Event) -> None:
            received.append(e)

        handle = transport.subscribe("test.ok", h)
        await transport.flush(timeout=1.0)
        await transport.publish(Event(type="test.ok", source="x", payload={}))
        await _wait_for_count(received, 1)
        assert len(received) == 1

        await handle.unsubscribe()
        await transport.flush(timeout=1.0)
        await transport.publish(Event(type="test.ok", source="x", payload={}))
        await _settle(transport, timeout=0.5)
        assert len(received) == 1  # no new delivery after unsubscribe


@pytest.mark.nats
async def test_cross_node_event_delivery(nats_url: str) -> None:
    """Signature test — two NATSEventTransport instances on same prefix."""
    from proctor.core.config import EventsConfig, NATSConfig
    from proctor.core.transport.nats import NATSEventTransport

    prefix = f"proctor_test_{uuid4().hex[:8]}"
    cfg_a = NATSConfig(
        servers=[nats_url],
        subject_prefix=prefix,
        name="node-a",
    )
    cfg_b = NATSConfig(
        servers=[nats_url],
        subject_prefix=prefix,
        name="node-b",
    )
    events = EventsConfig()
    a = NATSEventTransport(cfg_a, events_config=events)
    b = NATSEventTransport(cfg_b, events_config=events)

    queue: asyncio.Queue[Event] = asyncio.Queue()

    async def handler(e: Event) -> None:
        await queue.put(e)

    b.subscribe("cross.>", handler)

    await a.start()
    await b.start()
    try:
        await a.publish(Event(type="cross.ping", source="node-a", payload={"n": 1}))
        received = await asyncio.wait_for(queue.get(), timeout=3.0)
        assert received.type == "cross.ping"
        assert received.payload["n"] == 1
    finally:
        await a.stop()
        await b.stop()
