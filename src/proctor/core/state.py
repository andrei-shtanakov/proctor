"""StateManager — async SQLite wrapper for persistent state."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from proctor.core.models import Task, TaskStatus

logger = logging.getLogger(__name__)

_CREATE_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    spec_json TEXT NOT NULL,
    trigger_event TEXT,
    worker_id TEXT,
    result_json TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deadline TEXT
)
"""

_CREATE_SCHEDULES = """
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    expression TEXT NOT NULL,
    tz TEXT DEFAULT 'UTC',
    workflow_id TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run TEXT,
    last_run TEXT
)
"""

_CREATE_CONFIG_OVERRIDES = """
CREATE TABLE IF NOT EXISTS config_overrides (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_INDEX_TASKS_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
)

_UPSERT_TASK = """
INSERT INTO tasks (
    id, status, spec_json, trigger_event, worker_id,
    result_json, retries, created_at, updated_at, deadline
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    status=excluded.status,
    spec_json=excluded.spec_json,
    trigger_event=excluded.trigger_event,
    worker_id=excluded.worker_id,
    result_json=excluded.result_json,
    retries=excluded.retries,
    updated_at=excluded.updated_at,
    deadline=excluded.deadline
"""

_SELECT_TASK = "SELECT * FROM tasks WHERE id = ?"

_SELECT_TASKS_ALL = "SELECT * FROM tasks"

_SELECT_TASKS_BY_STATUS = "SELECT * FROM tasks WHERE status = ?"

_UPSERT_CONFIG = """
INSERT INTO config_overrides (key, value_json, updated_at)
VALUES (?, ?, datetime('now'))
ON CONFLICT(key) DO UPDATE SET
    value_json=excluded.value_json,
    updated_at=datetime('now')
"""

_SELECT_CONFIG = "SELECT value_json FROM config_overrides WHERE key = ?"

_SELECT_TABLES = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"


def _dt_to_str(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _str_to_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


class StateManager:
    """Async SQLite wrapper for persistent operational state."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open DB, create tables, enable WAL mode."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(_CREATE_TASKS)
        await self._db.execute(_CREATE_SCHEDULES)
        await self._db.execute(_CREATE_CONFIG_OVERRIDES)
        await self._db.execute(_CREATE_INDEX_TASKS_STATUS)
        await self._db.commit()
        logger.info("StateManager initialized: %s", self._db_path)

    async def close(self) -> None:
        """Close DB connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def save_task(self, task: Task) -> None:
        """Insert or update a task (upsert by id)."""
        assert self._db is not None
        await self._db.execute(
            _UPSERT_TASK,
            (
                task.id,
                task.status.value,
                json.dumps(task.spec),
                task.trigger_event,
                task.worker_id,
                json.dumps(task.result) if task.result is not None else None,
                task.retries,
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
                _dt_to_str(task.deadline),
            ),
        )
        await self._db.commit()

    async def get_task(self, task_id: str) -> Task | None:
        """Get task by ID. Returns None if not found."""
        assert self._db is not None
        cursor = await self._db.execute(_SELECT_TASK, (task_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_task(row)

    async def list_tasks(self, status: TaskStatus | None = None) -> list[Task]:
        """List tasks, optionally filtered by status."""
        assert self._db is not None
        if status is not None:
            cursor = await self._db.execute(_SELECT_TASKS_BY_STATUS, (status.value,))
        else:
            cursor = await self._db.execute(_SELECT_TASKS_ALL)
        rows = await cursor.fetchall()
        return [_row_to_task(row) for row in rows]

    async def set_config(self, key: str, value: Any) -> None:
        """Set a config override (upsert)."""
        assert self._db is not None
        await self._db.execute(_UPSERT_CONFIG, (key, json.dumps(value)))
        await self._db.commit()

    async def get_config(self, key: str, default: Any = None) -> Any:
        """Get a config override, or default."""
        assert self._db is not None
        cursor = await self._db.execute(_SELECT_CONFIG, (key,))
        row = await cursor.fetchone()
        if row is None:
            return default
        return json.loads(row["value_json"])

    async def list_tables(self) -> list[str]:
        """List all tables (for testing)."""
        assert self._db is not None
        cursor = await self._db.execute(_SELECT_TABLES)
        rows = await cursor.fetchall()
        return [row["name"] for row in rows]


def _row_to_task(row: aiosqlite.Row) -> Task:
    """Convert a SQLite row to a pydantic Task."""
    result_json = row["result_json"]
    return Task(
        id=row["id"],
        status=TaskStatus(row["status"]),
        spec=json.loads(row["spec_json"]),
        trigger_event=row["trigger_event"],
        worker_id=row["worker_id"],
        result=json.loads(result_json) if result_json is not None else None,
        retries=row["retries"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        deadline=_str_to_dt(row["deadline"]),
    )
