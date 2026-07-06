# tests/test_router/test_dispatch.py
"""Dispatch fencing at the Application level (no real workers)."""

from pathlib import Path

import anyio
import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import (
    ProctorConfig,
    RegistryConfig,
    RouterConfig,
    RouteRule,
    WorkerConfig,
)
from proctor.core.models import Event, TaskStatus
from proctor.core.transport import LocalEventTransport
from proctor.workflow.spec import WorkflowMode, WorkflowPolicies, WorkflowSpec

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(
    tmp_path: Path,
    *,
    retry: bool = False,
    max_runtime_seconds: int = 900,
    retry_delay_seconds: int = 0,
) -> ProctorConfig:
    policies = (
        WorkflowPolicies(
            retry_on_worker_loss=True,
            retry_delay_seconds=retry_delay_seconds,
            max_runtime_seconds=max_runtime_seconds,
        )
        if retry
        else WorkflowPolicies(max_runtime_seconds=max_runtime_seconds)
    )
    return ProctorConfig(
        data_dir=tmp_path / "data",
        router=RouterConfig(max_concurrency=4, queue_tick_seconds=0.05),
        registry=RegistryConfig(heartbeat_interval=0.05, liveness_timeout=0.15),
        worker=WorkerConfig(id="local", max_slots=1),
        workflows={
            "remote_job": WorkflowSpec(
                workflow_id="remote_job",
                mode=WorkflowMode.SIMPLE,
                requires=["python"],  # local has no capabilities
                policies=policies,
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


async def _mk_app(
    tmp_path: Path,
    *,
    retry: bool = False,
    max_runtime_seconds: int = 900,
    retry_delay_seconds: int = 0,
) -> tuple[Application, list[Event]]:
    app = Application(
        _config(
            tmp_path,
            retry=retry,
            max_runtime_seconds=max_runtime_seconds,
            retry_delay_seconds=retry_delay_seconds,
        ),
        event_transport=LocalEventTransport(),
    )
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
            Event(type="trigger.terminal", source="terminal", payload={"text": "go"})
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
            Event(type="trigger.terminal", source="terminal", payload={"text": "go"})
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
            Event(type="trigger.terminal", source="terminal", payload={"text": "go"})
        )
        # fake worker never heartbeats again → sweep (0.15s) → loss → fail
        failed = await _wait_for(collected, "task.failed")
        assert "worker_lost" in str(failed.payload)
    finally:
        await app.stop()


async def test_dispatch_save_failure_releases_slot(tmp_path: Path) -> None:
    """F1 regression: a save_task failure during dispatch must free the
    admit-time slot, not just drop the inflight bookkeeping entry."""
    app, collected = await _mk_app(tmp_path)
    try:
        await _register_fake_worker(app)
        orig_save_task = app.state.save_task
        armed = {"value": True}

        async def flaky_save_task(task: object) -> None:  # type: ignore[no-untyped-def]
            if armed["value"] and task.status == TaskStatus.ASSIGNED:  # type: ignore[attr-defined]
                armed["value"] = False
                raise RuntimeError("boom: surgical save failure on dispatch")
            await orig_save_task(task)  # type: ignore[arg-type]

        app.state.save_task = flaky_save_task  # type: ignore[method-assign]

        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "go"})
        )
        failed = await _wait_for(collected, "task.failed")
        assert "dispatch persist failed" in str(failed.payload)

        assert app._task_router is not None
        with anyio.fail_after(3):
            while app._task_router.running_count != 0:
                await anyio.sleep(0.01)
    finally:
        await app.stop()


