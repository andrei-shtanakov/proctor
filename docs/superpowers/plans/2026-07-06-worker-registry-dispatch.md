# Worker Registry + NATS Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distribution loop per `docs/superpowers/specs/2026-07-06-worker-registry-dispatch-design.md`: worker discovery with heartbeat liveness and first-alive-owns fencing, real capability scoring, remote dispatch over the EventBus with dispatch_id/instance_id fencing, worker-loss policy.

**Architecture:** New `workers/registry.py` (WorkerRegistry, loss via callback) and `workers/node.py` (WorkerNode). TaskRouter gets `agent_provider` + `retry()`. Bootstrap gets a dispatch layer (in-flight map, pop-if-current result handling, deadline reaper) and a worker-role branch.

**Tech Stack:** Python 3.12, pydantic 2.x, asyncio/anyio, existing EventBus (Local/NATS transports).

## Global Constraints

- uv only (`uv run pytest`); line length 88; `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check` clean before every commit; type hints everywhere; async tests use anyio (asyncio backend fixture where aiosqlite/LocalEventTransport involved).
- All models pydantic `BaseModel`.
- **Synchronous critical sections**: registry entry removal, in-flight map pop-if-current, and TaskRouter reservations mutate BEFORE their first `await`.
- `worker_id` charset `^[a-z][a-z0-9_]*$`. Worker-loss delivery = registry callback only; `worker.offline` bus events are observability.
- Event payloads exactly as in the spec's Protocol table (incl. `dispatch_id`, `instance_id`, `target_instance_id`; heartbeat carries the full profile).
- Branch: `feat/worker-registry`. TDD per task; commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Config groundwork (additive only)

**Files:**
- Modify: `src/proctor/core/config.py` (add `WorkerConfig`, `RegistryConfig`, two `ProctorConfig` fields + validator)
- Modify: `src/proctor/workflow/spec.py` (add `requires`, `retry_on_worker_loss`)
- Test: `tests/test_core/test_config.py` (append), `tests/test_workflow/test_spec_fields.py` (new)

**Interfaces:**
- Produces: `WorkerConfig(id: str = "local", capabilities: list[str] = [], max_slots: int = 4)`; `RegistryConfig(heartbeat_interval: float = 30.0, liveness_timeout: float = 90.0)`; `ProctorConfig.worker`, `ProctorConfig.registry`; `WorkflowSpec.requires: list[str]`; `WorkflowPolicies.retry_on_worker_loss: bool`.
- NOTE: `RouterConfig.agent` is NOT removed here (bootstrap still uses it) — removal happens in Task 5 so every task stays green.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_core/test_config.py` (class `TestLoadConfig` level, or a new top-level class):

```python
class TestWorkerAndRegistryConfig:
    def test_defaults(self) -> None:
        cfg = ProctorConfig()
        assert cfg.worker.id == "local"
        assert cfg.worker.max_slots == 4
        assert cfg.registry.heartbeat_interval == 30.0
        assert cfg.registry.liveness_timeout == 90.0

    def test_worker_id_charset(self) -> None:
        with pytest.raises(ValidationError):
            WorkerConfig(id="worker-1")  # hyphen not subject-safe

    def test_worker_role_requires_explicit_id(self) -> None:
        with pytest.raises(ValidationError, match="worker.id"):
            ProctorConfig(node_role="worker", worker=WorkerConfig(id="local"))

    def test_worker_role_with_explicit_id_ok(self) -> None:
        cfg = ProctorConfig(
            node_role="worker",
            worker=WorkerConfig(id="worker_a"),
            transport="local",
        )
        assert cfg.worker.id == "worker_a"

    def test_liveness_must_exceed_heartbeat(self) -> None:
        with pytest.raises(ValidationError):
            RegistryConfig(heartbeat_interval=30.0, liveness_timeout=30.0)
```

`ProctorConfig(node_role="worker", ...)` triggers the transport validator (worker resolves to nats) — pass `transport="local"`? No: resolution for `node_role="worker"` + `transport="auto"` is nats, and nats.servers defaults non-empty, so it passes; the explicit `transport="local"` in the ok-test keeps it independent of NATS defaults. Add imports `WorkerConfig, RegistryConfig` to the test file's import block.

New file `tests/test_workflow/test_spec_fields.py`:

```python
"""WorkflowSpec/WorkflowPolicies fields added for distribution."""

from proctor.workflow.spec import WorkflowMode, WorkflowPolicies, WorkflowSpec


def test_requires_defaults_empty() -> None:
    spec = WorkflowSpec(workflow_id="w", mode=WorkflowMode.SIMPLE)
    assert spec.requires == []


def test_requires_roundtrip() -> None:
    spec = WorkflowSpec(
        workflow_id="w", mode=WorkflowMode.SIMPLE, requires=["python"]
    )
    assert spec.requires == ["python"]


def test_retry_on_worker_loss_defaults_false() -> None:
    assert WorkflowPolicies().retry_on_worker_loss is False
```

(`tests/test_workflow/` already exists with an `__init__.py`; if not, create the empty `__init__.py`.)

- [ ] **Step 2: Run to verify failures**

`uv run pytest tests/test_core/test_config.py::TestWorkerAndRegistryConfig tests/test_workflow/test_spec_fields.py -v` — ImportError / ValidationError-not-raised failures expected.

- [ ] **Step 3: Implement**

In `src/proctor/core/config.py`, next to `RouterConfig`:

```python
class WorkerConfig(BaseModel):
    """Identity and capacity of this node's executor."""

    id: str = Field(default="local", pattern=r"^[a-z][a-z0-9_]*$")
    capabilities: list[str] = Field(default_factory=list)
    max_slots: int = Field(default=4, ge=1)


class RegistryConfig(BaseModel):
    """Worker discovery/liveness settings (core/standalone only)."""

    heartbeat_interval: float = Field(default=30.0, gt=0.0)
    liveness_timeout: float = Field(default=90.0, gt=0.0)

    @model_validator(mode="after")
    def _liveness_exceeds_heartbeat(self) -> Self:
        if self.liveness_timeout <= self.heartbeat_interval:
            raise ValueError(
                "registry.liveness_timeout must exceed heartbeat_interval"
            )
        return self
```

In `ProctorConfig`: add fields `worker: WorkerConfig = WorkerConfig()` and `registry: RegistryConfig = RegistryConfig()`, plus:

```python
    @model_validator(mode="after")
    def _worker_role_requires_explicit_id(self) -> Self:
        if self.node_role == "worker" and self.worker.id == "local":
            raise ValueError(
                "node_role 'worker' requires an explicit worker.id; "
                "'local' is reserved for the core's inline executor"
            )
        return self
```

In `src/proctor/workflow/spec.py`: add `retry_on_worker_loss: bool = False` to `WorkflowPolicies`; add `requires: list[str] = Field(default_factory=list)` to `WorkflowSpec` (next to `scope`).

- [ ] **Step 4: Run tests + full suite; gates; commit**

```bash
uv run pytest tests/test_core/test_config.py tests/test_workflow/ -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(config): worker/registry sections + requires/retry_on_worker_loss"
```

---

### Task 2: WorkerRegistry

**Files:**
- Create: `src/proctor/workers/registry.py`
- Test: `tests/test_workers/test_registry.py`

**Interfaces:**
- Consumes: `RegistryConfig` (Task 1), `AgentProfile`, `EventBus`, `Event`.
- Produces: `WorkerRegistry(bus, config, *, local_profile=None, now_fn=None)` with `alive_profiles() -> list[AgentProfile]`, `instance_of(worker_id) -> str | None`, `add_loss_listener(cb: Callable[[str, str], Awaitable[None]])`, `async sweep(now=None)`, `async start()`, `async stop()`. Task 5 wires it.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_workers/test_registry.py
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
    reg = WorkerRegistry(
        bus, RegistryConfig(), local_profile=local, now_fn=lambda: T0
    )
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
    reg = WorkerRegistry(
        bus, RegistryConfig(), local_profile=local, now_fn=lambda: T0
    )
    await reg._handle_alive(_alive_event(wid="local", iid="i9"))
    assert reg.instance_of("local") is None  # remote claim on local id ignored
    assert [p.id for p in reg.alive_profiles()] == ["local"]
```

