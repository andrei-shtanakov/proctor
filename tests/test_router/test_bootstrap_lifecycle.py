# tests/test_router/test_bootstrap_lifecycle.py
"""Regression tests for Application lifecycle hardening (final review F1-F3).

Covers:
- F1: the queue tick loop survives an exception in its body, and stop()
  tears down cleanly even if the tick task already died.
- F2: a crash in a dequeued/spawned execution is logged, not left as an
  unretrieved task exception.
- F3: stop() completes cleanly with a running + a queued task in flight,
  and no new execution is spawned once teardown has started.
"""

import asyncio
import contextlib
import logging
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
from proctor.core.models import Event, TaskStatus
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


async def _ok_llm(prompt: str) -> str:
    return "ok"


# --- F1: tick loop resilience -----------------------------------------


async def test_tick_loop_survives_transient_error_and_keeps_expiring(
    tmp_path: Path,
) -> None:
    """One bad tick (e.g. a transient router error) must not kill expiry."""
    gate = anyio.Event()

    async def llm(prompt: str) -> str:
        if prompt == "one":
            await gate.wait()
        return "ok"

    config = _config(tmp_path, queue_ttl_seconds=0.2, queue_tick_seconds=0.05)
    app = Application(config, event_transport=LocalEventTransport())
    app.set_llm_call(llm)
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    app.bus.subscribe("routing.>", collect)
    app.bus.subscribe("task.>", collect)

    await app.start()
    assert app._task_router is not None
    orig_expire = app._task_router.expire_overdue
    calls = {"n": 0}

    async def flaky_expire(now: object = None):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient router failure")
        return await orig_expire(now)  # type: ignore[arg-type]

    app._task_router.expire_overdue = flaky_expire  # type: ignore[method-assign]

    try:
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "one"})
        )
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal", payload={"text": "two"})
        )
        await _wait_for(collected, "routing.queued")
        # Despite the first tick raising, expiry must still eventually fire.
        await _wait_for(collected, "routing.expired")
        await _wait_for(collected, "task.failed")
    finally:
        gate.set()
        await app.stop()

    assert calls["n"] > 1  # loop kept ticking past the injected failure
    failed = [e for e in collected if e.type == "task.failed"]
    assert any("TTL expired" in str(e.payload) for e in failed)


async def test_stop_completes_when_tick_task_already_crashed(
    tmp_path: Path,
) -> None:
    """stop() must tear down cleanly even if the tick task died earlier
    from an exception other than CancelledError (simulated directly,
    since the F1 in-loop guard means ordinary exceptions no longer kill
    the loop on their own)."""
    app = Application(_config(tmp_path), event_transport=LocalEventTransport())
    app.set_llm_call(_ok_llm)
    await app.start()

    async def _boom() -> None:
        raise RuntimeError("tick task died earlier")

    assert app._tick_task is not None
    app._tick_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await app._tick_task
    app._tick_task = asyncio.create_task(_boom())
    await asyncio.sleep(0)  # let it raise and finish before stop() cancels it
    assert app._tick_task.done()

    with anyio.fail_after(3):
        await app.stop()  # must not raise


# --- F2: spawned-execution guard ----------------------------------------


async def test_dequeued_crash_is_logged_not_unretrieved(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gate = anyio.Event()

    async def llm(prompt: str) -> str:
        if prompt == "one":
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
    orig_save_task = app.state.save_task
    target = {"id": None}

    async def flaky_save_task(task: object) -> None:  # type: ignore[no-untyped-def]
        if task.id == target["id"] and task.status == TaskStatus.RUNNING:  # type: ignore[attr-defined]
            target["id"] = None  # only once
            raise RuntimeError("boom: surgical save failure on spawn")
        await orig_save_task(task)  # type: ignore[arg-type]

    app.state.save_task = flaky_save_task  # type: ignore[method-assign]

    try:
        with caplog.at_level(logging.ERROR, logger="proctor.core.bootstrap"):
            await app.bus.publish(
                Event(
                    type="trigger.terminal", source="terminal", payload={"text": "one"}
                )
            )
            await app.bus.publish(
                Event(
                    type="trigger.terminal", source="terminal", payload={"text": "two"}
                )
            )
            await _wait_for(collected, "routing.queued")
            queued = next(e for e in collected if e.type == "routing.queued")
            target["id"] = queued.payload["task_id"]

            gate.set()
            with anyio.fail_after(3):
                while not any("crashed" in r.getMessage() for r in caplog.records):
                    await anyio.sleep(0.01)
    finally:
        gate.set()
        await app.stop()

    assert any(
        "Dequeued task" in r.getMessage() and "crashed" in r.getMessage()
        for r in caplog.records
    )
    assert not any("never retrieved" in r.getMessage() for r in caplog.records)


# --- F3: drain seam -------------------------------------------------------


async def test_stop_with_running_and_queued_task_completes_cleanly(
    tmp_path: Path,
) -> None:
    gate = anyio.Event()
    calls: list[str] = []

    async def llm(prompt: str) -> str:
        calls.append(prompt)
        if prompt == "one":
            await gate.wait()
        return "ok"

    config = _config(tmp_path, queue_ttl_seconds=30.0)
    app = Application(config, event_transport=LocalEventTransport())
    app.set_llm_call(llm)
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    app.bus.subscribe("routing.>", collect)

    await app.start()
    await app.bus.publish(
        Event(type="trigger.terminal", source="terminal", payload={"text": "one"})
    )
    await app.bus.publish(
        Event(type="trigger.terminal", source="terminal", payload={"text": "two"})
    )
    await _wait_for(collected, "routing.queued")

    stop_task = asyncio.create_task(app.stop())
    await anyio.sleep(0.05)
    assert not stop_task.done()  # blocked draining the still-running handler

    gate.set()
    with anyio.fail_after(3):
        await stop_task  # must not raise

    # The queued task must never have been spawned during teardown.
    assert calls == ["one"]
    assert app._exec_tasks == set()
