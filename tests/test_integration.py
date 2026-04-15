"""Integration tests: terminal event -> workflow -> task persistence."""

import asyncio
import hashlib
import hmac
from pathlib import Path

import aiohttp
import anyio
import pytest

from proctor.core.bootstrap import Application
from proctor.core.bus import EventBus
from proctor.core.config import (
    HMACAuthConfig,
    ProctorConfig,
    RouteRule,
    ScheduleItemConfig,
    WebhookConfig,
    WebhookPathConfig,
)
from proctor.core.models import Event, TaskStatus
from proctor.core.transport import LocalEventTransport
from proctor.triggers.scheduler import SchedulerTrigger
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

# aiosqlite is asyncio-only
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """aiosqlite only supports asyncio."""
    return "asyncio"


@pytest.fixture
def tmp_config(tmp_path: Path) -> ProctorConfig:
    """Config with workflows and terminal route for router-driven integration tests."""
    return ProctorConfig(
        data_dir=tmp_path / "proctor_data",
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
            await app.bus.flush()

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
            await app.bus.flush()

            tasks = await app.state.list_tasks(status=TaskStatus.COMPLETED)
            assert len(tasks) == 1
            task = tasks[0]
            assert task.status == TaskStatus.COMPLETED
            # spec is the full WorkflowSpec dump; prompt is the resolved payload value
            assert task.spec["prompt"] == "hello"
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
            await app.bus.flush()

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
            await app.bus.flush()

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
            await app.bus.flush()

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
        bus = EventBus(LocalEventTransport())
        await bus.start()
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

        await anyio.sleep(0.55)
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
        bus = EventBus(LocalEventTransport())
        await bus.start()
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

        await anyio.sleep(0.15)
        await trigger.stop()

        # Record count at stop
        count_at_stop = len(received)
        assert count_at_stop >= 1

        # Wait and verify no new events arrive
        await anyio.sleep(0.15)
        assert len(received) == count_at_stop

        # Internal task group is cleared
        assert trigger._task_group is None


class TestWebhookIntegration:
    """End-to-end webhook -> Router -> workflow -> task persistence."""

    @pytest.mark.anyio
    async def test_webhook_routes_to_workflow_end_to_end(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GIVEN a webhook endpoint with a matching route rule
        WHEN a POST request arrives with valid HMAC
        THEN Router routes to the workflow, LLM executes, and episode
        persists with expected output.
        """

        async def llm_echo(prompt: str) -> str:
            return f"echo: {prompt}"

        monkeypatch.setenv("GITHUB_TEST_SECRET", "topsecret")
        cfg = ProctorConfig(
            data_dir=tmp_path,
            workflows={
                "gh": WorkflowSpec(
                    workflow_id="gh",
                    mode=WorkflowMode.SIMPLE,
                ),
            },
            routes=[
                RouteRule(
                    event_pattern="trigger.webhook.github",
                    workflow_id="gh",
                    prompt_from_payload="body.message",
                ),
            ],
            webhook=WebhookConfig(
                port=0,
                paths={
                    "/webhook/github": WebhookPathConfig(
                        source_name="github",
                        auth=HMACAuthConfig(
                            secret_env="GITHUB_TEST_SECRET",
                        ),
                    ),
                },
            ),
        )
        app = Application(cfg)
        app.set_llm_call(llm_echo)
        await app.start()
        try:
            port = app._webhook_trigger.bound_port  # type: ignore[union-attr]
            body = b'{"message": "hello from github"}'
            sig = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
            url = f"http://127.0.0.1:{port}/webhook/github"
            async with (
                aiohttp.ClientSession() as s,
                s.post(
                    url,
                    data=body,
                    headers={"X-Hub-Signature-256": sig},
                ) as r,
            ):
                assert r.status == 202
            # Give the workflow a moment to complete. Poll a few times
            # since the webhook's publish is decoupled from the response.
            for _ in range(20):
                await asyncio.sleep(0.05)
                await app.bus.flush()
                episodes = await app.memory.list_episodes(limit=10)
                if episodes:
                    break
            episodes = await app.memory.list_episodes(limit=10)
            assert any(
                ep.agent_response == "echo: hello from github" for ep in episodes
            )
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_unmatched_webhook_creates_no_task(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GIVEN a webhook path with no matching route rule
        WHEN a valid webhook request arrives
        THEN webhook returns 202 (accepted), Router publishes
        routing.unmatched, but no task or episode is created.
        """

        async def llm_unused(prompt: str) -> str:
            return "should not be called"

        monkeypatch.setenv("GH_SECRET", "s")
        cfg = ProctorConfig(
            data_dir=tmp_path,
            webhook=WebhookConfig(
                port=0,
                paths={
                    "/webhook/lonely": WebhookPathConfig(
                        source_name="lonely",
                        auth=HMACAuthConfig(secret_env="GH_SECRET"),
                    ),
                },
            ),
        )
        app = Application(cfg)
        app.set_llm_call(llm_unused)
        await app.start()
        try:
            port = app._webhook_trigger.bound_port  # type: ignore[union-attr]
            body = b'{"x": 1}'
            sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
            url = f"http://127.0.0.1:{port}/webhook/lonely"
            async with (
                aiohttp.ClientSession() as s,
                s.post(
                    url,
                    data=body,
                    headers={"X-Hub-Signature-256": sig},
                ) as r,
            ):
                assert r.status == 202  # webhook accepted
            await asyncio.sleep(0.1)
            episodes = await app.memory.list_episodes(limit=10)
            assert episodes == []  # no route → no task/episode
        finally:
            await app.stop()
