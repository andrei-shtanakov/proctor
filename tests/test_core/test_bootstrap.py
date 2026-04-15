"""Tests for Application bootstrap: lifecycle, state, and event wiring."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import (
    BearerAuthConfig,
    NoneAuthConfig,
    ProctorConfig,
    RouteRule,
    ScheduleItemConfig,
    SchedulerConfig,
    TelegramConfig,
    WebhookConfig,
    WebhookPathConfig,
)
from proctor.core.memory import EpisodicMemory
from proctor.core.models import Episode, Event
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

# aiosqlite is asyncio-only; override the anyio_backend fixture
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """aiosqlite only supports asyncio."""
    return "asyncio"


@pytest.fixture
def tmp_config(tmp_path: object) -> ProctorConfig:
    """Config with a temporary data directory."""
    from pathlib import Path

    return ProctorConfig(data_dir=Path(str(tmp_path)) / "proctor_data")


@pytest.fixture
def tmp_config_with_routes(tmp_path: object) -> ProctorConfig:
    """Config with workflows and terminal route for router-driven tests."""
    from pathlib import Path

    return ProctorConfig(
        data_dir=Path(str(tmp_path)) / "proctor_data",
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


class TestInit:
    def test_creates_bus_and_state(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        assert app.bus is not None
        assert app.state is not None
        assert app.is_running is False

    def test_creates_memory(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        assert app.memory is not None
        assert isinstance(app.memory, EpisodicMemory)

    def test_config_stored(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        assert app.config is tmp_config


class TestStartStop:
    @pytest.mark.anyio
    async def test_start_sets_running(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        await app.start()
        try:
            assert app.is_running is True
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_stop_clears_running(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        await app.start()
        await app.stop()
        assert app.is_running is False

    @pytest.mark.anyio
    async def test_data_dir_created(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        await app.start()
        try:
            assert tmp_config.data_dir.exists()
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_state_db_created(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        await app.start()
        try:
            db_path = tmp_config.data_dir / "state.db"
            assert db_path.exists()
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_state_tables_created(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        await app.start()
        try:
            tables = await app.state.list_tables()
            assert "tasks" in tables
            assert "config_overrides" in tables
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_episodes_db_created(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        await app.start()
        try:
            db_path = tmp_config.data_dir / "episodes.db"
            assert db_path.exists()
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_memory_usable_after_start(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        await app.start()
        try:
            episode = Episode(
                trigger_type="terminal",
                user_input="hello",
                agent_response="world",
            )
            await app.memory.save_episode(episode)
            fetched = await app.memory.get_episode(episode.id)
            assert fetched is not None
            assert fetched.user_input == "hello"
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_memory_closed_after_stop(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        await app.start()
        await app.stop()
        assert app.memory._db is None

    @pytest.mark.anyio
    async def test_double_stop_is_safe(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        await app.start()
        await app.stop()
        await app.stop()  # should not raise


class TestSetLLMCall:
    def test_set_llm_call(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)

        async def mock_llm(prompt: str) -> str:
            return "response"

        app.set_llm_call(mock_llm)
        assert app._llm_call is mock_llm


class TestEventBusFunctional:
    @pytest.mark.anyio
    async def test_bus_works_after_start(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        await app.start()
        try:
            received: list[Event] = []

            async def handler(e: Event) -> None:
                received.append(e)

            app.bus.subscribe("test.*", handler)
            event = Event(type="test.ping", source="test")
            await app.bus.publish(event)
            assert len(received) == 1
            assert received[0].id == event.id
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_terminal_handler_subscribed(
        self, tmp_config_with_routes: ProctorConfig
    ) -> None:
        """After start, trigger.terminal events are handled via trigger.*."""
        app = Application(tmp_config_with_routes)

        async def mock_llm(prompt: str) -> str:
            return f"echo: {prompt}"

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
                    payload={"text": "hello"},
                )
            )
            assert len(results) == 1
            assert results[0].type == "task.completed"
            assert results[0].payload["output"] == "echo: hello"
        finally:
            await app.stop()


class TestHandleTerminal:
    @pytest.mark.anyio
    async def test_no_llm_publishes_failed(
        self, tmp_config_with_routes: ProctorConfig
    ) -> None:
        """Router matches, but no engine → task.failed with 'No LLM' error."""
        app = Application(tmp_config_with_routes)
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
                    payload={"text": "hello"},
                )
            )
            assert len(results) == 1
            assert results[0].type == "task.failed"
            assert "engine" in results[0].payload["error"].lower()
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_empty_text_produces_task(
        self, tmp_config_with_routes: ProctorConfig
    ) -> None:
        """Router-driven flow: empty payload text still matches and creates a task.

        The old _handle_terminal guarded `if not text: return`, silently
        dropping empty input. With the Router in place, an empty string is a
        valid (if unusual) prompt — the route still matches and a task is
        created. Tests confirm the new semantics rather than the old guard.
        """
        app = Application(tmp_config_with_routes)

        async def mock_llm(prompt: str) -> str:
            return f"echo: {prompt}"

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
                    payload={"text": ""},
                )
            )
            # Router matched → task created with empty prompt → a task event emitted
            # (completed or failed depending on LLM handling of empty string)
            assert len(results) == 1
            assert results[0].type in ("task.completed", "task.failed")
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_llm_error_publishes_failed(
        self, tmp_config_with_routes: ProctorConfig
    ) -> None:
        app = Application(tmp_config_with_routes)

        async def bad_llm(prompt: str) -> str:
            raise RuntimeError("LLM down")

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
            assert len(results) == 1
            assert results[0].type == "task.failed"
            assert "LLM down" in results[0].payload["error"]
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_successful_workflow_saves_episode(
        self, tmp_config_with_routes: ProctorConfig
    ) -> None:
        """After successful workflow, an episode should be saved."""
        app = Application(tmp_config_with_routes)

        async def mock_llm(prompt: str) -> str:
            return f"echo: {prompt}"

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
            episodes = await app.memory.list_episodes(limit=10)
            assert len(episodes) == 1
            ep = episodes[0]
            assert ep.trigger_type == "terminal"
            assert ep.user_input == "hello"
            assert ep.agent_response == "echo: hello"
            assert ep.workflow_result is not None
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_failed_workflow_saves_episode(
        self, tmp_config_with_routes: ProctorConfig
    ) -> None:
        """Failed workflows should also save an episode."""
        app = Application(tmp_config_with_routes)

        async def bad_llm(prompt: str) -> str:
            raise RuntimeError("LLM down")

        app.set_llm_call(bad_llm)
        await app.start()
        try:
            await app.bus.publish(
                Event(
                    type="trigger.terminal",
                    source="terminal",
                    payload={"text": "test"},
                )
            )
            episodes = await app.memory.list_episodes(limit=10)
            assert len(episodes) == 1
            ep = episodes[0]
            assert ep.trigger_type == "terminal"
            assert ep.user_input == "test"
            assert ep.agent_response == ""
            assert ep.workflow_result is not None
            assert "LLM down" in str(ep.workflow_result)
        finally:
            await app.stop()


class TestTelegramTriggerBootstrap:
    """Test TelegramTrigger integration in Application lifecycle."""

    @pytest.fixture
    def telegram_config(self, tmp_path: object) -> ProctorConfig:
        from pathlib import Path

        return ProctorConfig(
            data_dir=Path(str(tmp_path)) / "proctor_data",
            telegram=TelegramConfig(
                bot_token="test-token",
                allowed_chat_ids=[],
            ),
        )

    @pytest.mark.anyio
    async def test_telegram_trigger_started_when_configured(
        self, telegram_config: ProctorConfig
    ) -> None:
        app = Application(telegram_config)
        mock_trigger = AsyncMock()
        with patch(
            "proctor.core.bootstrap.TelegramTrigger",
            return_value=mock_trigger,
        ):
            await app.start()
            try:
                mock_trigger.start.assert_called_once_with(app.bus)
                assert app._telegram_trigger is mock_trigger
            finally:
                await app.stop()

    @pytest.mark.anyio
    async def test_telegram_trigger_stopped_on_shutdown(
        self, telegram_config: ProctorConfig
    ) -> None:
        app = Application(telegram_config)
        mock_trigger = AsyncMock()
        with patch(
            "proctor.core.bootstrap.TelegramTrigger",
            return_value=mock_trigger,
        ):
            await app.start()
            await app.stop()
            mock_trigger.stop.assert_called_once()
            assert app._telegram_trigger is None

    @pytest.mark.anyio
    async def test_no_telegram_trigger_when_not_configured(
        self, tmp_config: ProctorConfig
    ) -> None:
        app = Application(tmp_config)
        await app.start()
        try:
            assert app._telegram_trigger is None
        finally:
            await app.stop()


class TestSchedulerIntegration:
    @pytest.mark.anyio
    async def test_scheduler_not_created_when_no_schedules(
        self, tmp_config: ProctorConfig
    ) -> None:
        app = Application(tmp_config)
        await app.start()
        try:
            assert app._scheduler is None
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_scheduler_not_created_when_disabled(self, tmp_path: object) -> None:
        from pathlib import Path

        config = ProctorConfig(
            data_dir=Path(str(tmp_path)) / "proctor_data",
            scheduler=SchedulerConfig(enabled=False),
            schedules=[
                ScheduleItemConfig(name="test", interval_seconds=60, payload={"k": "v"})
            ],
        )
        app = Application(config)
        await app.start()
        try:
            assert app._scheduler is None
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_scheduler_started_with_schedules(self, tmp_path: object) -> None:
        from pathlib import Path

        config = ProctorConfig(
            data_dir=Path(str(tmp_path)) / "proctor_data",
            scheduler=SchedulerConfig(enabled=True),
            schedules=[
                ScheduleItemConfig(
                    name="heartbeat",
                    interval_seconds=3600,
                    payload={"type": "ping"},
                )
            ],
        )
        app = Application(config)
        await app.start()
        try:
            assert app._scheduler is not None
            assert app._scheduler._task_group is not None
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_scheduler_stopped_on_app_stop(self, tmp_path: object) -> None:
        from pathlib import Path

        config = ProctorConfig(
            data_dir=Path(str(tmp_path)) / "proctor_data",
            scheduler=SchedulerConfig(enabled=True),
            schedules=[
                ScheduleItemConfig(
                    name="heartbeat",
                    interval_seconds=3600,
                    payload={},
                )
            ],
        )
        app = Application(config)
        await app.start()
        assert app._scheduler is not None
        await app.stop()
        assert app._scheduler is None

    @pytest.mark.anyio
    async def test_scheduler_publishes_events(self, tmp_path: object) -> None:
        """Scheduler trigger events arrive on the bus."""
        from pathlib import Path

        import anyio

        config = ProctorConfig(
            data_dir=Path(str(tmp_path)) / "proctor_data",
            scheduler=SchedulerConfig(enabled=True),
            schedules=[
                ScheduleItemConfig(
                    name="fast",
                    interval_seconds=0.05,
                    payload={"msg": "tick"},
                )
            ],
        )
        app = Application(config)
        received: list[Event] = []

        async def capture(e: Event) -> None:
            received.append(e)

        await app.start()
        try:
            app.bus.subscribe("trigger.scheduler", capture)
            await anyio.sleep(0.15)
            assert len(received) >= 1
            assert received[0].type == "trigger.scheduler"
            assert received[0].payload["msg"] == "tick"
        finally:
            await app.stop()


class TestPublicExports:
    def test_import_application(self) -> None:
        from proctor.core import Application

        assert Application is not None

    def test_import_llm_call(self) -> None:
        from proctor.core import LLMCall

        assert LLMCall is not None


class TestContextVarWiring:
    @pytest.mark.anyio
    async def test_ids_set_during_workflow_execute(self, tmp_path: Path) -> None:
        """task_id_ctx and episode_id_ctx are set during workflow execution."""
        from proctor.core.bootstrap import Application
        from proctor.core.config import ProctorConfig, RouteRule
        from proctor.core.models import Event
        from proctor.workers.llm import episode_id_ctx, task_id_ctx
        from proctor.workflow.spec import WorkflowMode, WorkflowSpec

        captured: list[tuple[str | None, str | None]] = []

        async def llm_recording_ctx(_prompt: str) -> str:
            captured.append((task_id_ctx.get(), episode_id_ctx.get()))
            return "ok"

        cfg = ProctorConfig(
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
        app = Application(cfg)
        app.set_llm_call(llm_recording_ctx)
        await app.start()
        try:
            await app._handle_trigger_event(
                Event(
                    type="trigger.terminal",
                    source="terminal",
                    payload={"text": "hi"},
                )
            )
        finally:
            await app.stop()

        assert captured, "LLM call was not made"
        task_id, episode_id = captured[0]
        assert task_id is not None
        assert episode_id is not None


class TestRouterIntegration:
    @pytest.mark.anyio
    async def test_telegram_trigger_routes_to_workflow(self, tmp_path: Path) -> None:
        """trigger.telegram events route through the Router to a workflow."""

        async def llm_echo(prompt: str) -> str:
            return f"echo: {prompt}"

        cfg = ProctorConfig(
            data_dir=tmp_path / "proctor_data",
            workflows={
                "chat": WorkflowSpec(workflow_id="chat", mode=WorkflowMode.SIMPLE),
            },
            routes=[
                RouteRule(
                    event_pattern="trigger.telegram",
                    workflow_id="chat",
                    prompt_from_payload="text",
                ),
            ],
        )
        app = Application(cfg)
        app.set_llm_call(llm_echo)
        await app.start()
        try:
            await app._handle_trigger_event(
                Event(
                    type="trigger.telegram",
                    source="telegram",
                    payload={"text": "hi telegram", "chat_id": 1},
                )
            )
            episodes = await app.memory.list_episodes(limit=10)
            assert any(ep.agent_response == "echo: hi telegram" for ep in episodes)
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_scheduler_trigger_routes_to_workflow(self, tmp_path: Path) -> None:
        """trigger.scheduler events route through the Router to a workflow."""

        async def llm_echo(prompt: str) -> str:
            return f"heartbeat-ack: {prompt}"

        cfg = ProctorConfig(
            data_dir=tmp_path / "proctor_data",
            workflows={
                "heartbeat": WorkflowSpec(
                    workflow_id="heartbeat", mode=WorkflowMode.SIMPLE
                ),
            },
            routes=[
                RouteRule(
                    event_pattern="trigger.scheduler",
                    workflow_id="heartbeat",
                    prompt_from_payload="prompt",
                ),
            ],
        )
        app = Application(cfg)
        app.set_llm_call(llm_echo)
        await app.start()
        try:
            await app._handle_trigger_event(
                Event(
                    type="trigger.scheduler",
                    source="scheduler:heartbeat",
                    payload={"prompt": "Check system"},
                )
            )
            episodes = await app.memory.list_episodes(limit=10)
            assert any(
                ep.agent_response == "heartbeat-ack: Check system" for ep in episodes
            )
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_unmatched_event_creates_no_task(self, tmp_path: Path) -> None:
        """Unmatched events (no matching route) produce no tasks or episodes."""

        async def llm_echo(prompt: str) -> str:
            return "should not be called"

        cfg = ProctorConfig(data_dir=tmp_path / "proctor_data")  # empty routes
        app = Application(cfg)
        app.set_llm_call(llm_echo)
        await app.start()
        try:
            await app._handle_trigger_event(
                Event(
                    type="trigger.webhook",
                    source="webhook",
                    payload={"body": "x"},
                )
            )
            episodes = await app.memory.list_episodes(limit=10)
            assert episodes == []  # router returned None → no task/episode
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_subscribe_pattern_is_trigger_star(self, tmp_path: Path) -> None:
        """Application subscribes to trigger.*, not trigger.terminal."""
        cfg = ProctorConfig(data_dir=tmp_path / "proctor_data")
        app = Application(cfg)
        await app.start()
        try:
            # _subscriptions is a list[_Subscription] with .pattern attribute
            patterns = {sub.pattern for sub in app.bus._subscriptions}
            assert "trigger.*" in patterns
            assert "trigger.terminal" not in patterns
        finally:
            await app.stop()


class TestWebhookBootstrap:
    @pytest.mark.anyio
    async def test_webhook_trigger_started_when_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WebhookTrigger is instantiated and started when
        config.webhook is not None."""
        monkeypatch.setenv("CI_TOKEN", "x")
        cfg = ProctorConfig(
            data_dir=tmp_path / "proctor_data",
            webhook=WebhookConfig(
                port=0,
                paths={
                    "/webhook/ci": WebhookPathConfig(
                        auth=BearerAuthConfig(secret_env="CI_TOKEN"),
                    ),
                },
            ),
        )
        app = Application(cfg)
        await app.start()
        try:
            assert app._webhook_trigger is not None
            assert app._webhook_trigger.bound_port is not None
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_no_webhook_when_config_missing(self, tmp_path: Path) -> None:
        """When config.webhook is None, _webhook_trigger stays None."""
        cfg = ProctorConfig(data_dir=tmp_path / "proctor_data")
        app = Application(cfg)
        await app.start()
        try:
            assert app._webhook_trigger is None
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_webhook_stopped_first_in_application_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Application.stop() must close WebhookTrigger before other
        components."""
        monkeypatch.setenv("CI_TOKEN", "x")
        cfg = ProctorConfig(
            data_dir=tmp_path / "proctor_data",
            webhook=WebhookConfig(
                port=0,
                paths={
                    "/webhook/open": WebhookPathConfig(
                        auth=NoneAuthConfig(),
                    ),
                },
            ),
        )
        app = Application(cfg)
        await app.start()

        order: list[str] = []
        real_webhook_stop = app._webhook_trigger.stop  # type: ignore[union-attr]
        real_memory_close = app.memory.close

        async def tagged_webhook_stop() -> None:
            order.append("webhook")
            await real_webhook_stop()

        async def tagged_memory_close() -> None:
            order.append("memory")
            await real_memory_close()

        assert app._webhook_trigger is not None
        app._webhook_trigger.stop = tagged_webhook_stop  # type: ignore[method-assign]
        app.memory.close = tagged_memory_close  # type: ignore[method-assign]

        await app.stop()
        assert order.index("webhook") < order.index("memory")
