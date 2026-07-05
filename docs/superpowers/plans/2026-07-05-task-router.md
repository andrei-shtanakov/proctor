# TaskRouter (M4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admission layer between the event→workflow Router and the WorkflowEngine: four safety invariants, a TTL pending queue, and the Phase 3 scoring seam, per `docs/superpowers/specs/2026-07-05-task-router-design.md`.

**Architecture:** New package `src/proctor/router/` (models, invariants, scoring, queue, TaskRouter facade). Bootstrap calls `TaskRouter.admit()` between `Router.route()` and `engine.execute()`; a tick-loop in `Application` expires and re-checks the queue. Shared glob heuristics move to `core/globs.py`.

**Tech Stack:** Python 3.12, pydantic 2.x, anyio/asyncio, pytest + anyio.

## Global Constraints

- Package manager: **uv only** (`uv run pytest`, `uv add`), never pip.
- Line length 88; run `uv run ruff format .` and `uv run ruff check .` before each commit.
- Type check: `uv run pyrefly check` after every change; type hints required everywhere.
- All models are pydantic `BaseModel`; async tests use **anyio, not asyncio** (`pytestmark = pytest.mark.anyio` + `anyio_backend` fixture returning `"asyncio"` where aiosqlite is involved).
- `admit()` MUST mutate running-set/slots **before its first `await`**; `routing.*` published only after reservation (spec §Admission atomicity).
- Queue entries use `expires_at`; never reuse the name `deadline` (collides with `Task.deadline`, a run-deadline).
- Branch: `feat/task-router`. Frequent commits, TDD per task.

---

### Task 1: Shared glob helpers — `core/globs.py`

**Files:**
- Create: `src/proctor/core/globs.py`
- Modify: `src/proctor/core/config.py` (remove `_is_strictly_broader`, import from globs)
- Test: `tests/test_core/test_globs.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `is_strictly_broader(a: str, b: str) -> bool`, `patterns_overlap(a: str, b: str) -> bool` — Task 3 imports `patterns_overlap`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_core/test_globs.py
"""Tests for shared fnmatch-glob heuristics."""

from proctor.core.globs import is_strictly_broader, patterns_overlap


class TestIsStrictlyBroader:
    def test_wildcard_subsumes_literal(self) -> None:
        assert is_strictly_broader("trigger.*", "trigger.terminal")

    def test_equal_patterns_not_strict(self) -> None:
        assert not is_strictly_broader("trigger.terminal", "trigger.terminal")

    def test_narrower_is_not_broader(self) -> None:
        assert not is_strictly_broader("trigger.terminal", "trigger.*")


class TestPatternsOverlap:
    def test_identical_literals(self) -> None:
        assert patterns_overlap("src/main.py", "src/main.py")

    def test_glob_covers_literal(self) -> None:
        assert patterns_overlap("src/**", "src/foo/bar.py")

    def test_literal_under_glob_reversed(self) -> None:
        assert patterns_overlap("src/foo/bar.py", "src/**")

    def test_path_prefix_without_wildcard(self) -> None:
        # fnmatch alone would miss this: "src" does not fnmatch "src/foo.py"
        assert patterns_overlap("src", "src/foo.py")

    def test_disjoint_trees(self) -> None:
        assert not patterns_overlap("src/**", "docs/**")

    def test_disjoint_literals(self) -> None:
        assert not patterns_overlap("src/a.py", "src/b.py")

    def test_sibling_prefix_not_confused(self) -> None:
        # "src" is not a path-prefix of "srcx/foo.py"
        assert not patterns_overlap("src", "srcx/foo.py")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core/test_globs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'proctor.core.globs'`

- [ ] **Step 3: Implement `core/globs.py`**

```python
# src/proctor/core/globs.py
"""Shared fnmatch-glob heuristics.

One home for the glob-heuristic family: config validation uses
subsumption (`is_strictly_broader`), the TaskRouter scope invariant
uses overlap (`patterns_overlap`). Both are heuristics over fnmatch
semantics — deliberately conservative, favouring false positives.
"""

from fnmatch import fnmatchcase


def is_strictly_broader(a: str, b: str) -> bool:
    """True if fnmatch pattern ``a`` strictly subsumes pattern ``b``.

    Heuristic: treat ``b`` as a literal string. If ``fnmatch(b, a)``
    matches and ``fnmatch(a, b)`` does not, then ``a`` covers every
    concrete value that ``b`` covers, plus more.
    """
    return fnmatchcase(b, a) and not fnmatchcase(a, b)


def _is_path_prefix(prefix: str, path: str) -> bool:
    """True if ``prefix`` is a whole-segment path prefix of ``path``."""
    return path.startswith(prefix.rstrip("/") + "/")


def patterns_overlap(a: str, b: str) -> bool:
    """Conservative overlap test between two scope globs.

    Two patterns conflict if either fnmatches the other or one is a
    path-prefix of the other. May report overlap where none exists
    (queues a runnable task); must not miss a real conflict.
    """
    return (
        fnmatchcase(a, b)
        or fnmatchcase(b, a)
        or _is_path_prefix(a, b)
        or _is_path_prefix(b, a)
    )
```

- [ ] **Step 4: Rewire `config.py`**

In `src/proctor/core/config.py`: delete the `_is_strictly_broader` function (currently ~line 309) and add to the imports block:

```python
from proctor.core.globs import is_strictly_broader
```

Then rename its call sites (grep `_is_strictly_broader` inside config.py, drop the underscore). Keep `fnmatchcase` import only if still used elsewhere in the file.

- [ ] **Step 5: Run tests + full suite**

Run: `uv run pytest tests/test_core/test_globs.py tests/test_core/ -q`
Expected: all PASS (config tests still green — the move is behaviour-preserving).