async def test_retry_on_worker_loss_requeues_task(tmp_path: Path) -> None:
    """F2 regression: retry_on_worker_loss must requeue (routing.queued
    with retry=True), not fail the task, and clear worker assignment."""
    app, collected = await _mk_app(tmp_path, retry=True)
    try:
        await _register_fake_worker(app)
        assigns: list[Event] = []

        async def on_assign(event: Event) -> None:
            assigns.append(event)

        app.bus.subscribe("task.assign.pyw", on_assign)
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "go"})
        )
        with anyio.fail_after(3):
            while not assigns:
                await anyio.sleep(0.01)
        task_id = assigns[0].payload["task"]["id"]

        # fake worker never heartbeats again → sweep (0.15s) → loss →
        # retry_on_worker_loss requeues instead of failing.
        queued = await _wait_for(collected, "routing.queued")
        assert queued.payload["retry"] is True
        assert not any(e.type == "task.failed" for e in collected)

        task = await app.state.get_task(task_id)
        assert task is not None
        assert task.status == TaskStatus.PENDING
        assert task.worker_id is None
    finally:
        await app.stop()


async def test_worker_busy_result_routes_through_loss_policy(tmp_path: Path) -> None:
    """F1 regression: a `worker_busy` result is transient over-capacity,
    not terminal — with retry_on_worker_loss it must requeue the task
    (routing.queued, retry=True), never task.failed."""
    # retry_delay_seconds > 0 so the queued entry's `not_before` is in
    # the future — otherwise the still-alive fake worker is the only
    # capable candidate and gets immediately re-dispatched by the same
    # release()->dequeue_ready() call, racing the PENDING assertion
    # below (a real second dispatch, not a bug).
    app, collected = await _mk_app(tmp_path, retry=True, retry_delay_seconds=5)
    try:
        await _register_fake_worker(app)
        assigns: list[Event] = []

        async def on_assign(event: Event) -> None:
            assigns.append(event)

        app.bus.subscribe("task.assign.pyw", on_assign)
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "go"})
        )
        with anyio.fail_after(3):
            while not assigns:
                await anyio.sleep(0.01)
        p = assigns[0].payload
        task_id = p["task"]["id"]

        await app.bus.publish(
            Event(
                type="task.result",
                source="worker:pyw",
                payload={
                    "task_id": task_id,
                    "dispatch_id": p["dispatch_id"],
                    "worker_id": "pyw",
                    "instance_id": "i1",
                    "ok": False,
                    "output": None,
                    "error": "worker_busy",
                },
            )
        )
        queued = await _wait_for(collected, "routing.queued")
        assert queued.payload["retry"] is True
        assert not any(e.type == "task.failed" for e in collected)

        task = await app.state.get_task(task_id)
        assert task is not None
        assert task.status == TaskStatus.PENDING
    finally:
        await app.stop()


async def test_deadline_reaper_fails_and_ignores_late_result(tmp_path: Path) -> None:
    """F2 regression: a zero-runtime deadline is reaped by the tick loop
    ("dispatch deadline exceeded"); a subsequent matching result must be
    ignored by pop-if-current (the entry is already gone)."""
    app, collected = await _mk_app(tmp_path, max_runtime_seconds=0)
    try:
        await _register_fake_worker(app)
        assigns: list[Event] = []

        async def on_assign(event: Event) -> None:
            assigns.append(event)

        app.bus.subscribe("task.assign.pyw", on_assign)
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "go"})
        )
        with anyio.fail_after(3):
            while not assigns:
                await anyio.sleep(0.01)
        p = assigns[0].payload
        task_id = p["task"]["id"]

        # never send a result — deadline is now, reaped at the next tick
        failed = await _wait_for(collected, "task.failed")
        assert "dispatch deadline exceeded" in str(failed.payload)
        task = await app.state.get_task(task_id)
        assert task is not None and task.status == TaskStatus.FAILED

        # a late result matching the reaped dispatch must be ignored
        await app.bus.publish(
            Event(
                type="task.result",
                source="worker:pyw",
                payload={
                    "task_id": task_id,
                    "dispatch_id": p["dispatch_id"],
                    "worker_id": "pyw",
                    "instance_id": "i1",
                    "ok": True,
                    "output": "too late",
                    "error": None,
                },
            )
        )
        await app.bus.flush()
        await anyio.sleep(0.05)
        assert not any(e.type == "task.completed" for e in collected)
        task = await app.state.get_task(task_id)
        assert task is not None and task.status == TaskStatus.FAILED
    finally:
        await app.stop()
