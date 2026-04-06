"""EpisodicMemory — async SQLite store for agent interaction history."""

import json
import logging
from pathlib import Path

import aiosqlite

from proctor.core.models import Episode

logger = logging.getLogger(__name__)

# --- DDL ---

_CREATE_EPISODES = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    user_input TEXT NOT NULL,
    agent_response TEXT NOT NULL,
    workflow_result_json TEXT
)
"""

_CREATE_INDEX_EPISODES_TS = (
    "CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes(timestamp)"
)

# --- DML ---

_INSERT_EPISODE = """
INSERT INTO episodes (
    id, timestamp, trigger_type, user_input,
    agent_response, workflow_result_json
) VALUES (?, ?, ?, ?, ?, ?)
"""

_SELECT_EPISODE = "SELECT * FROM episodes WHERE id = ?"

_SELECT_EPISODES_LIST = "SELECT * FROM episodes ORDER BY timestamp DESC LIMIT ?"

_SEARCH_EPISODES = (
    "SELECT * FROM episodes"
    " WHERE user_input LIKE ? OR agent_response LIKE ?"
    " ORDER BY timestamp DESC LIMIT ?"
)


class EpisodicMemory:
    """Async SQLite store for episodic interaction history."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open DB, create tables, enable WAL mode."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(_CREATE_EPISODES)
        await self._db.execute(_CREATE_INDEX_EPISODES_TS)
        await self._db.commit()
        logger.info("EpisodicMemory initialized: %s", self._db_path)

    async def close(self) -> None:
        """Close DB connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def save_episode(self, episode: Episode) -> None:
        """Insert an episode row."""
        assert self._db is not None
        await self._db.execute(
            _INSERT_EPISODE,
            (
                episode.id,
                episode.timestamp.isoformat(),
                episode.trigger_type,
                episode.user_input,
                episode.agent_response,
                (
                    json.dumps(episode.workflow_result)
                    if episode.workflow_result is not None
                    else None
                ),
            ),
        )
        await self._db.commit()

    async def get_episode(self, episode_id: str) -> Episode | None:
        """Get episode by ID. Returns None if not found."""
        assert self._db is not None
        cursor = await self._db.execute(_SELECT_EPISODE, (episode_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_episode(row)

    async def list_episodes(self, limit: int = 50) -> list[Episode]:
        """List episodes, most recent first."""
        assert self._db is not None
        cursor = await self._db.execute(_SELECT_EPISODES_LIST, (limit,))
        rows = await cursor.fetchall()
        return [_row_to_episode(row) for row in rows]

    async def search_episodes(self, query: str, limit: int = 20) -> list[Episode]:
        """Search episodes by user_input or agent_response."""
        assert self._db is not None
        pattern = f"%{query}%"
        cursor = await self._db.execute(_SEARCH_EPISODES, (pattern, pattern, limit))
        rows = await cursor.fetchall()
        return [_row_to_episode(row) for row in rows]


def _row_to_episode(row: aiosqlite.Row) -> Episode:
    """Convert a SQLite row to an Episode model."""
    from datetime import datetime

    wf_json = row["workflow_result_json"]
    return Episode(
        id=row["id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        trigger_type=row["trigger_type"],
        user_input=row["user_input"],
        agent_response=row["agent_response"],
        workflow_result=(json.loads(wf_json) if wf_json is not None else None),
    )
