"""Wire-format + schema-version tests for NATSEventTransport.

These tests poke the decode/dispatch pipeline directly using a fake
nats.Msg, so no NATS server (or testcontainers) is needed — lives
under tests/test_core/ and runs in the default `pytest` pass.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from proctor.core.config import EventsConfig, NATSConfig
from proctor.core.models import Event
from proctor.core.transport.base import ConnectionState
from proctor.core.transport.nats import (
    _DECODERS,
    NATSEventTransport,
    register_decoder,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class _FakeMsg:
    """Minimal nats.aio.msg.Msg stand-in for decode-path tests."""

    subject: str
    data: bytes
    headers: dict[str, str] = field(default_factory=dict)


def _build_headers(event_type: str, version: int = 1) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "schema-version": str(version),
        "event-type": event_type,
        "Nats-Msg-Id": str(uuid4()),
        "published-at": datetime.now(UTC).isoformat(timespec="microseconds"),
    }


@pytest.fixture
def transport() -> NATSEventTransport:
    """NATSEventTransport that can run decode-path tests without connect().

    We set state CONNECTED so publish-path invariants don't trip during
    dispatch; we never call start() so the client stays None.
    """
    t = NATSEventTransport(
        NATSConfig(servers=["nats://localhost:4222"], subject_prefix="proctor"),
        events_config=EventsConfig(max_payload=65_536),
    )
    t._state = ConnectionState.CONNECTED  # type: ignore[assignment]
    return t


class TestHandCraftedDecode:
    """External publishers (Go/JS/etc.) must interoperate — these tests
    encode the wire format manually and push it through _on_message.
    """

    async def test_handcrafted_message_roundtrips_to_event(
        self, transport: NATSEventTransport
    ) -> None:
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        event = Event(
            type="trigger.webhook.github",
            source="external",
            payload={"commit": "abc123"},
        )
        data = event.model_dump_json().encode("utf-8")
        msg = _FakeMsg(
            subject="proctor.events.trigger.webhook.github",
            data=data,
            headers=_build_headers(event.type, version=1),
        )
        await transport._on_message(msg, handler)
        # Let the dispatched task run
        for _ in range(10):
            if received:
                break
            await asyncio.sleep(0.01)
        assert len(received) == 1
        got = received[0]
        assert got.type == "trigger.webhook.github"
        assert got.source == "external"
        assert got.payload["commit"] == "abc123"


class TestSchemaVersion:
    async def test_unknown_version_dropped(self, transport: NATSEventTransport) -> None:
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        event = Event(type="test.future", source="x", payload={})
        msg = _FakeMsg(
            subject="proctor.events.test.future",
            data=event.model_dump_json().encode("utf-8"),
            headers=_build_headers(event.type, version=99),
        )
        await transport._on_message(msg, handler)
        await asyncio.sleep(0.05)
        assert received == []  # dropped — no decoder registered for v99

    async def test_registered_custom_decoder_used(
        self, transport: NATSEventTransport
    ) -> None:
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        # Register a decoder for (test.legacy, version=2) that builds an
        # Event from a bespoke byte format.
        marker = f"custom-{uuid4().hex[:6]}"

        def _legacy_decoder(data: bytes) -> Event:
            return Event(
                type="test.legacy",
                source="legacy-system",
                payload={"raw": data.decode("utf-8"), "marker": marker},
            )

        register_decoder("test.legacy", 2, _legacy_decoder)
        try:
            msg = _FakeMsg(
                subject="proctor.events.test.legacy",
                data=b"legacy-wire-bytes",
                headers=_build_headers("test.legacy", version=2),
            )
            await transport._on_message(msg, handler)
            for _ in range(10):
                if received:
                    break
                await asyncio.sleep(0.01)
            assert len(received) == 1
            assert received[0].payload["marker"] == marker
        finally:
            # Keep registry clean for other tests
            _DECODERS.pop(("test.legacy", 2), None)

    async def test_default_catchall_handles_unregistered_type(
        self, transport: NATSEventTransport
    ) -> None:
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        # No specific decoder; ("*", 1) catch-all should decode via Event
        event = Event(type="new.unexpected", source="x", payload={})
        msg = _FakeMsg(
            subject="proctor.events.new.unexpected",
            data=event.model_dump_json().encode("utf-8"),
            headers=_build_headers(event.type, version=1),
        )
        await transport._on_message(msg, handler)
        for _ in range(10):
            if received:
                break
            await asyncio.sleep(0.01)
        assert len(received) == 1


class TestMalformedHeaders:
    async def test_missing_header_dropped(self, transport: NATSEventTransport) -> None:
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        headers = _build_headers("test.ok")
        del headers["schema-version"]
        msg = _FakeMsg(
            subject="proctor.events.test.ok",
            data=Event(type="test.ok", source="x", payload={})
            .model_dump_json()
            .encode("utf-8"),
            headers=headers,
        )
        await transport._on_message(msg, handler)
        await asyncio.sleep(0.05)
        assert received == []

    async def test_non_integer_schema_version_dropped(
        self, transport: NATSEventTransport
    ) -> None:
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        headers = _build_headers("test.ok")
        headers["schema-version"] = "abc"
        msg = _FakeMsg(
            subject="proctor.events.test.ok",
            data=Event(type="test.ok", source="x", payload={})
            .model_dump_json()
            .encode("utf-8"),
            headers=headers,
        )
        await transport._on_message(msg, handler)
        await asyncio.sleep(0.05)
        assert received == []

    async def test_subject_header_mismatch_dropped(
        self, transport: NATSEventTransport
    ) -> None:
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        # header says "test.a" but subject is for "test.b"
        msg = _FakeMsg(
            subject="proctor.events.test.b",
            data=Event(type="test.b", source="x", payload={})
            .model_dump_json()
            .encode("utf-8"),
            headers=_build_headers("test.a"),
        )
        await transport._on_message(msg, handler)
        await asyncio.sleep(0.05)
        assert received == []

    async def test_unsupported_content_type_dropped(
        self, transport: NATSEventTransport
    ) -> None:
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        headers = _build_headers("test.ok")
        headers["content-type"] = "application/msgpack"
        msg = _FakeMsg(
            subject="proctor.events.test.ok",
            data=b"\x00",
            headers=headers,
        )
        await transport._on_message(msg, handler)
        await asyncio.sleep(0.05)
        assert received == []


class TestPublishHeaders:
    """Verify publish() produces the 5 spec'd headers with correct values."""

    async def test_headers_have_required_keys(self) -> None:
        t = NATSEventTransport(
            NATSConfig(servers=["nats://x:4222"], subject_prefix="proctor"),
            events_config=EventsConfig(),
        )
        captured: dict[str, object] = {}

        class _FakeClient:
            async def publish(
                self,
                subject: str,
                data: bytes,
                headers: dict[str, str] | None = None,
            ) -> None:
                captured["subject"] = subject
                captured["data"] = data
                captured["headers"] = headers

        t._nc = _FakeClient()
        t._state = ConnectionState.CONNECTED  # type: ignore[assignment]

        event = Event(type="test.pub", source="x", payload={"k": "v"})
        await t.publish(event)

        assert captured["subject"] == "proctor.events.test.pub"
        headers = captured["headers"]
        assert isinstance(headers, dict)
        assert headers["content-type"] == "application/json"
        assert headers["schema-version"] == "1"
        assert headers["event-type"] == "test.pub"
        assert "Nats-Msg-Id" in headers
        assert "published-at" in headers
        # published-at is parseable ISO-8601 UTC
        published = datetime.fromisoformat(headers["published-at"])
        assert published.tzinfo is not None
