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


def _assign(node: WorkerNode, task_id: str = "t1", dispatch_id: str = "d1") -> Event:
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


async def test_invalid_spec_reports_error_result(bus: EventBus) -> None:
    """F4 regression: a malformed spec in an assignment must produce an
    immediate error task.result, never reach the engine."""
    results = await _collect(bus, "task.result")
    engine = _StubEngine()
    node = _node(bus, engine)
    await node.start()
    try:
        event = _assign(node, task_id="t1", dispatch_id="d1")
        event.payload["spec"] = {"not": "a valid workflow spec"}
        await bus.publish(event)
        with anyio.fail_after(2):
            while not results:
                await anyio.sleep(0.01)
        p = results[0].payload
        assert p["task_id"] == "t1"
        assert p["dispatch_id"] == "d1"
        assert p["ok"] is False
        assert p["error"] is not None and "invalid spec" in p["error"]
        assert engine.calls == []
    finally:
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
        e.type for e in worker_events[worker_events.index(worker_events[-1]) + 1 :]
    ]
    assert types_after == []