- [ ] **Step 6: Quality gates + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/core/globs.py src/proctor/core/config.py tests/test_core/test_globs.py
git commit -m "refactor(core): extract shared glob heuristics into core/globs.py"
```

---

### Task 2: Router models + scoring seam

**Files:**
- Create: `src/proctor/router/__init__.py`, `src/proctor/router/models.py`, `src/proctor/router/scoring.py`
- Test: `tests/test_router/__init__.py`, `tests/test_router/test_scoring.py`

**Interfaces:**
- Consumes: `Task` from `proctor.core.models`, `WorkflowSpec` from `proctor.workflow.spec`.
- Produces: `AgentProfile(id, capabilities, max_slots)`, `Candidate(profile, score)`, `RunningTask(task_id, agent_id, scope, branch)`, `AdmitDecision(verdict, reason)`, `QueueEntry(task, spec, trigger_source, enqueued_at, expires_at, reason)`, `score_candidates(spec, agents) -> list[Candidate]`. Tasks 3–6 import these exact names.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_router/__init__.py  (empty file)
```

```python
# tests/test_router/test_scoring.py
"""Tests for the v1 scoring seam."""

from proctor.router.models import AgentProfile
from proctor.router.scoring import score_candidates
from proctor.workflow.spec import WorkflowMode, WorkflowSpec


def _spec() -> WorkflowSpec:
    return WorkflowSpec(workflow_id="w", mode=WorkflowMode.SIMPLE)


def test_single_agent_scores_one() -> None:
    agents = [AgentProfile(id="local", max_slots=4)]
    candidates = score_candidates(_spec(), agents)
    assert len(candidates) == 1
    assert candidates[0].profile.id == "local"
    assert candidates[0].score == 1.0


def test_order_preserved() -> None:
    agents = [AgentProfile(id="a"), AgentProfile(id="b")]
    candidates = score_candidates(_spec(), agents)
    assert [c.profile.id for c in candidates] == ["a", "b"]


def test_empty_agents() -> None:
    assert score_candidates(_spec(), []) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_router/ -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'proctor.router'`

- [ ] **Step 3: Implement models and scoring**

```python
# src/proctor/router/__init__.py
"""TaskRouter — admission layer (M4): invariants, TTL queue, scoring."""

from proctor.router.models import (
    AdmitDecision,
    AgentProfile,
    Candidate,
    QueueEntry,
    RunningTask,
)
from proctor.router.router import TaskRouter

__all__ = [
    "AdmitDecision",
    "AgentProfile",
    "Candidate",
    "QueueEntry",
    "RunningTask",
    "TaskRouter",
]
```

Note: `router.py` does not exist until Task 5 — create `__init__.py` in this task WITHOUT the `TaskRouter` import/export (add both lines in Task 5), i.e. for now:

```python
# src/proctor/router/__init__.py  (Task 2 version)
"""TaskRouter — admission layer (M4): invariants, TTL queue, scoring."""

from proctor.router.models import (
    AdmitDecision,
    AgentProfile,
    Candidate,
    QueueEntry,
    RunningTask,
)

__all__ = [
    "AdmitDecision",
    "AgentProfile",
    "Candidate",
    "QueueEntry",
    "RunningTask",
]
```

```python
# src/proctor/router/models.py
"""Data models for the TaskRouter admission layer."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from proctor.core.models import Task
from proctor.workflow.spec import WorkflowSpec


class AgentProfile(BaseModel):
    """An execution candidate. v1: the single local AgentRuntime."""

    id: str
    capabilities: list[str] = Field(default_factory=list)
    max_slots: int = 4


class Candidate(BaseModel):
    """A scored agent candidate for a task."""

    profile: AgentProfile
    score: float


class RunningTask(BaseModel):
    """TaskRouter's bookkeeping view of an admitted task."""

    task_id: str
    agent_id: str
    scope: list[str] = Field(default_factory=list)
    branch: str | None = None


class AdmitDecision(BaseModel):
    """Outcome of TaskRouter.admit()."""

    verdict: Literal["admitted", "queued", "rejected"]
    reason: str | None = None


class QueueEntry(BaseModel):
    """A blocked task waiting in the pending queue.

    ``expires_at`` is the admit-TTL — deliberately NOT ``Task.deadline``
    (run-deadline, a different lifecycle stage). ``trigger_source`` is
    opaque passthrough so bootstrap can build the Episode later.
    """

    task: Task
    spec: WorkflowSpec
    trigger_source: str
    enqueued_at: datetime
    expires_at: datetime
    reason: str
```

```python
# src/proctor/router/scoring.py
"""Capability scoring — the seam Phase 3 fills with real candidates."""

from proctor.router.models import AgentProfile, Candidate
from proctor.workflow.spec import WorkflowSpec


def score_candidates(
    spec: WorkflowSpec, agents: list[AgentProfile]
) -> list[Candidate]:
    """Score agents for a spec. v1: every agent scores 1.0, order kept.

    Phase 3 replaces the body with capability matching against the
    worker registry; the signature is the contract.
    """
    return [Candidate(profile=agent, score=1.0) for agent in agents]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_router/ -v`
Expected: 3 PASS

