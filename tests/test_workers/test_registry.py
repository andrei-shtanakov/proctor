"""WorkerRegistry: liveness, first-alive-owns fencing, loss callback."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest

from proctor.core.bus import EventBus
from proctor.core.config import RegistryConfig
from proctor.core.models import Event
from proctor.core.transport import LocalEventTransport
from proctor.router.models import AgentProfile
from proctor.workers.registry import WorkerRegistry

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def bus() -> AsyncGenerator[EventBus, None]:
    b = EventBus(LocalEventTransport())
    await b.start()
    yield b
    await b.stop()


def _alive_event(
    wid: str = "worker_a", iid: str = "i1", event_type: str = "worker.heartbeat"
) -> Event:
    return Event(
        type=event_type,
        source=f"worker:{wid}",
        payload={
            "worker_id": wid,
            "instance_id": iid,
            "capabilities": ["python"],
            "max_slots": 2,
        },
    )


def _registry(bus: EventBus, now: datetime = T0) -> WorkerRegistry:
    clock = {"now": now}
    reg = WorkerRegistry(
        bus,
        RegistryConfig(heartbeat_interval=30.0, liveness_timeout=90.0),
        now_fn=lambda: clock["now"],
    )
    reg._test_clock = clock  # type: ignore[attr-defined]
    return reg


async def test_heartbeat_alone_creates_entry(bus: EventBus) -> None:
    """Core restart recovery: no prior worker.registered needed."""
    reg = _registry(bus)
    await reg._handle_alive(_alive_event())
    profiles = reg.alive_profiles()
    assert [p.id for p in profiles] == ["worker_a"]
    assert profiles[0].capabilities == ["python"]
    assert reg.instance_of("worker_a") == "i1"


async def test_local_profile_seeded_first(bus: EventBus) -> None:
    local = AgentProfile(id="local", max_slots=4)
    reg = WorkerRegistry(bus, RegistryConfig(), local_profile=local, now_fn=lambda: T0)
    await reg._handle_alive(_alive_event())
    assert [p.id for p in reg.alive_profiles()] == ["local", "worker_a"]


async def test_first_alive_owns_no_ping_pong(bus: EventBus) -> None:
    reg = _registry(bus)
    await reg._handle_alive(_alive_event(iid="i1"))
    await reg._handle_alive(_alive_event(iid="i2"))  # rejected
    await reg._handle_alive(_alive_event(iid="i1"))  # owner refresh
    await reg._handle_alive(_alive_event(iid="i2"))  # still rejected
    assert reg.instance_of("worker_a") == "i1"


async def test_graceful_offline_releases_then_next_claims(bus: EventBus) -> None:
    reg = _registry(bus)
    lost: list[tuple[str, str]] = []

    async def on_loss(wid: str, iid: str) -> None:
        lost.append((wid, iid))

    reg.add_loss_listener(on_loss)
    await reg._handle_alive(_alive_event(iid="i1"))
    await reg._handle_offline(
        Event(
            type="worker.offline",
            source="worker:worker_a",
            payload={
                "worker_id": "worker_a",
                "instance_id": "i1",
                "reason": "shutdown",
            },
        )
    )
    assert lost == [("worker_a", "i1")]
    assert reg.alive_profiles() == []
    await reg._handle_alive(_alive_event(iid="i2"))
    assert reg.instance_of("worker_a") == "i2"


async def test_stale_offline_ignored(bus: EventBus) -> None:
    reg = _registry(bus)
    lost: list[tuple[str, str]] = []

    async def on_loss(wid: str, iid: str) -> None:
        lost.append((wid, iid))

    reg.add_loss_listener(on_loss)
    await reg._handle_alive(_alive_event(iid="i2"))
    await reg._handle_offline(
        Event(
            type="worker.offline",
            source="worker:worker_a",
            payload={
                "worker_id": "worker_a",
                "instance_id": "i1",  # stale incarnation
                "reason": "shutdown",
            },
        )
    )
    assert lost == []
    assert reg.instance_of("worker_a") == "i2"


async def test_sweep_times_out_silent_worker(bus: EventBus) -> None:
    reg = _registry(bus)
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    bus.subscribe("worker.offline", collect)
    lost: list[tuple[str, str]] = []

    async def on_loss(wid: str, iid: str) -> None:
        lost.append((wid, iid))

    reg.add_loss_listener(on_loss)
    await reg._handle_alive(_alive_event(iid="i1"))
    await reg.sweep(now=T0 + timedelta(seconds=89))
    assert lost == []
    await reg.sweep(now=T0 + timedelta(seconds=90))
    assert lost == [("worker_a", "i1")]
    await reg.sweep(now=T0 + timedelta(seconds=91))  # exactly once
    assert lost == [("worker_a", "i1")]
    await bus.flush()
    offline = [e for e in collected if e.type == "worker.offline"]
    assert len(offline) == 1
    assert offline[0].payload["reason"] == "timeout"


async def test_local_id_conflict_rejected(bus: EventBus) -> None:
    local = AgentProfile(id="local", max_slots=4)
    reg = WorkerRegistry(bus, RegistryConfig(), local_profile=local, now_fn=lambda: T0)
    await reg._handle_alive(_alive_event(wid="local", iid="i9"))
    assert reg.instance_of("local") is None  # remote claim on local id ignored
    assert [p.id for p in reg.alive_profiles()] == ["local"]
