"""Integration tests: terminal event -> workflow -> task persistence."""

import asyncio
from pathlib import Path

import pytest

from proctor.core.bootstrap import Application
from proctor.core.bus import EventBus
from proctor.core.config import ProctorConfig, ScheduleItemConfig
from proctor.core.models import Event, TaskStatus
from proctor.triggers.scheduler import SchedulerTrigger

# aiosqlite is asyncio-only
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """aiosqlite only supports asyncio."""
    return "asyncio"


@pytest.fixture
def tmp_config(tmp_path: Path) -> ProctorConfig:
    """Config with a temporary data directory."""
    return ProctorConfig(data_dir=tmp_path / "proctor_data")


class TestTerminalToResult:
    """End-to-end: terminal command -> workflow -> task.completed event."""

    @pytest.mark.anyio
    async def test_terminal_creates_workflow_and_emits_result(
        self, tmp_config: ProctorConfig
    ) -> None:
        """GIVEN a started Application with a configured LLM
        WHEN a trigger.terminal event is published
        THEN a workflow executes and task.completed is emitted.
        """
        app = Application(tmp_config)

        async def mock_llm(prompt: str) -> str:
            return f"LLM says: {prompt}"

        app.set_llm_call(mock_llm)
        await app.start()
        try:
            results: list[Event] = []

            async def capture(e: Event) -> None:
                results.append(e)

            app.bus.subscribe("task.*", capture)

            await app.bus.publish(
                Event(
                    type="trigger.terminal",
                    source="terminal",
                    payload={"text": "Tell me about RISC-V"},
                )
            )

            assert len(results) == 1
            assert results[0].type == "task.completed"
            assert results[0].payload["output"] == "LLM says: Tell me about RISC-V"
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_task_persisted_in_sqlite(self, tmp_config: ProctorConfig) -> None:
        """GIVEN a started Application with a configured LLM
        WHEN a terminal command is processed
        THEN the task is persisted in SQLite with COMPLETED status.
        """
        app = Application(tmp_config)

        async def mock_llm(prompt: str) -> str:
            return f"response: {prompt}"

        app.set_llm_call(mock_llm)
        await app.start()
        try:
            await app.bus.publish(
                Event(
                    type="trigger.terminal",
                    source="terminal",
                    payload={"text": "hello"},
                )
            )

            tasks = await app.state.list_tasks(status=TaskStatus.COMPLETED)
            assert len(tasks) == 1
            task = tasks[0]
            assert task.status == TaskStatus.COMPLETED
            assert task.spec == {"prompt": "hello"}
            assert task.result == {"output": "response: hello"}
            assert task.trigger_event is not None
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_task_status_transitions_tracked(
        self, tmp_config: ProctorConfig
    ) -> None:
        """Tasks go through PENDING -> RUNNING -> COMPLETED."""
        app = Application(tmp_config)
        statuses: list[TaskStatus] = []

        # Patch save_task to track status transitions
        original_save = app.state.save_task

        async def tracking_save(task: object) -> None:
            from proctor.core.models import Task

            assert isinstance(task, Task)
            statuses.append(task.status)
            await original_save(task)

        app.state.save_task = tracking_save  # type: ignore[assignment]

        async def mock_llm(prompt: str) -> str:
            return "done"

        app.set_llm_call(mock_llm)
        await app.start()
        try:
            await app.bus.publish(
                Event(
                    type="trigger.terminal",
                    source="terminal",
                    payload={"text": "test"},
                )
            )

            assert statuses == [
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
                TaskStatus.COMPLETED,
            ]
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_failed_workflow_persists_failed_task(
        self, tmp_config: ProctorConfig
    ) -> None:
        """LLM errors result in FAILED task in SQLite."""
        app = Application(tmp_config)

        async def bad_llm(prompt: str) -> str:
            raise RuntimeError("LLM unavailable")

        app.set_llm_call(bad_llm)
        await app.start()
        try:
            results: list[Event] = []

            async def capture(e: Event) -> None:
                results.append(e)

            app.bus.subscribe("task.*", capture)

            await app.bus.publish(
                Event(
                    type="trigger.terminal",
                    source="terminal",
                    payload={"text": "test"},
                )
            )

            # Event emitted
            assert len(results) == 1
            assert results[0].type == "task.failed"

            # Task persisted as FAILED
            tasks = await app.state.list_tasks(status=TaskStatus.FAILED)
            assert len(tasks) == 1
            assert "LLM unavailable" in str(tasks[0].result)
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_workflow_engine_used_not_raw_llm(
        self, tmp_config: ProctorConfig
    ) -> None:
        """Verify WorkflowEngine is wired (not raw LLM call)."""
        app = Application(tmp_config)

        async def mock_llm(prompt: str) -> str:
            return f"via-engine: {prompt}"

        app.set_llm_call(mock_llm)
        assert app._engine is not None  # Engine created by set_llm_call
        await app.start()
        try:
            results: list[Event] = []

            async def capture(e: Event) -> None:
                results.append(e)

            app.bus.subscribe("task.*", capture)

            await app.bus.publish(
                Event(
                    type="trigger.terminal",
                    source="terminal",
                    payload={"text": "ping"},
                )
            )

            assert results[0].type == "task.completed"
            assert results[0].payload["output"] == "via-engine: ping"
        finally:
            await app.stop()


class TestSchedulerTriggerIntegration:
    """Integration: SchedulerTrigger publishes events on a real EventBus."""

    @pytest.mark.anyio
    async def test_interval_publishes_events_on_eventbus(self) -> None:
        """GIVEN a SchedulerTrigger with a 0.1s interval on a real EventBus
        WHEN it runs for ~0.5s
        THEN at least 2 trigger.scheduler events are received.
        """
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.scheduler", handler)

        item = ScheduleItemConfig(
            name="fast-tick",
            interval_seconds=0.1,
            payload={"tick": True},
        )
        trigger = SchedulerTrigger(schedules=[item])
        await trigger.start(bus)

        await asyncio.sleep(0.55)
        await trigger.stop()

        # At least 2 events in ~0.5s with 0.1s interval
        assert len(received) >= 2
        for ev in received:
            assert ev.type == "trigger.scheduler"
            assert ev.source == "scheduler:fast-tick"
            assert ev.payload == {"tick": True}
            assert ev.id  # auto-generated UUID
            assert ev.timestamp  # auto-generated timestamp

    @pytest.mark.anyio
    async def test_clean_shutdown_no_events_after_stop(self) -> None:
        """GIVEN a running SchedulerTrigger
        WHEN stop() is called
        THEN no more events are published and tasks list is empty.
        """
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.scheduler", handler)

        item = ScheduleItemConfig(
            name="shutdown-test",
            interval_seconds=0.05,
            payload={},
        )
        trigger = SchedulerTrigger(schedules=[item])
        await trigger.start(bus)

        await asyncio.sleep(0.15)
        await trigger.stop()

        # Record count at stop
        count_at_stop = len(received)
        assert count_at_stop >= 1

        # Wait and verify no new events arrive
        await asyncio.sleep(0.15)
        assert len(received) == count_at_stop

        # Internal tasks list is cleared
        assert trigger._tasks == []