- [ ] **Step 5: Quality gates + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/router/ tests/test_router/
git commit -m "feat(router): models + v1 scoring seam"
```

---

### Task 3: Invariants — `router/invariants.py`

**Files:**
- Create: `src/proctor/router/invariants.py`
- Test: `tests/test_router/test_invariants.py`

**Interfaces:**
- Consumes: `patterns_overlap` (Task 1), `AgentProfile`, `RunningTask` (Task 2).
- Produces: `check_concurrency_limit(running, max_concurrency) -> str | None`, `check_agent_available(profile, running) -> str | None`, `check_scope_isolation(scope, running) -> str | None`, `check_branch_not_locked(branch, running) -> str | None`. `None` = pass; string = human-readable block reason. Task 5 composes them.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_router/test_invariants.py
"""Unit tests for the four critical admission invariants."""

from proctor.router.invariants import (
    check_agent_available,
    check_branch_not_locked,
    check_concurrency_limit,
    check_scope_isolation,
)
from proctor.router.models import AgentProfile, RunningTask


def _running(n: int, agent_id: str = "local") -> list[RunningTask]:
    return [
        RunningTask(task_id=f"t{i}", agent_id=agent_id) for i in range(n)
    ]


class TestConcurrencyLimit:
    def test_below_limit_passes(self) -> None:
        assert check_concurrency_limit(_running(3), 4) is None

    def test_exactly_at_limit_blocks(self) -> None:
        reason = check_concurrency_limit(_running(4), 4)
        assert reason is not None
        assert "concurrency_limit" in reason

    def test_empty_running_passes(self) -> None:
        assert check_concurrency_limit([], 1) is None


class TestAgentAvailable:
    def test_free_slot_passes(self) -> None:
        profile = AgentProfile(id="local", max_slots=2)
        assert check_agent_available(profile, _running(1)) is None

    def test_full_slots_block(self) -> None:
        profile = AgentProfile(id="local", max_slots=2)
        reason = check_agent_available(profile, _running(2))
        assert reason is not None
        assert "agent_available" in reason

    def test_other_agents_tasks_do_not_count(self) -> None:
        profile = AgentProfile(id="local", max_slots=1)
        running = _running(3, agent_id="remote")
        assert check_agent_available(profile, running) is None


class TestScopeIsolation:
    def test_empty_scope_never_conflicts(self) -> None:
        running = [
            RunningTask(task_id="t", agent_id="a", scope=["src/**"])
        ]
        assert check_scope_isolation([], running) is None

    def test_running_without_scope_never_conflicts(self) -> None:
        running = [RunningTask(task_id="t", agent_id="a")]
        assert check_scope_isolation(["src/**"], running) is None

    def test_overlapping_globs_block(self) -> None:
        running = [
            RunningTask(task_id="t", agent_id="a", scope=["src/**"])
        ]
        reason = check_scope_isolation(["src/foo.py"], running)
        assert reason is not None
        assert "scope_isolation" in reason
        assert "t" in reason

    def test_disjoint_globs_pass(self) -> None:
        running = [
            RunningTask(task_id="t", agent_id="a", scope=["docs/**"])
        ]
        assert check_scope_isolation(["src/**"], running) is None


class TestBranchNotLocked:
    def test_none_branch_passes(self) -> None:
        running = [
            RunningTask(task_id="t", agent_id="a", branch="release")
        ]
        assert check_branch_not_locked(None, running) is None

    def test_same_branch_blocks(self) -> None:
        running = [
            RunningTask(task_id="t", agent_id="a", branch="release")
        ]
        reason = check_branch_not_locked("release", running)
        assert reason is not None
        assert "branch_not_locked" in reason

    def test_different_branch_passes(self) -> None:
        running = [
            RunningTask(task_id="t", agent_id="a", branch="main")
        ]
        assert check_branch_not_locked("release", running) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_router/test_invariants.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'proctor.router.invariants'`

- [ ] **Step 3: Implement invariants**

```python
# src/proctor/router/invariants.py
"""The four critical admission invariants (arch plan M4).

Each check is a pure function: ``None`` means pass, a string is the
human-readable block reason (prefixed with the invariant name).
"""

from proctor.core.globs import patterns_overlap
from proctor.router.models import AgentProfile, RunningTask


def check_concurrency_limit(
    running: list[RunningTask], max_concurrency: int
) -> str | None:
    """Block when the global running count is at the limit."""
    if len(running) >= max_concurrency:
        return (
            f"concurrency_limit: {len(running)}/{max_concurrency} "
            "tasks running"
        )
    return None


def check_agent_available(
    profile: AgentProfile, running: list[RunningTask]
) -> str | None:
    """Block when the candidate agent has no free slot.

    Bookkeeping over what TaskRouter admitted — not a live load query
    (AgentRuntime has no slot concept until Phase 3).
    """
    used = sum(1 for r in running if r.agent_id == profile.id)
    if used >= profile.max_slots:
        return (
            f"agent_available: agent {profile.id!r} has no free slots "
            f"({used}/{profile.max_slots})"
        )
    return None


def check_scope_isolation(
    scope: list[str], running: list[RunningTask]
) -> str | None:
    """Block when any scope glob overlaps a running task's scope."""
    for r in running:
        for ours in scope:
            for theirs in r.scope:
                if patterns_overlap(ours, theirs):
                    return (
                        f"scope_isolation: {ours!r} overlaps {theirs!r} "
                        f"held by task {r.task_id}"
                    )
    return None


def check_branch_not_locked(
    branch: str | None, running: list[RunningTask]
) -> str | None:
    """Block when the exact branch is held by a running task."""
    if branch is None:
        return None
    for r in running:
        if r.branch == branch:
            return (
                f"branch_not_locked: branch {branch!r} held by "
                f"task {r.task_id}"
            )
    return None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_router/test_invariants.py -v`
Expected: 12 PASS

