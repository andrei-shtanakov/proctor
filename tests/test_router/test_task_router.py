"""Unit tests for the TaskRouter facade."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import anyio
import pytest

from proctor.core.bus import EventBus
from proctor.core.config import RouterAgentConfig, RouterConfig
from proctor.core.models import Event, Task
from proctor.core.transport import LocalEventTransport
from proctor.router.models import AgentProfile
from proctor.router.router import TaskRouter
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    """LocalEventTransport uses asyncio.create_task internally."""
    return "asyncio"


def _spec(name: str = "w", **kwargs: object) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=name,
        mode=WorkflowMode.SIMPLE,
        **kwargs,  # type: ignore[arg-type]
    )


def _router(bus: EventBus, **overrides: object) -> TaskRouter:
    defaults: dict[str, object] = {
        "max_concurrency": 2,
        "queue_ttl_seconds": 60.0,
        "agent": RouterAgentConfig(max_slots=2),
    }
    defaults.update(overrides)
    config = RouterConfig(**defaults)  # type: ignore[arg-type]
    return TaskRouter(
        bus=bus,
        config=config,
        agents=[AgentProfile(id="local", max_slots=config.agent.max_slots)],
    )


@pytest.fixture
async def bus() -> AsyncGenerator[EventBus, None]:
    b = EventBus(LocalEventTransport())
    await b.start()
    yield b
    await b.stop()


@pytest.fixture
def routing_events(bus: EventBus) -> list[Event]:
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    bus.subscribe("routing.>", collect)
    return collected


async def test_admit_within_limits(bus: EventBus) -> None:
    router = _router(bus)
    decision = await router.admit(Task(), _spec(), "test", now=T0)
    assert decision.verdict == "admitted"
    assert router.running_count == 1


async def test_blocked_task_queues(bus: EventBus, routing_events: list[Event]) -> None:
    router = _router(bus, max_concurrency=1)
    await router.admit(Task(), _spec("a"), "test", now=T0)
    decision = await router.admit(Task(), _spec("b"), "test", now=T0)
    assert decision.verdict == "queued"
    assert decision.reason is not None
    assert "concurrency_limit" in decision.reason
    await bus.flush()
    assert [e.type for e in routing_events] == ["routing.queued"]


async def test_ttl_zero_rejects(bus: EventBus, routing_events: list[Event]) -> None:
    router = _router(bus, max_concurrency=1, queue_ttl_seconds=0.0)
    await router.admit(Task(), _spec("a"), "test", now=T0)
    decision = await router.admit(Task(), _spec("b"), "test", now=T0)
    assert decision.verdict == "rejected"
    await bus.flush()
    assert [e.type for e in routing_events] == ["routing.rejected"]


async def test_release_dequeues_fifo(
    bus: EventBus, routing_events: list[Event]
) -> None:
    router = _router(bus, max_concurrency=1)
    first = Task()
    await router.admit(first, _spec("a"), "test", now=T0)
    await router.admit(Task(), _spec("b"), "test", now=T0)
    ready = await router.release(first.id, now=T0 + timedelta(seconds=5))
    assert [e.spec.workflow_id for e in ready] == ["b"]
    assert router.running_count == 1  # b's slot re-reserved
    await bus.flush()
    types = [e.type for e in routing_events]
    assert types == ["routing.queued", "routing.dequeued"]
    dequeued = routing_events[1]
    assert dequeued.payload["waited_seconds"] == 5.0


async def test_expire_overdue_emits_expired(
    bus: EventBus, routing_events: list[Event]
) -> None:
    router = _router(bus, max_concurrency=1, queue_ttl_seconds=30.0)
    await router.admit(Task(), _spec("a"), "test", now=T0)
    await router.admit(Task(), _spec("b"), "test", now=T0)
    expired = await router.expire_overdue(now=T0 + timedelta(seconds=31))
    assert [e.spec.workflow_id for e in expired] == ["b"]
    await bus.flush()
    types = [e.type for e in routing_events]
    assert types == ["routing.queued", "routing.expired"]


async def test_scope_conflict_queues(bus: EventBus) -> None:
    router = _router(bus)
    await router.admit(Task(), _spec("a", scope=["src/**"]), "test", now=T0)
    decision = await router.admit(
        Task(), _spec("b", scope=["src/foo.py"]), "test", now=T0
    )
    assert decision.verdict == "queued"
    assert decision.reason is not None
    assert "scope_isolation" in decision.reason


async def test_branch_conflict_queues(bus: EventBus) -> None:
    router = _router(bus)
    await router.admit(Task(), _spec("a", branch="rel"), "test", now=T0)
    decision = await router.admit(Task(), _spec("b", branch="rel"), "test", now=T0)
    assert decision.verdict == "queued"


async def test_concurrent_admit_race(bus: EventBus) -> None:
    """Spec §Admission atomicity: two admits at max_concurrency=1."""
    router = _router(bus, max_concurrency=1)
    decisions: list[str] = []

    async def admit_one() -> None:
        d = await router.admit(Task(), _spec(), "test", now=T0)
        decisions.append(d.verdict)

    async with anyio.create_task_group() as tg:
        tg.start_soon(admit_one)
        tg.start_soon(admit_one)

    assert sorted(decisions) == ["admitted", "queued"]
    assert router.running_count == 1
