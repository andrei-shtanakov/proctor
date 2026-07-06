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
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path, *, retry: bool = False) -> ProctorConfig:
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