- [ ] **Step 5: Quality gates + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/router/invariants.py tests/test_router/test_invariants.py
git commit -m "feat(router): four critical admission invariants"
```

---

### Task 4: Pending queue — `router/queue.py`

**Files:**
- Create: `src/proctor/router/queue.py`
- Test: `tests/test_router/test_queue.py`

**Interfaces:**
- Consumes: `QueueEntry` (Task 2).
- Produces: `PendingQueue` with `push(entry)`, `__len__()`, `pop_expired(now: datetime) -> list[QueueEntry]`, `pop_admissible(try_admit: Callable[[QueueEntry], bool]) -> list[QueueEntry]`. Pure structure: no I/O, no clock — `now` is injected. Task 5 drives it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_router/test_queue.py
"""Unit tests for PendingQueue — pure FIFO with injected clock."""

from datetime import UTC, datetime, timedelta

from proctor.core.models import Task
from proctor.router.models import QueueEntry
from proctor.router.queue import PendingQueue
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

T0 = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _entry(name: str, ttl_seconds: float = 60.0) -> QueueEntry:
    return QueueEntry(
        task=Task(),
        spec=WorkflowSpec(workflow_id=name, mode=WorkflowMode.SIMPLE),
        trigger_source="test",
        enqueued_at=T0,
        expires_at=T0 + timedelta(seconds=ttl_seconds),
        reason="test block",
    )


def test_fifo_order_preserved() -> None:
    q = PendingQueue()
    first, second = _entry("first"), _entry("second")
    q.push(first)
    q.push(second)
    popped = q.pop_admissible(lambda e: True)
    assert [e.spec.workflow_id for e in popped] == ["first", "second"]
    assert len(q) == 0


def test_pop_admissible_keeps_blocked_entries() -> None:
    q = PendingQueue()
    q.push(_entry("blocked"))
    q.push(_entry("ready"))
    popped = q.pop_admissible(lambda e: e.spec.workflow_id == "ready")
    assert [e.spec.workflow_id for e in popped] == ["ready"]
    assert len(q) == 1


def test_pop_expired_returns_each_entry_once() -> None:
    q = PendingQueue()
    q.push(_entry("old", ttl_seconds=10))
    q.push(_entry("fresh", ttl_seconds=120))
    now = T0 + timedelta(seconds=60)
    expired = q.pop_expired(now)
    assert [e.spec.workflow_id for e in expired] == ["old"]
    assert q.pop_expired(now) == []
    assert len(q) == 1


def test_boundary_exactly_at_expiry_is_expired() -> None:
    q = PendingQueue()
    q.push(_entry("edge", ttl_seconds=60))
    expired = q.pop_expired(T0 + timedelta(seconds=60))
    assert len(expired) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_router/test_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'proctor.router.queue'`

- [ ] **Step 3: Implement PendingQueue**

```python
# src/proctor/router/queue.py
"""PendingQueue — FIFO of blocked tasks with injected clock.

Pure data structure: no I/O, no clock reads. The caller passes ``now``
and the admission predicate; TaskRouter owns all side effects.
"""

from collections.abc import Callable
from datetime import datetime

from proctor.router.models import QueueEntry


class PendingQueue:
    """FIFO queue of QueueEntry with TTL expiry."""

    def __init__(self) -> None:
        self._entries: list[QueueEntry] = []

    def __len__(self) -> int:
        return len(self._entries)

    def push(self, entry: QueueEntry) -> None:
        """Append an entry at the tail."""
        self._entries.append(entry)

    def pop_expired(self, now: datetime) -> list[QueueEntry]:
        """Remove and return entries whose ``expires_at`` <= now."""
        expired = [e for e in self._entries if e.expires_at <= now]
        self._entries = [e for e in self._entries if e.expires_at > now]
        return expired

    def pop_admissible(
        self, try_admit: Callable[[QueueEntry], bool]
    ) -> list[QueueEntry]:
        """Scan FIFO; remove and return entries ``try_admit`` accepts.

        ``try_admit`` is expected to commit a reservation when it
        returns True (TaskRouter passes a reserving closure), so
        later entries see the effect of earlier admissions.
        """
        admitted: list[QueueEntry] = []
        remaining: list[QueueEntry] = []
        for entry in self._entries:
            if try_admit(entry):
                admitted.append(entry)
            else:
                remaining.append(entry)
        self._entries = remaining
        return admitted
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_router/test_queue.py -v`
Expected: 4 PASS

- [ ] **Step 5: Quality gates + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/router/queue.py tests/test_router/test_queue.py
git commit -m "feat(router): PendingQueue — pure FIFO with TTL"
```

---

### Task 5: Config section + WorkflowSpec fields + TaskRouter facade

**Files:**
- Modify: `src/proctor/core/config.py` (add `RouterAgentConfig`, `RouterConfig`, `ProctorConfig.router`)
- Modify: `src/proctor/workflow/spec.py` (add `scope`, `branch` to `WorkflowSpec`)
- Create: `src/proctor/router/router.py`
- Modify: `src/proctor/router/__init__.py` (export `TaskRouter`)
- Test: `tests/test_router/test_task_router.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4; `EventBus` (`publish(event)`), `Event`, `Task`.
- Produces:
  - `RouterConfig(max_concurrency: int = 4, queue_ttl_seconds: float = 600.0, queue_tick_seconds: float = 30.0, agent: RouterAgentConfig)`; `RouterAgentConfig(max_slots: int = 4)`; `ProctorConfig.router: RouterConfig`.
  - `WorkflowSpec.scope: list[str]` (default `[]`), `WorkflowSpec.branch: str | None` (default `None`).
  - `TaskRouter(bus, config, agents)` with `async admit(task, spec, trigger_source, now=None) -> AdmitDecision`, `async release(task_id, now=None) -> list[QueueEntry]`, `async dequeue_ready(now=None) -> list[QueueEntry]`, `async expire_overdue(now=None) -> list[QueueEntry]`, property `running_count: int`. Task 6 wires these into bootstrap.

- [ ] **Step 1: Add config models and spec fields**

In `src/proctor/core/config.py`, next to the other nested config classes:

```python
class RouterAgentConfig(BaseModel):
    """Slot budget of the single local agent (Phase 2)."""

    max_slots: int = 4


class RouterConfig(BaseModel):
    """TaskRouter admission settings."""

    max_concurrency: int = 4
    queue_ttl_seconds: float = 600.0  # 0 = reject immediately, never queue
    queue_tick_seconds: float = 30.0
    agent: RouterAgentConfig = RouterAgentConfig()
```

