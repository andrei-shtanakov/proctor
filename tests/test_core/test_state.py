"""Tests for StateManager (SQLite wrapper)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from proctor.core.models import Task, TaskStatus
from proctor.core.state import StateManager

# aiosqlite is asyncio-only; override the anyio_backend fixture
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """aiosqlite only supports asyncio."""
    return "asyncio"


@pytest.fixture
async def state_manager(tmp_path: Path) -> AsyncGenerator[StateManager, None]:
    """Create and initialize a StateManager with a temp DB."""
    sm = StateManager(tmp_path / "test.db")
    await sm.initialize()
    yield sm  # type: ignore[misc]
    await sm.close()


class TestInitialize:
    async def test_tables_created(self, state_manager: StateManager) -> None:
        tables = await state_manager.list_tables()
        assert "tasks" in tables
        assert "schedules" in tables
        assert "config_overrides" in tables

    async def test_table_count(self, state_manager: StateManager) -> None:
        tables = await state_manager.list_tables()
        assert len(tables) == 3

    async def test_idempotent_initialize(self, tmp_path: Path) -> None:
        sm = StateManager(tmp_path / "idem.db")
        await sm.initialize()
        await sm.initialize()  # second call should not fail
        tables = await sm.list_tables()
        assert len(tables) == 3
        await sm.close()


class TestSaveAndGetTask:
    async def test_roundtrip(self, state_manager: StateManager) -> None:
        task = Task(
            spec={"prompt": "hello"},
            trigger_event="evt-1",
            worker_id="w-1",
            result={"output": "world"},
            retries=1,
        )
        await state_manager.save_task(task)
        loaded = await state_manager.get_task(task.id)

        assert loaded is not None
        assert loaded.id == task.id
        assert loaded.status == TaskStatus.PENDING
        assert loaded.spec == {"prompt": "hello"}
        assert loaded.trigger_event == "evt-1"
        assert loaded.worker_id == "w-1"
        assert loaded.result == {"output": "world"}
        assert loaded.retries == 1

    async def test_roundtrip_minimal(self, state_manager: StateManager) -> None:
        task = Task()
        await state_manager.save_task(task)
        loaded = await state_manager.get_task(task.id)

        assert loaded is not None
        assert loaded.id == task.id
        assert loaded.status == TaskStatus.PENDING
        assert loaded.spec == {}
        assert loaded.trigger_event is None
        assert loaded.worker_id is None
        assert loaded.result is None
        assert loaded.retries == 0
        assert loaded.deadline is None

    async def test_roundtrip_with_deadline(self, state_manager: StateManager) -> None:
        deadline = datetime(2026, 12, 31, tzinfo=UTC)
        task = Task(deadline=deadline)
        await state_manager.save_task(task)
        loaded = await state_manager.get_task(task.id)

        assert loaded is not None
        assert loaded.deadline == deadline

    async def test_timestamps_preserved(self, state_manager: StateManager) -> None:
        task = Task(spec={"prompt": "test"})
        await state_manager.save_task(task)
        loaded = await state_manager.get_task(task.id)

        assert loaded is not None
        assert loaded.created_at == task.created_at
        assert loaded.updated_at == task.updated_at


class TestUpdateTask:
    async def test_update_status(self, state_manager: StateManager) -> None:
        task = Task(spec={"prompt": "test"})
        await state_manager.save_task(task)

        task.status = TaskStatus.RUNNING
        await state_manager.save_task(task)
        loaded = await state_manager.get_task(task.id)

        assert loaded is not None
        assert loaded.status == TaskStatus.RUNNING

    async def test_update_preserves_id(self, state_manager: StateManager) -> None:
        task = Task(spec={"prompt": "test"})
        await state_manager.save_task(task)
        original_id = task.id

        task.status = TaskStatus.COMPLETED
        task.result = {"output": "done"}
        await state_manager.save_task(task)
        loaded = await state_manager.get_task(original_id)

        assert loaded is not None
        assert loaded.id == original_id
        assert loaded.result == {"output": "done"}


class TestListTasks:
    async def test_list_all(self, state_manager: StateManager) -> None:
        for i in range(3):
            await state_manager.save_task(Task(spec={"i": i}))
        tasks = await state_manager.list_tasks()
        assert len(tasks) == 3

    async def test_list_by_status(self, state_manager: StateManager) -> None:
        await state_manager.save_task(Task(status=TaskStatus.PENDING))
        await state_manager.save_task(Task(status=TaskStatus.PENDING))
        await state_manager.save_task(Task(status=TaskStatus.RUNNING))
        await state_manager.save_task(Task(status=TaskStatus.COMPLETED))

        pending = await state_manager.list_tasks(status=TaskStatus.PENDING)
        assert len(pending) == 2

        running = await state_manager.list_tasks(status=TaskStatus.RUNNING)
        assert len(running) == 1

        completed = await state_manager.list_tasks(status=TaskStatus.COMPLETED)
        assert len(completed) == 1

    async def test_list_empty(self, state_manager: StateManager) -> None:
        tasks = await state_manager.list_tasks()
        assert tasks == []

    async def test_list_nonexistent_status(self, state_manager: StateManager) -> None:
        await state_manager.save_task(Task(status=TaskStatus.PENDING))
        failed = await state_manager.list_tasks(status=TaskStatus.FAILED)
        assert failed == []


class TestGetNonexistent:
    async def test_get_nonexistent_returns_none(
        self, state_manager: StateManager
    ) -> None:
        result = await state_manager.get_task("does-not-exist")
        assert result is None


class TestConfigOverrides:
    async def test_set_and_get(self, state_manager: StateManager) -> None:
        await state_manager.set_config("log_level", "DEBUG")
        value = await state_manager.get_config("log_level")
        assert value == "DEBUG"

    async def test_get_with_default(self, state_manager: StateManager) -> None:
        value = await state_manager.get_config("missing_key", default="fallback")
        assert value == "fallback"

    async def test_get_missing_default_none(self, state_manager: StateManager) -> None:
        value = await state_manager.get_config("missing")
        assert value is None

    async def test_update_existing(self, state_manager: StateManager) -> None:
        await state_manager.set_config("key", "v1")
        await state_manager.set_config("key", "v2")
        value = await state_manager.get_config("key")
        assert value == "v2"

    async def test_complex_value(self, state_manager: StateManager) -> None:
        config = {"model": "gpt-4", "temperature": 0.7}
        await state_manager.set_config("llm", config)
        loaded = await state_manager.get_config("llm")
        assert loaded == config

    async def test_numeric_value(self, state_manager: StateManager) -> None:
        await state_manager.set_config("max_retries", 5)
        value = await state_manager.get_config("max_retries")
        assert value == 5

    async def test_boolean_value(self, state_manager: StateManager) -> None:
        await state_manager.set_config("enabled", True)
        value = await state_manager.get_config("enabled")
        assert value is True


class TestPublicExports:
    def test_import_from_core(self) -> None:
        from proctor.core import StateManager

        assert StateManager is not None
