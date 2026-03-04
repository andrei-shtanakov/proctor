"""Tests for EventBus: async pub/sub with wildcard pattern matching."""

import logging

import pytest

from proctor.core.bus import EventBus, _Subscription
from proctor.core.models import Event


class TestSubscription:
    def test_auto_generated_id(self) -> None:
        async def noop(e: Event) -> None:
            pass

        s1 = _Subscription(pattern="test.*", handler=noop)
        s2 = _Subscription(pattern="test.*", handler=noop)
        assert s1.id != s2.id
        assert len(s1.id) == 36  # UUID format

    def test_fields(self) -> None:
        async def noop(e: Event) -> None:
            pass

        sub = _Subscription(pattern="trigger.*", handler=noop)
        assert sub.pattern == "trigger.*"
        assert sub.handler is noop


class TestSubscribe:
    def test_returns_subscription_id(self) -> None:
        bus = EventBus()

        async def handler(e: Event) -> None:
            pass

        sub_id = bus.subscribe("trigger.*", handler)
        assert isinstance(sub_id, str)
        assert len(sub_id) == 36

    def test_multiple_subscriptions(self) -> None:
        bus = EventBus()

        async def handler(e: Event) -> None:
            pass

        id1 = bus.subscribe("trigger.*", handler)
        id2 = bus.subscribe("task.*", handler)
        assert id1 != id2

    def test_same_pattern_different_ids(self) -> None:
        bus = EventBus()

        async def h1(e: Event) -> None:
            pass

        async def h2(e: Event) -> None:
            pass

        id1 = bus.subscribe("trigger.*", h1)
        id2 = bus.subscribe("trigger.*", h2)
        assert id1 != id2


class TestUnsubscribe:
    def test_removes_subscription(self) -> None:
        bus = EventBus()

        async def handler(e: Event) -> None:
            pass

        sub_id = bus.subscribe("test.*", handler)
        assert len(bus._subscriptions) == 1

        bus.unsubscribe(sub_id)
        assert len(bus._subscriptions) == 0

    def test_unknown_id_is_noop(self) -> None:
        bus = EventBus()

        async def handler(e: Event) -> None:
            pass

        bus.subscribe("test.*", handler)
        bus.unsubscribe("nonexistent-id")
        assert len(bus._subscriptions) == 1

    @pytest.mark.anyio
    async def test_unsubscribe_stops_delivery(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        sub_id = bus.subscribe("test.*", handler)

        await bus.publish(Event(type="test.first", source="test"))
        assert len(received) == 1

        bus.unsubscribe(sub_id)

        await bus.publish(Event(type="test.second", source="test"))
        assert len(received) == 1  # no new delivery


class TestPublish:
    @pytest.mark.anyio
    async def test_exact_match(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.terminal", handler)

        event = Event(type="trigger.terminal", source="terminal")
        await bus.publish(event)

        assert len(received) == 1
        assert received[0].id == event.id

    @pytest.mark.anyio
    async def test_wildcard_match(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.*", handler)

        await bus.publish(Event(type="trigger.terminal", source="test"))
        await bus.publish(Event(type="trigger.webhook", source="test"))

        assert len(received) == 2

    @pytest.mark.anyio
    async def test_wildcard_no_match(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.*", handler)

        await bus.publish(Event(type="task.completed", source="test"))

        assert len(received) == 0

    @pytest.mark.anyio
    async def test_star_matches_all(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("*", handler)

        await bus.publish(Event(type="anything", source="test"))

        assert len(received) == 1

    @pytest.mark.anyio
    async def test_multiple_subscribers_same_pattern(self) -> None:
        bus = EventBus()
        calls_a: list[Event] = []
        calls_b: list[Event] = []

        async def handler_a(e: Event) -> None:
            calls_a.append(e)

        async def handler_b(e: Event) -> None:
            calls_b.append(e)

        bus.subscribe("trigger.*", handler_a)
        bus.subscribe("trigger.*", handler_b)

        event = Event(type="trigger.terminal", source="test")
        await bus.publish(event)

        assert len(calls_a) == 1
        assert len(calls_b) == 1
        assert calls_a[0].id == event.id
        assert calls_b[0].id == event.id

    @pytest.mark.anyio
    async def test_no_subscribers_is_silent(self) -> None:
        bus = EventBus()
        await bus.publish(Event(type="orphan.event", source="test"))

    @pytest.mark.anyio
    async def test_event_payload_preserved(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("data.*", handler)

        event = Event(
            type="data.update",
            source="sensor",
            payload={"key": "value", "count": 42},
        )
        await bus.publish(event)

        assert received[0].payload == {"key": "value", "count": 42}


class TestErrorIsolation:
    @pytest.mark.anyio
    async def test_handler_error_doesnt_crash_bus(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def bad_handler(e: Event) -> None:
            raise ValueError("boom")

        async def good_handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("test.*", bad_handler)
        bus.subscribe("test.*", good_handler)

        event = Event(type="test.item", source="test")
        await bus.publish(event)

        assert len(received) == 1
        assert received[0].id == event.id

    @pytest.mark.anyio
    async def test_error_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        bus = EventBus()

        async def bad_handler(e: Event) -> None:
            raise RuntimeError("intentional error")

        bus.subscribe("test.*", bad_handler)

        with caplog.at_level(logging.ERROR):
            await bus.publish(Event(type="test.err", source="test"))

        assert any("Handler error" in r.message for r in caplog.records)

    @pytest.mark.anyio
    async def test_multiple_errors_dont_stop_others(self) -> None:
        bus = EventBus()
        received: list[str] = []

        async def bad1(e: Event) -> None:
            raise TypeError("bad1")

        async def bad2(e: Event) -> None:
            raise KeyError("bad2")

        async def good(e: Event) -> None:
            received.append("ok")

        bus.subscribe("x.*", bad1)
        bus.subscribe("x.*", bad2)
        bus.subscribe("x.*", good)

        await bus.publish(Event(type="x.y", source="test"))

        assert received == ["ok"]


class TestPublicExports:
    def test_import_handler_type(self) -> None:
        from proctor.core.bus import Handler

        assert Handler is not None

    def test_import_event_bus(self) -> None:
        from proctor.core.bus import EventBus

        assert EventBus is not None

    def test_import_from_core_package(self) -> None:
        from proctor.core import EventBus, Handler

        assert EventBus is not None
        assert Handler is not None