In `ProctorConfig`, after `events: EventsConfig = EventsConfig()`:

```python
    router: RouterConfig = RouterConfig()
```

In `src/proctor/workflow/spec.py`, add to `WorkflowSpec` after `channels`:

```python
    scope: list[str] = Field(default_factory=list)
    branch: str | None = None
```

- [ ] **Step 2: Write the failing TaskRouter tests**

```python
# tests/test_router/test_task_router.py
"""Unit tests for the TaskRouter facade."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import anyio
import pytest

from proctor.core.bus import EventBus
from proctor.core.config import RouterAgentConfig, RouterConfig
from proctor.core.models import Event, Task
from proctor.core.transport import LocalEventTransport
from proctor.router.models import AgentProfile
from proctor.router.router import TaskRouter
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _spec(name: str = "w", **kwargs: object) -> WorkflowSpec:
    return WorkflowSpec(workflow_id=name, mode=WorkflowMode.SIMPLE, **kwargs)


def _router(bus: EventBus, **overrides: object) -> TaskRouter:
    config = RouterConfig(
        max_concurrency=2,
        queue_ttl_seconds=60.0,
        agent=RouterAgentConfig(max_slots=2),
        **overrides,
    )
    return TaskRouter(
        bus=bus,
        config=config,
        agents=[AgentProfile(id="local", max_slots=config.agent.max_slots)],
    )


@pytest.fixture
async def bus() -> AsyncGenerator[EventBus, None]:
    b = EventBus(LocalEventTransport())
    await b.start()
    yield b
    await b.stop()


@pytest.fixture
def routing_events(bus: EventBus) -> list[Event]:
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    bus.subscribe("routing.>", collect)
    return collected


async def test_admit_within_limits(bus: EventBus) -> None:
    router = _router(bus)
    decision = await router.admit(Task(), _spec(), "test", now=T0)
    assert decision.verdict == "admitted"
    assert router.running_count == 1


async def test_blocked_task_queues(
    bus: EventBus, routing_events: list[Event]
) -> None:
    router = _router(bus, max_concurrency=1)
    await router.admit(Task(), _spec("a"), "test", now=T0)
    decision = await router.admit(Task(), _spec("b"), "test", now=T0)
    assert decision.verdict == "queued"
    assert decision.reason is not None
    assert "concurrency_limit" in decision.reason
    await bus.flush()
    assert [e.type for e in routing_events] == ["routing.queued"]


async def test_ttl_zero_rejects(
    bus: EventBus, routing_events: list[Event]
) -> None:
    router = _router(bus, max_concurrency=1, queue_ttl_seconds=0.0)
    await router.admit(Task(), _spec("a"), "test", now=T0)
    decision = await router.admit(Task(), _spec("b"), "test", now=T0)
    assert decision.verdict == "rejected"
    await bus.flush()
    assert [e.type for e in routing_events] == ["routing.rejected"]


async def test_release_dequeues_fifo(
    bus: EventBus, routing_events: list[Event]
) -> None:
    router = _router(bus, max_concurrency=1)
    first = Task()
    await router.admit(first, _spec("a"), "test", now=T0)
    await router.admit(Task(), _spec("b"), "test", now=T0)
    ready = await router.release(first.id, now=T0 + timedelta(seconds=5))
    assert [e.spec.workflow_id for e in ready] == ["b"]
    assert router.running_count == 1  # b's slot re-reserved
    await bus.flush()
    types = [e.type for e in routing_events]
    assert types == ["routing.queued", "routing.dequeued"]
    dequeued = routing_events[1]
    assert dequeued.payload["waited_seconds"] == 5.0


async def test_expire_overdue_emits_expired(
    bus: EventBus, routing_events: list[Event]
) -> None:
    router = _router(bus, max_concurrency=1, queue_ttl_seconds=30.0)
    await router.admit(Task(), _spec("a"), "test", now=T0)
    await router.admit(Task(), _spec("b"), "test", now=T0)
    expired = await router.expire_overdue(now=T0 + timedelta(seconds=31))
    assert [e.spec.workflow_id for e in expired] == ["b"]
    await bus.flush()
    types = [e.type for e in routing_events]
    assert types == ["routing.queued", "routing.expired"]


async def test_scope_conflict_queues(bus: EventBus) -> None:
    router = _router(bus)
    await router.admit(
        Task(), _spec("a", scope=["src/**"]), "test", now=T0
    )
    decision = await router.admit(
        Task(), _spec("b", scope=["src/foo.py"]), "test", now=T0
    )
    assert decision.verdict == "queued"
    assert decision.reason is not None
    assert "scope_isolation" in decision.reason


async def test_branch_conflict_queues(bus: EventBus) -> None:
    router = _router(bus)
    await router.admit(Task(), _spec("a", branch="rel"), "test", now=T0)
    decision = await router.admit(
        Task(), _spec("b", branch="rel"), "test", now=T0
    )
    assert decision.verdict == "queued"


async def test_concurrent_admit_race(bus: EventBus) -> None:
    """Spec §Admission atomicity: two admits at max_concurrency=1."""
    router = _router(bus, max_concurrency=1)
    decisions: list[str] = []

    async def admit_one() -> None:
        d = await router.admit(Task(), _spec(), "test", now=T0)
        decisions.append(d.verdict)

    async with anyio.create_task_group() as tg:
        tg.start_soon(admit_one)
        tg.start_soon(admit_one)

    assert sorted(decisions) == ["admitted", "queued"]
    assert router.running_count == 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_router/test_task_router.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'proctor.router.router'`

- [ ] **Step 4: Implement TaskRouter**

