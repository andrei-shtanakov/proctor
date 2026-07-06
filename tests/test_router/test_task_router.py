"""Unit tests for the TaskRouter facade."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from pydantic import ValidationError

from proctor.core.bus import EventBus
from proctor.core.config import RouterConfig
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


def _router(
    bus: EventBus, agents: list[AgentProfile] | None = None, **overrides: object
) -> TaskRouter:
    defaults: dict[str, object] = {
        "max_concurrency": 2,
        "queue_ttl_seconds": 60.0,
    }
    defaults.update(overrides)
    config = RouterConfig(**defaults)  # type: ignore[arg-type]
    resolved = agents if agents is not None else [AgentProfile(id="local", max_slots=2)]
    return TaskRouter(bus=bus, config=config, agent_provider=lambda: resolved)


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


class TestRouterConfigBounds:
    """RouterConfig rejects nonsensical values at the model level."""

    def test_zero_max_concurrency_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RouterConfig(max_concurrency=0)  # type: ignore[bad-argument-type]

    def test_negative_ttl_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RouterConfig(queue_ttl_seconds=-1.0)

    def test_zero_ttl_allowed(self) -> None:
        # 0 is meaningful: reject immediately, never queue.
        assert RouterConfig(queue_ttl_seconds=0.0).queue_ttl_seconds == 0.0

    def test_zero_tick_rejected(self) -> None:
        # sleep(0) would spin the tick loop hot.
        with pytest.raises(ValidationError):
            RouterConfig(queue_tick_seconds=0.0)

    def test_legacy_router_agent_rejected(self) -> None:
        with pytest.raises(ValidationError, match="worker.max_slots"):
            RouterConfig.model_validate({"agent": {"max_slots": 2}})


async def test_no_agents_reason_is_prefixed(bus: EventBus) -> None:
    """Even the no-candidates reason follows the `name: detail` convention."""
    router = TaskRouter(bus=bus, config=RouterConfig(), agent_provider=lambda: [])
    decision = await router.admit(Task(), _spec(), "test", now=T0)
    assert decision.verdict == "queued"
    assert decision.reason is not None
    assert decision.reason.startswith("no_candidates:")


async def test_admit_reports_winning_agent(bus: EventBus) -> None:
    router = _router(bus)
    decision = await router.admit(Task(), _spec(), "test", now=T0)
    assert decision.verdict == "admitted"
    assert decision.agent_id == "local"


async def test_requires_filters_to_capable_agent(bus: EventBus) -> None:
    agents = [
        AgentProfile(id="local", max_slots=2),
        AgentProfile(id="py_worker", capabilities=["python"], max_slots=2),
    ]
    router = _router(bus, agents=agents)
    spec = WorkflowSpec(workflow_id="w", mode=WorkflowMode.SIMPLE, requires=["python"])
    decision = await router.admit(Task(), spec, "test", now=T0)
    assert decision.agent_id == "py_worker"


async def test_agent_provider_sees_live_list(bus: EventBus) -> None:
    agents: list[AgentProfile] = []
    router = TaskRouter(bus=bus, config=RouterConfig(), agent_provider=lambda: agents)
    d1 = await router.admit(Task(), _spec(), "test", now=T0)
    assert d1.verdict == "queued"  # no candidates yet
    agents.append(AgentProfile(id="local", max_slots=2))
    d2 = await router.admit(Task(), _spec(), "test", now=T0)
    assert d2.verdict == "admitted"


async def test_retry_enqueues_never_runs_inline(bus: EventBus) -> None:
    router = _router(bus, max_concurrency=8)
    task, spec = Task(), _spec()
    await router.retry(task, spec, "test", now=T0)
    assert router.running_count == 0  # queued, not reserved


async def test_retry_not_before_delays_dequeue(bus: EventBus) -> None:
    router = _router(bus)
    task, spec = Task(), _spec()
    await router.retry(
        task,
        spec,
        "test",
        not_before=T0 + timedelta(seconds=30),
        now=T0,
    )
    assert await router.dequeue_ready(now=T0 + timedelta(seconds=29)) == []
    ready = await router.dequeue_ready(now=T0 + timedelta(seconds=30))
    assert [e.task.id for e in ready] == [task.id]
    assert ready[0].agent_id == "local"


async def test_retry_ttl_anchored_at_not_before(bus: EventBus) -> None:
    router = _router(bus, queue_ttl_seconds=10.0)
    await router.retry(
        Task(),
        _spec(),
        "test",
        not_before=T0 + timedelta(seconds=60),
        now=T0,
    )
    # delay (60s) > ttl (10s): still alive right after becoming runnable
    assert await router.expire_overdue(now=T0 + timedelta(seconds=69)) == []
    expired = await router.expire_overdue(now=T0 + timedelta(seconds=70))
    assert len(expired) == 1
