"""Tests for Application bootstrap: lifecycle, state, and event wiring."""

import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import ProctorConfig, ScheduleItemConfig, SchedulerConfig
from proctor.core.models import Event

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


class TestInit:
    def test_creates_bus_and_state(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
        assert app.bus is not None
        assert app.state is not None
        assert app.is_running is False

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
    async def test_terminal_handler_subscribed(self, tmp_config: ProctorConfig) -> None:
        """After start, trigger.terminal events are handled."""
        app = Application(tmp_config)

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
    async def test_no_llm_publishes_failed(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
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
            assert "No LLM" in results[0].payload["error"]
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_empty_text_ignored(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)
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
            assert len(results) == 0
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_llm_error_publishes_failed(self, tmp_config: ProctorConfig) -> None:
        app = Application(tmp_config)

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
            assert len(app._scheduler._tasks) == 1
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
        import asyncio
        from pathlib import Path

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
            await asyncio.sleep(0.15)
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
