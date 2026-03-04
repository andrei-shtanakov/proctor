# Proctor Phase 0 + Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a working Proctor core that can receive a task from the terminal, decompose it into a DAG of steps, execute steps via an LLM agent with MCP tools, and return the result.

**Architecture:** Microkernel with internal asyncio EventBus, SQLite state, NATS messaging (mocked in tests). Terminal trigger sends command → Workflow engine builds DAG → Agent Runtime executes steps via LiteLLM + MCP client → Result displayed.

**Tech Stack:** Python 3.12, pydantic 2.x, asyncio, aiosqlite, nats-py, litellm, mcp SDK, aiohttp, pytest + anyio, ruff, pyrefly

---

## Phase 0: Foundation

### Task 1: Project scaffold and dependencies

**Files:**
- Modify: `pyproject.toml`
- Create: `src/proctor/__init__.py`
- Create: `src/proctor/__main__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Modify: `.gitignore`

**Step 1: Set up pyproject.toml with src layout and core dependencies**

```toml
[project]
name = "proctor"
version = "0.1.0"
description = "Distributed autonomous agent system"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.10",
    "aiosqlite>=0.20",
    "nats-py>=2.9",
    "litellm>=1.55",
    "tiktoken>=0.8",
    "mcp>=1.0",
    "aiohttp>=3.11",
    "pyyaml>=6.0",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "anyio[trio]>=4.0",
    "pytest-asyncio>=0.24",
    "ruff>=0.8",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/proctor"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