```python
# src/proctor/router/router.py
"""TaskRouter — the M4 admission facade.

Decides whether an already-routed task may run *now*: reserves a slot
(admitted), parks it in the TTL queue (queued), or fails it fast
(rejected, when ``queue_ttl_seconds`` is 0). Never executes anything —
execution stays in bootstrap.

Atomicity: reservation mutates ``_running`` synchronously, before the
first ``await``. The local transport dispatches each handler as its own
asyncio task, so two ``admit()`` calls interleave only at await points;
mutating first makes the check-then-reserve step atomic. ``routing.*``
events are published only after the reservation is committed.
"""

import logging
from datetime import UTC, datetime, timedelta

from proctor.core.bus import EventBus
from proctor.core.config import RouterConfig
from proctor.core.models import Event, Task
from proctor.router.invariants import (
    check_agent_available,
    check_branch_not_locked,
    check_concurrency_limit,
    check_scope_isolation,
)
from proctor.router.models import (
    AdmitDecision,
    AgentProfile,
    QueueEntry,
    RunningTask,
)
from proctor.router.queue import PendingQueue
from proctor.router.scoring import score_candidates
from proctor.workflow.spec import WorkflowSpec

logger = logging.getLogger(__name__)

_SOURCE = "task_router"


class TaskRouter:
    """Admission control: invariants + TTL queue + scoring seam."""

    def __init__(
        self,
        bus: EventBus,
        config: RouterConfig,
        agents: list[AgentProfile],
    ) -> None:
        self._bus = bus
        self._config = config
        self._agents = agents
        self._running: list[RunningTask] = []
        self._queue = PendingQueue()

    @property
    def running_count(self) -> int:
        """Number of currently reserved (admitted) tasks."""
        return len(self._running)

    def _check(self, spec: WorkflowSpec, profile: AgentProfile) -> str | None:
        return (
            check_concurrency_limit(self._running, self._config.max_concurrency)
            or check_agent_available(profile, self._running)
            or check_scope_isolation(spec.scope, self._running)
            or check_branch_not_locked(spec.branch, self._running)
        )

    def _try_reserve(self, task: Task, spec: WorkflowSpec) -> str | None:
        """Reserve a slot synchronously. None = committed, str = reason.

        MUST stay free of awaits — this is the atomic section.
        """
        reason = "no agent candidates"
        for candidate in score_candidates(spec, self._agents):
            reason = self._check(spec, candidate.profile)
            if reason is None:
                self._running.append(
                    RunningTask(
                        task_id=task.id,
                        agent_id=candidate.profile.id,
                        scope=spec.scope,
                        branch=spec.branch,
                    )
                )
                return None
        return reason

    async def admit(
        self,
        task: Task,
        spec: WorkflowSpec,
        trigger_source: str,
        now: datetime | None = None,
    ) -> AdmitDecision:
        """Admit, queue, or reject a routed task."""
        now = now or datetime.now(UTC)
        reason = self._try_reserve(task, spec)  # atomic: no await above
        if reason is None:
            return AdmitDecision(verdict="admitted")

        if self._config.queue_ttl_seconds <= 0:
            logger.warning("Task %s rejected: %s", task.id, reason)
            await self._bus.publish(
                Event(
                    type="routing.rejected",
                    source=_SOURCE,
                    payload={"task_id": task.id, "reason": reason},
                )
            )
            return AdmitDecision(verdict="rejected", reason=reason)

        entry = QueueEntry(
            task=task,
            spec=spec,
            trigger_source=trigger_source,
            enqueued_at=now,
            expires_at=now
            + timedelta(seconds=self._config.queue_ttl_seconds),
            reason=reason,
        )
        self._queue.push(entry)
        logger.info("Task %s queued: %s", task.id, reason)
        await self._bus.publish(
            Event(
                type="routing.queued",
                source=_SOURCE,
                payload={
                    "task_id": task.id,
                    "reason": reason,
                    "expires_at": entry.expires_at.isoformat(),
                },
            )
        )
        return AdmitDecision(verdict="queued", reason=reason)

    async def release(
        self, task_id: str, now: datetime | None = None
    ) -> list[QueueEntry]:
        """Free a finished task's slot, then re-check the queue."""
        self._running = [r for r in self._running if r.task_id != task_id]
        return await self.dequeue_ready(now)

    async def dequeue_ready(
        self, now: datetime | None = None
    ) -> list[QueueEntry]:
        """Admit queued entries whose invariants now pass (FIFO scan).

        Returned entries already hold a reservation — the caller MUST
        run them and eventually call release().
        """
        now = now or datetime.now(UTC)
        ready = self._queue.pop_admissible(
            lambda entry: self._try_reserve(entry.task, entry.spec) is None
        )
        for entry in ready:
            waited = (now - entry.enqueued_at).total_seconds()
            await self._bus.publish(
                Event(
                    type="routing.dequeued",
                    source=_SOURCE,
                    payload={
                        "task_id": entry.task.id,
                        "waited_seconds": waited,
                    },
                )
            )
        return ready

    async def expire_overdue(
        self, now: datetime | None = None
    ) -> list[QueueEntry]:
        """Drop queue entries past their expires_at; caller fails them."""
        now = now or datetime.now(UTC)
        expired = self._queue.pop_expired(now)
        for entry in expired:
            logger.warning(
                "Task %s expired in queue: %s", entry.task.id, entry.reason
            )
            await self._bus.publish(
                Event(
                    type="routing.expired",
                    source=_SOURCE,
                    payload={
                        "task_id": entry.task.id,
                        "reason": entry.reason,
                    },
                )
            )
        return expired
```

Update `src/proctor/router/__init__.py` to its final form (add the import and `"TaskRouter"` to `__all__` — full listing shown in Task 2 Step 3, first block).

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_router/ -v`
Expected: all PASS (including the race test).

- [ ] **Step 6: Config round-trip check**

Run: `uv run pytest tests/test_core/ -q`
Expected: PASS — new `router:` section and spec fields are optional with defaults; existing configs unaffected.

- [ ] **Step 7: Quality gates + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/router/ src/proctor/core/config.py src/proctor/workflow/spec.py tests/test_router/
git commit -m "feat(router): TaskRouter facade + RouterConfig + spec scope/branch"
```

