# tests/test_router/test_admission_integration.py
"""Integration: admission gating through a real Application."""

from pathlib import Path

import anyio
import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import (
    ProctorConfig,
    RouterAgentConfig,
    RouterConfig,
    RouteRule,
)
from proctor.core.models import Event
from proctor.core.transport import LocalEventTransport
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """aiosqlite only supports asyncio."""
    return "asyncio"


def _config(tmp_path: Path, **router_overrides: object) -> ProctorConfig:
    router_config = RouterConfig(
        max_concurrency=1,
        queue_ttl_seconds=5.0,
        queue_tick_seconds=0.05,
        agent=RouterAgentConfig(max_slots=1),
    ).model_copy(update=router_overrides)
    return ProctorConfig(
        data_dir=tmp_path / "proctor_data",
        router=router_config,
        workflows={
            "chat": WorkflowSpec(workflow_id="chat", mode=WorkflowMode.SIMPLE),
        },
        routes=[
            RouteRule(
                event_pattern="trigger.terminal",
                workflow_id="chat",
                prompt_from_payload="text",
            ),
        ],
    )


async def _wait_for(collected: list[Event], event_type: str) -> None:
    with anyio.fail_after(3):
        while not any(e.type == event_type for e in collected):
            await anyio.sleep(0.01)


async def test_second_task_queues_then_runs_after_release(
    tmp_path: Path,
) -> None:
    gate = anyio.Event()
    calls: list[str] = []

    async def llm(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            await gate.wait()
        return "ok"

    app = Application(_config(tmp_path), event_transport=LocalEventTransport())
    app.set_llm_call(llm)
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    app.bus.subscribe("routing.>", collect)
    app.bus.subscribe("task.>", collect)

    await app.start()
    try:
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "one"})
        )
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "two"})
        )
        await _wait_for(collected, "routing.queued")
        gate.set()
        await _wait_for(collected, "routing.dequeued")
        with anyio.fail_after(3):
            while sum(e.type == "task.completed" for e in collected) < 2:
                await anyio.sleep(0.01)
    finally:
        gate.set()
        await app.stop()

    assert len(calls) == 2


async def test_queue_ttl_expiry_fails_task(tmp_path: Path) -> None:
    gate = anyio.Event()

    async def llm(prompt: str) -> str:
        await gate.wait()
        return "ok"

    config = _config(tmp_path, queue_ttl_seconds=0.2)
    app = Application(config, event_transport=LocalEventTransport())
    app.set_llm_call(llm)
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    app.bus.subscribe("routing.>", collect)
    app.bus.subscribe("task.>", collect)

    await app.start()
    try:
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "one"})
        )
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "two"})
        )
        await _wait_for(collected, "routing.queued")
        await _wait_for(collected, "routing.expired")
        await _wait_for(collected, "task.failed")
    finally:
        gate.set()
        await app.stop()

    failed = [e for e in collected if e.type == "task.failed"]
    assert any("TTL expired" in str(e.payload) for e in failed)