line-length = 88
src = ["src"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP"]
```

**Step 2: Create package structure**

```python
# src/proctor/__init__.py
"""Proctor: Distributed autonomous agent system."""

__version__ = "0.1.0"
```

```python
# src/proctor/__main__.py
"""Entrypoint: python -m proctor"""

import asyncio


def main() -> None:
    """Start Proctor."""
    print(f"Proctor starting...")
    asyncio.run(_run())


async def _run() -> None:
    """Async main loop placeholder."""
    print("Proctor running. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()
```

```python
# tests/__init__.py
```

```python
# tests/conftest.py
"""Shared test fixtures."""

import asyncio
from collections.abc import AsyncGenerator

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
```

Update `.gitignore` to add:
```
data/
*.db
*.age
```

**Step 3: Install dependencies**

Run: `uv sync`
Expected: all packages install successfully

**Step 4: Verify package runs**

Run: `uv run python -m proctor &; sleep 1; kill %1`
Expected: "Proctor starting..." output

**Step 5: Run ruff**

Run: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`
Expected: no errors

**Step 6: Commit**

```bash
git add src/ tests/ pyproject.toml .gitignore uv.lock
git commit -m "feat: project scaffold with src layout and dependencies"
```

---

### Task 2: Core models (Event, Task, Envelope)

**Files:**
- Create: `src/proctor/core/__init__.py`
- Create: `src/proctor/core/models.py`
- Create: `tests/test_core/__init__.py`
- Create: `tests/test_core/test_models.py`

**Step 1: Write failing tests for core models**

```python
# tests/test_core/__init__.py
```

```python
# tests/test_core/test_models.py
"""Tests for core pydantic models."""

from datetime import datetime, timezone

import pytest

from proctor.core.models import (
    Envelope,
    Event,
    Task,
    TaskStatus,
)


class TestEvent:
    def test_create_event(self):
        event = Event(
            type="trigger.telegram",
            source="telegram-trigger",
            payload={"text": "hello"},
        )
        assert event.id  # auto-generated UUID
        assert event.type == "trigger.telegram"
        assert event.timestamp <= datetime.now(timezone.utc)

    def test_event_id_is_unique(self):
        e1 = Event(type="test", source="test", payload={})
        e2 = Event(type="test", source="test", payload={})
        assert e1.id != e2.id


class TestTask:
    def test_create_task_minimal(self):
        task = Task(spec={"mode": "simple", "prompt": "hello"})
        assert task.status == TaskStatus.PENDING
        assert task.retries == 0
        assert task.worker_id is None

    def test_task_status_transitions(self):
        task = Task(spec={"mode": "simple", "prompt": "hello"})
        assert task.status == TaskStatus.PENDING
        task.status = TaskStatus.ASSIGNED
        assert task.status == TaskStatus.ASSIGNED


class TestTaskStatus:
    def test_all_statuses_exist(self):
        assert TaskStatus.PENDING
        assert TaskStatus.ASSIGNED
        assert TaskStatus.RUNNING
        assert TaskStatus.COMPLETED
        assert TaskStatus.FAILED


class TestEnvelope:
    def test_create_envelope(self):
        env = Envelope(
            type="task.assign",
            source="core",
            target="worker-1",
            payload={"task_id": "123"},
        )
        assert env.id
        assert env.reply_to is None
        assert env.ttl_seconds is None

    def test_envelope_with_correlation(self):
        env = Envelope(
            type="task.result",
            source="worker-1",
            payload={"result": "ok"},
            correlation_id="original-123",
        )
        assert env.correlation_id == "original-123"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core/test_models.py -v`
Expected: FAIL (module not found)

**Step 3: Implement core models**

```python
# src/proctor/core/__init__.py
"""Core module: EventBus, State, Config, Models."""
```

```python
# src/proctor/core/models.py
"""Core pydantic models: Event, Task, Envelope."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class TaskStatus(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Event(BaseModel):
    """Something that happened in the system."""

    id: str = Field(default_factory=_uuid)
    type: str
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utcnow)


class Task(BaseModel):
    """A unit of work to be executed by a worker."""

    id: str = Field(default_factory=_uuid)
    status: TaskStatus = TaskStatus.PENDING
    spec: dict[str, Any]
    trigger_event: str | None = None
    worker_id: str | None = None
    result: Any | None = None
    retries: int = 0
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    deadline: datetime | None = None


class Envelope(BaseModel):
    """NATS message wrapper."""

    id: str = Field(default_factory=_uuid)
    type: str
    source: str
    target: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    reply_to: str | None = None
    correlation_id: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)
    ttl_seconds: int | None = None
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_core/test_models.py -v`
Expected: all PASS

**Step 5: Lint and format**

Run: `uv run ruff check src/proctor/core/ && uv run ruff format src/proctor/core/`

**Step 6: Commit**

```bash
git add src/proctor/core/ tests/test_core/
git commit -m "feat(core): add Event, Task, Envelope pydantic models"
```

---

### Task 3: Config loading

**Files:**
- Create: `src/proctor/core/config.py`
- Create: `config/proctor.yaml`
- Create: `tests/test_core/test_config.py`

**Step 1: Write failing tests**

```python
# tests/test_core/test_config.py
"""Tests for config loading."""

from pathlib import Path

import pytest

from proctor.core.config import ProctorConfig, load_config


class TestProctorConfig:
    def test_default_config(self):
        cfg = ProctorConfig()
        assert cfg.node_role == "standalone"
        assert cfg.nats_url == "nats://localhost:4222"
        assert cfg.data_dir == Path("data")

    def test_load_from_yaml(self, tmp_path):
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(
            "node_role: core\n"
            "nats_url: nats://192.168.1.10:4222\n"
            "data_dir: /var/proctor/data\n"
        )
        cfg = load_config(yaml_file)
        assert cfg.node_role == "core"
        assert cfg.nats_url == "nats://192.168.1.10:4222"

    def test_load_missing_file_returns_default(self):
        cfg = load_config(Path("/nonexistent/path.yaml"))
        assert cfg.node_role == "standalone"

    def test_llm_config_defaults(self):
        cfg = ProctorConfig()
        assert cfg.llm.default_model is not None
        assert cfg.llm.max_tokens > 0
```

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_core/test_config.py -v`
Expected: FAIL

**Step 3: Implement config**

```python
# src/proctor/core/config.py
"""Configuration loading from YAML files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    default_model: str = "claude-sonnet-4-20250514"
    fallback_model: str = "ollama/llama3.2"
    max_tokens: int = 4096
    temperature: float = 0.7


class NATSConfig(BaseModel):
    """NATS connection settings."""

    url: str = "nats://localhost:4222"
    connect_timeout: float = 5.0
    reconnect_time_wait: float = 2.0
    max_reconnect_attempts: int = 60


class SchedulerConfig(BaseModel):
    """Scheduler settings."""

    poll_interval_seconds: int = 30
    enabled: bool = True


class ProctorConfig(BaseModel):
    """Root configuration for Proctor."""

    node_role: str = "standalone"  # standalone | core | worker
    node_id: str = "node-1"
    nats_url: str = "nats://localhost:4222"
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    llm: LLMConfig = Field(default_factory=LLMConfig)
    nats: NATSConfig = Field(default_factory=NATSConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)


def load_config(path: Path) -> ProctorConfig:
    """Load config from YAML. Returns defaults if file missing."""
    if not path.exists():
        logger.warning("Config file %s not found, using defaults", path)
        return ProctorConfig()

    with open(path) as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    return ProctorConfig(**raw)
```

```yaml
# config/proctor.yaml
node_role: standalone
node_id: node-local
nats_url: nats://localhost:4222
data_dir: data
log_level: INFO

llm:
  default_model: claude-sonnet-4-20250514
  fallback_model: ollama/llama3.2
  max_tokens: 4096

scheduler:
  poll_interval_seconds: 30
  enabled: true
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_core/test_config.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add src/proctor/core/config.py config/ tests/test_core/test_config.py
git commit -m "feat(core): config loading from YAML with pydantic models"
```

---

### Task 4: EventBus (internal async pub/sub)

**Files:**
- Create: `src/proctor/core/bus.py`
- Create: `tests/test_core/test_bus.py`

**Step 1: Write failing tests**

```python
# tests/test_core/test_bus.py
"""Tests for internal EventBus."""

import asyncio

import pytest

from proctor.core.bus import EventBus
from proctor.core.models import Event


@pytest.mark.asyncio
async def test_subscribe_and_publish():
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("test.topic", handler)
    event = Event(type="test.topic", source="test", payload={"x": 1})
    await bus.publish(event)

    await asyncio.sleep(0.05)
    assert len(received) == 1
    assert received[0].payload == {"x": 1}


@pytest.mark.asyncio
async def test_wildcard_subscribe():
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("trigger.*", handler)

    await bus.publish(Event(type="trigger.telegram", source="t", payload={}))
    await bus.publish(Event(type="trigger.webhook", source="t", payload={}))
    await bus.publish(Event(type="task.created", source="t", payload={}))

    await asyncio.sleep(0.05)
    assert len(received) == 2


@pytest.mark.asyncio
async def test_multiple_subscribers():
    bus = EventBus()
    counts = {"a": 0, "b": 0}

    async def handler_a(event: Event) -> None:
        counts["a"] += 1

    async def handler_b(event: Event) -> None:
        counts["b"] += 1

    bus.subscribe("test", handler_a)
    bus.subscribe("test", handler_b)

    await bus.publish(Event(type="test", source="t", payload={}))
    await asyncio.sleep(0.05)

    assert counts["a"] == 1
    assert counts["b"] == 1


@pytest.mark.asyncio
async def test_unsubscribe():
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    sub_id = bus.subscribe("test", handler)
    bus.unsubscribe(sub_id)

    await bus.publish(Event(type="test", source="t", payload={}))
    await asyncio.sleep(0.05)

    assert len(received) == 0


@pytest.mark.asyncio
async def test_handler_error_does_not_crash_bus():
    bus = EventBus()
    received: list[Event] = []

    async def bad_handler(event: Event) -> None:
        raise ValueError("oops")

    async def good_handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("test", bad_handler)
    bus.subscribe("test", good_handler)

    await bus.publish(Event(type="test", source="t", payload={}))
    await asyncio.sleep(0.05)

    assert len(received) == 1
```

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_core/test_bus.py -v`
Expected: FAIL

**Step 3: Implement EventBus**

```python
# src/proctor/core/bus.py
"""Internal async EventBus (intra-process pub/sub)."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from collections.abc import Awaitable, Callable

from proctor.core.models import Event

logger = logging.getLogger(__name__)

Handler = Callable[[Event], Awaitable[None]]


class _Subscription:
    def __init__(self, pattern: str, handler: Handler) -> None:
        self.id = str(uuid.uuid4())
        self.pattern = pattern
        self.handler = handler

    def matches(self, event_type: str) -> bool:
        return fnmatch.fnmatch(event_type, self.pattern)


class EventBus:
    """Simple async pub/sub for internal module communication."""

    def __init__(self) -> None:
        self._subscriptions: dict[str, _Subscription] = {}

    def subscribe(self, pattern: str, handler: Handler) -> str:
        """Subscribe to events matching pattern. Returns subscription ID."""
        sub = _Subscription(pattern, handler)
        self._subscriptions[sub.id] = sub
        return sub.id

    def unsubscribe(self, sub_id: str) -> None:
        """Remove a subscription by ID."""
        self._subscriptions.pop(sub_id, None)

    async def publish(self, event: Event) -> None:
        """Publish event to all matching subscribers."""
        for sub in list(self._subscriptions.values()):
            if sub.matches(event.type):
                asyncio.create_task(self._safe_call(sub, event))

    async def _safe_call(self, sub: _Subscription, event: Event) -> None:
        try:
            await sub.handler(event)
        except Exception:
            logger.exception(
                "Handler %s failed for event %s",
                sub.handler.__name__,
                event.type,
            )
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_core/test_bus.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add src/proctor/core/bus.py tests/test_core/test_bus.py
git commit -m "feat(core): async EventBus with wildcard subscriptions"
```

---

### Task 5: StateManager (SQLite wrapper)

**Files:**
- Create: `src/proctor/core/state.py`
- Create: `tests/test_core/test_state.py`

**Step 1: Write failing tests**

```python
# tests/test_core/test_state.py
"""Tests for SQLite state manager."""

import pytest

from proctor.core.models import Task, TaskStatus
from proctor.core.state import StateManager


@pytest.fixture
async def state(tmp_path):
    sm = StateManager(tmp_path / "test_state.db")
    await sm.initialize()
    yield sm
    await sm.close()


@pytest.mark.asyncio
async def test_initialize_creates_tables(state: StateManager):
    tables = await state.list_tables()
    assert "tasks" in tables
    assert "schedules" in tables
    assert "config_overrides" in tables


@pytest.mark.asyncio
async def test_save_and_get_task(state: StateManager):
    task = Task(spec={"mode": "simple", "prompt": "test"})
    await state.save_task(task)

    loaded = await state.get_task(task.id)
    assert loaded is not None
    assert loaded.id == task.id
    assert loaded.spec == task.spec
    assert loaded.status == TaskStatus.PENDING


@pytest.mark.asyncio
async def test_update_task_status(state: StateManager):
    task = Task(spec={"mode": "simple", "prompt": "test"})
    await state.save_task(task)

    task.status = TaskStatus.RUNNING
    task.worker_id = "worker-1"
    await state.save_task(task)

    loaded = await state.get_task(task.id)
    assert loaded is not None
    assert loaded.status == TaskStatus.RUNNING
    assert loaded.worker_id == "worker-1"


@pytest.mark.asyncio
async def test_list_tasks_by_status(state: StateManager):
    t1 = Task(spec={"mode": "simple", "prompt": "a"})
    t2 = Task(spec={"mode": "simple", "prompt": "b"}, status=TaskStatus.RUNNING)
    t3 = Task(spec={"mode": "simple", "prompt": "c"})

    await state.save_task(t1)
    await state.save_task(t2)
    await state.save_task(t3)

    pending = await state.list_tasks(status=TaskStatus.PENDING)
    assert len(pending) == 2

    running = await state.list_tasks(status=TaskStatus.RUNNING)
    assert len(running) == 1


@pytest.mark.asyncio
async def test_get_nonexistent_task_returns_none(state: StateManager):
    result = await state.get_task("nonexistent-id")
    assert result is None


@pytest.mark.asyncio
async def test_config_override_set_and_get(state: StateManager):
    await state.set_config("retry_limit", 5)
    val = await state.get_config("retry_limit")
    assert val == 5

    await state.set_config("retry_limit", 10)
    val = await state.get_config("retry_limit")
    assert val == 10


@pytest.mark.asyncio
async def test_config_get_missing_returns_default(state: StateManager):
    val = await state.get_config("nonexistent", default=42)
    assert val == 42
```

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_core/test_state.py -v`
Expected: FAIL

**Step 3: Implement StateManager**

```python
# src/proctor/core/state.py
"""SQLite state manager for tasks, schedules, and config."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from proctor.core.models import Task, TaskStatus

logger = logging.getLogger(__name__)

_SCHEMA = """
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
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    expression TEXT NOT NULL,
    tz TEXT DEFAULT 'UTC',
    workflow_id TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run TEXT,
    last_run TEXT
);

CREATE TABLE IF NOT EXISTS config_overrides (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""


class StateManager:
    """Async SQLite wrapper for persistent state."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open DB and create tables."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        """Close DB connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "StateManager not initialized"
        return self._db

    # --- Tasks ---

    async def save_task(self, task: Task) -> None:
        """Insert or update a task."""
        await self.db.execute(
            """INSERT INTO tasks
               (id, status, spec_json, trigger_event, worker_id,
                result_json, retries, created_at, updated_at, deadline)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 status=excluded.status,
                 worker_id=excluded.worker_id,
                 result_json=excluded.result_json,
                 retries=excluded.retries,
                 updated_at=excluded.updated_at,
                 deadline=excluded.deadline
            """,
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
                task.deadline.isoformat() if task.deadline else None,
            ),
        )
        await self.db.commit()

    async def get_task(self, task_id: str) -> Task | None:
        """Get task by ID."""
        cursor = await self.db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    async def list_tasks(
        self, status: TaskStatus | None = None
    ) -> list[Task]:
        """List tasks, optionally filtered by status."""
        if status:
            cursor = await self.db.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at",
                (status.value,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM tasks ORDER BY created_at"
            )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    def _row_to_task(self, row: aiosqlite.Row) -> Task:
        return Task(
            id=row["id"],
            status=TaskStatus(row["status"]),
            spec=json.loads(row["spec_json"]),
            trigger_event=row["trigger_event"],
            worker_id=row["worker_id"],
            result=json.loads(row["result_json"])
            if row["result_json"]
            else None,
            retries=row["retries"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deadline=row["deadline"],
        )

    # --- Config overrides ---

    async def set_config(self, key: str, value: Any) -> None:
        """Set a config override."""
        await self.db.execute(
            """INSERT INTO config_overrides (key, value_json)
               VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value_json=excluded.value_json,
                 updated_at=datetime('now')
            """,
            (key, json.dumps(value)),
        )
        await self.db.commit()

    async def get_config(
        self, key: str, default: Any = None
    ) -> Any:
        """Get a config override, or default."""
        cursor = await self.db.execute(
            "SELECT value_json FROM config_overrides WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return default
        return json.loads(row["value_json"])

    # --- Utility ---

    async def list_tables(self) -> list[str]:
        """List all tables (for testing)."""
        cursor = await self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = await cursor.fetchall()
        return [r["name"] for r in rows]
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_core/test_state.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add src/proctor/core/state.py tests/test_core/test_state.py
git commit -m "feat(core): SQLite StateManager for tasks and config"
```

---

### Task 6: Bootstrap (application startup and shutdown)

**Files:**
- Create: `src/proctor/core/bootstrap.py`
- Modify: `src/proctor/__main__.py`
- Create: `tests/test_core/test_bootstrap.py`

**Step 1: Write failing tests**

```python
# tests/test_core/test_bootstrap.py
"""Tests for application bootstrap."""

import asyncio

import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import ProctorConfig


@pytest.mark.asyncio
async def test_app_starts_and_stops(tmp_path):
    config = ProctorConfig(data_dir=tmp_path / "data")
    app = Application(config)

    await app.start()
    assert app.bus is not None
    assert app.state is not None
    assert app.is_running

    await app.stop()
    assert not app.is_running


@pytest.mark.asyncio
async def test_app_state_db_created(tmp_path):
    config = ProctorConfig(data_dir=tmp_path / "data")
    app = Application(config)

    await app.start()
    assert (tmp_path / "data" / "state.db").exists()
    await app.stop()


@pytest.mark.asyncio
async def test_app_event_bus_works(tmp_path):
    config = ProctorConfig(data_dir=tmp_path / "data")
    app = Application(config)
    await app.start()

    received = []
    from proctor.core.models import Event

    async def handler(event: Event) -> None:
        received.append(event)

    app.bus.subscribe("test", handler)
    await app.bus.publish(Event(type="test", source="test", payload={}))
    await asyncio.sleep(0.05)

    assert len(received) == 1
    await app.stop()
```

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_core/test_bootstrap.py -v`
Expected: FAIL

**Step 3: Implement bootstrap**

```python
# src/proctor/core/bootstrap.py
"""Application lifecycle: start, run, stop."""

from __future__ import annotations

import logging

from proctor.core.bus import EventBus
from proctor.core.config import ProctorConfig
from proctor.core.state import StateManager

logger = logging.getLogger(__name__)


class Application:
    """Main application container. Owns all core components."""

    def __init__(self, config: ProctorConfig) -> None:
        self.config = config
        self.bus = EventBus()
        self.state = StateManager(config.data_dir / "state.db")
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Initialize all core components."""
        logger.info("Starting Proctor (node=%s, role=%s)",
                     self.config.node_id, self.config.node_role)

        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        await self.state.initialize()
        self._running = True

        logger.info("Proctor started")

    async def stop(self) -> None:
        """Gracefully shut down all components."""
        logger.info("Stopping Proctor...")
        self._running = False
        await self.state.close()
        logger.info("Proctor stopped")
```

Update `src/proctor/__main__.py`:

```python
# src/proctor/__main__.py
"""Entrypoint: python -m proctor"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from proctor.core.bootstrap import Application
from proctor.core.config import load_config


def main() -> None:
    """Start Proctor."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config_path = Path("config/proctor.yaml")
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--config" and i < len(sys.argv):
            config_path = Path(sys.argv[i + 1])

    config = load_config(config_path)
    asyncio.run(_run(config))


async def _run(config) -> None:
    """Async main loop."""
    app = Application(config)
    loop = asyncio.get_event_loop()

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await app.start()
    try:
        await stop_event.wait()
    finally:
        await app.stop()


if __name__ == "__main__":
    main()
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_core/test_bootstrap.py -v`
Expected: all PASS

**Step 5: Run all core tests**

Run: `uv run pytest tests/test_core/ -v`
Expected: all PASS

**Step 6: Lint + format full project**

Run: `uv run ruff check src/ tests/ && uv run ruff format src/ tests/`

**Step 7: Commit**

```bash
git add src/proctor/core/bootstrap.py src/proctor/__main__.py tests/test_core/test_bootstrap.py
git commit -m "feat(core): Application bootstrap with startup/shutdown lifecycle"
```

---

**Phase 0 complete.** At this point we have:
- Working package with `python -m proctor`
- Core models (Event, Task, Envelope)
- Config loading from YAML
- Internal EventBus with wildcard pub/sub
- SQLite StateManager for tasks and config
- Application lifecycle (start/stop)
- Full test coverage for all of the above

---

## Phase 1: MVP — Planner + Executor

### Task 7: WorkflowSpec model

**Files:**
- Create: `src/proctor/workflow/__init__.py`
- Create: `src/proctor/workflow/spec.py`
- Create: `tests/test_workflow/__init__.py`
- Create: `tests/test_workflow/test_spec.py`

**Step 1: Write failing tests**

```python
# tests/test_workflow/__init__.py
```

```python
# tests/test_workflow/test_spec.py
"""Tests for WorkflowSpec model."""

import json

import pytest

from proctor.workflow.spec import (
    Step,
    StepType,
    WorkflowMode,
    WorkflowPolicies,
    WorkflowSpec,
)


class TestWorkflowSpec:
    def test_simple_spec(self):
        spec = WorkflowSpec(
            workflow_id="test",
            mode=WorkflowMode.SIMPLE,
            description="Just do it",
            prompt="Tell me about RISC-V",
        )
        assert spec.mode == WorkflowMode.SIMPLE
        assert spec.prompt is not None

    def test_dag_spec_with_steps(self):
        spec = WorkflowSpec(
            workflow_id="news",
            mode=WorkflowMode.DAG,
            description="Daily news",
            steps=[
                Step(
                    id="plan",
                    type=StepType.LLM,
                    description="Plan collection",
                    depends_on=[],
                ),
                Step(
                    id="collect",
                    type=StepType.LLM,
                    description="Collect news",
                    depends_on=["plan"],
                ),
                Step(
                    id="deliver",
                    type=StepType.SYSTEM,
                    description="Send report",
                    depends_on=["collect"],
                ),
            ],
        )
        assert len(spec.steps) == 3
        assert spec.steps[1].depends_on == ["plan"]

    def test_policies_defaults(self):
        policies = WorkflowPolicies()
        assert policies.max_retries >= 0
        assert policies.max_runtime_seconds > 0

    def test_spec_serialization_roundtrip(self):
        spec = WorkflowSpec(
            workflow_id="test",
            mode=WorkflowMode.DAG,
            description="Test",
            steps=[
                Step(id="s1", type=StepType.LLM, description="step 1"),
            ],
        )
        data = spec.model_dump()
        restored = WorkflowSpec(**data)
        assert restored.workflow_id == spec.workflow_id
        assert len(restored.steps) == 1

    def test_step_inputs_outputs(self):
        step = Step(
            id="analyze",
            type=StepType.LLM,
            description="Analyze data",
            inputs={"data": "{{steps.collect.outputs.raw}}"},
            outputs={"summary": "$.summary"},
        )
        assert "data" in step.inputs
        assert "summary" in step.outputs
```

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_workflow/test_spec.py -v`
Expected: FAIL

**Step 3: Implement WorkflowSpec**

```python
# src/proctor/workflow/__init__.py
"""Workflow module: spec, engine, DAG/FSM/DT executors."""
```

```python
# src/proctor/workflow/spec.py
"""WorkflowSpec pydantic model — the JSON format for all pipeline types."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WorkflowMode(StrEnum):
    SIMPLE = "simple"
    DAG = "dag"
    FSM = "fsm"
    ORCHESTRATOR = "orchestrator"


class StepType(StrEnum):
    LLM = "llm"
    SHELL = "shell"
    HTTP = "http"
    SYSTEM = "system"
    WAIT_EVENT = "wait_event"


class StepRetry(BaseModel):
    max_retries: int = 1
    retry_on: list[str] = Field(default_factory=list)


class Step(BaseModel):
    """A single step in a DAG workflow."""

    id: str
    type: StepType = StepType.LLM
    description: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    tool: str | None = None
    retry: StepRetry = Field(default_factory=StepRetry)
    conditions: dict[str, Any] = Field(default_factory=dict)


class WorkflowPolicies(BaseModel):
    max_runtime_seconds: int = 900
    max_retries: int = 2
    retry_delay_seconds: int = 60
    require_security_review: bool = False
    require_llm_quality_review: bool = False


class WorkflowSpec(BaseModel):
    """Universal workflow specification (DAG, FSM, orchestrator, or simple)."""

    workflow_id: str
    version: str = "1.0.0"
    mode: WorkflowMode
    description: str = ""

    # Simple mode: just a prompt
    prompt: str | None = None

    # DAG mode: ordered steps
    steps: list[Step] = Field(default_factory=list)

    # FSM mode: states (Phase 4, placeholder)
    states: dict[str, Any] = Field(default_factory=dict)
    start_at: str | None = None

    # Orchestrator mode: agent definitions (Phase 4, placeholder)
    orchestrator: dict[str, Any] = Field(default_factory=dict)
    agents: list[dict[str, Any]] = Field(default_factory=list)

    # Shared
    policies: WorkflowPolicies = Field(default_factory=WorkflowPolicies)
    security: dict[str, Any] = Field(default_factory=dict)
    channels: dict[str, Any] = Field(default_factory=dict)
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_workflow/test_spec.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add src/proctor/workflow/ tests/test_workflow/
git commit -m "feat(workflow): WorkflowSpec model with DAG steps and policies"
```

---

### Task 8: DAG executor

**Files:**
- Create: `src/proctor/workflow/dag.py`
- Create: `tests/test_workflow/test_dag.py`

**Step 1: Write failing tests**

```python
# tests/test_workflow/test_dag.py
"""Tests for DAG executor."""

import pytest

from proctor.workflow.dag import topo_sort, DAGExecutor, StepResult
from proctor.workflow.spec import Step, StepType


class TestTopoSort:
    def test_linear_chain(self):
        steps = [
            Step(id="a", type=StepType.LLM, depends_on=[]),
            Step(id="b", type=StepType.LLM, depends_on=["a"]),
            Step(id="c", type=StepType.LLM, depends_on=["b"]),
        ]
        order = topo_sort(steps)
        ids = [s.id for s in order]
        assert ids.index("a") < ids.index("b") < ids.index("c")

    def test_parallel_steps(self):
        steps = [
            Step(id="a", type=StepType.LLM, depends_on=[]),
            Step(id="b", type=StepType.LLM, depends_on=["a"]),
            Step(id="c", type=StepType.LLM, depends_on=["a"]),
            Step(id="d", type=StepType.LLM, depends_on=["b", "c"]),
        ]
        order = topo_sort(steps)
        ids = [s.id for s in order]
        assert ids[0] == "a"
        assert ids[-1] == "d"

    def test_cycle_detection(self):
        steps = [
            Step(id="a", type=StepType.LLM, depends_on=["b"]),
            Step(id="b", type=StepType.LLM, depends_on=["a"]),
        ]
        with pytest.raises(ValueError, match="cycle"):
            topo_sort(steps)

    def test_single_step(self):
        steps = [Step(id="only", type=StepType.LLM)]
        order = topo_sort(steps)
        assert len(order) == 1


class TestDAGExecutor:
    @pytest.mark.asyncio
    async def test_execute_linear_dag(self):
        steps = [
            Step(id="a", type=StepType.LLM, description="step a"),
            Step(id="b", type=StepType.LLM, description="step b", depends_on=["a"]),
        ]

        call_order: list[str] = []

        async def mock_runner(step: Step, context: dict) -> StepResult:
            call_order.append(step.id)
            return StepResult(step_id=step.id, output=f"result_{step.id}")

        executor = DAGExecutor(steps=steps, step_runner=mock_runner)
        results = await executor.execute()

        assert call_order == ["a", "b"]
        assert results["a"].output == "result_a"
        assert results["b"].output == "result_b"

    @pytest.mark.asyncio
    async def test_execute_parallel_dag(self):
        steps = [
            Step(id="root", type=StepType.LLM),
            Step(id="left", type=StepType.LLM, depends_on=["root"]),
            Step(id="right", type=StepType.LLM, depends_on=["root"]),
            Step(id="join", type=StepType.LLM, depends_on=["left", "right"]),
        ]

        call_order: list[str] = []

        async def mock_runner(step: Step, context: dict) -> StepResult:
            call_order.append(step.id)
            return StepResult(step_id=step.id, output=f"result_{step.id}")

        executor = DAGExecutor(steps=steps, step_runner=mock_runner)
        results = await executor.execute()

        assert call_order[0] == "root"
        assert call_order[-1] == "join"
        assert set(call_order[1:3]) == {"left", "right"}
        assert len(results) == 4

    @pytest.mark.asyncio
    async def test_step_failure_stops_dependents(self):
        steps = [
            Step(id="a", type=StepType.LLM),
            Step(id="b", type=StepType.LLM, depends_on=["a"]),
        ]

        async def failing_runner(step: Step, context: dict) -> StepResult:
            if step.id == "a":
                return StepResult(
                    step_id=step.id, output=None, error="boom"
                )
            return StepResult(step_id=step.id, output="ok")

        executor = DAGExecutor(steps=steps, step_runner=failing_runner)
        results = await executor.execute()

        assert results["a"].error == "boom"
        assert results["b"].error is not None  # skipped due to dep failure
```

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_workflow/test_dag.py -v`
Expected: FAIL

**Step 3: Implement DAG executor**

```python
# src/proctor/workflow/dag.py
"""DAG executor: topological sort + parallel step execution."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from proctor.workflow.spec import Step

logger = logging.getLogger(__name__)


class StepResult(BaseModel):
    step_id: str
    output: Any | None = None
    error: str | None = None


StepRunner = Callable[[Step, dict[str, StepResult]], Awaitable[StepResult]]


def topo_sort(steps: list[Step]) -> list[Step]:
    """Topological sort with cycle detection. Returns ordered steps."""
    graph: dict[str, list[str]] = {s.id: list(s.depends_on) for s in steps}
    step_map = {s.id: s for s in steps}

    visited: set[str] = set()
    in_stack: set[str] = set()
    order: list[str] = []

    def dfs(node: str) -> None:
        if node in in_stack:
            raise ValueError(f"DAG contains a cycle involving '{node}'")
        if node in visited:
            return
        in_stack.add(node)
        for dep in graph.get(node, []):
            dfs(dep)
        in_stack.discard(node)
        visited.add(node)
        order.append(node)

    for node_id in graph:
        dfs(node_id)

    return [step_map[sid] for sid in order]


class DAGExecutor:
    """Execute a DAG of steps with dependency-based parallelism."""

    def __init__(
        self,
        steps: list[Step],
        step_runner: StepRunner,
    ) -> None:
        self._steps = steps
        self._step_runner = step_runner
        self._step_map = {s.id: s for s in steps}

    async def execute(self) -> dict[str, StepResult]:
        """Run all steps respecting dependencies. Returns results dict."""
        # Validate DAG (will raise on cycle)
        topo_sort(self._steps)

        results: dict[str, StepResult] = {}
        pending: dict[str, asyncio.Event] = {
            s.id: asyncio.Event() for s in self._steps
        }

        async def run_step(step: Step) -> None:
            # Wait for all dependencies
            for dep_id in step.depends_on:
                await pending[dep_id].wait()

                # Check if dependency failed
                if results.get(dep_id) and results[dep_id].error:
                    results[step.id] = StepResult(
                        step_id=step.id,
                        error=f"Skipped: dependency '{dep_id}' failed",
                    )
                    pending[step.id].set()
                    return

            # Execute
            try:
                result = await self._step_runner(step, results)
                results[step.id] = result
            except Exception as exc:
                logger.exception("Step %s failed", step.id)
                results[step.id] = StepResult(
                    step_id=step.id, error=str(exc)
                )
            finally:
                pending[step.id].set()

        # Launch all steps concurrently; they wait for deps internally
        async with asyncio.TaskGroup() as tg:
            for step in self._steps:
                tg.create_task(run_step(step))

        return results
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_workflow/test_dag.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add src/proctor/workflow/dag.py tests/test_workflow/test_dag.py
git commit -m "feat(workflow): DAG executor with topo-sort and parallel execution"
```

---

### Task 9: Workflow engine (dispatcher)

**Files:**
- Create: `src/proctor/workflow/engine.py`
- Create: `tests/test_workflow/test_engine.py`

**Step 1: Write failing tests**

```python
# tests/test_workflow/test_engine.py
"""Tests for workflow engine dispatcher."""

import pytest

from proctor.workflow.dag import StepResult
from proctor.workflow.engine import WorkflowEngine
from proctor.workflow.spec import Step, StepType, WorkflowMode, WorkflowSpec


@pytest.mark.asyncio
async def test_execute_simple_workflow():
    spec = WorkflowSpec(
        workflow_id="test",
        mode=WorkflowMode.SIMPLE,
        description="Simple test",
        prompt="Hello world",
    )

    call_log: list[str] = []

    async def mock_llm_call(prompt: str) -> str:
        call_log.append(prompt)
        return "LLM says hello"

    engine = WorkflowEngine(llm_call=mock_llm_call)
    result = await engine.execute(spec)

    assert result.output is not None
    assert "Hello world" in call_log[0]


@pytest.mark.asyncio
async def test_execute_dag_workflow():
    spec = WorkflowSpec(
        workflow_id="test-dag",
        mode=WorkflowMode.DAG,
        description="DAG test",
        steps=[
            Step(id="s1", type=StepType.LLM, description="first step"),
            Step(
                id="s2",
                type=StepType.LLM,
                description="second step",
                depends_on=["s1"],
            ),
        ],
    )

    async def mock_llm_call(prompt: str) -> str:
        return f"result for: {prompt}"

    engine = WorkflowEngine(llm_call=mock_llm_call)
    result = await engine.execute(spec)

    assert result.output is not None
    assert result.error is None


@pytest.mark.asyncio
async def test_unsupported_mode_returns_error():
    spec = WorkflowSpec(
        workflow_id="test",
        mode=WorkflowMode.FSM,
        description="Not implemented yet",
    )

    async def mock_llm_call(prompt: str) -> str:
        return ""

    engine = WorkflowEngine(llm_call=mock_llm_call)
    result = await engine.execute(spec)

    assert result.error is not None
    assert "not supported" in result.error.lower()
```

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_workflow/test_engine.py -v`
Expected: FAIL

**Step 3: Implement workflow engine**

```python
# src/proctor/workflow/engine.py
"""Workflow engine: dispatches spec to the appropriate executor."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from proctor.workflow.dag import DAGExecutor, StepResult
from proctor.workflow.spec import Step, WorkflowMode, WorkflowSpec

logger = logging.getLogger(__name__)

LLMCall = Callable[[str], Awaitable[str]]


class WorkflowResult(BaseModel):
    workflow_id: str
    output: Any | None = None
    step_results: dict[str, StepResult] | None = None
    error: str | None = None


class WorkflowEngine:
    """Dispatches WorkflowSpec to the appropriate executor."""

    def __init__(self, llm_call: LLMCall) -> None:
        self._llm_call = llm_call

    async def execute(self, spec: WorkflowSpec) -> WorkflowResult:
        """Execute a workflow spec."""
        logger.info("Executing workflow %s (mode=%s)", spec.workflow_id, spec.mode)

        match spec.mode:
            case WorkflowMode.SIMPLE:
                return await self._execute_simple(spec)
            case WorkflowMode.DAG:
                return await self._execute_dag(spec)
            case _:
                return WorkflowResult(
                    workflow_id=spec.workflow_id,
                    error=f"Mode '{spec.mode}' not supported yet",
                )

    async def _execute_simple(self, spec: WorkflowSpec) -> WorkflowResult:
        """Execute a simple single-prompt workflow."""
        if not spec.prompt:
            return WorkflowResult(
                workflow_id=spec.workflow_id,
                error="Simple workflow requires a prompt",
            )
        try:
            result = await self._llm_call(spec.prompt)
            return WorkflowResult(
                workflow_id=spec.workflow_id, output=result
            )
        except Exception as exc:
            return WorkflowResult(
                workflow_id=spec.workflow_id, error=str(exc)
            )

    async def _execute_dag(self, spec: WorkflowSpec) -> WorkflowResult:
        """Execute a DAG workflow."""
        async def step_runner(
            step: Step, context: dict[str, StepResult]
        ) -> StepResult:
            prompt = step.description
            # Inject dependency outputs into prompt
            for dep_id in step.depends_on:
                dep_result = context.get(dep_id)
                if dep_result and dep_result.output:
                    prompt += f"\n\nContext from {dep_id}: {dep_result.output}"

            try:
                output = await self._llm_call(prompt)
                return StepResult(step_id=step.id, output=output)
            except Exception as exc:
                return StepResult(step_id=step.id, error=str(exc))

        executor = DAGExecutor(
            steps=spec.steps, step_runner=step_runner
        )
        try:
            results = await executor.execute()
            # Final output is the last step's output
            last_step = spec.steps[-1] if spec.steps else None
            output = (
                results[last_step.id].output if last_step else None
            )
            return WorkflowResult(
                workflow_id=spec.workflow_id,
                output=output,
                step_results=results,
            )
        except Exception as exc:
            return WorkflowResult(
                workflow_id=spec.workflow_id, error=str(exc)
            )
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_workflow/test_engine.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add src/proctor/workflow/engine.py tests/test_workflow/test_engine.py
git commit -m "feat(workflow): WorkflowEngine dispatcher for simple and DAG modes"
```

---

### Task 10: Agent Runtime (LLM loop with tool calls)

**Files:**
- Create: `src/proctor/workers/__init__.py`
- Create: `src/proctor/workers/runtime.py`
- Create: `tests/test_workers/__init__.py`
- Create: `tests/test_workers/test_runtime.py`

**Step 1: Write failing tests**

```python
# tests/test_workers/__init__.py
```

```python
# tests/test_workers/test_runtime.py
"""Tests for Agent Runtime (LLM loop)."""

import pytest

from proctor.workers.runtime import AgentRuntime, ToolDef, ToolResult


class TestAgentRuntime:
    @pytest.mark.asyncio
    async def test_simple_completion_no_tools(self):
        responses = iter(["The answer is 42."])

        async def mock_llm(messages, tools=None):
            return {"type": "text", "content": next(responses)}

        runtime = AgentRuntime(llm_fn=mock_llm, tools=[], max_turns=5)
        result = await runtime.run("What is the meaning of life?")

        assert result.output == "The answer is 42."
        assert result.turns == 1

    @pytest.mark.asyncio
    async def test_tool_call_and_response(self):
        call_count = 0

        async def mock_llm(messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "type": "tool_call",
                    "tool_name": "get_weather",
                    "tool_args": {"city": "Tbilisi"},
                }
            return {"type": "text", "content": "Weather in Tbilisi: 15C"}

        async def weather_fn(city: str) -> str:
            return f"Temperature in {city}: 15C"

        tool = ToolDef(
            name="get_weather",
            description="Get weather for city",
            handler=weather_fn,
        )

        runtime = AgentRuntime(llm_fn=mock_llm, tools=[tool], max_turns=5)
        result = await runtime.run("What's the weather?")

        assert "15C" in result.output
        assert result.turns == 2

    @pytest.mark.asyncio
    async def test_max_turns_limit(self):
        async def infinite_tool_calls(messages, tools=None):
            return {
                "type": "tool_call",
                "tool_name": "noop",
                "tool_args": {},
            }

        async def noop_handler() -> str:
            return "done"

        tool = ToolDef(name="noop", description="noop", handler=noop_handler)
        runtime = AgentRuntime(
            llm_fn=infinite_tool_calls, tools=[tool], max_turns=3
        )
        result = await runtime.run("loop forever")

        assert result.turns == 3
        assert result.output is not None  # should return last state

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        call_count = 0

        async def mock_llm(messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "type": "tool_call",
                    "tool_name": "nonexistent",
                    "tool_args": {},
                }
            return {"type": "text", "content": "ok"}

        runtime = AgentRuntime(llm_fn=mock_llm, tools=[], max_turns=5)
        result = await runtime.run("test")

        # Should recover and return text on second turn
        assert result.output == "ok"
```

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_workers/test_runtime.py -v`
Expected: FAIL

**Step 3: Implement AgentRuntime**

```python
# src/proctor/workers/__init__.py
"""Workers module: Agent Runtime and worker launchers."""
```

```python
# src/proctor/workers/runtime.py
"""Agent Runtime: LLM loop with tool calling."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ToolDef(BaseModel):
    """A tool available to the agent."""

    name: str
    description: str
    handler: Any  # Callable, but pydantic can't validate callables easily

    model_config = {"arbitrary_types_allowed": True}


class ToolResult(BaseModel):
    tool_name: str
    output: str | None = None
    error: str | None = None


class AgentResult(BaseModel):
    output: str
    turns: int
    tool_calls: list[ToolResult] = []


# LLM function signature: (messages, tools) -> response dict
LLMFn = Callable[[list[dict], list[dict] | None], Awaitable[dict[str, Any]]]


class AgentRuntime:
    """LLM agent loop: prompt → tool calls → result."""

    def __init__(
        self,
        llm_fn: LLMFn,
        tools: list[ToolDef],
        max_turns: int = 10,
    ) -> None:
        self._llm_fn = llm_fn
        self._tools = {t.name: t for t in tools}
        self._max_turns = max_turns

    async def run(self, instruction: str) -> AgentResult:
        """Run the agent loop until text response or max turns."""
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": instruction}
        ]
        tool_defs = [
            {"name": t.name, "description": t.description}
            for t in self._tools.values()
        ] or None

        tool_results: list[ToolResult] = []
        turns = 0

        for _ in range(self._max_turns):
            turns += 1
            response = await self._llm_fn(messages, tool_defs)

            if response["type"] == "text":
                return AgentResult(
                    output=response["content"],
                    turns=turns,
                    tool_calls=tool_results,
                )

            if response["type"] == "tool_call":
                tool_name = response["tool_name"]
                tool_args = response.get("tool_args", {})

                result = await self._call_tool(tool_name, tool_args)
                tool_results.append(result)

                # Add tool call and result to messages
                messages.append({
                    "role": "assistant",
                    "content": f"[tool_call: {tool_name}({tool_args})]",
                })
                messages.append({
                    "role": "tool",
                    "content": result.output or result.error or "",
                })

        # Max turns reached — return what we have
        last_content = messages[-1].get("content", "Max turns reached")
        return AgentResult(
            output=last_content,
            turns=turns,
            tool_calls=tool_results,
        )

    async def _call_tool(
        self, name: str, args: dict[str, Any]
    ) -> ToolResult:
        """Execute a tool call."""
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(
                tool_name=name,
                error=f"Unknown tool: {name}",
            )

        try:
            output = await tool.handler(**args)
            return ToolResult(tool_name=name, output=str(output))
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            return ToolResult(tool_name=name, error=str(exc))
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_workers/test_runtime.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add src/proctor/workers/ tests/test_workers/
git commit -m "feat(workers): AgentRuntime with LLM loop and tool calling"
```

---

### Task 11: Terminal trigger

**Files:**
- Create: `src/proctor/triggers/__init__.py`
- Create: `src/proctor/triggers/base.py`
- Create: `src/proctor/triggers/terminal.py`
- Create: `tests/test_triggers/__init__.py`
- Create: `tests/test_triggers/test_terminal.py`

**Step 1: Write failing tests**

```python
# tests/test_triggers/__init__.py
```

```python
# tests/test_triggers/test_terminal.py
"""Tests for terminal trigger."""

import asyncio

import pytest

from proctor.core.bus import EventBus
from proctor.core.models import Event
from proctor.triggers.terminal import TerminalTrigger


@pytest.mark.asyncio
async def test_terminal_trigger_processes_line():
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("trigger.terminal", handler)

    trigger = TerminalTrigger()
    # Simulate a line of input (without actually reading stdin)
    await trigger._process_line("search for RISC-V info", bus)

    await asyncio.sleep(0.05)
    assert len(received) == 1
    assert received[0].payload["text"] == "search for RISC-V info"
    assert received[0].source == "terminal"


@pytest.mark.asyncio
async def test_terminal_trigger_ignores_empty_lines():
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("trigger.terminal", handler)

    trigger = TerminalTrigger()
    await trigger._process_line("", bus)
    await trigger._process_line("   ", bus)

    await asyncio.sleep(0.05)
    assert len(received) == 0


@pytest.mark.asyncio
async def test_terminal_trigger_quit_command():
    bus = EventBus()
    trigger = TerminalTrigger()

    result = await trigger._process_line("/quit", bus)
    assert result == "quit"
```

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_triggers/test_terminal.py -v`
Expected: FAIL

**Step 3: Implement terminal trigger**

```python
# src/proctor/triggers/__init__.py
"""Triggers module: input adapters for external events."""
```

```python
# src/proctor/triggers/base.py
"""Base class for all triggers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from proctor.core.bus import EventBus


class Trigger(ABC):
    """Abstract base for event triggers."""

    @abstractmethod
    async def start(self, bus: EventBus) -> None:
        """Start listening for events."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop listening."""
        ...
```

```python
# src/proctor/triggers/terminal.py
"""Terminal trigger: reads commands from stdin."""

from __future__ import annotations

import asyncio
import logging
import sys

from proctor.core.bus import EventBus
from proctor.core.models import Event
from proctor.triggers.base import Trigger

logger = logging.getLogger(__name__)


class TerminalTrigger(Trigger):
    """Reads lines from stdin and publishes as events."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self, bus: EventBus) -> None:
        self._running = True
        self._task = asyncio.create_task(self._read_loop(bus))
        logger.info("Terminal trigger started. Type commands or /quit to exit.")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _read_loop(self, bus: EventBus) -> None:
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while self._running:
            try:
                line_bytes = await reader.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode().strip()
                result = await self._process_line(line, bus)
                if result == "quit":
                    self._running = False
                    break
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error reading terminal input")

    async def _process_line(
        self, line: str, bus: EventBus
    ) -> str | None:
        """Process a single line of input. Returns 'quit' to exit."""
        stripped = line.strip()

        if not stripped:
            return None

        if stripped.lower() in ("/quit", "/exit", "/q"):
            return "quit"

        event = Event(
            type="trigger.terminal",
            source="terminal",
            payload={"text": stripped},
        )
        await bus.publish(event)
        return None
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_triggers/test_terminal.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add src/proctor/triggers/ tests/test_triggers/
git commit -m "feat(triggers): Terminal trigger with stdin reader"
```

---

### Task 12: Integration — wire everything together

**Files:**
- Modify: `src/proctor/core/bootstrap.py` (add workflow engine, terminal trigger)
- Create: `tests/test_integration.py`

**Step 1: Write failing integration test**

```python
# tests/test_integration.py
"""Integration test: terminal command → workflow → result."""

import asyncio

import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import ProctorConfig
from proctor.core.models import Event


@pytest.mark.asyncio
async def test_terminal_command_executes_simple_workflow(tmp_path):
    """Simulate: operator types command → task created → workflow executed."""
    config = ProctorConfig(data_dir=tmp_path / "data")
    app = Application(config)
    await app.start()

    results: list[str] = []

    # Register a mock LLM
    async def mock_llm(prompt: str) -> str:
        return f"Mock result for: {prompt}"

    app.set_llm_call(mock_llm)

    # Subscribe to results
    async def on_result(event: Event) -> None:
        results.append(event.payload.get("output", ""))

    app.bus.subscribe("task.completed", on_result)

    # Simulate terminal input
    event = Event(
        type="trigger.terminal",
        source="terminal",
        payload={"text": "Tell me about RISC-V"},
    )
    await app.bus.publish(event)

    # Wait for processing
    await asyncio.sleep(0.2)

    assert len(results) == 1
    assert "RISC-V" in results[0]

    await app.stop()
```

**Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_integration.py -v`
Expected: FAIL (set_llm_call not implemented)

**Step 3: Update bootstrap to wire components**

Update `src/proctor/core/bootstrap.py`:

```python
# src/proctor/core/bootstrap.py
"""Application lifecycle: start, run, stop."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from proctor.core.bus import EventBus
from proctor.core.config import ProctorConfig
from proctor.core.models import Event
from proctor.core.state import StateManager
from proctor.workflow.engine import WorkflowEngine, WorkflowResult
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

logger = logging.getLogger(__name__)

LLMCall = Callable[[str], Awaitable[str]]


class Application:
    """Main application container. Owns all core components."""

    def __init__(self, config: ProctorConfig) -> None:
        self.config = config
        self.bus = EventBus()
        self.state = StateManager(config.data_dir / "state.db")
        self._running = False
        self._llm_call: LLMCall | None = None
        self._engine: WorkflowEngine | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def set_llm_call(self, llm_call: LLMCall) -> None:
        """Set the LLM function and initialize the workflow engine."""
        self._llm_call = llm_call
        self._engine = WorkflowEngine(llm_call=llm_call)

    async def start(self) -> None:
        """Initialize all core components."""
        logger.info(
            "Starting Proctor (node=%s, role=%s)",
            self.config.node_id,
            self.config.node_role,
        )

        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        await self.state.initialize()

        # Wire: terminal trigger events → task execution
        self.bus.subscribe("trigger.terminal", self._handle_terminal)

        self._running = True
        logger.info("Proctor started")

    async def stop(self) -> None:
        """Gracefully shut down all components."""
        logger.info("Stopping Proctor...")
        self._running = False
        await self.state.close()
        logger.info("Proctor stopped")

    async def _handle_terminal(self, event: Event) -> None:
        """Handle a terminal command: create simple workflow and execute."""
        text = event.payload.get("text", "")
        if not text or not self._engine:
            return

        spec = WorkflowSpec(
            workflow_id=f"terminal-{event.id}",
            mode=WorkflowMode.SIMPLE,
            description="Terminal command",
            prompt=text,
        )

        result = await self._engine.execute(spec)

        # Publish result event
        await self.bus.publish(
            Event(
                type="task.completed" if not result.error else "task.failed",
                source="workflow-engine",
                payload={
                    "workflow_id": result.workflow_id,
                    "output": result.output,
                    "error": result.error,
                },
            )
        )
```

**Step 4: Run integration test**

Run: `uv run pytest tests/test_integration.py -v`
Expected: PASS

**Step 5: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: all PASS

**Step 6: Lint and format**

Run: `uv run ruff check src/ tests/ && uv run ruff format src/ tests/`

**Step 7: Commit**

```bash
git add src/proctor/core/bootstrap.py tests/test_integration.py
git commit -m "feat: wire terminal trigger → workflow engine → result events"
```

---

**Phase 1 MVP complete.** At this point the system can:
- Start as `python -m proctor`
- Accept terminal commands
- Execute simple (single-prompt) and DAG workflows
- Run an LLM agent loop with tool calling
- Store task state in SQLite
- Publish events through internal EventBus
- All with tests

### What's next

Phase 2 (Proactivity) adds scheduler, Telegram trigger, router with invariants, and episodic memory. These build on the foundation established here without modifying core interfaces.