---

### Task 6: Bootstrap integration + tick-loop + integration tests

**Files:**
- Modify: `src/proctor/core/bootstrap.py`
- Test: `tests/test_router/test_admission_integration.py`

**Interfaces:**
- Consumes: `TaskRouter`, `AgentProfile`, `QueueEntry` (Task 5); existing `Application` internals.
- Produces: admission-gated `_handle_trigger_event`; `Application._queue_tick_loop` started in `start()`, cancelled in `stop()` before drain.

- [ ] **Step 1: Refactor bootstrap**

In `src/proctor/core/bootstrap.py`:

Add imports:

```python
import asyncio
import contextlib

from proctor.router.models import AgentProfile, QueueEntry
from proctor.router.router import TaskRouter
```

In `Application.__init__`, after `self._router: Router | None = None`:

```python
        self._task_router: TaskRouter | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._exec_tasks: set[asyncio.Task[None]] = set()
```

In `start()`, right after the `self._router = Router(...)` block:

```python
        self._task_router = TaskRouter(
            bus=self.bus,
            config=self.config.router,
            agents=[
                AgentProfile(
                    id="local",
                    max_slots=self.config.router.agent.max_slots,
                )
            ],
        )
        self._tick_task = asyncio.create_task(self._queue_tick_loop())
```

In `stop()`, right after `self.is_running = False` (before triggers are stopped — no tick may fire against a stopping app):

```python
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None
```

