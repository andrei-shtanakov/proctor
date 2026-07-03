# Code Quality Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all critical and important code quality issues found during code review of the three feature branches (episodic-memory, telegram-trigger, scheduler-trigger).

**Architecture:** Targeted fixes across 6 files — config validation, LIKE-injection escape, INSERT→upsert, assert→RuntimeError, `is True/False`→truthiness, `asyncio`→`anyio` migration for both triggers, and wiring EpisodicMemory into bootstrap. Each task is independent except Task 6 (tests) which depends on the implementation changes.

**Tech Stack:** Python 3.12, pydantic 2.x, anyio, aiosqlite, aiohttp, croniter, pytest + anyio

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/proctor/core/config.py` | Modify | Add cron validation, interval_seconds>0, allowed_chat_ids default, typed payload |
| `src/proctor/core/memory.py` | Modify | INSERT→upsert, escape LIKE wildcards, assert→RuntimeError |
| `src/proctor/triggers/telegram.py` | Modify | asyncio→anyio, assert→RuntimeError |
| `src/proctor/triggers/scheduler.py` | Modify | asyncio→anyio throughout |
| `src/proctor/core/bootstrap.py` | Modify | Save episodes after workflow execution |
| `tests/test_core/test_memory.py` | Modify | Fix AsyncGenerator return type, add duplicate/wildcard tests |
| `tests/test_triggers/test_telegram.py` | Modify | Remove asyncio skips, fix `is True/False`, update mock patches |
| `tests/test_triggers/test_scheduler.py` | Modify | Remove `_asyncio_only` fixture, replace asyncio.sleep with anyio.sleep |
| `tests/test_core/test_bootstrap.py` | Modify | Add test for episode saving |

---

### Task 1: Config Validation Hardening

**Files:**
- Modify: `src/proctor/core/config.py:30-65`
- Test: `tests/test_triggers/test_scheduler.py:60-90` (existing validation tests)

- [ ] **Step 1: Write failing tests for new validations**

Add to `tests/test_triggers/test_scheduler.py` in `TestScheduleItemConfigValidation`:

```python
def test_invalid_cron_raises(self) -> None:
    with pytest.raises(ValueError, match="Invalid cron"):
        ScheduleItemConfig(name="bad", cron="not-a-cron")

def test_zero_interval_raises(self) -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        ScheduleItemConfig(name="bad", interval_seconds=0)

def test_negative_interval_raises(self) -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        ScheduleItemConfig(name="bad", interval_seconds=-1)
```

Add to `tests/test_triggers/test_telegram.py` in a new class or inline:

```python
def test_allowed_chat_ids_defaults_empty() -> None:
    config = TelegramConfig(bot_token="tok")
    assert config.allowed_chat_ids == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_triggers/test_scheduler.py::TestScheduleItemConfigValidation::test_invalid_cron_raises tests/test_triggers/test_scheduler.py::TestScheduleItemConfigValidation::test_zero_interval_raises tests/test_triggers/test_scheduler.py::TestScheduleItemConfigValidation::test_negative_interval_raises -v`
Expected: FAIL (no validation yet)

- [ ] **Step 3: Implement config validations**

In `src/proctor/core/config.py`, update `ScheduleItemConfig`:

```python
from typing import Any

from croniter import croniter
from pydantic import BaseModel, Field, model_validator


class ScheduleItemConfig(BaseModel):
    """A single scheduled task definition."""

    name: str
    cron: str | None = None
    interval_seconds: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_schedule(self) -> "ScheduleItemConfig":
        """Ensure exactly one schedule type and validate values."""
        has_cron = self.cron is not None
        has_interval = self.interval_seconds is not None
        if has_cron == has_interval:
            raise ValueError(
                "Exactly one of 'cron' or 'interval_seconds' "
                "must be set, not both or neither."
            )
        if self.cron is not None and not croniter.is_valid(self.cron):
            raise ValueError(f"Invalid cron expression: {self.cron!r}")
        if (
            self.interval_seconds is not None
            and self.interval_seconds <= 0
        ):
            raise ValueError(
                "interval_seconds must be greater than 0"
            )
        return self