- [ ] **Step 2: Run to verify failure**

`uv run pytest tests/test_workers/test_registry.py -v` — `ModuleNotFoundError: proctor.workers.registry`

- [ ] **Step 3: Implement**

```python
# src/proctor/workers/registry.py
"""WorkerRegistry — live worker catalog with heartbeat liveness.

Fencing policy is first-alive-owns: a worker_id is bound to the first
instance seen and released only on offline (graceful or timeout).
Worker loss is delivered to listeners via an awaited callback, exactly
once per lost incarnation, at the moment the entry is removed — bus
``worker.offline`` events are observability only. Publication is
asymmetric: on graceful shutdown the worker already published the
event, so the registry publishes nothing; on timeout the registry is
the publisher.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from proctor.core.bus import EventBus
from proctor.core.config import RegistryConfig
from proctor.core.models import Event
from proctor.router.models import AgentProfile

logger = logging.getLogger(__name__)

_SOURCE = "worker_registry"

LossListener = Callable[[str, str], Awaitable[None]]


class WorkerEntry(BaseModel):
    """Registry bookkeeping for one live remote worker."""

    profile: AgentProfile
    instance_id: str
    last_seen: datetime


class WorkerRegistry:
    """Live catalog of workers; the only source of scoring candidates."""

    def __init__(
        self,
        bus: EventBus,
        config: RegistryConfig,
        *,
        local_profile: AgentProfile | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._bus = bus
        self._config = config
        self._local = local_profile
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._entries: dict[str, WorkerEntry] = {}
        self._loss_listeners: list[LossListener] = []
        self._sweep_task: asyncio.Task[None] | None = None
        bus.subscribe("worker.registered", self._handle_alive)
        bus.subscribe("worker.heartbeat", self._handle_alive)
        bus.subscribe("worker.offline", self._handle_offline)

    def add_loss_listener(self, cb: LossListener) -> None:
        """Register a callback awaited once per lost incarnation."""
        self._loss_listeners.append(cb)

    def alive_profiles(self) -> list[AgentProfile]:
        """Current candidates: seeded local profile plus live remotes."""
        remotes = [e.profile for e in self._entries.values()]
        return ([self._local] if self._local is not None else []) + remotes

    def instance_of(self, worker_id: str) -> str | None:
        """Current owning instance of a remote worker id, if any."""
        entry = self._entries.get(worker_id)
        return entry.instance_id if entry is not None else None

    async def start(self) -> None:
        """Start the periodic liveness sweep."""
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        """Cancel the sweep loop (idempotent)."""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await self._sweep_task
                except Exception:
                    logger.exception("Registry sweep task exited with error")
            self._sweep_task = None

    async def sweep(self, now: datetime | None = None) -> None:
        """Remove workers silent past liveness_timeout; notify + publish."""
        now = now or self._now()
        cutoff = timedelta(seconds=self._config.liveness_timeout)
        dead = [
            (wid, entry)
            for wid, entry in self._entries.items()
            if now - entry.last_seen >= cutoff
        ]
        for wid, _ in dead:
            del self._entries[wid]  # sync removal before any await
        for wid, entry in dead:
            logger.warning("Worker %s timed out (instance %s)", wid, entry.instance_id)
            await self._notify_loss(wid, entry.instance_id)
            await self._bus.publish(
                Event(
                    type="worker.offline",
                    source=_SOURCE,
                    payload={
                        "worker_id": wid,
                        "instance_id": entry.instance_id,
                        "reason": "timeout",
                    },
                )
            )

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.heartbeat_interval)
            try:
                await self.sweep()
            except Exception:
                logger.exception("Registry sweep failed")

    async def _handle_alive(self, event: Event) -> None:
        wid = event.payload.get("worker_id")
        iid = event.payload.get("instance_id")
        if not isinstance(wid, str) or not isinstance(iid, str):
            logger.warning("Malformed %s payload: %s", event.type, event.payload)
            return
        if self._local is not None and wid == self._local.id:
            logger.warning(
                "Rejecting remote claim on reserved local id %r", wid
            )
            return
        entry = self._entries.get(wid)
        if entry is not None and entry.instance_id != iid:
            logger.warning(
                "Worker %s owned by instance %s; rejecting %s",
                wid,
                entry.instance_id,
                iid,
            )
            return
        capabilities = event.payload.get("capabilities") or []
        max_slots = event.payload.get("max_slots") or 1
        self._entries[wid] = WorkerEntry(
            profile=AgentProfile(
                id=wid, capabilities=capabilities, max_slots=max_slots
            ),
            instance_id=iid,
            last_seen=self._now(),
        )

    async def _handle_offline(self, event: Event) -> None:
        wid = event.payload.get("worker_id")
        iid = event.payload.get("instance_id")
        if not isinstance(wid, str) or not isinstance(iid, str):
            return
        if event.source == _SOURCE:
            return  # our own timeout publication — already handled
        entry = self._entries.get(wid)
        if entry is None or entry.instance_id != iid:
            logger.info("Ignoring stale worker.offline for %s/%s", wid, iid)
            return
        del self._entries[wid]  # sync removal before any await
        await self._notify_loss(wid, iid)
        # Graceful path: the worker already published the event —
        # re-publishing would duplicate observability signals.

    async def _notify_loss(self, worker_id: str, instance_id: str) -> None:
        for cb in self._loss_listeners:
            try:
                await cb(worker_id, instance_id)
            except Exception:
                logger.exception("Worker-loss listener failed for %s", worker_id)
```

- [ ] **Step 4: Run tests; gates; commit**

```bash
uv run pytest tests/test_workers/test_registry.py -v && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(workers): WorkerRegistry — liveness, fencing, loss callback"
```

---

### Task 3: Scoring + TaskRouter API (agent_provider, agent_id, retry, not_before)

**Files:**
- Modify: `src/proctor/router/scoring.py`, `src/proctor/router/models.py`, `src/proctor/router/router.py`
- Modify: `tests/test_router/test_scoring.py` (rewrite), `tests/test_router/test_task_router.py` (constructor updates + new tests)

