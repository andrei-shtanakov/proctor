"""Tests for EpisodicMemory (SQLite episodic store)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from proctor.core.memory import EpisodicMemory
from proctor.core.models import Episode

# aiosqlite is asyncio-only; override the anyio_backend fixture
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """aiosqlite only supports asyncio."""
    return "asyncio"


@pytest.fixture
async def memory(tmp_path: Path) -> EpisodicMemory:
    """Create and initialize an EpisodicMemory with a temp DB."""
    mem = EpisodicMemory(tmp_path / "episodes.db")
    await mem.initialize()
    yield mem  # type: ignore[misc]
    await mem.close()


def _make_episode(**kwargs: object) -> Episode:
    """Helper to create an Episode with defaults."""
    defaults: dict[str, object] = {
        "trigger_type": "terminal",
        "user_input": "hello",
        "agent_response": "world",
    }
    defaults.update(kwargs)
    return Episode(**defaults)  # type: ignore[arg-type]


class TestEpisodeModel:
    def test_defaults(self) -> None:
        ep = _make_episode()
        assert ep.id  # non-empty UUID
        assert ep.timestamp <= datetime.now(UTC)
        assert ep.trigger_type == "terminal"
        assert ep.workflow_result is None

    def test_all_fields_explicit(self) -> None:
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        ep = Episode(
            id="ep-1",
            timestamp=ts,
            trigger_type="webhook",
            user_input="search RISC-V",
            agent_response="Here is info on RISC-V",
            workflow_result={"status": "ok"},
        )
        assert ep.id == "ep-1"
        assert ep.timestamp == ts
        assert ep.workflow_result == {"status": "ok"}

    def test_serialization_roundtrip(self) -> None:
        ep = _make_episode(workflow_result={"key": "val"})
        data = ep.model_dump()
        restored = Episode(**data)
        assert restored == ep


class TestInitializeAndClose:
    async def test_creates_db_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "sub" / "episodes.db"
        mem = EpisodicMemory(db_path)
        await mem.initialize()
        assert db_path.exists()
        await mem.close()

    async def test_creates_episodes_table(self, memory: EpisodicMemory) -> None:
        assert memory._db is not None
        cursor = await memory._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='episodes'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_close_sets_db_none(self, tmp_path: Path) -> None:
        mem = EpisodicMemory(tmp_path / "test.db")
        await mem.initialize()
        assert mem._db is not None
        await mem.close()
        assert mem._db is None

    async def test_close_idempotent(self, tmp_path: Path) -> None:
        mem = EpisodicMemory(tmp_path / "test.db")
        await mem.initialize()
        await mem.close()
        await mem.close()  # should not raise
        assert mem._db is None

    async def test_wal_mode_enabled(self, memory: EpisodicMemory) -> None:
        assert memory._db is not None
        cursor = await memory._db.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "wal"


class TestSaveAndGetEpisode:
    async def test_save_and_retrieve_all_fields(
        self, memory: EpisodicMemory
    ) -> None:
        ts = datetime(2026, 3, 15, 12, 30, 0, tzinfo=UTC)
        ep = Episode(
            id="ep-roundtrip",
            timestamp=ts,
            trigger_type="webhook",
            user_input="search RISC-V",
            agent_response="Here is info on RISC-V",
            workflow_result={"status": "ok", "steps": 2},
        )
        await memory.save_episode(ep)
        retrieved = await memory.get_episode(ep.id)
        assert retrieved is not None
        assert retrieved.id == ep.id
        assert retrieved.timestamp == ep.timestamp
        assert retrieved.trigger_type == ep.trigger_type
        assert retrieved.user_input == ep.user_input
        assert retrieved.agent_response == ep.agent_response
        assert retrieved.workflow_result == ep.workflow_result

    async def test_get_nonexistent_returns_none(self, memory: EpisodicMemory) -> None:
        result = await memory.get_episode("no-such-id")
        assert result is None

    async def test_workflow_result_roundtrip(self, memory: EpisodicMemory) -> None:
        ep = _make_episode(workflow_result={"output": "done", "steps": 3})
        await memory.save_episode(ep)
        retrieved = await memory.get_episode(ep.id)
        assert retrieved is not None
        assert retrieved.workflow_result == {"output": "done", "steps": 3}

    async def test_workflow_result_none(self, memory: EpisodicMemory) -> None:
        ep = _make_episode(workflow_result=None)
        await memory.save_episode(ep)
        retrieved = await memory.get_episode(ep.id)
        assert retrieved is not None
        assert retrieved.workflow_result is None


class TestListEpisodes:
    async def test_list_empty(self, memory: EpisodicMemory) -> None:
        episodes = await memory.list_episodes()
        assert episodes == []

    async def test_list_returns_newest_first(self, memory: EpisodicMemory) -> None:
        for i in range(3):
            ep = _make_episode(
                timestamp=datetime(2026, 1, i + 1, tzinfo=UTC),
                user_input=f"msg-{i}",
            )
            await memory.save_episode(ep)
        episodes = await memory.list_episodes()
        assert len(episodes) == 3
        assert episodes[0].user_input == "msg-2"
        assert episodes[2].user_input == "msg-0"

    async def test_list_respects_limit(self, memory: EpisodicMemory) -> None:
        for _ in range(5):
            await memory.save_episode(_make_episode())
        episodes = await memory.list_episodes(limit=2)
        assert len(episodes) == 2


class TestSearchEpisodes:
    async def test_search_matches_user_input(self, memory: EpisodicMemory) -> None:
        await memory.save_episode(_make_episode(user_input="tell me about RISC-V"))
        await memory.save_episode(_make_episode(user_input="what is ARM?"))
        results = await memory.search_episodes("RISC-V")
        assert len(results) == 1
        assert "RISC-V" in results[0].user_input

    async def test_search_matches_agent_response(self, memory: EpisodicMemory) -> None:
        await memory.save_episode(_make_episode(agent_response="RISC-V is an ISA"))
        results = await memory.search_episodes("RISC-V")
        assert len(results) == 1

    async def test_search_no_match(self, memory: EpisodicMemory) -> None:
        await memory.save_episode(_make_episode(user_input="hello"))
        results = await memory.search_episodes("nonexistent")
        assert results == []

    async def test_search_respects_limit(self, memory: EpisodicMemory) -> None:
        for _ in range(5):
            await memory.save_episode(_make_episode(user_input="keyword match"))
        results = await memory.search_episodes("keyword", limit=2)
        assert len(results) == 2
