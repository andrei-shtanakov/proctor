"""Distribution loop across two real NATSEventTransport connections.

The local-transport suite (tests/test_workers/test_distribution_integration.py)
already covers dispatch semantics exhaustively. This test proves the wire:
a core Application and a worker Application, each on its own
NATSEventTransport against the same server, exchange task.assign /
task.result over real NATS subjects. One test is enough.

Gated by the `nats` marker and a running NATS server — mirrors
tests/integration/test_transport_contract.py conventions (NATS_URL env
or testcontainers via the session-scoped `nats_url` fixture).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import anyio
import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import (
    EventsConfig,
    NATSConfig,
    ProctorConfig,
    RegistryConfig,
    RouterConfig,
    RouteRule,
    WorkerConfig,
)
from proctor.core.models import Event
from proctor.workflow.spec import WorkflowMode, WorkflowPolicies, WorkflowSpec

pytestmark = [pytest.mark.anyio, pytest.mark.nats]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _core_config(tmp_path: Path, nats_url: str, prefix: str) -> ProctorConfig:
    policies = WorkflowPolicies(max_runtime_seconds=900)
    return ProctorConfig(
        data_dir=tmp_path / "core",
        transport="nats",
        nats=NATSConfig(servers=[nats_url], subject_prefix=prefix, name="core"),
        events=EventsConfig(max_payload=65_536),
        router=RouterConfig(max_concurrency=4, queue_tick_seconds=0.05),
        registry=RegistryConfig(heartbeat_interval=0.05, liveness_timeout=0.3),
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


def _worker_config(tmp_path: Path, nats_url: str, prefix: str) -> ProctorConfig:
    return ProctorConfig(
        node_role="worker",
        data_dir=tmp_path / "worker",
        transport="nats",
        nats=NATSConfig(servers=[nats_url], subject_prefix=prefix, name="worker"),
        events=EventsConfig(max_payload=65_536),
        worker=WorkerConfig(id="pyw", capabilities=["python"], max_slots=2),
        registry=RegistryConfig(heartbeat_interval=0.05, liveness_timeout=0.3),
    )


async def _wait_for(collected: list[Event], event_type: str) -> Event:
    with anyio.fail_after(5):
        while True:
            for e in collected:
                if e.type == event_type:
                    return e
            await anyio.sleep(0.02)


async def test_distribution_over_nats(tmp_path: Path, nats_url: str) -> None:
    """Core dispatches a `requires: [python]` task to a remote NATS worker."""
    prefix = f"proctor_test_{uuid4().hex[:8]}"

    core = Application(_core_config(tmp_path, nats_url, prefix))

    async def core_llm(prompt: str) -> str:
        return "core should never run this workflow"

    core.set_llm_call(core_llm)

    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    core.bus.subscribe("task.>", collect)
    await core.start()

    worker = Application(_worker_config(tmp_path, nats_url, prefix))

    async def worker_llm(prompt: str) -> str:
        return "worker ran over nats"

    worker.set_llm_call(worker_llm)
    await worker.start()
    try:
        await core.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "go"})
        )
        done = await _wait_for(collected, "task.completed")
        assert done.payload == {"output": "worker ran over nats"}
    finally:
        await worker.stop()
        await core.stop()