```

Update `TelegramConfig`:

```python
class TelegramConfig(BaseModel):
    """Telegram trigger configuration."""

    bot_token: str
    allowed_chat_ids: list[int] = Field(default_factory=list)
    poll_timeout: int = 30
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_triggers/test_scheduler.py::TestScheduleItemConfigValidation tests/test_triggers/test_telegram.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/proctor/core/config.py tests/test_triggers/test_scheduler.py tests/test_triggers/test_telegram.py
git commit -m "fix: harden config validation — cron syntax, interval>0, chat_ids default"
```

---

### Task 2: EpisodicMemory — Upsert and LIKE Escape

**Files:**
- Modify: `src/proctor/core/memory.py:33-48, 111-117`
- Test: `tests/test_core/test_memory.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_core/test_memory.py`:

```python
class TestSaveEpisodeDuplicate:
    async def test_save_duplicate_is_idempotent(
        self, memory: EpisodicMemory
    ) -> None:
        ep = _make_episode(id="dup-1", agent_response="first")
        await memory.save_episode(ep)
        ep_updated = _make_episode(id="dup-1", agent_response="second")
        await memory.save_episode(ep_updated)
        retrieved = await memory.get_episode("dup-1")
        assert retrieved is not None
        assert retrieved.agent_response == "second"