Still in `stop()`, right after `await self.bus.drain(...)` (dequeued executions are spawned tasks, not bus handlers — the drain doesn't cover them):

```python
        if self._exec_tasks:
            await asyncio.wait(
                self._exec_tasks,
                timeout=self.config.events.drain_timeout,
            )
```

Replace the body of `_handle_trigger_event` from `resolved_prompt = ...` down to the end of the method with:

```python
        task = Task(trigger_event=event.id, spec=spec.model_dump())
        await self.state.save_task(task)  # persisted as PENDING

        assert self._task_router is not None  # created in start()
        decision = await self._task_router.admit(task, spec, event.source)
        if decision.verdict == "queued":
            return  # TaskRouter emitted routing.queued; tick/release will run it
        if decision.verdict == "rejected":
            task.status = TaskStatus.FAILED
            task.result = {"error": f"admission rejected: {decision.reason}"}
            task.updated_at = datetime.now(UTC)
            await self.state.save_task(task)
            await self.bus.publish(
                Event(
                    type="task.failed",
                    source="application",
                    payload=task.result,
                )
            )
            return

        await self._run_admitted(task, spec, event.source)
```

Add three new methods to `Application` (the body of `_run_admitted` is the former `_handle_trigger_event` execution path — RUNNING transition, episode, ctxvars, execute, status updates, task.completed/failed publish — plus a `finally` that releases the slot):

```python
    async def _run_admitted(
        self, task: Task, spec: WorkflowSpec, trigger_source: str
    ) -> None:
        """Execute an admitted task; always release its slot at the end."""
        assert self._engine is not None and self._task_router is not None
        try:
            resolved_prompt = spec.prompt or ""
            task.status = TaskStatus.RUNNING
            task.updated_at = datetime.now(UTC)
            await self.state.save_task(task)

            episode = Episode(
                trigger_type=trigger_source,
                user_input=resolved_prompt,
                agent_response="",
            )
            await self.memory.save_episode(episode)

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
        finally:
            ready = await self._task_router.release(task.id)
            self._spawn_ready(ready)

    def _spawn_ready(self, entries: list[QueueEntry]) -> None:
        """Launch execution of dequeued entries as tracked asyncio tasks."""
        for entry in entries:
            exec_task = asyncio.create_task(
                self._run_admitted(entry.task, entry.spec, entry.trigger_source)
            )
            self._exec_tasks.add(exec_task)
            exec_task.add_done_callback(self._exec_tasks.discard)

    async def _queue_tick_loop(self) -> None:
        """Expire overdue queue entries and re-check the queue."""
        assert self._task_router is not None
        while True:
            await asyncio.sleep(self.config.router.queue_tick_seconds)
            expired = await self._task_router.expire_overdue()
            for entry in expired:
                entry.task.status = TaskStatus.FAILED
                entry.task.result = {
                    "error": f"queue TTL expired: {entry.reason}"
                }
                entry.task.updated_at = datetime.now(UTC)
                await self.state.save_task(entry.task)
                await self.bus.publish(
                    Event(
                        type="task.failed",
                        source="application",
                        payload=entry.task.result,
                    )
                )
            self._spawn_ready(await self._task_router.dequeue_ready())
```

Also extend the early-guard in `_handle_trigger_event` to include the task router (`if self._router is None or self._engine is None or self._task_router is None:`) and add `WorkflowSpec` to the imports from `proctor.workflow.spec` if not already imported.

- [ ] **Step 2: Run the existing suite — refactor must not regress**

Run: `uv run pytest -q`
Expected: all PASS. The admission path is additive (defaults: `max_concurrency=4` — existing single-task tests admit immediately).

- [ ] **Step 3: Write integration tests**

```python
# tests/test_router/test_admission_integration.py
"""Integration: admission gating through a real Application."""

from pathlib import Path

import anyio
import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import (
    ProctorConfig,
    RouteRule,
    RouterAgentConfig,
    RouterConfig,
)
from proctor.core.models import Event
from proctor.core.transport import LocalEventTransport
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """aiosqlite only supports asyncio."""
    return "asyncio"


def _config(tmp_path: Path, **router_overrides: object) -> ProctorConfig:
    return ProctorConfig(
        data_dir=tmp_path / "proctor_data",
        router=RouterConfig(
            max_concurrency=1,
            queue_ttl_seconds=5.0,
            queue_tick_seconds=0.05,
            agent=RouterAgentConfig(max_slots=1),
            **router_overrides,
        ),
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


async def _wait_for(collected: list[Event], event_type: str) -> None:
    with anyio.fail_after(3):
        while not any(e.type == event_type for e in collected):
            await anyio.sleep(0.01)


async def test_second_task_queues_then_runs_after_release(
    tmp_path: Path,
) -> None:
    gate = anyio.Event()
    calls: list[str] = []

    async def llm(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            await gate.wait()
        return "ok"

    app = Application(_config(tmp_path), event_transport=LocalEventTransport())
    app.set_llm_call(llm)
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    app.bus.subscribe("routing.>", collect)
    app.bus.subscribe("task.>", collect)

    await app.start()
    try:
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal",
                  payload={"text": "one"})
        )
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal",
                  payload={"text": "two"})
        )
        await _wait_for(collected, "routing.queued")
        gate.set()
        await _wait_for(collected, "routing.dequeued")
        with anyio.fail_after(3):
            while sum(e.type == "task.completed" for e in collected) < 2:
                await anyio.sleep(0.01)
    finally:
        gate.set()
        await app.stop()

    assert len(calls) == 2


async def test_queue_ttl_expiry_fails_task(tmp_path: Path) -> None:
    gate = anyio.Event()

    async def llm(prompt: str) -> str:
        await gate.wait()
        return "ok"

    config = _config(tmp_path, queue_ttl_seconds=0.2)
    app = Application(config, event_transport=LocalEventTransport())
    app.set_llm_call(llm)
    collected: list[Event] = []

    async def collect(event: Event) -> None:
        collected.append(event)

    app.bus.subscribe("routing.>", collect)
    app.bus.subscribe("task.>", collect)

    await app.start()
    try:
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal",
                  payload={"text": "one"})
        )
        await app.bus.publish(
            Event(type="trigger.terminal", source="terminal",
                  payload={"text": "two"})
        )
        await _wait_for(collected, "routing.queued")
        await _wait_for(collected, "routing.expired")
        await _wait_for(collected, "task.failed")
    finally:
        gate.set()
        await app.stop()

    failed = [e for e in collected if e.type == "task.failed"]
    assert any("TTL expired" in str(e.payload) for e in failed)
```

- [ ] **Step 4: Run integration tests**

Run: `uv run pytest tests/test_router/test_admission_integration.py -v`
Expected: 2 PASS

- [ ] **Step 5: Full suite + quality gates + commit**

```bash
uv run pytest -q                      # expect: all pass
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/core/bootstrap.py tests/test_router/test_admission_integration.py
git commit -m "feat(router): admission gate + tick-loop wired into bootstrap"
```

---

### Task 7: Docs sync + PR

**Files:**
- Modify: `CLAUDE.md` (module table `router/` row: planned → implemented; Implementation Status; Next → Phase 3)
- Modify: `TODO.md` (Phase 2 complete)
- Modify: `config/proctor.yaml` (commented example `router:` section)

**Interfaces:** none — documentation only.

- [ ] **Step 1: Update CLAUDE.md**

In the planned-modules table, remove the `router/` row; in the module layout table add:

```markdown
| `router/` | Admission layer (M4) — TaskRouter: 4 safety invariants, TTL pending queue, capability-scoring seam for Phase 3 |
```

In **Implementation Status**: add `TaskRouter (admission invariants + TTL queue)` to Completed; change **Next** to `Phase 3 — workers/registry, Docker/SSH workers, MCP.`

- [ ] **Step 2: Update TODO.md**

In `## Текущее состояние` replace the Phase 2 line with:

```markdown
- ✅ Phase 2 завершена: triggers (terminal/telegram/scheduler/webhook), NATS-транспорт, EpisodicMemory, TaskRouter (admission: 4 инварианта + TTL-очередь)
```

- [ ] **Step 3: Add example config**

Append to `config/proctor.yaml`:

```yaml
# TaskRouter admission (M4). Defaults shown; scope/branch are declared
# per workflow in the workflows: catalog.
# router:
#   max_concurrency: 4
#   queue_ttl_seconds: 600    # 0 = reject immediately, never queue
#   queue_tick_seconds: 30
#   agent:
#     max_slots: 4
```

- [ ] **Step 4: Final gates, push, PR**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add CLAUDE.md TODO.md config/proctor.yaml
git commit -m "docs: Phase 2 complete — TaskRouter shipped"
git push -u origin feat/task-router
gh pr create --base master --title "feat(router): TaskRouter — M4 admission layer (invariants + TTL queue)" --body "..."
```

PR body should reference the spec (`docs/superpowers/specs/2026-07-05-task-router-design.md`), list the four invariants, the queue-with-TTL semantics, the atomicity guarantee, and the test evidence (unit + race + integration).

---

## Self-Review Notes

- Spec coverage: globs consolidation (T1), models/scoring (T2), invariants (T3), queue (T4), config + spec fields + facade + atomicity race test (T5), bootstrap + tick lifecycle + integration tests incl. TTL expiry (T6), docs (T7). `routing.rejected` at `queue_ttl_seconds<=0` covered in T5 tests.
- Type consistency: `QueueEntry.trigger_source` threaded T2→T5→T6; `admit(task, spec, trigger_source, now=None)` consistent across T5 tests and T6 bootstrap.
- Known judgment call: dequeued executions run as tracked `asyncio.create_task` (same pattern as `transport/local.py`); admitted-inline executions stay inside bus handler tasks so `bus.drain()` covers them, spawned ones are awaited separately in `stop()`.
