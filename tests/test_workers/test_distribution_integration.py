"""Full distribution loop on one in-process bus (no NATS)."""

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
from proctor.core.models import Event
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
        registry=RegistryConfig(heartbeat_interval=0.05, liveness_timeout=0.15),
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
            Event(type="trigger.terminal", source="terminal", payload={"text": "go"})
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
            Event(type="trigger.terminal", source="terminal", payload={"text": "go"})
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