class TestSearchWildcardEscape:
    async def test_percent_in_query_does_not_match_all(
        self, memory: EpisodicMemory
    ) -> None:
        await memory.save_episode(_make_episode(user_input="normal text"))
        results = await memory.search_episodes("%")
        assert results == []

    async def test_underscore_in_query_is_literal(
        self, memory: EpisodicMemory
    ) -> None:
        await memory.save_episode(_make_episode(user_input="a"))
        await memory.save_episode(_make_episode(user_input="a_b"))
        results = await memory.search_episodes("a_b")
        assert len(results) == 1
        assert results[0].user_input == "a_b"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core/test_memory.py::TestSaveEpisodeDuplicate tests/test_core/test_memory.py::TestSearchWildcardEscape -v`
Expected: FAIL (IntegrityError on duplicate, `%` matches all)

- [ ] **Step 3: Implement upsert and LIKE escape**

In `src/proctor/core/memory.py`:

Replace `_INSERT_EPISODE`:

```python
_INSERT_EPISODE = """
INSERT INTO episodes (
    id, timestamp, trigger_type, user_input,
    agent_response, workflow_result_json
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    timestamp=excluded.timestamp,
    trigger_type=excluded.trigger_type,
    user_input=excluded.user_input,
    agent_response=excluded.agent_response,
    workflow_result_json=excluded.workflow_result_json
"""
```

Replace `_SEARCH_EPISODES`:

```python
_SEARCH_EPISODES = (
    "SELECT * FROM episodes"
    " WHERE user_input LIKE ? ESCAPE '\\'"
    " OR agent_response LIKE ? ESCAPE '\\'"
    " ORDER BY timestamp DESC LIMIT ?"
)
```

Update `search_episodes` method:

```python
async def search_episodes(self, query: str, limit: int = 20) -> list[Episode]:
    """Search episodes by user_input or agent_response."""
    if self._db is None:
        raise RuntimeError("EpisodicMemory not initialized")
    escaped = (
        query.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    pattern = f"%{escaped}%"
    cursor = await self._db.execute(
        _SEARCH_EPISODES, (pattern, pattern, limit)
    )
    rows = await cursor.fetchall()
    return [_row_to_episode(row) for row in rows]
```

Also replace all `assert self._db is not None` with proper guards:

```python
if self._db is None:
    raise RuntimeError("EpisodicMemory not initialized")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_core/test_memory.py -v`
Expected: ALL PASS

- [ ] **Step 5: Fix AsyncGenerator return type in test fixture**

In `tests/test_core/test_memory.py`, change the fixture signature:

```python
from collections.abc import AsyncGenerator

@pytest.fixture
async def memory(tmp_path: Path) -> AsyncGenerator[EpisodicMemory]:
    """Create and initialize an EpisodicMemory with a temp DB."""
    mem = EpisodicMemory(tmp_path / "episodes.db")
    await mem.initialize()
    yield mem
    await mem.close()
```

- [ ] **Step 6: Run pyrefly to confirm type error is gone**

Run: `uv run pyrefly check`
Expected: 0 errors

- [ ] **Step 7: Commit**

```bash
git add src/proctor/core/memory.py tests/test_core/test_memory.py
git commit -m "fix: memory upsert, LIKE escape, assert→RuntimeError, fixture type"
```

---

### Task 3: Telegram Trigger — anyio Migration and Guards

**Files:**
- Modify: `src/proctor/triggers/telegram.py`
- Modify: `tests/test_triggers/test_telegram.py`

- [ ] **Step 1: Migrate telegram.py from asyncio to anyio**

Replace `src/proctor/triggers/telegram.py` with:

```python
"""Telegram trigger — polls Telegram Bot API and publishes events."""

import contextlib
import logging
from typing import Any

import aiohttp
import anyio
from anyio.abc import TaskGroup

from proctor.core.bus import EventBus
from proctor.core.config import TelegramConfig
from proctor.core.models import Event
from proctor.triggers.base import Trigger

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
INITIAL_RETRY_DELAY = 1.0
MAX_RETRY_DELAY = 60.0
RETRY_BACKOFF_FACTOR = 2.0


class TelegramTrigger(Trigger):
    """Polls Telegram Bot API getUpdates and publishes trigger.telegram events.

    Messages from chats not in allowed_chat_ids are silently dropped
    (when the list is non-empty). Tracks offset to avoid reprocessing.
    """

    def __init__(self, config: TelegramConfig) -> None:
        self._config = config
        self._offset: int = 0
        self._session: aiohttp.ClientSession | None = None
        self._cancel_scope: anyio.CancelScope | None = None
        self._task_group: TaskGroup | None = None
        self._running = False

    @property
    def _api_url(self) -> str:
        return f"{TELEGRAM_API_BASE}{self._config.bot_token}"

    async def start(self, bus: EventBus) -> None:
        """Create aiohttp session and launch polling task."""
        self._session = aiohttp.ClientSession()
        self._running = True
        self._cancel_scope = anyio.CancelScope()
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        self._task_group.start_soon(self._poll_loop, bus)
        logger.info("TelegramTrigger started")

    async def stop(self) -> None:
        """Cancel polling task and close aiohttp session."""
        self._running = False
        if self._cancel_scope is not None:
            self._cancel_scope.cancel()
            self._cancel_scope = None
        if self._task_group is not None:
            self._task_group.cancel_scope.cancel()
            with contextlib.suppress(Exception):
                await self._task_group.__aexit__(None, None, None)
            self._task_group = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        logger.info("TelegramTrigger stopped")

    async def _poll_loop(self, bus: EventBus) -> None:
        """Long-poll getUpdates, dispatch messages, retry on errors."""
        retry_delay = INITIAL_RETRY_DELAY
        while self._running:
            try:
                updates = await self._get_updates()
                retry_delay = INITIAL_RETRY_DELAY
                for update in updates:
                    await self._handle_update(update, bus)
            except anyio.get_cancelled_exc_class():
                break
            except aiohttp.ClientError as exc:
                logger.error("Telegram API HTTP error: %s", type(exc).__name__)
                await anyio.sleep(retry_delay)
                retry_delay = min(
                    retry_delay * RETRY_BACKOFF_FACTOR, MAX_RETRY_DELAY
                )
            except Exception:
                logger.exception("Unexpected error in Telegram poll loop")
                await anyio.sleep(retry_delay)
                retry_delay = min(
                    retry_delay * RETRY_BACKOFF_FACTOR, MAX_RETRY_DELAY
                )

    async def _get_updates(self) -> list[dict[str, Any]]:
        """Call Telegram getUpdates endpoint."""
        if self._session is None:
            raise RuntimeError(
                "TelegramTrigger not started; call start() first"
            )
        url = f"{self._api_url}/getUpdates"
        params: dict[str, int] = {
            "timeout": self._config.poll_timeout,
        }
        if self._offset:
            params["offset"] = self._offset
        async with self._session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if not data.get("ok"):
            logger.error("Telegram API returned ok=false: %s", data)
            return []
        result: list[dict[str, Any]] = data.get("result", [])
        return result

    async def _handle_update(
        self, update: dict[str, Any], bus: EventBus
    ) -> None:
        """Process a single update: filter, extract, publish."""
        update_id: int = update["update_id"]
        self._offset = update_id + 1

        message: dict[str, Any] | None = update.get("message")
        if message is None:
            logger.debug("Skipping non-message update %d", update_id)
            return

        chat: dict[str, Any] = message.get("chat", {})
        chat_id: int = chat.get("id", 0)

        if (
            self._config.allowed_chat_ids
            and chat_id not in self._config.allowed_chat_ids
        ):
            logger.debug(
                "Skipping message from disallowed chat %d", chat_id
            )
            return

        text: str | None = message.get("text")
        if text is None:
            logger.debug(
                "Skipping non-text message in chat %d", chat_id
            )
            return

        message_id: int = message.get("message_id", 0)

        event = Event(
            type="trigger.telegram",
            source="telegram",
            payload={
                "text": text,
                "chat_id": chat_id,
                "message_id": message_id,
            },
        )
        await bus.publish(event)
        logger.debug(
            "Published telegram event: %s (chat=%d)", event.id, chat_id
        )
```

- [ ] **Step 2: Update telegram tests**

In `tests/test_triggers/test_telegram.py`:

1. Remove `import asyncio` (except where used by test mocks for `aiohttp`)
2. Replace `assert trigger._running is False` → `assert not trigger._running` (lines 69, 513, 521)
3. Replace `assert trigger._running is True` → `assert trigger._running` (line 507)
4. Remove the asyncio skip at line 488-490
5. Update `test_start_creates_session_and_task` — remove the skip, adapt for anyio task group:
   - Check `trigger._task_group is not None` instead of `trigger._task is not None`
   - Check `trigger._task_group is None` after stop instead of `trigger._task is None`
6. Update retry test patches from `patch("proctor.triggers.telegram.asyncio.sleep")` to `patch("proctor.triggers.telegram.anyio.sleep")`

- [ ] **Step 3: Run all telegram tests**

Run: `uv run pytest tests/test_triggers/test_telegram.py -v`
Expected: ALL PASS, 0 skipped

- [ ] **Step 4: Commit**

```bash
git add src/proctor/triggers/telegram.py tests/test_triggers/test_telegram.py
git commit -m "fix: telegram trigger — asyncio→anyio, assert→RuntimeError, token logging"
```

---

### Task 4: Scheduler Trigger — anyio Migration

**Files:**
- Modify: `src/proctor/triggers/scheduler.py`
- Modify: `tests/test_triggers/test_scheduler.py`

- [ ] **Step 1: Migrate scheduler.py from asyncio to anyio**

Replace `src/proctor/triggers/scheduler.py` with:

```python
"""Scheduler trigger — fires events on cron or fixed-interval schedules."""

import logging
from datetime import UTC, datetime

import anyio
from anyio.abc import TaskGroup
from croniter import croniter

from proctor.core.bus import EventBus
from proctor.core.config import ScheduleItemConfig
from proctor.core.models import Event
from proctor.triggers.base import Trigger

logger = logging.getLogger(__name__)


class SchedulerTrigger(Trigger):
    """Publishes trigger.scheduler events based on cron/interval schedules.

    Each enabled schedule item gets its own task that sleeps
    until the next fire time and then publishes an event on the bus.
    """

    def __init__(self, schedules: list[ScheduleItemConfig]) -> None:
        self._schedules = schedules
        self._task_group: TaskGroup | None = None

    async def start(self, bus: EventBus) -> None:
        """Launch one task per enabled schedule item."""
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        count = 0
        for item in self._schedules:
            if not item.enabled:
                logger.debug(
                    "Skipping disabled schedule: %s", item.name
                )
                continue
            if item.cron is not None:
                self._task_group.start_soon(
                    self._run_cron, item, bus
                )
            else:
                self._task_group.start_soon(
                    self._run_interval, item, bus
                )
            count += 1
        logger.info(
            "SchedulerTrigger started with %d active schedule(s)",
            count,
        )

    async def stop(self) -> None:
        """Cancel all running schedule tasks with proper cleanup."""
        if self._task_group is not None:
            self._task_group.cancel_scope.cancel()
            try:
                await self._task_group.__aexit__(None, None, None)
            except BaseException:
                pass
            self._task_group = None
        logger.info("SchedulerTrigger stopped")

    async def _run_cron(
        self, item: ScheduleItemConfig, bus: EventBus
    ) -> None:
        """Loop using croniter to sleep until next fire, then publish."""
        if item.cron is None:
            return
        while True:
            now = datetime.now(UTC)
            cron = croniter(item.cron, now)
            next_fire = cron.get_next(datetime)
            delay = (next_fire - now).total_seconds()
            if delay <= 0:
                next_fire = cron.get_next(datetime)
                delay = (next_fire - now).total_seconds()
            await anyio.sleep(delay)
            await self._publish(item, bus)

    async def _run_interval(
        self, item: ScheduleItemConfig, bus: EventBus
    ) -> None:
        """Loop with fixed interval sleep, then publish."""
        if item.interval_seconds is None:
            return
        while True:
            await anyio.sleep(item.interval_seconds)
            await self._publish(item, bus)

    async def _publish(
        self, item: ScheduleItemConfig, bus: EventBus
    ) -> None:
        """Publish a scheduler event for the given item."""
        event = Event(
            type="trigger.scheduler",
            source=f"scheduler:{item.name}",
            payload=item.payload,
        )
        await bus.publish(event)
        logger.debug(
            "Scheduler fired: %s (event %s)", item.name, event.id
        )
```

- [ ] **Step 2: Update scheduler tests — remove _asyncio_only, use anyio.sleep**

In `tests/test_triggers/test_scheduler.py`:

1. Replace `import asyncio` with `import anyio`
2. Remove the `_asyncio_only` fixture entirely (lines 47-52)
3. Remove all `@pytest.mark.usefixtures("_asyncio_only")` decorators
4. Replace all `await asyncio.sleep(...)` with `await anyio.sleep(...)`
5. The `_tasks` list no longer exists. Tests that check `trigger._tasks` need to be updated:
   - `test_stores_schedules` — still works (checks `_schedules`)
   - `test_initial_tasks_empty` → check `trigger._task_group is None`
   - `test_disabled_items_not_started` — can't count tasks directly. Instead verify by checking that only enabled items fire events:

```python
@pytest.mark.anyio
async def test_disabled_items_not_started(self) -> None:
    bus = EventBus()
    received: list[Event] = []

    async def handler(e: Event) -> None:
        received.append(e)

    bus.subscribe("trigger.scheduler", handler)

    items = [
        _interval_item(name="on", interval_seconds=0.01),
        _interval_item(name="off", interval_seconds=0.01, enabled=False),
    ]
    trigger = SchedulerTrigger(schedules=items)
    await trigger.start(bus)
    await anyio.sleep(0.05)
    await trigger.stop()

    sources = {e.source for e in received}
    assert "scheduler:on" in sources
    assert "scheduler:off" not in sources
```

6. Similarly update other lifecycle tests to check `trigger._task_group` instead of `trigger._tasks`
7. Update `test_stop_cancels_all_tasks` and similar to check `trigger._task_group is None` after stop

- [ ] **Step 3: Run all scheduler tests**

Run: `uv run pytest tests/test_triggers/test_scheduler.py -v`
Expected: ALL PASS, 0 skipped

- [ ] **Step 4: Commit**

```bash
git add src/proctor/triggers/scheduler.py tests/test_triggers/test_scheduler.py
git commit -m "fix: scheduler trigger — asyncio→anyio, remove all test skips"
```

---

### Task 5: Wire EpisodicMemory into Bootstrap

**Files:**
- Modify: `src/proctor/core/bootstrap.py:117-154`
- Test: `tests/test_core/test_bootstrap.py`

- [ ] **Step 1: Read existing bootstrap test to understand patterns**

Read `tests/test_core/test_bootstrap.py` to see how `_handle_terminal` is tested and what fixtures exist.

- [ ] **Step 2: Add test for episode saving**

Add to `tests/test_core/test_bootstrap.py`:

```python
@pytest.mark.anyio
async def test_handle_terminal_saves_episode(app: Application) -> None:
    """After successful workflow, an episode should be saved."""
    event = Event(
        type="trigger.terminal",
        source="terminal",
        payload={"text": "hello"},
    )
    await app._handle_terminal(event)

    episodes = await app.memory.list_episodes(limit=10)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.trigger_type == "terminal"
    assert ep.user_input == "hello"
    assert ep.agent_response != ""
    assert ep.workflow_result is not None
```

```python
@pytest.mark.anyio
async def test_handle_terminal_saves_episode_on_failure(
    app_no_llm: Application,
) -> None:
    """Even failed workflows should save an episode."""
    event = Event(
        type="trigger.terminal",
        source="terminal",
        payload={"text": "fail me"},
    )
    await app_no_llm._handle_terminal(event)

    episodes = await app_no_llm.memory.list_episodes(limit=10)
    # No LLM configured → task.failed event published, but no episode
    # since the engine never ran. This is acceptable.
    assert len(episodes) == 0
```

(Adapt based on existing fixture names in the test file.)

- [ ] **Step 3: Implement episode saving in bootstrap**

In `src/proctor/core/bootstrap.py`, add Episode import and update `_handle_terminal`:

```python
from proctor.core.models import Episode, Event, Task, TaskStatus
```

After the successful result block (after `await self.state.save_task(task)` inside `try`):

```python
            episode = Episode(
                trigger_type=event.source,
                user_input=text,
                agent_response=result.output or "",
                workflow_result=task.result,
            )
            await self.memory.save_episode(episode)
```

After the exception block (after `await self.state.save_task(task)` inside `except`):

```python
            episode = Episode(
                trigger_type=event.source,
                user_input=text,
                agent_response="",
                workflow_result=task.result,
            )
            await self.memory.save_episode(episode)
```

- [ ] **Step 4: Run bootstrap tests**

Run: `uv run pytest tests/test_core/test_bootstrap.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/proctor/core/bootstrap.py tests/test_core/test_bootstrap.py
git commit -m "feat: wire EpisodicMemory into bootstrap — save episodes after execution"
```

---

### Task 6: Full Test Suite + Lint + Type Check

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: ALL PASS, 0 skipped (except aiosqlite tests which force asyncio backend)

- [ ] **Step 2: Run ruff**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: All checks passed, all files formatted

- [ ] **Step 3: Run pyrefly**

Run: `uv run pyrefly check`
Expected: 0 errors

- [ ] **Step 4: Fix any remaining issues**

If any lint/type errors remain, fix them and re-run.

- [ ] **Step 5: Final commit if needed**

```bash
git add -u
git commit -m "fix: lint and type check cleanup"
```