**Interfaces:**
- Produces: `score_candidates(spec, agents, used_slots: Mapping[str, int] | None = None) -> list[Candidate]`; `AdmitDecision.agent_id: str | None`; `QueueEntry.not_before: datetime | None`, `QueueEntry.agent_id: str | None` (set on dequeue — Task 5's dispatch branch reads it); `TaskRouter(bus, config, agent_provider: Callable[[], list[AgentProfile]])`; `TaskRouter.retry(task, spec, trigger_source, not_before=None, now=None) -> None`.
- `_try_reserve` now returns `tuple[str | None, str | None]` (reason, agent_id).

- [ ] **Step 1: Rewrite scoring tests (failing)**

Replace `tests/test_router/test_scoring.py` content:

```python
"""Tests for capability scoring."""

from proctor.router.models import AgentProfile
from proctor.router.scoring import score_candidates
from proctor.workflow.spec import WorkflowMode, WorkflowSpec


def _spec(requires: list[str] | None = None) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="w", mode=WorkflowMode.SIMPLE, requires=requires or []
    )


def test_capability_filter() -> None:
    agents = [
        AgentProfile(id="py", capabilities=["python"], max_slots=2),
        AgentProfile(id="sh", capabilities=["shell"], max_slots=2),
    ]
    got = score_candidates(_spec(["python"]), agents)
    assert [c.profile.id for c in got] == ["py"]


def test_empty_requires_matches_all() -> None:
    agents = [AgentProfile(id="a"), AgentProfile(id="b")]
    assert len(score_candidates(_spec(), agents)) == 2


def test_free_slot_ranking() -> None:
    agents = [
        AgentProfile(id="busy", max_slots=4),
        AgentProfile(id="idle", max_slots=4),
    ]
    got = score_candidates(_spec(), agents, used_slots={"busy": 3})
    assert [c.profile.id for c in got] == ["idle", "busy"]
    assert got[0].score == 4.0
    assert got[1].score == 1.0


def test_zero_free_slots_kept() -> None:
    # agent_available (one place) decides, not the scorer
    agents = [AgentProfile(id="full", max_slots=2)]
    got = score_candidates(_spec(), agents, used_slots={"full": 2})
    assert [c.profile.id for c in got] == ["full"]


def test_stable_order_on_ties() -> None:
    agents = [AgentProfile(id="a"), AgentProfile(id="b")]
    got = score_candidates(_spec(), agents)
    assert [c.profile.id for c in got] == ["a", "b"]


def test_no_agents() -> None:
    assert score_candidates(_spec(), []) == []
```

- [ ] **Step 2: Add failing TaskRouter tests**

In `tests/test_router/test_task_router.py`, change the `_router` helper to the new constructor and add tests. Update the helper:

```python
def _router(bus: EventBus, agents: list[AgentProfile] | None = None,
            **overrides: object) -> TaskRouter:
    defaults: dict[str, object] = {
        "max_concurrency": 2,
        "queue_ttl_seconds": 60.0,
    }
    defaults.update(overrides)
    config = RouterConfig(**defaults)  # type: ignore[arg-type]
    resolved = agents if agents is not None else [
        AgentProfile(id="local", max_slots=2)
    ]
    return TaskRouter(bus=bus, config=config, agent_provider=lambda: resolved)
```

(Keep `RouterAgentConfig` out of the helper — the `agent=` kwarg disappears; the agent's slots now come from the profile. Existing tests that pass `agent=RouterAgentConfig(...)` change to `agents=[AgentProfile(id="local", max_slots=N)]`. The bounds-test class keeps its `RouterAgentConfig` cases until Task 5 removes the model — leave them untouched here.)

Add new tests:

```python
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
    spec = WorkflowSpec(
        workflow_id="w", mode=WorkflowMode.SIMPLE, requires=["python"]
    )
    decision = await router.admit(Task(), spec, "test", now=T0)
    assert decision.agent_id == "py_worker"


async def test_agent_provider_sees_live_list(bus: EventBus) -> None:
    agents: list[AgentProfile] = []
    router = TaskRouter(
        bus=bus, config=RouterConfig(), agent_provider=lambda: agents
    )
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
        task, spec, "test",
        not_before=T0 + timedelta(seconds=30), now=T0,
    )
    assert await router.dequeue_ready(now=T0 + timedelta(seconds=29)) == []
    ready = await router.dequeue_ready(now=T0 + timedelta(seconds=30))
    assert [e.task.id for e in ready] == [task.id]
    assert ready[0].agent_id == "local"


async def test_retry_ttl_anchored_at_not_before(bus: EventBus) -> None:
    router = _router(bus, queue_ttl_seconds=10.0)
    await router.retry(
        Task(), _spec(), "test",
        not_before=T0 + timedelta(seconds=60), now=T0,
    )
    # delay (60s) > ttl (10s): still alive right after becoming runnable
    assert await router.expire_overdue(now=T0 + timedelta(seconds=69)) == []
    expired = await router.expire_overdue(now=T0 + timedelta(seconds=70))
    assert len(expired) == 1
```

(Needs `timedelta` and `WorkflowMode, WorkflowSpec` already imported in the file; add `timedelta` if missing.)

- [ ] **Step 3: Run to verify failures**, then **Step 4: Implement**

`scoring.py` (full replacement):

```python
"""Capability scoring: filter by requires, rank by free slots."""

from collections.abc import Mapping

from proctor.router.models import AgentProfile, Candidate
from proctor.workflow.spec import WorkflowSpec


def score_candidates(
    spec: WorkflowSpec,
    agents: list[AgentProfile],
    used_slots: Mapping[str, int] | None = None,
) -> list[Candidate]:
    """Candidates able to run ``spec``, best (most free slots) first.

    Zero-free-slot agents stay in the list: the agent_available
    invariant is the single place that rejects them. Sort is stable —
    ties keep registry order.
    """
    used = used_slots or {}
    required = set(spec.requires)
    eligible = [a for a in agents if required <= set(a.capabilities)]
    scored = [
        Candidate(profile=a, score=float(a.max_slots - used.get(a.id, 0)))
        for a in eligible
    ]
    return sorted(scored, key=lambda c: -c.score)
```

`models.py`: add to `AdmitDecision`: `agent_id: str | None = None`; add to `QueueEntry`:

```python
    not_before: datetime | None = None
    agent_id: str | None = None  # set when a dequeue reserves a slot
```

`router.py` changes:

```python
from collections.abc import Callable
```

Constructor:

```python
    def __init__(
        self,
        bus: EventBus,
        config: RouterConfig,
        agent_provider: Callable[[], list[AgentProfile]],
    ) -> None:
        self._bus = bus
        self._config = config
        self._agent_provider = agent_provider
        self._running: list[RunningTask] = []
        self._queue = PendingQueue()
```

`_try_reserve` (full replacement):

```python
    def _try_reserve(
        self, task: Task, spec: WorkflowSpec
    ) -> tuple[str | None, str | None]:
        """Reserve a slot synchronously: (None, agent_id) on success,
        (reason, None) otherwise. MUST stay free of awaits."""
        agents = self._agent_provider()
        used: dict[str, int] = {}
        for r in self._running:
            used[r.agent_id] = used.get(r.agent_id, 0) + 1
        candidates = score_candidates(spec, agents, used)
        if not candidates:
            if spec.requires:
                return (
                    "no_candidates: no live worker offers "
                    f"{sorted(spec.requires)}",
                    None,
                )
            return "no_candidates: no live workers", None
        reason = "no_candidates: no live workers"
        for candidate in candidates:
            reason = self._check(spec, candidate.profile)
            if reason is None:
                self._running.append(
                    RunningTask(
                        task_id=task.id,
                        agent_id=candidate.profile.id,
                        scope=spec.scope,
                        branch=spec.branch,
                    )
                )
                return None, candidate.profile.id
        return reason, None
```

`admit`: unpack `reason, agent_id = self._try_reserve(task, spec)`; the admitted branch returns `AdmitDecision(verdict="admitted", agent_id=agent_id)`; the rest unchanged.

`dequeue_ready`: predicate honors `not_before` and records the winner:

```python
        now = now or datetime.now(UTC)

        def _try_admit(entry: QueueEntry) -> bool:
            if entry.not_before is not None and entry.not_before > now:
                return False
            reason, agent_id = self._try_reserve(entry.task, entry.spec)
            if reason is None:
                entry.agent_id = agent_id
                return True
            return False

        ready = self._queue.pop_admissible(_try_admit)
```

New method:

```python
    async def retry(
        self,
        task: Task,
        spec: WorkflowSpec,
        trigger_source: str,
        not_before: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        """Park a task for re-dispatch. Never re-admits inline.

        TTL is anchored at the moment the entry becomes runnable, so a
        retry delay longer than the TTL still gets a full TTL window.
        """
        now = now or datetime.now(UTC)
        runnable_at = max(now, not_before) if not_before is not None else now
        entry = QueueEntry(
            task=task,
            spec=spec,
            trigger_source=trigger_source,
            enqueued_at=now,
            not_before=not_before,
            expires_at=runnable_at
            + timedelta(seconds=self._config.queue_ttl_seconds),
            reason="retry: worker lost",
        )
        self._queue.push(entry)
        await self._bus.publish(
            Event(
                type="routing.queued",
                source=_SOURCE,
                payload={
                    "task_id": task.id,
                    "reason": entry.reason,
                    "retry": True,
                    "expires_at": entry.expires_at.isoformat(),
                },
            )
        )
```

Also update the two existing tests that assert `no_candidates:` prefixes if wording changed (`test_no_agents_reason_is_prefixed` still passes — provider returns `[]` → "no_candidates: no live workers").

- [ ] **Step 5: Run router tests + full suite; gates; commit**

```bash
uv run pytest tests/test_router/ -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(router): agent_provider, capability scoring, retry with not_before"
```

Note: bootstrap still constructs `TaskRouter(..., agents=[...])` — that call site breaks the moment this task lands. Fix it in the SAME commit with the minimal shim so the suite stays green:
in `bootstrap.py` `start()` replace the `TaskRouter(...)` block with:

```python
        local_profile = AgentProfile(
            id=self.config.worker.id,
            capabilities=self.config.worker.capabilities,
            max_slots=self.config.worker.max_slots,
        )
        self._task_router = TaskRouter(
            bus=self.bus,
            config=self.config.router,
            agent_provider=lambda: [local_profile],
        )
```

(The registry replaces this lambda in Task 5; `router.agent` is thereby already unused by code and its removal in Task 5 is config-only.)

---

### Task 4: WorkerNode

**Files:**
- Create: `src/proctor/workers/node.py`
- Test: `tests/test_workers/test_node.py`

**Interfaces:**
- Consumes: `WorkerConfig`, `EventBus` (incl. `flush()`), `WorkflowEngine` (only `.execute(spec) -> result with .output/.error`).
- Produces: `WorkerNode(bus, config: WorkerConfig, engine, *, heartbeat_interval: float, drain_timeout: float = 10.0)` with `instance_id: str`, `async start()`, `async stop()`. Task 6 wires it into worker-role bootstrap.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_workers/test_node.py
"""WorkerNode: barrier, fencing, execution, busy, shutdown order."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import anyio
import pytest

from proctor.core.bus import EventBus
from proctor.core.config import WorkerConfig
from proctor.core.models import Event
from proctor.core.transport import LocalEventTransport
from proctor.workers.node import WorkerNode

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _StubResult:
    def __init__(self, output: str | None, error: str | None) -> None:
        self.output = output
        self.error = error


class _StubEngine:
    def __init__(self) -> None:
        self.gate: anyio.Event | None = None
        self.calls: list[Any] = []

    async def execute(self, spec: Any) -> _StubResult:
        self.calls.append(spec)
        if self.gate is not None:
            await self.gate.wait()
        return _StubResult(output="done", error=None)


@pytest.fixture
async def bus() -> AsyncGenerator[EventBus, None]:
    b = EventBus(LocalEventTransport())
    await b.start()
    yield b
    await b.stop()


def _node(bus: EventBus, engine: _StubEngine | None = None) -> WorkerNode:
    return WorkerNode(
        bus,
        WorkerConfig(id="worker_a", capabilities=["python"], max_slots=1),
        engine or _StubEngine(),
        heartbeat_interval=0.05,
        drain_timeout=0.5,
    )


def _assign(node: WorkerNode, task_id: str = "t1",
            dispatch_id: str = "d1") -> Event:
    return Event(
        type="task.assign.worker_a",
        source="application",
        payload={
            "dispatch_id": dispatch_id,
            "target_instance_id": node.instance_id,
            "task": {"id": task_id},
            "spec": {"workflow_id": "w", "mode": "simple", "prompt": "hi"},
        },
    )


async def _collect(bus: EventBus, subject: str) -> list[Event]:
    collected: list[Event] = []

    async def handler(event: Event) -> None:
        collected.append(event)

    bus.subscribe(subject, handler)
    return collected


async def test_start_registers_after_subscribe(bus: EventBus) -> None:
    worker_events = await _collect(bus, "worker.>")
    node = _node(bus)
    await node.start()
    try:
        await bus.flush()
        assert worker_events[0].type == "worker.registered"
        p = worker_events[0].payload
        assert p["worker_id"] == "worker_a"
        assert p["instance_id"] == node.instance_id
        assert p["capabilities"] == ["python"]
        assert p["max_slots"] == 1
    finally:
        await node.stop()


async def test_assign_executes_and_results(bus: EventBus) -> None:
    results = await _collect(bus, "task.result")
    engine = _StubEngine()
    node = _node(bus, engine)
    await node.start()
    try:
        await bus.publish(_assign(node))
        with anyio.fail_after(2):
            while not results:
                await anyio.sleep(0.01)
        p = results[0].payload
        assert p["task_id"] == "t1"
        assert p["dispatch_id"] == "d1"
        assert p["instance_id"] == node.instance_id
        assert p["ok"] is True
        assert p["output"] == "done"
    finally:
        await node.stop()


async def test_foreign_instance_assign_dropped(bus: EventBus) -> None:
    results = await _collect(bus, "task.result")
    engine = _StubEngine()
    node = _node(bus, engine)
    await node.start()
    try:
        event = _assign(node)
        event.payload["target_instance_id"] = "someone_else"
        await bus.publish(event)
        await bus.flush()
        await anyio.sleep(0.05)
        assert engine.calls == []
        assert results == []
    finally:
        await node.stop()


async def test_over_capacity_reports_busy(bus: EventBus) -> None:
    results = await _collect(bus, "task.result")
    engine = _StubEngine()
    engine.gate = anyio.Event()  # first task blocks
    node = _node(bus, engine)  # max_slots=1
    await node.start()
    try:
        await bus.publish(_assign(node, task_id="t1", dispatch_id="d1"))
        with anyio.fail_after(2):
            while not engine.calls:
                await anyio.sleep(0.01)
        await bus.publish(_assign(node, task_id="t2", dispatch_id="d2"))
        with anyio.fail_after(2):
            while not results:
                await anyio.sleep(0.01)
        assert results[0].payload["task_id"] == "t2"
        assert results[0].payload["ok"] is False
        assert results[0].payload["error"] == "worker_busy"
    finally:
        engine.gate.set()
        await node.stop()


async def test_stop_order_offline_is_last_worker_event(bus: EventBus) -> None:
    worker_events = await _collect(bus, "worker.>")
    node = _node(bus)
    await node.start()
    await asyncio.sleep(0.12)  # let a couple of heartbeats out
    await node.stop()
    await bus.flush()
    assert worker_events[-1].type == "worker.offline"
    assert worker_events[-1].payload["reason"] == "shutdown"
    assert worker_events[-1].payload["instance_id"] == node.instance_id
    # nothing after offline
    types_after = [
        e.type for e in worker_events[worker_events.index(worker_events[-1]) + 1:]
    ]
    assert types_after == []
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: Implement**

```python
# src/proctor/workers/node.py
"""WorkerNode — the worker-role runtime.

Startup order matters: subscribe → bus.flush() (the transport registers
subscriptions via a background task; flush completes them) → publish
worker.registered. Shutdown order matters more: stop accepting → cancel
AND await the heartbeat loop (a heartbeat escaping after offline would
re-register the worker) → drain executions → worker.offline(shutdown)
→ flush.
"""

import asyncio
import contextlib
import logging
from typing import Any, Protocol
from uuid import uuid4

from proctor.core.bus import EventBus
from proctor.core.config import WorkerConfig
from proctor.core.models import Event
from proctor.workflow.spec import WorkflowSpec

logger = logging.getLogger(__name__)


class _Engine(Protocol):
    async def execute(self, spec: WorkflowSpec) -> Any: ...


class WorkerNode:
    """Register, heartbeat, execute assignments, publish results."""

    def __init__(
        self,
        bus: EventBus,
        config: WorkerConfig,
        engine: _Engine,
        *,
        heartbeat_interval: float,
        drain_timeout: float = 10.0,
    ) -> None:
        self._bus = bus
        self._config = config
        self._engine = engine
        self._interval = heartbeat_interval
        self._drain_timeout = drain_timeout
        self.instance_id = str(uuid4())
        self._source = f"worker:{config.id}"
        self._accepting = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._exec_tasks: set[asyncio.Task[None]] = set()

    def _profile_payload(self) -> dict[str, Any]:
        return {
            "worker_id": self._config.id,
            "instance_id": self.instance_id,
            "capabilities": self._config.capabilities,
            "max_slots": self._config.max_slots,
        }

    async def start(self) -> None:
        """Subscribe, wait for subscription readiness, then announce."""
        self._bus.subscribe(
            f"task.assign.{self._config.id}", self._handle_assign
        )
        await self._bus.flush()  # readiness barrier — see module docstring
        self._accepting = True
        await self._bus.publish(
            Event(
                type="worker.registered",
                source=self._source,
                payload=self._profile_payload(),
            )
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            "WorkerNode %s started (instance %s)",
            self._config.id,
            self.instance_id,
        )

    async def stop(self) -> None:
        """Tear down in the safe order (no post-offline heartbeats)."""
        self._accepting = False
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await self._heartbeat_task
                except Exception:
                    logger.exception("Heartbeat loop exited with error")
            self._heartbeat_task = None
        if self._exec_tasks:
            _, pending = await asyncio.wait(
                self._exec_tasks, timeout=self._drain_timeout
            )
            for exec_task in pending:
                exec_task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await self._bus.publish(
            Event(
                type="worker.offline",
                source=self._source,
                payload={
                    "worker_id": self._config.id,
                    "instance_id": self.instance_id,
                    "reason": "shutdown",
                },
            )
        )
        await self._bus.flush()
        logger.info("WorkerNode %s stopped", self._config.id)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._bus.publish(
                    Event(
                        type="worker.heartbeat",
                        source=self._source,
                        payload=self._profile_payload(),
                    )
                )
            except Exception:
                logger.exception("Heartbeat publish failed")

    async def _handle_assign(self, event: Event) -> None:
        if not self._accepting:
            return
        payload = event.payload
        if payload.get("target_instance_id") != self.instance_id:
            logger.warning(
                "Dropping assignment for foreign instance %s",
                payload.get("target_instance_id"),
            )
            return
        dispatch_id = payload.get("dispatch_id")
        task_payload = payload.get("task") or {}
        task_id = task_payload.get("id")
        if not isinstance(dispatch_id, str) or not isinstance(task_id, str):
            logger.warning("Malformed task.assign payload: %s", payload)
            return
        if len(self._exec_tasks) >= self._config.max_slots:
            await self._publish_result(
                task_id, dispatch_id, ok=False, error="worker_busy"
            )
            return
        spec = WorkflowSpec.model_validate(payload.get("spec"))
        exec_task = asyncio.create_task(
            self._execute(task_id, dispatch_id, spec)
        )
        self._exec_tasks.add(exec_task)
        exec_task.add_done_callback(self._exec_tasks.discard)

    async def _execute(
        self, task_id: str, dispatch_id: str, spec: WorkflowSpec
    ) -> None:
        try:
            result = await self._engine.execute(spec)
            if result.error:
                await self._publish_result(
                    task_id, dispatch_id, ok=False, error=result.error
                )
            else:
                await self._publish_result(
                    task_id, dispatch_id, ok=True, output=result.output
                )
        except Exception as exc:
            logger.exception("Execution of task %s crashed", task_id)
            with contextlib.suppress(Exception):
                await self._publish_result(
                    task_id, dispatch_id, ok=False, error=str(exc)
                )

    async def _publish_result(
        self,
        task_id: str,
        dispatch_id: str,
        *,
        ok: bool,
        output: str | None = None,
        error: str | None = None,
    ) -> None:
        await self._bus.publish(
            Event(
                type="task.result",
                source=self._source,
                payload={
                    "task_id": task_id,
                    "dispatch_id": dispatch_id,
                    "worker_id": self._config.id,
                    "instance_id": self.instance_id,
                    "ok": ok,
                    "output": output,
                    "error": error,
                },
            )
        )
```

- [ ] **Step 4: Run tests; gates; commit**

```bash
uv run pytest tests/test_workers/ -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(workers): WorkerNode — barrier, fencing, execution, safe shutdown"
```

---

### Task 5: Core dispatch layer in bootstrap + router.agent removal

**Files:**
- Modify: `src/proctor/core/bootstrap.py`, `src/proctor/core/config.py` (remove `RouterAgentConfig`/`RouterConfig.agent`, add migration guard)
- Modify: `tests/test_router/test_task_router.py`, `tests/test_router/test_bootstrap_lifecycle.py`, `tests/test_router/test_admission_integration.py` (drop `RouterAgentConfig` usages)
- Test: `tests/test_router/test_dispatch.py` (new)

**Interfaces:**
- Consumes: `WorkerRegistry` (Task 2), TaskRouter API (Task 3), protocol payloads (Task 4 mirrors them).
- Produces: `Application` dispatch layer: `_inflight: dict[str, InflightDispatch]`, `_dispatch_remote`, `_handle_task_result` (pop-if-current), `_handle_worker_lost` (registry callback), `_apply_loss_policy`, deadline reaper in tick loop, `_spawn_ready` local/remote branch. Config: `RouterConfig` without `agent`, rejecting legacy keys.

- [ ] **Step 1: Config removal + migration guard + test updates**

In `config.py`: delete `RouterAgentConfig` and the `agent` field; add to `RouterConfig`:

```python
    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_agent(cls, data: object) -> object:
        if isinstance(data, dict) and "agent" in data:
            raise ValueError(
                "router.agent has been removed; configure worker.max_slots "
                "(top-level worker: section) instead"
            )
        return data
```

Update tests: in `test_task_router.py` remove the `RouterAgentConfig` import and the `test_zero_agent_slots_rejected` case, replacing it with:

```python
    def test_legacy_router_agent_rejected(self) -> None:
        with pytest.raises(ValidationError, match="worker.max_slots"):
            RouterConfig.model_validate({"agent": {"max_slots": 2}})
```

In `test_bootstrap_lifecycle.py` and `test_admission_integration.py`: drop `RouterAgentConfig` from imports and from `_config()` (worker slots now come from `ProctorConfig.worker` — add `worker=WorkerConfig(id="local", max_slots=1)` where the old `agent=RouterAgentConfig(max_slots=1)` was; import `WorkerConfig`).

- [ ] **Step 2: Bootstrap dispatch layer**

In `bootstrap.py` add imports:

```python
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel

from proctor.workers.registry import WorkerRegistry
```

Module-level model (after `LLMCall`):

```python
class InflightDispatch(BaseModel):
    """One remote dispatch attempt awaiting its result."""

    task: Task
    spec: WorkflowSpec
    agent_id: str
    instance_id: str
    dispatch_id: str
    trigger_source: str
```

`__init__` additions: `self._registry: WorkerRegistry | None = None`, `self._inflight: dict[str, InflightDispatch] = {}`, `self._local_worker_id: str = config.worker.id`, and (next to the trigger subscription) `self.bus.subscribe("task.result", self._handle_task_result)`.

`start()`: replace the Task-3 shim with registry wiring:

```python
        local_profile = AgentProfile(
            id=self.config.worker.id,
            capabilities=self.config.worker.capabilities,
            max_slots=self.config.worker.max_slots,
        )
        self._registry = WorkerRegistry(
            self.bus, self.config.registry, local_profile=local_profile
        )
        self._registry.add_loss_listener(self._handle_worker_lost)
        await self._registry.start()
        self._task_router = TaskRouter(
            bus=self.bus,
            config=self.config.router,
            agent_provider=self._registry.alive_profiles,
        )
```

`stop()`: right after the tick-task teardown block:

```python
        if self._registry is not None:
            await self._registry.stop()
            self._registry = None
```

`_handle_trigger_event` admitted branch (replace the final `await self._run_admitted(...)` line):

```python
        assert decision.agent_id is not None
        if decision.agent_id == self._local_worker_id:
            await self._run_admitted(task, spec, event.source)
        else:
            await self._dispatch_remote(
                task, spec, decision.agent_id, event.source
            )
```

New methods:

```python
    async def _dispatch_remote(
        self,
        task: Task,
        spec: WorkflowSpec,
        agent_id: str,
        trigger_source: str,
    ) -> None:
        """Send an admitted task to a remote worker (slot already held)."""
        assert self._registry is not None and self._task_router is not None
        instance_id = self._registry.instance_of(agent_id)
        entry = InflightDispatch(
            task=task,
            spec=spec,
            agent_id=agent_id,
            instance_id=instance_id or "",
            dispatch_id=str(uuid4()),
            trigger_source=trigger_source,
        )
        if instance_id is None:
            # Raced an offline between admit and dispatch.
            await self._apply_loss_policy(entry, f"worker_lost: {agent_id}")
            return
        now = datetime.now(UTC)
        task.status = TaskStatus.ASSIGNED
        task.worker_id = agent_id
        task.deadline = now + timedelta(
            seconds=spec.policies.max_runtime_seconds
        )
        task.updated_at = now
        self._inflight[task.id] = entry
        try:
            await self.state.save_task(task)
        except Exception as exc:
            # The task never left the core — plain failure, slot freed.
            self._inflight.pop(task.id, None)
            logger.exception("Persisting dispatch of task %s failed", task.id)
            await self._finish_failed(task, f"dispatch persist failed: {exc}")
            return
        try:
            await self.bus.publish(
                Event(
                    type=f"task.assign.{agent_id}",
                    source="application",
                    payload={
                        "dispatch_id": entry.dispatch_id,
                        "target_instance_id": instance_id,
                        "task": task.model_dump(mode="json"),
                        "spec": spec.model_dump(mode="json"),
                    },
                )
            )
        except Exception:
            logger.exception("Publishing assignment of task %s failed", task.id)
            popped = self._inflight.pop(task.id, None)
            if popped is not None:
                # Provably never departed — loss policy now, not after
                # max_runtime_seconds.
                await self._apply_loss_policy(
                    popped, "dispatch publish failed"
                )

    async def _handle_task_result(self, event: Event) -> None:
        """Accept a worker result — pop-if-current, then finalize."""
        p = event.payload
        task_id = p.get("task_id")
        if not isinstance(task_id, str):
            return
        # Synchronous critical section: match and remove before any await.
        entry = self._inflight.get(task_id)
        if (
            entry is None
            or entry.dispatch_id != p.get("dispatch_id")
            or entry.instance_id != p.get("instance_id")
        ):
            logger.warning("Ignoring stale/unknown task.result for %s", task_id)
            return
        del self._inflight[task_id]

        task, spec = entry.task, entry.spec
        if p.get("ok"):
            task.status = TaskStatus.COMPLETED
            task.result = {"output": p.get("output")}
        else:
            task.status = TaskStatus.FAILED
            task.result = {"error": p.get("error")}
        task.updated_at = datetime.now(UTC)
        await self.state.save_task(task)

        episode = Episode(
            trigger_type=entry.trigger_source,
            user_input=spec.prompt or "",
            agent_response=p.get("output") or "",
            workflow_result=task.result,
        )
        await self.memory.save_episode(episode)

        await self.bus.publish(
            Event(
                type=(
                    "task.completed"
                    if task.status == TaskStatus.COMPLETED
                    else "task.failed"
                ),
                source="application",
                payload=task.result,
            )
        )
        await self._release_and_spawn(task.id)

    async def _handle_worker_lost(
        self, worker_id: str, instance_id: str
    ) -> None:
        """Registry loss callback — exactly once per lost incarnation."""
        lost = [
            e
            for e in self._inflight.values()
            if e.agent_id == worker_id and e.instance_id == instance_id
        ]
        for entry in lost:
            self._inflight.pop(entry.task.id, None)  # sync, before awaits
        for entry in lost:
            await self._apply_loss_policy(
                entry, f"worker_lost: {worker_id}"
            )

    async def _apply_loss_policy(
        self, entry: InflightDispatch, reason: str
    ) -> None:
        """Retry (opt-in) or fail a dispatch whose worker is gone."""
        assert self._task_router is not None
        task, spec = entry.task, entry.spec
        now = datetime.now(UTC)
        if (
            spec.policies.retry_on_worker_loss
            and task.retries < spec.policies.max_retries
        ):
            task.retries += 1
            task.status = TaskStatus.PENDING
            task.worker_id = None
            task.deadline = None
            task.updated_at = now
            await self.state.save_task(task)
            await self._task_router.retry(
                task,
                spec,
                entry.trigger_source,
                not_before=now
                + timedelta(seconds=spec.policies.retry_delay_seconds),
            )
        else:
            await self._finish_failed(task, reason)
        await self._release_and_spawn(task.id)

    async def _finish_failed(self, task: Task, reason: str) -> None:
        task.status = TaskStatus.FAILED
        task.result = {"error": reason}
        task.updated_at = datetime.now(UTC)
        await self.state.save_task(task)
        await self.bus.publish(
            Event(
                type="task.failed",
                source="application",
                payload=task.result,
            )
        )

    async def _release_and_spawn(self, task_id: str) -> None:
        """Release a slot and spawn whatever became runnable."""
        assert self._task_router is not None
        try:
            ready = await self._task_router.release(task_id)
            self._spawn_ready(ready)
        except TransportDrainingError:
            logger.debug(
                "Skipping post-release dequeue for task %s: draining", task_id
            )
```

Refactor `_run_admitted`'s `finally` to call `await self._release_and_spawn(task.id)` (replacing its inline try/except block — identical semantics, one implementation).

`_run_spawned` gains the local/remote branch:

```python
        try:
            if (
                entry.agent_id is None
                or entry.agent_id == self._local_worker_id
            ):
                await self._run_admitted(
                    entry.task, entry.spec, entry.trigger_source
                )
            else:
                await self._dispatch_remote(
                    entry.task, entry.spec, entry.agent_id,
                    entry.trigger_source,
                )
        except Exception:
            logger.exception("Dequeued task %s crashed", entry.task.id)
```

Tick loop — append the deadline reaper inside the existing `try`:

```python
                now = datetime.now(UTC)
                overdue = [
                    e
                    for e in self._inflight.values()
                    if e.task.deadline is not None and e.task.deadline <= now
                ]
                for entry in overdue:
                    self._inflight.pop(entry.task.id, None)
                for entry in overdue:
                    await self._apply_loss_policy(
                        entry, "dispatch deadline exceeded"
                    )
```

- [ ] **Step 3: Dispatch unit tests**

```python
# tests/test_router/test_dispatch.py
"""Dispatch fencing at the Application level (no real workers)."""

from pathlib import Path

import anyio
import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import (
    ProctorConfig,
    RegistryConfig,
    RouteRule,
    RouterConfig,
    WorkerConfig,
)
from proctor.core.models import Event, TaskStatus
from proctor.core.transport import LocalEventTransport
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path, *, retry: bool = False) -> ProctorConfig:
    return ProctorConfig(
        data_dir=tmp_path / "data",
        router=RouterConfig(max_concurrency=4, queue_tick_seconds=0.05),
        registry=RegistryConfig(
            heartbeat_interval=0.05, liveness_timeout=0.15
        ),
        worker=WorkerConfig(id="local", max_slots=1),
        workflows={
            "remote_job": WorkflowSpec(
                workflow_id="remote_job",
                mode=WorkflowMode.SIMPLE,
                requires=["python"],   # local has no capabilities
            ),
        },
        routes=[
            RouteRule(
                event_pattern="trigger.terminal",
                workflow_id="remote_job",
                prompt_from_payload="text",
            ),
        ],
    )
    # retry variant is built by the test via model mutation below


async def _register_fake_worker(app: Application, iid: str = "i1") -> None:
    await app.bus.publish(
        Event(
            type="worker.registered",
            source="worker:pyw",
            payload={
                "worker_id": "pyw",
                "instance_id": iid,
                "capabilities": ["python"],
                "max_slots": 2,
            },
        )
    )
    await app.bus.flush()


async def _wait_for(collected: list[Event], event_type: str) -> Event:
    with anyio.fail_after(3):
        while True:
            for e in collected:
                if e.type == event_type:
                    return e
            await anyio.sleep(0.01)


async def _mk_app(tmp_path: Path) -> tuple[Application, list[Event]]:
    app = Application(_config(tmp_path), event_transport=LocalEventTransport())
    app.set_llm_call(lambda prompt: _never(prompt))  # type: ignore[arg-type]
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    app.bus.subscribe("task.>", collect)
    app.bus.subscribe("routing.>", collect)
    await app.start()
    return app, collected


async def _never(prompt: str) -> str:
    await anyio.sleep(3600)
    return "unreachable"


async def test_remote_dispatch_and_result(tmp_path: Path) -> None:
    app, collected = await _mk_app(tmp_path)
    try:
        await _register_fake_worker(app)
        assigns: list[Event] = []

        async def on_assign(event: Event) -> None:
            assigns.append(event)

        app.bus.subscribe("task.assign.pyw", on_assign)
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal",
                  payload={"text": "go"})
        )
        with anyio.fail_after(3):
            while not assigns:
                await anyio.sleep(0.01)
        p = assigns[0].payload
        assert p["target_instance_id"] == "i1"
        # deliver a matching result
        await app.bus.publish(
            Event(
                type="task.result",
                source="worker:pyw",
                payload={
                    "task_id": p["task"]["id"],
                    "dispatch_id": p["dispatch_id"],
                    "worker_id": "pyw",
                    "instance_id": "i1",
                    "ok": True,
                    "output": "remote done",
                    "error": None,
                },
            )
        )
        done = await _wait_for(collected, "task.completed")
        assert done.payload == {"output": "remote done"}
        task = await app.state.get_task(p["task"]["id"])
        assert task is not None and task.status == TaskStatus.COMPLETED
    finally:
        await app.stop()


async def test_stale_result_ignored(tmp_path: Path) -> None:
    app, collected = await _mk_app(tmp_path)
    try:
        await _register_fake_worker(app)
        assigns: list[Event] = []

        async def on_assign(event: Event) -> None:
            assigns.append(event)

        app.bus.subscribe("task.assign.pyw", on_assign)
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal",
                  payload={"text": "go"})
        )
        with anyio.fail_after(3):
            while not assigns:
                await anyio.sleep(0.01)
        p = assigns[0].payload
        await app.bus.publish(
            Event(
                type="task.result",
                source="worker:pyw",
                payload={
                    "task_id": p["task"]["id"],
                    "dispatch_id": "wrong_dispatch",
                    "worker_id": "pyw",
                    "instance_id": "i1",
                    "ok": True,
                    "output": "stale",
                    "error": None,
                },
            )
        )
        await app.bus.flush()
        await anyio.sleep(0.05)
        assert not any(e.type == "task.completed" for e in collected)
        task = await app.state.get_task(p["task"]["id"])
        assert task is not None and task.status == TaskStatus.ASSIGNED
    finally:
        await app.stop()


async def test_worker_timeout_fails_task_by_default(tmp_path: Path) -> None:
    app, collected = await _mk_app(tmp_path)
    try:
        await _register_fake_worker(app)
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal",
                  payload={"text": "go"})
        )
        # fake worker never heartbeats again → sweep (0.15s) → loss → fail
        failed = await _wait_for(collected, "task.failed")
        assert "worker_lost" in str(failed.payload)
    finally:
        await app.stop()
```

(`StateManager.get_task` — verify the exact getter name in `src/proctor/core/state.py` before writing the assertions; if it differs (e.g. `load_task`), use that name in both tests.)

- [ ] **Step 4: Run everything; gates; commit**

```bash
uv run pytest tests/test_router/ tests/test_workers/ -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(core): remote dispatch layer — inflight fencing, loss policy, reaper"
```

---

### Task 6: Worker-role bootstrap branch

**Files:**
- Modify: `src/proctor/core/bootstrap.py` (role branch), `docs/superpowers/specs/2026-07-06-worker-registry-dispatch-design.md` (one amendment)
- Test: `tests/test_workers/test_worker_role.py` (new)

**Interfaces:**
- Consumes: `WorkerNode` (Task 4).
- Produces: `Application` with `node_role: worker` runs bus + episodic memory (LLM telemetry only) + engine + WorkerNode; NO triggers, NO Router/TaskRouter/registry/tick loop, NO state.db, and — critically — NO `trigger.>` / `task.result` subscriptions (on a shared NATS bus a worker must not react to the core's traffic).

- [ ] **Step 1: Failing test**

```python
# tests/test_workers/test_worker_role.py
"""node_role: worker wires a WorkerNode and nothing core-side."""

from pathlib import Path

import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import ProctorConfig, WorkerConfig
from proctor.core.transport import LocalEventTransport

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _worker_config(tmp_path: Path) -> ProctorConfig:
    return ProctorConfig(
        node_role="worker",
        transport="local",  # keep the unit test off real NATS
        data_dir=tmp_path / "data",
        worker=WorkerConfig(id="worker_a", capabilities=["python"]),
    )


async def test_worker_role_starts_node_not_core(tmp_path: Path) -> None:
    app = Application(
        _worker_config(tmp_path), event_transport=LocalEventTransport()
    )

    async def llm(prompt: str) -> str:
        return "ok"

    app.set_llm_call(llm)
    await app.start()
    try:
        assert app._worker_node is not None
        assert app._router is None
        assert app._task_router is None
        assert app._registry is None
        assert app._tick_task is None
        # worker must not listen to core-side subjects
        subjects = {
            sub.subject for sub in app.bus._transport._subscriptions  # type: ignore[attr-defined]
        }
        assert "trigger.>" not in subjects
        assert "task.result" not in subjects
        assert any(s.startswith("task.assign.worker_a") for s in subjects)
    finally:
        await app.stop()
```

(The `_subscriptions` peek mirrors the existing pattern in `tests/test_core/test_bootstrap.py:746`. `transport="local"` for `node_role: worker` logs a warning but is valid — the test injects a LocalEventTransport anyway.)

- [ ] **Step 2: Implement role branch**

`__init__`: add `self._worker_node: WorkerNode | None = None` (import `WorkerNode`), and make the core-side subscriptions conditional:

```python
        if config.node_role != "worker":
            self.bus.subscribe("trigger.>", self._handle_trigger_event)
            self.bus.subscribe("task.result", self._handle_task_result)
```

`start()`: first line of the body after `data_dir.mkdir`:

```python
        if self.config.node_role == "worker":
            await self._start_worker_role()
            return
```

New methods:

```python
    async def _start_worker_role(self) -> None:
        """Worker node: bus + LLM telemetry memory + WorkerNode only.

        state.db and interaction episodes stay core-owned; the local
        episodes.db here records LLM-call telemetry from build_llm_call.
        """
        assert self._engine is not None, "set_llm_call() before start()"
        await self.bus.start()
        await self.memory.initialize()
        self._worker_node = WorkerNode(
            self.bus,
            self.config.worker,
            self._engine,
            heartbeat_interval=self.config.registry.heartbeat_interval,
            drain_timeout=self.config.events.drain_timeout,
        )
        await self._worker_node.start()
        self.is_running = True
        logger.info(
            "Application started in worker role (id=%s)", self.config.worker.id
        )

    async def _stop_worker_role(self) -> None:
        self.is_running = False
        if self._worker_node is not None:
            await self._worker_node.stop()
            self._worker_node = None
        await self.bus.drain(timeout=self.config.events.drain_timeout)
        await self.memory.close()
        await self.bus.stop()
        logger.info("Application stopped (worker role)")
```

`stop()` first line: `if self.config.node_role == "worker": await self._stop_worker_role(); return`.

- [ ] **Step 3: Amend the spec (one line)**

In the spec's Scope bullet about role-dependent bootstrap, change "(no triggers, no Router/TaskRouter, no SQLite)" to "(no triggers, no Router/TaskRouter, no state.db; a worker-local `episodes.db` exists solely for LLM-call telemetry recorded inside `build_llm_call` — interaction episodes stay core-owned)".

- [ ] **Step 4: Run; gates; commit**

```bash
uv run pytest tests/test_workers/ -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(core): worker-role bootstrap — WorkerNode wiring, no core subscriptions"
```

---

### Task 7: End-to-end integration over LocalEventTransport

**Files:**
- Test: `tests/test_workers/test_distribution_integration.py` (new)

**Interfaces:** consumes everything; no new production code. If a test exposes a bug, fix it in the owning module within this task.

- [ ] **Step 1: Write the tests** (core `Application` + real `WorkerNode` sharing the core's bus — same-process integration without NATS)

```python
# tests/test_workers/test_distribution_integration.py
"""Full distribution loop on one in-process bus (no NATS)."""

from pathlib import Path

import anyio
import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import (
    ProctorConfig,
    RegistryConfig,
    RouteRule,
    RouterConfig,
    WorkerConfig,
)
from proctor.core.models import Event, TaskStatus
from proctor.core.transport import LocalEventTransport
from proctor.workers.node import WorkerNode
from proctor.workflow.spec import (
    WorkflowMode,
    WorkflowPolicies,
    WorkflowSpec,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _EchoEngine:
    async def execute(self, spec: WorkflowSpec) -> object:
        class R:
            output = f"worker ran {spec.workflow_id}"
            error = None

        return R()


def _config(tmp_path: Path, *, retry: bool = False) -> ProctorConfig:
    policies = WorkflowPolicies(
        retry_on_worker_loss=retry,
        retry_delay_seconds=0,
        max_runtime_seconds=900,
    )
    return ProctorConfig(
        data_dir=tmp_path / "data",
        router=RouterConfig(max_concurrency=4, queue_tick_seconds=0.05),
        registry=RegistryConfig(
            heartbeat_interval=0.05, liveness_timeout=0.15
        ),
        worker=WorkerConfig(id="local", max_slots=1),
        workflows={
            "job": WorkflowSpec(
                workflow_id="job",
                mode=WorkflowMode.SIMPLE,
                requires=["python"],
                policies=policies,
            ),
        },
        routes=[
            RouteRule(
                event_pattern="trigger.terminal",
                workflow_id="job",
                prompt_from_payload="text",
            ),
        ],
    )


async def _wait_for(collected: list[Event], event_type: str) -> Event:
    with anyio.fail_after(3):
        while True:
            for e in collected:
                if e.type == event_type:
                    return e
            await anyio.sleep(0.01)


async def test_full_loop_remote_execution(tmp_path: Path) -> None:
    app = Application(_config(tmp_path), event_transport=LocalEventTransport())

    async def llm(prompt: str) -> str:
        return "local ok"

    app.set_llm_call(llm)
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    app.bus.subscribe("task.>", collect)
    await app.start()
    worker = WorkerNode(
        app.bus,
        WorkerConfig(id="pyw", capabilities=["python"], max_slots=2),
        _EchoEngine(),
        heartbeat_interval=0.05,
        drain_timeout=0.5,
    )
    await worker.start()
    try:
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal",
                  payload={"text": "go"})
        )
        done = await _wait_for(collected, "task.completed")
        assert done.payload == {"output": "worker ran job"}
    finally:
        await worker.stop()
        await app.stop()


async def test_worker_loss_retry_redispatches(tmp_path: Path) -> None:
    """retry_on_worker_loss: silent worker dies, real worker completes."""
    app = Application(
        _config(tmp_path, retry=True), event_transport=LocalEventTransport()
    )

    async def llm(prompt: str) -> str:
        return "local ok"

    app.set_llm_call(llm)
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    app.bus.subscribe("task.>", collect)
    app.bus.subscribe("routing.>", collect)
    await app.start()
    try:
        # a fake worker that registers, accepts, then goes silent
        await app.bus.publish(
            Event(
                type="worker.registered",
                source="worker:ghost",
                payload={
                    "worker_id": "ghost",
                    "instance_id": "g1",
                    "capabilities": ["python"],
                    "max_slots": 9,  # scoring prefers it over the real one
                },
            )
        )
        await app.bus.flush()
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal",
                  payload={"text": "go"})
        )
        # ghost never results, never heartbeats → sweep → retry path
        await _wait_for(collected, "routing.queued")
        # bring up a real worker to take the retry
        worker = WorkerNode(
            app.bus,
            WorkerConfig(id="pyw", capabilities=["python"], max_slots=2),
            _EchoEngine(),
            heartbeat_interval=0.05,
            drain_timeout=0.5,
        )
        await worker.start()
        try:
            done = await _wait_for(collected, "task.completed")
            assert done.payload == {"output": "worker ran job"}
        finally:
            await worker.stop()
    finally:
        await app.stop()
```

- [ ] **Step 2: Run, stabilize (5 consecutive runs), full suite, gates, commit**

```bash
for i in 1 2 3 4 5; do uv run pytest tests/test_workers/test_distribution_integration.py -q || break; done
uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "test(workers): end-to-end distribution loop over local transport"
```

---

### Task 8: NATS multi-node test + docs + PR

**Files:**
- Test: `tests/integration/test_distribution_nats.py` (new, `-m nats`)
- Modify: `CLAUDE.md`, `TODO.md`, `config/proctor.yaml`

- [ ] **Step 1: NATS test** — mirror `tests/integration/test_transport_contract.py` conventions (skip logic, `NATS_URL` env, `pytestmark = pytest.mark.nats`; read that file first and reuse its fixture style). Shape: core `Application` on one `NATSEventTransport` + worker `Application` (`node_role="worker"`, explicit `worker.id="pyw"`, `transport="nats"`) on a second transport, same server; mock LLM on both; publish `trigger.terminal` on the core bus; assert `task.completed` with the worker's output arrives on the core bus; then stop worker first, core second. One test is enough — the local-transport suite covers semantics; this proves the wire.

- [ ] **Step 2: Docs**
  - `CLAUDE.md` module table: `workers/` row becomes "Agent Runtime + WorkerRegistry (discovery/liveness) + WorkerNode (worker-role runtime). Future: docker.py, remote.py"; Implementation Status: add registry/dispatch to Completed, set **Next:** "Phase 3 continues — workers/docker.py, workers/remote.py, mcp/".
  - `TODO.md`: current state line mentions distribution loop done.
  - `config/proctor.yaml`: commented `worker:`/`registry:` example blocks mirroring the spec's Config section (with the `router.agent` line removed from any existing example).

- [ ] **Step 3: Final gates, push, PR**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git push -u origin feat/worker-registry
gh pr create --base master --title "feat(workers): worker registry + remote dispatch (Phase 3, part 1)" --body "..."
```

PR body: reference the spec, the three review rounds, protocol summary (fencing: dispatch_id + first-alive-owns instance ownership), at-most-once-per-dispatch caveat, test evidence (unit + integration + `-m nats`).

---

## Self-Review Notes

- Spec coverage: protocol+registry (T2), scoring/agent_provider/retry/not_before (T3), node incl. barrier+shutdown order (T4), dispatch layer incl. rollback, pop-if-current, loss policy, reaper (T5), role branch + no-core-subscriptions (T6), integration both transports (T7-T8), config+migration (T1, T5), spec amendment for worker-local episodes.db (T6).
- Type consistency: `_try_reserve` tuple return used by admit/dequeue (T3) and relied on in T5; `QueueEntry.agent_id` written by dequeue (T3), read by `_run_spawned` (T5); `InflightDispatch.instance_id` matched against result payload (T4 producers echo `dispatch_id`/`instance_id`).
- Known judgment calls: Task 3 patches bootstrap's constructor call in the same commit to keep the suite green; `StateManager` getter name must be verified in T5 Step 3; the T7 retry test gives the ghost worker more slots so scoring deterministically picks it first.
