# LiteLLM Integration (LABS-67) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mock LLM callable currently wired into `WorkflowEngine` with a real LiteLLM-backed implementation, with transient-error retry, optional fallback model, and per-call token accounting in a new `llm_calls` table.

**Architecture:** A factory `build_llm_call(config, memory) -> LLMCall` returns a closure compatible with the existing single-arg `Callable[[str], Awaitable[str]]` interface. The closure reads `task_id` / `step_id` / `episode_id` from `ContextVar` slots set by `WorkflowEngine` and `bootstrap`, calls `litellm.acompletion`, persists every attempt as an `LLMCallRecord` in the new `llm_calls` table (`episodes.db`), and implements retry + fallback semantics explicitly (LiteLLM's own retries are disabled via `num_retries=0` so each attempt is visible in telemetry).

**Tech Stack:** Python 3.12, LiteLLM (`litellm.acompletion`), aiosqlite, anyio, pydantic, pytest + pytest-anyio, `contextvars.ContextVar`.

**Spec:** [`docs/superpowers/specs/2026-04-15-litellm-integration-design.md`](../specs/2026-04-15-litellm-integration-design.md)

---

## File Structure

### New files

- `src/proctor/workers/llm.py` — public `build_llm_call` factory, internal `llm_call` closure, `ContextVar` slots, `_TRANSIENT` classification, `_persist` helper.
- `tests/test_workers/test_llm.py` — unit tests for `llm_call` with mocked `litellm.acompletion`.
- `tests/integration/__init__.py` — package marker.
- `tests/integration/test_llm_ollama.py` — skip-if-unavailable integration test.

### Modified files

- `src/proctor/core/models.py` — add `LLMCallRecord`.
- `src/proctor/core/config.py` — extend `LLMConfig` (`fallback_model: str | None = None`, `request_timeout: float = 60.0`, `max_retries: int = 1`).
- `src/proctor/core/memory.py` — add `llm_calls` DDL in `initialize()` + `save_llm_call` method + `_row_to_llm_call` helper.
- `src/proctor/workflow/engine.py` — set `step_id_ctx` inside the DAG `step_runner` closure before each call; keep simple path unchanged (step_id stays `None`).
- `src/proctor/core/bootstrap.py` — create/save `Episode` **before** executing the workflow, set `task_id_ctx`/`episode_id_ctx` around `engine.execute()`, update the same episode row afterwards with the real `agent_response`.
- `src/proctor/__main__.py` — replace mock `llm_call` with `build_llm_call(app.config.llm, app.memory)`.
- `tests/test_core/test_memory.py` — round-trip and idempotency tests for `save_llm_call` / `llm_calls` table.
- `tests/test_core/test_config.py` — tests for new `LLMConfig` defaults.
- `tests/test_core/test_models.py` — test `LLMCallRecord` defaults.
- `pyproject.toml` — register `integration` marker.
- `README.md` — "Running integration tests" subsection.

---

## Task 1: Data model — `LLMCallRecord` + extended `LLMConfig`

**Files:**
- Modify: `src/proctor/core/models.py`
- Modify: `src/proctor/core/config.py`
- Modify: `tests/test_core/test_models.py`
- Modify: `tests/test_core/test_config.py`

- [ ] **Step 1.1: Write failing test for `LLMCallRecord` defaults**

Append to `tests/test_core/test_models.py`:

```python
from datetime import UTC, datetime

from proctor.core.models import LLMCallRecord


class TestLLMCallRecord:
    def test_defaults(self) -> None:
        rec = LLMCallRecord(model="claude-sonnet-4-20250514")
        assert rec.id
        assert rec.created_at <= datetime.now(UTC)
        assert rec.model == "claude-sonnet-4-20250514"
        assert rec.fallback_used is False
        assert rec.prompt_tokens is None
        assert rec.completion_tokens is None
        assert rec.cache_read_tokens is None
        assert rec.cache_write_tokens is None
        assert rec.latency_ms is None
        assert rec.error is None
        assert rec.episode_id is None
        assert rec.task_id is None
        assert rec.step_id is None

    def test_all_fields_set(self) -> None:
        rec = LLMCallRecord(
            id="llm-1",
            episode_id="ep-1",
            task_id="task-1",
            step_id="step-a",
            model="ollama/llama3.2",
            fallback_used=True,
            prompt_tokens=120,
            completion_tokens=45,
            cache_read_tokens=10,
            cache_write_tokens=20,
            latency_ms=350,
            error=None,
        )
        assert rec.fallback_used is True
        assert rec.prompt_tokens == 120
        assert rec.cache_write_tokens == 20
```

- [ ] **Step 1.2: Run test, confirm failure**

Run: `uv run pytest tests/test_core/test_models.py::TestLLMCallRecord -v`
Expected: `ImportError` on `LLMCallRecord`.

- [ ] **Step 1.3: Implement `LLMCallRecord`**

Append to `src/proctor/core/models.py`:

```python
class LLMCallRecord(BaseModel):
    """Record of a single LLM API call attempt (success or failure)."""

    id: str = Field(default_factory=_uuid)
    episode_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    model: str
    fallback_used: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
```

- [ ] **Step 1.4: Run test, confirm pass**

Run: `uv run pytest tests/test_core/test_models.py::TestLLMCallRecord -v`
Expected: 2 passed.

- [ ] **Step 1.5: Write failing test for new `LLMConfig` defaults**

Append to `tests/test_core/test_config.py`:

```python
from proctor.core.config import LLMConfig


class TestLLMConfigExtended:
    def test_fallback_model_default_none(self) -> None:
        cfg = LLMConfig()
        assert cfg.fallback_model is None

    def test_request_timeout_default(self) -> None:
        cfg = LLMConfig()
        assert cfg.request_timeout == 60.0

    def test_max_retries_default(self) -> None:
        cfg = LLMConfig()
        assert cfg.max_retries == 1

    def test_overrides(self) -> None:
        cfg = LLMConfig(
            fallback_model="ollama/llama3.2",
            request_timeout=10.0,
            max_retries=3,
        )
        assert cfg.fallback_model == "ollama/llama3.2"
        assert cfg.request_timeout == 10.0
        assert cfg.max_retries == 3
```

- [ ] **Step 1.6: Run test, confirm failure**

Run: `uv run pytest tests/test_core/test_config.py::TestLLMConfigExtended -v`
Expected: attribute errors / assertion failures.

- [ ] **Step 1.7: Update `LLMConfig`**

Edit `src/proctor/core/config.py`, replace the `LLMConfig` class body:

```python
class LLMConfig(BaseModel):
    """LLM provider configuration."""

    default_model: str = "claude-sonnet-4-20250514"
    fallback_model: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.7
    request_timeout: float = 60.0
    max_retries: int = 1
```

- [ ] **Step 1.8: Run full test suite for affected modules**

Run: `uv run pytest tests/test_core/test_config.py tests/test_core/test_models.py -v`
Expected: all pass.

- [ ] **Step 1.9: Format + type check**

Run: `uv run ruff format . && uv run ruff check . && pyrefly check`
Expected: no errors.

- [ ] **Step 1.10: Commit**

```bash
git add src/proctor/core/models.py src/proctor/core/config.py tests/test_core/test_models.py tests/test_core/test_config.py
git commit -m "feat(llm): add LLMCallRecord model and extend LLMConfig

New fields: fallback_model (Optional, default None), request_timeout,
max_retries. LLMCallRecord will back per-call token accounting for
LABS-67."
```

---

## Task 2: Persistence — `llm_calls` table + `save_llm_call`

**Files:**
- Modify: `src/proctor/core/memory.py`
- Modify: `tests/test_core/test_memory.py`

- [ ] **Step 2.1: Write failing test for `save_llm_call` round-trip**

Append to `tests/test_core/test_memory.py`:

```python
from proctor.core.models import LLMCallRecord


class TestLLMCallsTable:
    async def test_save_and_read_back(self, memory: EpisodicMemory) -> None:
        rec = LLMCallRecord(
            episode_id="ep-1",
            task_id="task-1",
            step_id="step-a",
            model="claude-sonnet-4-20250514",
            fallback_used=False,
            prompt_tokens=100,
            completion_tokens=50,
            cache_read_tokens=10,
            cache_write_tokens=20,
            latency_ms=250,
            error=None,
        )
        await memory.save_llm_call(rec)

        # Read via raw SQL — we don't add a public getter yet
        assert memory._db is not None
        cursor = await memory._db.execute(
            "SELECT * FROM llm_calls WHERE id = ?", (rec.id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["episode_id"] == "ep-1"
        assert row["task_id"] == "task-1"
        assert row["step_id"] == "step-a"
        assert row["model"] == "claude-sonnet-4-20250514"
        assert row["fallback_used"] == 0
        assert row["prompt_tokens"] == 100
        assert row["completion_tokens"] == 50
        assert row["cache_read_tokens"] == 10
        assert row["cache_write_tokens"] == 20
        assert row["latency_ms"] == 250
        assert row["error"] is None

    async def test_save_error_record(self, memory: EpisodicMemory) -> None:
        rec = LLMCallRecord(
            model="claude-sonnet-4-20250514",
            error="RateLimitError: 429",
        )
        await memory.save_llm_call(rec)
        assert memory._db is not None
        cursor = await memory._db.execute(
            "SELECT error FROM llm_calls WHERE id = ?", (rec.id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["error"] == "RateLimitError: 429"

    async def test_indexes_exist(self, memory: EpisodicMemory) -> None:
        assert memory._db is not None
        cursor = await memory._db.execute("PRAGMA index_list('llm_calls')")
        names = {row["name"] for row in await cursor.fetchall()}
        assert "idx_llm_calls_episode" in names
        assert "idx_llm_calls_task" in names
        assert "idx_llm_calls_created" in names

    async def test_initialize_idempotent(self, tmp_path: Path) -> None:
        mem = EpisodicMemory(tmp_path / "episodes.db")
        await mem.initialize()
        await mem.close()
        # Re-open on the same file — CREATE TABLE IF NOT EXISTS must not raise
        mem2 = EpisodicMemory(tmp_path / "episodes.db")
        await mem2.initialize()
        await mem2.close()
```

- [ ] **Step 2.2: Run, confirm failure**

Run: `uv run pytest tests/test_core/test_memory.py::TestLLMCallsTable -v`
Expected: `AttributeError: 'EpisodicMemory' object has no attribute 'save_llm_call'` or SQL errors.

- [ ] **Step 2.3: Add DDL constants and method to `memory.py`**

Edit `src/proctor/core/memory.py`. Below `_CREATE_INDEX_EPISODES_TS`, add:

```python
_CREATE_LLM_CALLS = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id                 TEXT PRIMARY KEY,
    episode_id         TEXT,
    task_id            TEXT,
    step_id            TEXT,
    model              TEXT NOT NULL,
    fallback_used      INTEGER NOT NULL DEFAULT 0,
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    latency_ms         INTEGER,
    error              TEXT,
    created_at         TEXT NOT NULL
)
"""

_CREATE_INDEX_LLM_CALLS_EPISODE = (
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_episode ON llm_calls(episode_id)"
)
_CREATE_INDEX_LLM_CALLS_TASK = (
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_task ON llm_calls(task_id)"
)
_CREATE_INDEX_LLM_CALLS_CREATED = (
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at)"
)

_INSERT_LLM_CALL = """
INSERT INTO llm_calls (
    id, episode_id, task_id, step_id, model, fallback_used,
    prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens,
    latency_ms, error, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
```

Add the model import at the top:

```python
from proctor.core.models import Episode, LLMCallRecord
```

In `initialize()`, after the episodes index creation, add:

```python
        await self._db.execute(_CREATE_LLM_CALLS)
        await self._db.execute(_CREATE_INDEX_LLM_CALLS_EPISODE)
        await self._db.execute(_CREATE_INDEX_LLM_CALLS_TASK)
        await self._db.execute(_CREATE_INDEX_LLM_CALLS_CREATED)
```

Add the `save_llm_call` method (after `save_episode`):

```python
    async def save_llm_call(self, record: LLMCallRecord) -> None:
        """Insert an LLM call record. Each call produces a new row."""
        if self._db is None:
            raise RuntimeError("EpisodicMemory not initialized")
        await self._db.execute(
            _INSERT_LLM_CALL,
            (
                record.id,
                record.episode_id,
                record.task_id,
                record.step_id,
                record.model,
                1 if record.fallback_used else 0,
                record.prompt_tokens,
                record.completion_tokens,
                record.cache_read_tokens,
                record.cache_write_tokens,
                record.latency_ms,
                record.error,
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()
```

- [ ] **Step 2.4: Run tests, confirm pass**

Run: `uv run pytest tests/test_core/test_memory.py -v`
Expected: all pass (existing + new class).

- [ ] **Step 2.5: Format + type check**

Run: `uv run ruff format . && uv run ruff check . && pyrefly check`
Expected: no errors.

- [ ] **Step 2.6: Commit**

```bash
git add src/proctor/core/memory.py tests/test_core/test_memory.py
git commit -m "feat(memory): add llm_calls table and save_llm_call

New table in episodes.db with per-call token accounting. Idempotent
CREATE IF NOT EXISTS, three indexes (episode, task, created_at).
Cross-DB FK to state.db.tasks is intentionally not enforced."
```

---

## Task 3: `llm.py` skeleton — ContextVars, factory, happy path

**Files:**
- Create: `src/proctor/workers/llm.py`
- Create: `tests/test_workers/test_llm.py`

- [ ] **Step 3.1: Write failing test — happy path**

Create `tests/test_workers/test_llm.py`:

```python
"""Tests for llm_call (LiteLLM-backed with retry + fallback)."""

from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from proctor.core.config import LLMConfig
from proctor.core.memory import EpisodicMemory
from proctor.workers.llm import build_llm_call

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """aiosqlite only supports asyncio."""
    return "asyncio"


@pytest.fixture
async def memory(tmp_path: Path) -> AsyncGenerator[EpisodicMemory]:
    mem = EpisodicMemory(tmp_path / "episodes.db")
    await mem.initialize()
    yield mem
    await mem.close()


def _make_response(
    content: str = "hi",
    prompt_tokens: int | None = 10,
    completion_tokens: int | None = 5,
    cache_creation: int | None = None,
    cache_read: int | None = None,
) -> SimpleNamespace:
    """Build a SimpleNamespace mimicking litellm's ModelResponse."""
    usage_fields: dict[str, Any] = {}
    if prompt_tokens is not None:
        usage_fields["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        usage_fields["completion_tokens"] = completion_tokens
    if cache_creation is not None:
        usage_fields["cache_creation_input_tokens"] = cache_creation
    if cache_read is not None:
        usage_fields["cache_read_input_tokens"] = cache_read
    usage = SimpleNamespace(**usage_fields) if usage_fields else None
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage)


async def _fetch_rows(memory: EpisodicMemory) -> list[dict[str, Any]]:
    assert memory._db is not None
    cursor = await memory._db.execute(
        "SELECT * FROM llm_calls ORDER BY created_at ASC"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


class TestHappyPath:
    async def test_happy_path_returns_text_and_writes_row(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
            return _make_response(content="hello world")

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        cfg = LLMConfig()
        call = build_llm_call(cfg, memory)
        result = await call("Say hi")

        assert result == "hello world"
        rows = await _fetch_rows(memory)
        assert len(rows) == 1
        row = rows[0]
        assert row["model"] == cfg.default_model
        assert row["fallback_used"] == 0
        assert row["error"] is None
        assert row["prompt_tokens"] == 10
        assert row["completion_tokens"] == 5
```

- [ ] **Step 3.2: Run, confirm failure**

Run: `uv run pytest tests/test_workers/test_llm.py -v`
Expected: `ModuleNotFoundError: proctor.workers.llm`.

- [ ] **Step 3.3: Create `src/proctor/workers/llm.py` with factory and happy-path logic**

```python
"""LiteLLM-backed llm_call with retry + fallback + per-call persistence.

Exposes build_llm_call(config, memory) -> Callable[[str], Awaitable[str]]
compatible with the LLMCall alias in bootstrap.py. Reads contextvars
task_id_ctx / step_id_ctx / episode_id_ctx to tag persisted records.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from time import monotonic
from typing import Any

import litellm

from proctor.core.config import LLMConfig
from proctor.core.memory import EpisodicMemory
from proctor.core.models import LLMCallRecord

logger = logging.getLogger(__name__)

task_id_ctx: ContextVar[str | None] = ContextVar("task_id_ctx", default=None)
step_id_ctx: ContextVar[str | None] = ContextVar("step_id_ctx", default=None)
episode_id_ctx: ContextVar[str | None] = ContextVar(
    "episode_id_ctx", default=None
)

LLMCall = Callable[[str], Awaitable[str]]


def build_llm_call(config: LLMConfig, memory: EpisodicMemory) -> LLMCall:
    """Return a closure capturing config + memory.

    The returned callable has signature
    ``async def _call(prompt: str, model: str | None = None) -> str``.
    Because ``model`` has a default, it is compatible with the single-arg
    ``LLMCall = Callable[[str], Awaitable[str]]`` alias used by
    WorkflowEngine and bootstrap — no caller change required.
    """

    async def _call(prompt: str, model: str | None = None) -> str:
        chosen = model or config.default_model
        start = monotonic()
        resp = await litellm.acompletion(
            model=chosen,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.request_timeout,
            num_retries=0,
        )
        latency_ms = int((monotonic() - start) * 1000)
        text, usage = _extract_text_and_usage(resp)
        await _persist(
            memory,
            _record(
                model=chosen,
                fallback_used=False,
                usage=usage,
                latency_ms=latency_ms,
            ),
        )
        return text

    return _call


def _extract_text_and_usage(resp: Any) -> tuple[str, Any]:
    """Pull choices[0].message.content and usage (may be None)."""
    text = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    return text, usage


def _record(
    *,
    model: str,
    fallback_used: bool,
    usage: Any = None,
    latency_ms: int | None = None,
    error: str | None = None,
) -> LLMCallRecord:
    """Construct an LLMCallRecord, extracting token fields from usage."""
    prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    completion_tokens = (
        getattr(usage, "completion_tokens", None) if usage else None
    )
    cache_write_tokens = (
        getattr(usage, "cache_creation_input_tokens", None) if usage else None
    )
    cache_read_tokens = (
        getattr(usage, "cache_read_input_tokens", None) if usage else None
    )
    return LLMCallRecord(
        episode_id=episode_id_ctx.get(),
        task_id=task_id_ctx.get(),
        step_id=step_id_ctx.get(),
        model=model,
        fallback_used=fallback_used,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        latency_ms=latency_ms,
        error=error,
    )


async def _persist(memory: EpisodicMemory, record: LLMCallRecord) -> None:
    """Save record; swallow errors so telemetry never blocks real work."""
    try:
        await memory.save_llm_call(record)
    except Exception:
        logger.warning(
            "Failed to persist LLM call record id=%s model=%s",
            record.id,
            record.model,
            exc_info=True,
        )
```

- [ ] **Step 3.4: Run test, confirm pass**

Run: `uv run pytest tests/test_workers/test_llm.py -v`
Expected: 1 passed.

- [ ] **Step 3.5: Format + type check**

Run: `uv run ruff format . && uv run ruff check . && pyrefly check`
Expected: no errors.

- [ ] **Step 3.6: Commit**

```bash
git add src/proctor/workers/llm.py tests/test_workers/test_llm.py
git commit -m "feat(llm): add build_llm_call factory with happy path

Exposes llm_call(prompt, model=None) -> str compatible with the
existing LLMCall alias. ContextVars for task_id/step_id/episode_id
propagation. LiteLLM num_retries=0 so our retry logic owns all
retries. Save failures are logged and swallowed."
```

---

## Task 4: Explicit model override

**Files:**
- Modify: `tests/test_workers/test_llm.py`

- [ ] **Step 4.1: Add test**

Append to `TestHappyPath` in `tests/test_workers/test_llm.py`:

```python
    async def test_explicit_model_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        seen_models: list[str] = []

        async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
            seen_models.append(kwargs["model"])
            return _make_response()

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        cfg = LLMConfig(default_model="claude-sonnet-4-20250514")
        call = build_llm_call(cfg, memory)
        await call("hi", model="gpt-4o")

        assert seen_models == ["gpt-4o"]
        rows = await _fetch_rows(memory)
        assert rows[0]["model"] == "gpt-4o"
```

- [ ] **Step 4.2: Run test, confirm pass**

Run: `uv run pytest tests/test_workers/test_llm.py::TestHappyPath::test_explicit_model_override -v`
Expected: 1 passed (no impl change needed — signature already supports override).

- [ ] **Step 4.3: Commit**

```bash
git add tests/test_workers/test_llm.py
git commit -m "test(llm): explicit model override is honored"
```

---

## Task 5: Retry on transient errors

**Files:**
- Modify: `src/proctor/workers/llm.py`
- Modify: `tests/test_workers/test_llm.py`

- [ ] **Step 5.1: Write failing test — retry then success**

Append to `tests/test_workers/test_llm.py`:

```python
class TestRetry:
    async def test_retry_then_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        calls: list[int] = []

        async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
            calls.append(1)
            if len(calls) == 1:
                raise litellm.RateLimitError(
                    "429", model="m", llm_provider="test"
                )
            return _make_response(content="ok after retry")

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )
        # Use a sleep that returns immediately to keep the test fast
        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("proctor.workers.llm.anyio.sleep", no_sleep)

        cfg = LLMConfig(max_retries=1, fallback_model=None)
        call = build_llm_call(cfg, memory)
        result = await call("hi")

        assert result == "ok after retry"
        assert len(calls) == 2
        rows = await _fetch_rows(memory)
        assert len(rows) == 2
        assert rows[0]["error"] is not None  # first attempt failed
        assert rows[0]["fallback_used"] == 0
        assert rows[1]["error"] is None  # second attempt succeeded
        assert rows[1]["fallback_used"] == 0
        assert rows[1]["model"] == cfg.default_model
```

Add `import litellm` to the test file's imports (top of file, next to the other imports).

- [ ] **Step 5.2: Run, confirm failure**

Run: `uv run pytest tests/test_workers/test_llm.py::TestRetry -v`
Expected: fails — no retry logic yet, exception bubbles up.

- [ ] **Step 5.3: Add `anyio` import and `_TRANSIENT` classification**

Edit the top of `src/proctor/workers/llm.py`, under existing imports:

```python
import anyio
```

Add below the existing imports:

```python
_TRANSIENT: tuple[type[Exception], ...] = (
    litellm.RateLimitError,
    litellm.APIConnectionError,
    litellm.Timeout,
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
)

_RETRY_BACKOFF_SECONDS = 1.0
```

- [ ] **Step 5.4: Replace the body of the `_call` closure with retry loop**

In `build_llm_call`, replace the current `async def _call` body with:

```python
    async def _call(prompt: str, model: str | None = None) -> str:
        chosen = model or config.default_model
        last_transient: BaseException | None = None

        for attempt in range(config.max_retries + 1):
            start = monotonic()
            try:
                resp = await litellm.acompletion(
                    model=chosen,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=config.max_tokens,
                    temperature=config.temperature,
                    timeout=config.request_timeout,
                    num_retries=0,
                )
                latency_ms = int((monotonic() - start) * 1000)
                text, usage = _extract_text_and_usage(resp)
                await _persist(
                    memory,
                    _record(
                        model=chosen,
                        fallback_used=False,
                        usage=usage,
                        latency_ms=latency_ms,
                    ),
                )
                return text

            except _TRANSIENT as exc:
                latency_ms = int((monotonic() - start) * 1000)
                last_transient = exc
                await _persist(
                    memory,
                    _record(
                        model=chosen,
                        fallback_used=False,
                        latency_ms=latency_ms,
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                )
                if attempt < config.max_retries:
                    await anyio.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                # Retries exhausted — fall through to fallback handling.
                break

            except Exception as exc:
                latency_ms = int((monotonic() - start) * 1000)
                await _persist(
                    memory,
                    _record(
                        model=chosen,
                        fallback_used=False,
                        latency_ms=latency_ms,
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                )
                raise

        # If we get here, all primary attempts failed with transient errors.
        # Fallback handling is added in Task 6.
        assert last_transient is not None
        raise last_transient
```

- [ ] **Step 5.5: Run test, confirm pass**

Run: `uv run pytest tests/test_workers/test_llm.py -v`
Expected: all pass (happy path + retry).

- [ ] **Step 5.6: Format + type check**

Run: `uv run ruff format . && uv run ruff check . && pyrefly check`
Expected: no errors.

- [ ] **Step 5.7: Commit**

```bash
git add src/proctor/workers/llm.py tests/test_workers/test_llm.py
git commit -m "feat(llm): retry primary model on transient errors

max_retries+1 total attempts with flat 1-second backoff. Each attempt
is recorded in llm_calls, including the failed ones, so retry behavior
stays observable. Fallback branch is a stub (raises last transient)
and is completed in the next commit."
```

---

## Task 6: Fallback on exhausted retries

**Files:**
- Modify: `src/proctor/workers/llm.py`
- Modify: `tests/test_workers/test_llm.py`

- [ ] **Step 6.1: Write failing test — fallback success**

Append to `tests/test_workers/test_llm.py`:

```python
class TestFallback:
    async def test_fallback_transient_primary_fails_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        models_called: list[str] = []

        async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
            models_called.append(kwargs["model"])
            if kwargs["model"] == "primary":
                raise litellm.RateLimitError(
                    "429", model="primary", llm_provider="test"
                )
            return _make_response(content="fallback ok")

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("proctor.workers.llm.anyio.sleep", no_sleep)

        cfg = LLMConfig(
            default_model="primary",
            fallback_model="fallback",
            max_retries=0,
        )
        call = build_llm_call(cfg, memory)
        result = await call("hi")

        assert result == "fallback ok"
        assert models_called == ["primary", "fallback"]
        rows = await _fetch_rows(memory)
        assert len(rows) == 2
        assert rows[0]["error"] is not None
        assert rows[0]["fallback_used"] == 0
        assert rows[0]["model"] == "primary"
        assert rows[1]["error"] is None
        assert rows[1]["fallback_used"] == 1
        assert rows[1]["model"] == "fallback"

    async def test_retries_exhausted_then_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        primary_attempts = 0

        async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
            nonlocal primary_attempts
            if kwargs["model"] == "primary":
                primary_attempts += 1
                raise litellm.Timeout(
                    "slow", model="primary", llm_provider="test"
                )
            return _make_response(content="fallback result")

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("proctor.workers.llm.anyio.sleep", no_sleep)

        cfg = LLMConfig(
            default_model="primary",
            fallback_model="fallback",
            max_retries=1,
        )
        call = build_llm_call(cfg, memory)
        result = await call("hi")

        assert result == "fallback result"
        assert primary_attempts == 2
        rows = await _fetch_rows(memory)
        assert len(rows) == 3
        assert [r["model"] for r in rows] == ["primary", "primary", "fallback"]
        assert [r["error"] is None for r in rows] == [False, False, True]
        assert [r["fallback_used"] for r in rows] == [0, 0, 1]
```

- [ ] **Step 6.2: Run, confirm failure**

Run: `uv run pytest tests/test_workers/test_llm.py::TestFallback -v`
Expected: fails — fallback code not yet reachable in a useful way.

- [ ] **Step 6.3: Replace the stub after the primary loop with real fallback**

In `src/proctor/workers/llm.py`, replace the final section starting from `# If we get here, all primary attempts failed ...` with:

```python
        # All primary attempts failed with transient errors.
        assert last_transient is not None
        if config.fallback_model is None:
            raise RuntimeError(
                f"Primary model {chosen} failed with transient error "
                f"and fallback_model is not configured"
            ) from last_transient

        logger.warning(
            "Primary model %s exhausted retries (%s: %s), falling back to %s",
            chosen,
            type(last_transient).__name__,
            last_transient,
            config.fallback_model,
        )

        fb_model = config.fallback_model
        start = monotonic()
        try:
            resp = await litellm.acompletion(
                model=fb_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                timeout=config.request_timeout,
                num_retries=0,
            )
            latency_ms = int((monotonic() - start) * 1000)
            text, usage = _extract_text_and_usage(resp)
            await _persist(
                memory,
                _record(
                    model=fb_model,
                    fallback_used=True,
                    usage=usage,
                    latency_ms=latency_ms,
                ),
            )
            return text
        except Exception as fb_exc:
            latency_ms = int((monotonic() - start) * 1000)
            await _persist(
                memory,
                _record(
                    model=fb_model,
                    fallback_used=True,
                    latency_ms=latency_ms,
                    error=f"{type(fb_exc).__name__}: {fb_exc}",
                ),
            )
            raise
```

- [ ] **Step 6.4: Run tests, confirm pass**

Run: `uv run pytest tests/test_workers/test_llm.py::TestFallback -v`
Expected: 2 passed.

- [ ] **Step 6.5: Full test run + format + types**

Run: `uv run pytest tests/test_workers/test_llm.py -v && uv run ruff format . && uv run ruff check . && pyrefly check`
Expected: all pass, no errors.

- [ ] **Step 6.6: Commit**

```bash
git add src/proctor/workers/llm.py tests/test_workers/test_llm.py
git commit -m "feat(llm): fall back to fallback_model after retries exhausted

One attempt on fallback, no retry. Warning log on fallback engagement
includes the underlying exception class + message."
```

---

## Task 7: Fallback error paths

**Files:**
- Modify: `tests/test_workers/test_llm.py`

- [ ] **Step 7.1: Append two tests**

Append to `TestFallback`:

```python
    async def test_fallback_itself_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
            if kwargs["model"] == "primary":
                raise litellm.RateLimitError(
                    "429", model="primary", llm_provider="test"
                )
            raise litellm.APIConnectionError(
                "down", model="fallback", llm_provider="test"
            )

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("proctor.workers.llm.anyio.sleep", no_sleep)

        cfg = LLMConfig(
            default_model="primary",
            fallback_model="fallback",
            max_retries=0,
        )
        call = build_llm_call(cfg, memory)

        with pytest.raises(litellm.APIConnectionError):
            await call("hi")

        rows = await _fetch_rows(memory)
        assert [r["model"] for r in rows] == ["primary", "fallback"]
        assert [r["error"] is not None for r in rows] == [True, True]
        assert [r["fallback_used"] for r in rows] == [0, 1]

    async def test_fallback_model_none_raises_runtime_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        fallback_calls = 0

        async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
            nonlocal fallback_calls
            if kwargs["model"] != "primary":
                fallback_calls += 1
            raise litellm.RateLimitError(
                "429", model=kwargs["model"], llm_provider="test"
            )

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("proctor.workers.llm.anyio.sleep", no_sleep)

        cfg = LLMConfig(
            default_model="primary", fallback_model=None, max_retries=1
        )
        call = build_llm_call(cfg, memory)

        with pytest.raises(RuntimeError, match="fallback_model is not configured"):
            await call("hi")

        assert fallback_calls == 0
        rows = await _fetch_rows(memory)
        assert len(rows) == 2  # both primary attempts recorded
        assert all(r["model"] == "primary" for r in rows)
```

- [ ] **Step 7.2: Run tests, confirm pass**

Run: `uv run pytest tests/test_workers/test_llm.py::TestFallback -v`
Expected: 4 passed (existing 2 + new 2).

- [ ] **Step 7.3: Commit**

```bash
git add tests/test_workers/test_llm.py
git commit -m "test(llm): fallback failure and fallback_model=None paths"
```

---

## Task 8: Non-transient exception propagates without retry/fallback

**Files:**
- Modify: `tests/test_workers/test_llm.py`

- [ ] **Step 8.1: Add test**

Append to `tests/test_workers/test_llm.py`:

```python
class TestNonTransient:
    async def test_authentication_error_propagates_immediately(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        call_count = 0

        async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
            nonlocal call_count
            call_count += 1
            raise litellm.AuthenticationError(
                "bad key", model="primary", llm_provider="test"
            )

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        cfg = LLMConfig(
            default_model="primary",
            fallback_model="fallback",
            max_retries=3,  # would retry if it were transient
        )
        call = build_llm_call(cfg, memory)

        with pytest.raises(litellm.AuthenticationError):
            await call("hi")

        assert call_count == 1  # no retry, no fallback
        rows = await _fetch_rows(memory)
        assert len(rows) == 1
        assert rows[0]["error"] is not None
        assert rows[0]["fallback_used"] == 0
```

- [ ] **Step 8.2: Run, confirm pass**

Run: `uv run pytest tests/test_workers/test_llm.py::TestNonTransient -v`
Expected: 1 passed (implementation already handles this branch in Task 5).

- [ ] **Step 8.3: Commit**

```bash
git add tests/test_workers/test_llm.py
git commit -m "test(llm): non-transient exception raises without retry/fallback"
```

---

## Task 9: ContextVars propagation

**Files:**
- Modify: `tests/test_workers/test_llm.py`

- [ ] **Step 9.1: Add test**

Append to `tests/test_workers/test_llm.py`:

```python
from proctor.workers.llm import episode_id_ctx, step_id_ctx, task_id_ctx


class TestContextVars:
    async def test_ids_flow_into_record(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
            return _make_response()

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        cfg = LLMConfig()
        call = build_llm_call(cfg, memory)

        tokens = (
            task_id_ctx.set("task-42"),
            step_id_ctx.set("step-A"),
            episode_id_ctx.set("ep-7"),
        )
        try:
            await call("hi")
        finally:
            task_id_ctx.reset(tokens[0])
            step_id_ctx.reset(tokens[1])
            episode_id_ctx.reset(tokens[2])

        rows = await _fetch_rows(memory)
        assert rows[0]["task_id"] == "task-42"
        assert rows[0]["step_id"] == "step-A"
        assert rows[0]["episode_id"] == "ep-7"

    async def test_no_ctx_set_records_null(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
            return _make_response()

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        cfg = LLMConfig()
        call = build_llm_call(cfg, memory)
        await call("hi")

        rows = await _fetch_rows(memory)
        assert rows[0]["task_id"] is None
        assert rows[0]["step_id"] is None
        assert rows[0]["episode_id"] is None
```

- [ ] **Step 9.2: Run, confirm pass**

Run: `uv run pytest tests/test_workers/test_llm.py::TestContextVars -v`
Expected: 2 passed.

- [ ] **Step 9.3: Commit**

```bash
git add tests/test_workers/test_llm.py
git commit -m "test(llm): contextvars (task/step/episode) flow into records"
```

---

## Task 10: Usage extraction (missing usage, cache tokens)

**Files:**
- Modify: `tests/test_workers/test_llm.py`

- [ ] **Step 10.1: Add tests**

Append to `tests/test_workers/test_llm.py`:

```python
class TestUsageExtraction:
    async def test_missing_usage_writes_null_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
            # No usage attribute at all
            message = SimpleNamespace(content="hi")
            choice = SimpleNamespace(message=message)
            return SimpleNamespace(choices=[choice])

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        cfg = LLMConfig()
        call = build_llm_call(cfg, memory)
        result = await call("hi")

        assert result == "hi"
        rows = await _fetch_rows(memory)
        assert rows[0]["prompt_tokens"] is None
        assert rows[0]["completion_tokens"] is None
        assert rows[0]["cache_read_tokens"] is None
        assert rows[0]["cache_write_tokens"] is None

    async def test_cache_tokens_are_mapped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
            return _make_response(
                prompt_tokens=50,
                completion_tokens=10,
                cache_creation=30,
                cache_read=80,
            )

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        cfg = LLMConfig()
        call = build_llm_call(cfg, memory)
        await call("hi")

        rows = await _fetch_rows(memory)
        assert rows[0]["cache_write_tokens"] == 30
        assert rows[0]["cache_read_tokens"] == 80
```

- [ ] **Step 10.2: Run, confirm pass**

Run: `uv run pytest tests/test_workers/test_llm.py::TestUsageExtraction -v`
Expected: 2 passed (impl already handles this via `_record`).

- [ ] **Step 10.3: Commit**

```bash
git add tests/test_workers/test_llm.py
git commit -m "test(llm): usage extraction handles missing usage and cache fields"
```

---

## Task 11: Latency measurement + num_retries=0 enforcement

**Files:**
- Modify: `tests/test_workers/test_llm.py`

- [ ] **Step 11.1: Add tests**

Append to `tests/test_workers/test_llm.py`:

```python
class TestTelemetryMechanics:
    async def test_latency_measured(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        async def slow_acompletion(**_kwargs: Any) -> SimpleNamespace:
            import anyio as _anyio

            await _anyio.sleep(0.02)
            return _make_response()

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", slow_acompletion
        )

        cfg = LLMConfig()
        call = build_llm_call(cfg, memory)
        await call("hi")

        rows = await _fetch_rows(memory)
        assert rows[0]["latency_ms"] is not None
        assert rows[0]["latency_ms"] >= 15  # 20 - 5ms slack

    async def test_num_retries_zero_in_kwargs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        seen: list[dict[str, Any]] = []

        async def capture_kwargs(**kwargs: Any) -> SimpleNamespace:
            seen.append(kwargs)
            return _make_response()

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", capture_kwargs
        )

        cfg = LLMConfig()
        call = build_llm_call(cfg, memory)
        await call("hi")

        assert seen[0]["num_retries"] == 0
        assert seen[0]["timeout"] == cfg.request_timeout
```

- [ ] **Step 11.2: Run, confirm pass**

Run: `uv run pytest tests/test_workers/test_llm.py::TestTelemetryMechanics -v`
Expected: 2 passed.

- [ ] **Step 11.3: Commit**

```bash
git add tests/test_workers/test_llm.py
git commit -m "test(llm): latency is measured and num_retries=0 is passed"
```

---

## Task 12: `save_llm_call` failure is non-fatal

**Files:**
- Modify: `tests/test_workers/test_llm.py`

- [ ] **Step 12.1: Add test**

Append to `tests/test_workers/test_llm.py`:

```python
class TestPersistenceIsNonFatal:
    async def test_save_failure_does_not_break_return(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
            return _make_response(content="still works")

        monkeypatch.setattr(
            "proctor.workers.llm.litellm.acompletion", fake_acompletion
        )

        async def broken_save(_record: Any) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(memory, "save_llm_call", broken_save)

        cfg = LLMConfig()
        call = build_llm_call(cfg, memory)

        import logging

        with caplog.at_level(logging.WARNING, logger="proctor.workers.llm"):
            result = await call("hi")

        assert result == "still works"
        assert any("Failed to persist" in rec.message for rec in caplog.records)
```

- [ ] **Step 12.2: Run, confirm pass**

Run: `uv run pytest tests/test_workers/test_llm.py::TestPersistenceIsNonFatal -v`
Expected: 1 passed (impl already catches in `_persist`).

- [ ] **Step 12.3: Commit**

```bash
git add tests/test_workers/test_llm.py
git commit -m "test(llm): save_llm_call failure is logged and swallowed"
```

---

## Task 13: Wire DAG step_runner to set `step_id_ctx`

**Files:**
- Modify: `src/proctor/workflow/engine.py`
- Modify: `tests/test_workflow/test_engine.py`

- [ ] **Step 13.1: Write failing test**

Append to `tests/test_workflow/test_engine.py` (place near existing DAG tests; add `import` at top of file if missing):

```python
from proctor.workers.llm import step_id_ctx


class TestStepIdContext:
    async def test_step_runner_sets_step_id_ctx(self) -> None:
        captured: list[str | None] = []

        async def llm_call_recording_ctx(_prompt: str) -> str:
            captured.append(step_id_ctx.get())
            return "ok"

        from proctor.workflow.engine import WorkflowEngine
        from proctor.workflow.spec import Step, WorkflowMode, WorkflowSpec

        engine = WorkflowEngine(llm_call_recording_ctx)
        spec = WorkflowSpec(
            workflow_id="wf-1",
            mode=WorkflowMode.DAG,
            steps=[
                Step(id="a", description="do a", depends_on=[]),
                Step(id="b", description="do b", depends_on=["a"]),
            ],
        )
        await engine.execute(spec)

        assert set(captured) == {"a", "b"}

    async def test_simple_mode_leaves_step_id_none(self) -> None:
        captured: list[str | None] = []

        async def llm_call_recording_ctx(_prompt: str) -> str:
            captured.append(step_id_ctx.get())
            return "ok"

        from proctor.workflow.engine import WorkflowEngine
        from proctor.workflow.spec import WorkflowMode, WorkflowSpec

        engine = WorkflowEngine(llm_call_recording_ctx)
        spec = WorkflowSpec(
            workflow_id="wf-2", mode=WorkflowMode.SIMPLE, prompt="hi"
        )
        await engine.execute(spec)

        assert captured == [None]
```

Mark the class with `pytestmark` if the file doesn't already have a module-level `pytest.mark.anyio` — check `tests/test_workflow/test_engine.py` header and mirror the existing pattern (same as `test_memory.py`).

- [ ] **Step 13.2: Run, confirm failure**

Run: `uv run pytest tests/test_workflow/test_engine.py::TestStepIdContext -v`
Expected: first test fails (captured contains `None` values).

- [ ] **Step 13.3: Modify `step_runner` in `engine.py`**

In `src/proctor/workflow/engine.py`, add an import at the top:

```python
from proctor.workers.llm import step_id_ctx
```

Replace the body of `step_runner` inside `_execute_dag`:

```python
        async def step_runner(
            step: Step, results: dict[str, StepResult]
        ) -> StepResult:
            """Run a single DAG step by calling LLM with step context."""
            # Build prompt from step inputs and dependency outputs
            dep_context = ""
            for dep_id in step.depends_on:
                dep_result = results.get(dep_id)
                if dep_result and dep_result.output is not None:
                    dep_context += f"\n[{dep_id}]: {dep_result.output}"

            prompt = step.description or step.id
            if dep_context:
                prompt = f"{prompt}\n\nContext:{dep_context}"

            token = step_id_ctx.set(step.id)
            try:
                output = await self._llm_call(prompt)
            finally:
                step_id_ctx.reset(token)
            return StepResult(step_id=step.id, output=output)
```

- [ ] **Step 13.4: Run test, confirm pass**

Run: `uv run pytest tests/test_workflow/test_engine.py -v`
Expected: all pass (existing + new class).

- [ ] **Step 13.5: Full suite + format + types**

Run: `uv run pytest && uv run ruff format . && uv run ruff check . && pyrefly check`
Expected: all green.

- [ ] **Step 13.6: Commit**

```bash
git add src/proctor/workflow/engine.py tests/test_workflow/test_engine.py
git commit -m "feat(workflow): set step_id_ctx per DAG step

DAG step runner now sets/resets step_id_ctx around each llm_call so
llm_calls rows carry the originating step id. Simple mode keeps
step_id as None."
```

---

## Task 14: Wire bootstrap — create `Episode` before execution, set context vars

**Files:**
- Modify: `src/proctor/core/bootstrap.py`
- Modify: `tests/test_core/test_bootstrap.py`

- [ ] **Step 14.1: Read the existing bootstrap test pattern**

Run: `uv run pytest tests/test_core/test_bootstrap.py -v --collect-only`
Review the output to see existing test names. Model new tests after the existing `_handle_terminal` tests.

- [ ] **Step 14.2: Write failing test — episode_id_ctx is set during execute**

Append to `tests/test_core/test_bootstrap.py`:

```python
from proctor.workers.llm import episode_id_ctx, task_id_ctx


class TestContextVarWiring:
    async def test_ids_set_during_workflow_execute(
        self, tmp_path: Path
    ) -> None:
        from proctor.core.bootstrap import Application
        from proctor.core.config import ProctorConfig
        from proctor.core.models import Event

        captured: list[tuple[str | None, str | None]] = []

        async def llm_recording_ctx(_prompt: str) -> str:
            captured.append((task_id_ctx.get(), episode_id_ctx.get()))
            return "ok"

        cfg = ProctorConfig(data_dir=tmp_path)
        app = Application(cfg)
        app.set_llm_call(llm_recording_ctx)
        await app.start()
        try:
            await app._handle_terminal(
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
```

Add `from pathlib import Path` to the imports if not present.

- [ ] **Step 14.3: Run, confirm failure**

Run: `uv run pytest tests/test_core/test_bootstrap.py::TestContextVarWiring -v`
Expected: fails — bootstrap does not currently set the contextvars.

- [ ] **Step 14.4: Update `_handle_terminal` in `bootstrap.py`**

Edit `src/proctor/core/bootstrap.py`:

Add imports at the top, next to existing imports:

```python
from proctor.workers.llm import episode_id_ctx, task_id_ctx
```

Replace the body of `_handle_terminal` (lines 77 to end of method) with:

```python
    async def _handle_terminal(self, event: Event) -> None:
        """Handle terminal trigger events.

        Creates a Task and a pre-execution Episode, sets
        ``task_id_ctx``/``episode_id_ctx``, runs the workflow, then
        updates the Episode row with the real agent_response.
        """
        text = event.payload.get("text", "")
        if not text:
            return

        if self._engine is None:
            await self.bus.publish(
                Event(
                    type="task.failed",
                    source="application",
                    payload={"error": "No LLM configured"},
                )
            )
            return

        task = Task(trigger_event=event.id, spec={"prompt": text})
        await self.state.save_task(task)
        task.status = TaskStatus.RUNNING
        task.updated_at = datetime.now(UTC)
        await self.state.save_task(task)

        # Create a placeholder episode up front so llm_calls rows can
        # reference episode_id. We update agent_response after execute.
        episode = Episode(
            trigger_type=event.source,
            user_input=text,
            agent_response="",
        )
        await self.memory.save_episode(episode)

        spec = WorkflowSpec(
            workflow_id=task.id,
            mode=WorkflowMode.SIMPLE,
            prompt=text,
        )

        task_token = task_id_ctx.set(task.id)
        episode_token = episode_id_ctx.set(episode.id)
        try:
            result = await self._engine.execute(spec)
        except Exception as exc:
            logger.exception("Workflow execution failed")
            task.status = TaskStatus.FAILED
            task.result = {"error": str(exc)}
            task.updated_at = datetime.now(UTC)
            await self.state.save_task(task)

            episode.workflow_result = task.result
            await self.memory.save_episode(episode)

            await self.bus.publish(
                Event(
                    type="task.failed",
                    source="application",
                    payload={"error": str(exc)},
                )
            )
            return
        finally:
            task_id_ctx.reset(task_token)
            episode_id_ctx.reset(episode_token)

        if result.error:
            task.status = TaskStatus.FAILED
            task.result = {"error": result.error}
        else:
            task.status = TaskStatus.COMPLETED
            task.result = {"output": result.output}

        task.updated_at = datetime.now(UTC)
        await self.state.save_task(task)

        episode.agent_response = result.output or ""
        episode.workflow_result = task.result
        await self.memory.save_episode(episode)

        await self.bus.publish(
            Event(
                type=(
                    "task.completed"
                    if task.status == TaskStatus.COMPLETED
                    else "task.failed"
                ),
                source="application",
                payload=task.result,
            )
        )
```

- [ ] **Step 14.5: Run tests, confirm pass**

Run: `uv run pytest tests/test_core/test_bootstrap.py -v`
Expected: existing tests still pass, new `TestContextVarWiring` passes.

- [ ] **Step 14.6: Format + types**

Run: `uv run ruff format . && uv run ruff check . && pyrefly check`
Expected: no errors.

- [ ] **Step 14.7: Commit**

```bash
git add src/proctor/core/bootstrap.py tests/test_core/test_bootstrap.py
git commit -m "feat(bootstrap): create Episode before workflow and set ctx vars

Episode is now saved before engine.execute so llm_calls rows can
reference episode_id. task_id_ctx / episode_id_ctx are set around
the execute call and reset in a finally block. The Episode row is
updated with agent_response after execute (save_episode is already
idempotent via ON CONFLICT(id) DO UPDATE)."
```

---

## Task 15: Wire `__main__.py` to use `build_llm_call`

**Files:**
- Modify: `src/proctor/__main__.py`

- [ ] **Step 15.1: Read current `__main__.py` to see the mock being replaced**

Run: `uv run cat src/proctor/__main__.py` — locate the line where `set_llm_call` is invoked with a mock (likely something like `app.set_llm_call(mock_llm)`).

- [ ] **Step 15.2: Replace the mock wiring**

In `src/proctor/__main__.py`, add at the top:

```python
from proctor.workers.llm import build_llm_call
```

Replace the mock wiring (the `app.set_llm_call(...)` line) with:

```python
    app.set_llm_call(build_llm_call(app.config.llm, app.memory))
```

Move this call **after** `await app.start()` — `build_llm_call` captures `app.memory`, and memory must be initialized first. If the current code calls `set_llm_call` before `start()`, move it or introduce a helper that defers wiring:

If the structure forces `set_llm_call` before `start()` (because `WorkflowEngine` is built in `set_llm_call`), the safe rewrite is:

```python
    cfg = load_config(args.config)
    app = Application(cfg)
    app.set_llm_call(build_llm_call(cfg.llm, app.memory))
    await app.start()
```

This works because `build_llm_call` only reads `app.memory` lazily inside the closure at call time; `memory.initialize()` runs in `start()` before any workflow request arrives.

- [ ] **Step 15.3: Smoke-run the module**

Run: `uv run python -m proctor --help 2>&1 | head -20`
Expected: CLI help renders without import errors.

- [ ] **Step 15.4: Format + types**

Run: `uv run ruff format . && uv run ruff check . && pyrefly check`
Expected: no errors.

- [ ] **Step 15.5: Commit**

```bash
git add src/proctor/__main__.py
git commit -m "feat(main): wire build_llm_call in place of mock

Application entry point now constructs the real LiteLLM-backed
llm_call. Requires provider credentials (ANTHROPIC_API_KEY etc.)
in the environment — LiteLLM reads them directly."
```

---

## Task 16: Register `integration` marker + Ollama integration test

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_llm_ollama.py`

- [ ] **Step 16.1: Register the marker in `pyproject.toml`**

Edit `pyproject.toml`, replace the `[tool.pytest.ini_options]` block:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "strict"
markers = ["integration: requires external services (Ollama, etc.)"]
```

- [ ] **Step 16.2: Create integration package marker**

Create `tests/integration/__init__.py`:

```python
```

(Empty file — just a package marker.)

- [ ] **Step 16.3: Create the integration test**

Create `tests/integration/test_llm_ollama.py`:

```python
"""Integration test against a locally running Ollama server.

Skipped automatically when Ollama is not reachable at localhost:11434.
Run explicitly with: uv run pytest -m integration
"""

from pathlib import Path

import aiohttp
import pytest

from proctor.core.config import LLMConfig
from proctor.core.memory import EpisodicMemory
from proctor.workers.llm import build_llm_call

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _ollama_up() -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://localhost:11434/api/tags",
                timeout=aiohttp.ClientTimeout(total=2),
            ) as resp:
                return resp.status == 200
    except Exception:
        return False


async def test_ollama_real_call(tmp_path: Path) -> None:
    if not await _ollama_up():
        pytest.skip("Ollama not running at localhost:11434")

    cfg = LLMConfig(
        default_model="ollama/llama3.2",
        fallback_model=None,
        max_retries=0,
        request_timeout=30.0,
    )
    memory = EpisodicMemory(tmp_path / "episodes.db")
    await memory.initialize()
    try:
        call = build_llm_call(cfg, memory)
        result = await call("Say 'hello' and nothing else.")
        assert result.strip()  # non-empty response
    finally:
        await memory.close()
```

- [ ] **Step 16.4: Confirm integration test is skipped by default**

Run: `uv run pytest -v`
Expected: all unit tests pass; `test_ollama_real_call` does not run (unless Ollama happens to be up, in which case it passes). No marker warnings in output.

- [ ] **Step 16.5: Confirm integration test runs explicitly**

Run: `uv run pytest -m integration -v`
Expected: one test run, either passes (if Ollama is up) or skips with `"Ollama not running"` reason.

- [ ] **Step 16.6: Commit**

```bash
git add pyproject.toml tests/integration/
git commit -m "test(llm): integration test against local Ollama

Registers the 'integration' pytest marker. Test skips automatically
when Ollama is not reachable. Run with: uv run pytest -m integration."
```

---

## Task 17: README — running integration tests

**Files:**
- Modify: `README.md`

- [ ] **Step 17.1: Locate an appropriate section**

Run: `uv run head -80 README.md` to find the existing "Testing" / "Development" section.

- [ ] **Step 17.2: Add a subsection**

Append this subsection under the existing test/development instructions (pick the nearest-matching heading):

```markdown
### Running integration tests

Integration tests against external services (currently: Ollama) are
marked with the `integration` pytest marker and skipped by default.

To run them, start a local Ollama server and pull the default model:

```bash
ollama serve
ollama pull llama3.2
```

Then:

```bash
uv run pytest -m integration
```

If Ollama is not reachable at `localhost:11434`, the tests skip
cleanly — no action required for the default `uv run pytest` run.
```

- [ ] **Step 17.3: Commit**

```bash
git add README.md
git commit -m "docs: explain how to run integration tests"
```

---

## Final verification

- [ ] **Step F.1: Full test suite (unit only)**

Run: `uv run pytest -v`
Expected: all pass; `test_ollama_real_call` skipped (or passes if Ollama is up).

- [ ] **Step F.2: With integration if Ollama available**

Run: `uv run pytest -m integration -v`
Expected: pass if Ollama running, skip with clear reason otherwise.

- [ ] **Step F.3: Format + lint + types**

Run: `uv run ruff format . && uv run ruff check . && pyrefly check`
Expected: clean.

- [ ] **Step F.4: Confirm AC from the spec**

Cross-check against the Acceptance criteria block in
`docs/superpowers/specs/2026-04-15-litellm-integration-design.md`:

- `src/proctor/workers/llm.py` exports `build_llm_call` ✓ (Task 3)
- Config fields read from `LLMConfig` ✓ (Task 1)
- Retry + fallback on transient only, non-transient propagates ✓ (Tasks 5–8)
- `WARNING` on fallback engagement ✓ (Task 6)
- `save_llm_call` failures are non-fatal ✓ (Tasks 3, 12)
- Token accounting in `llm_calls` ✓ (Tasks 2, 10)
- Unit tests for all enumerated cases ✓ (Tasks 3–12)
- Integration test against Ollama (skip if unavailable) ✓ (Task 16)
- README updated ✓ (Task 17)

- [ ] **Step F.5: Push branch and open PR against `master`**

```bash
git push -u origin <current-branch>
gh pr create --title "feat: LiteLLM integration (LABS-67)" --body "$(cat <<'EOF'
## Summary
- Real `litellm.acompletion`-backed LLM call replaces the mock in `WorkflowEngine` (LABS-67)
- New `llm_calls` table in `episodes.db` persists every attempt with token counts, latency, model, fallback flag, and error
- Retry on transient errors (default 1 retry) before falling back to `fallback_model` (opt-in, default `None`)
- Context propagation via `contextvars` — `task_id`, `step_id`, `episode_id` attached to every record

## Test plan
- [x] Unit tests (happy path, explicit override, retry, fallback, non-transient, contextvars, usage, cache tokens, latency, num_retries=0, non-fatal persistence)
- [x] Memory tests for `llm_calls` round-trip, indexes, idempotent init
- [ ] Integration test against local Ollama (`uv run pytest -m integration`)

Related spec: `docs/superpowers/specs/2026-04-15-litellm-integration-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review notes

- **Spec coverage:** every requirement from the spec's AC maps to at least one task (see `F.4`).
- **Placeholder scan:** every step has concrete code/commands. The only conditional branch is in Task 15 (depending on current `__main__.py` shape) — the engineer reads the file first and picks the right edit.
- **Type consistency:** `LLMCallRecord` fields match SQL columns; `ContextVar` names (`task_id_ctx`, `step_id_ctx`, `episode_id_ctx`) used identically throughout tests and source; `_TRANSIENT` tuple shared between retry and fallback branches.
- **Cross-file dependencies:** Task 13 (`engine.py`) and Task 14 (`bootstrap.py`) both import from `proctor.workers.llm` (created in Task 3), so task ordering is critical.
- **Scope:** tool-calling wiring, system prompts, and Router changes are intentionally excluded (tracked in separate issues noted in the spec's "Out of scope" section).
