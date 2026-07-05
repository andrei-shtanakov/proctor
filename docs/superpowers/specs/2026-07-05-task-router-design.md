# TaskRouter (M4, Phase 2) — Design

**Status:** Draft
**Date:** 2026-07-05
**Phase:** 2 (Proactivity) — last remaining Phase 2 item

## Goal

Add the M4 admission layer from the architecture plan
(`docs/plans/2026-03-04-proctor-architecture-design.md`): before a routed
workflow starts executing, check safety invariants (concurrency, agent
slots, file-scope isolation, branch locking) and either run the task now,
queue it with a TTL, or reject it. Also introduce the capability-scoring
interface that Phase 3 (`workers/registry.py`) will plug real agent
candidates into.

Not to be confused with the existing `core/router.py` `Router` (LABS-65),
which maps trigger events to catalog workflows. The two compose
sequentially: `Router` decides *what* to run, `TaskRouter` decides
*whether it may run now*.

## Scope

In scope:

- New package `src/proctor/router/` with `models.py`, `invariants.py`,
  `scoring.py`, `queue.py`, `router.py`.
- The four critical invariants from the architecture doc:
  `concurrency_limit`, `agent_available`, `scope_isolation`,
  `branch_not_locked`.
- Pending queue with per-entry TTL; FIFO re-check on task completion and
  on a periodic tick.
- Optional `scope: list[str]` and `branch: str | None` fields on
  `WorkflowSpec` (declared per catalog entry in YAML).
- `router:` config section: `max_concurrency`, `queue_ttl_seconds`,
  local agent slots.
- `routing.queued` / `routing.dequeued` / `routing.expired` /
  `routing.rejected` observability events (same namespace as the existing
  `routing.unmatched` / `routing.binding_failed`).
- Bootstrap integration in `Application._handle_trigger_event`.

Out of scope (deferred):

- Warning invariants (`budget_remaining`, `retry_limit`, `rate_limit`,
  `agent_health`, `task_compatible`, `sla_feasible`) — later phases.
- Real multi-agent scoring and fallback across up to 3 candidates —
  Phase 3, when `workers/registry.py` provides candidates.
- Queue persistence across restarts (see Limitations).
- NATS-distributed admission (single-node kernel decision for now).

## Data flow

```
trigger.* event
  → Router.route(event)            # existing: event → WorkflowSpec | None
  → TaskRouter.admit(task, spec)
      ├─ all invariants pass → task RUNNING → engine.execute(spec)
      │                         → on finish: TaskRouter.release(task_id)
      └─ some invariant blocks → task PENDING → PendingQueue (expires_at = now + TTL)

TaskRouter.release(task_id) / periodic tick
  → re-check queue in FIFO order:
      ├─ invariants pass → dequeue → RUNNING → execute
      ├─ still blocked   → stays queued
      └─ expires_at passed → dequeue → FAILED + routing.expired
```

`TaskRouter` never executes anything itself; execution stays in
bootstrap. Task status transitions (`pending → running →
completed/failed`) reuse the existing `Task` model and are persisted via
`StateManager` at every transition, as elsewhere.

## Components

| File | Contents |
|------|----------|
| `router/models.py` | `AgentProfile` (id, capabilities, max_slots), `Candidate` (profile + score), `AdmitDecision` (admitted \| queued \| rejected, reason), `RunningTask` (task_id, agent_id, scope, branch) |
| `router/invariants.py` | Four pure functions `check_<name>(spec, running, profile) -> str \| None` — `None` means pass, a string is the human-readable block reason |
| `router/scoring.py` | `score_candidates(spec, agents) -> list[Candidate]` — v1 returns the single local agent with score 1.0; the seam Phase 3 fills |
| `router/queue.py` | `PendingQueue` — FIFO of `(task, spec, expires_at)`; pure data structure, no I/O, no clock (caller passes `now`) |
| `router/router.py` | `TaskRouter` facade: `admit()`, `release()`, `expire_overdue()`; owns the running-task view and slot accounting; publishes `routing.*` events on the bus |

### Tick-loop lifecycle

`Application` currently has no background loops of its own (triggers own
theirs), so this surface is built, not reused. The tick-loop is an asyncio
task started in `Application.start()` after the transport is up; every
`queue_tick_seconds` it calls `expire_overdue()` and re-checks the
queue. `Application.stop()` cancels it (and suppresses the
cancellation) **before** draining the transport, so no tick fires
against a stopping bus. The loop holds no state of its own — everything
lives in `TaskRouter` — so cancellation at any point is safe.

## Invariant semantics

- **concurrency_limit** — `len(running) < max_concurrency`.
- **agent_available** — chosen candidate has a free slot
  (`slots_used < max_slots`). Near-duplicate of `concurrency_limit` while
  there is one agent; kept separate because Phase 3 makes it per-agent.
- **scope_isolation** — `spec.scope` globs do not overlap any running
  task's scope. Overlap test is conservative: two patterns conflict if
  either `fnmatch`es the other or one is a path-prefix of the other. A
  task with empty scope conflicts with nothing (declaring scope is
  opt-in). The overlap predicate lives in a shared `core/globs.py`
  helper module together with the existing `_is_strictly_broader`
  subsumption check (moved out of `core/config.py`, which re-imports
  it) — one glob-heuristic family, one home, not a third private copy.
- **branch_not_locked** — `spec.branch` (exact string) is not held by any
  running task. `None` always passes.

## Config

```yaml
router:
  max_concurrency: 4        # default 4
  queue_ttl_seconds: 600    # default 600; 0 = reject immediately, never queue
  queue_tick_seconds: 30    # default 30; the Application's asyncio tick loop calls
                            # expire_overdue() — needed because a TTL can
                            # lapse with no release() ever firing
  agent:
    max_slots: 4            # local AgentRuntime slots

workflows:
  deploy:
    # ... existing fields ...
    scope: ["src/**", "config/*.yaml"]   # optional, default []
    branch: "release"                     # optional, default null
```

## Error handling

- Blocked at admit → `routing.queued` (task_id, reason, expires_at).
- Dequeued and started → `routing.dequeued` (task_id, waited_seconds).
- TTL expired → task FAILED with the last block reason;
  `routing.expired` published so the operator layer (Phase 5 `control/`)
  or any subscriber can alert.
- `queue_ttl_seconds: 0` → blocked tasks are immediately FAILED with
  `routing.rejected` (the architecture doc's original reject semantics,
  available via config).
- Invariant checks are synchronous and in-process; there is no partial
  admission.

### Admission atomicity

The local transport dispatches every subscriber as its own
`asyncio.create_task` (`transport/local.py`), so multiple
`_handle_trigger_event` — and therefore multiple `admit()` — run
concurrently. `admit()` MUST therefore be atomic with respect to the
event loop: check all invariants and mutate the running-set / slot
accounting **before its first `await`**. Publishing `routing.*` events
happens only after the reservation is committed. Otherwise two admits
can both observe `len(running) < max_concurrency` and over-admit.
Regression test: two simultaneous admits at `max_concurrency: 1` — one
runs, one queues.

## Limitations (v1, recorded deliberately)

- The pending queue is in-memory. PENDING task rows survive a restart in
  SQLite, but v1 does not re-enqueue them on boot; they surface via state
  inspection only. Revisit alongside Phase 3 distribution.
- Scope overlap is heuristic (mutual fnmatch + prefix), favouring false
  positives (queueing something that could have run) over false negatives
  (running something that conflicts).
- Single-node: admission state lives in the kernel process, which is
  correct while execution is in-process (Phase 2 reality).
- `agent_available` is slot **bookkeeping** in TaskRouter, not a live
  load query — `AgentRuntime` has no slot concept. Until Phase 3's
  registry, the invariant reflects what TaskRouter admitted, not what
  the runtime is actually doing.
- Queue entries carry `expires_at` (admit-TTL). This is deliberately
  NOT the existing `Task.deadline` field (run-deadline, a different
  lifecycle stage); the two must not be conflated.

## Testing

- **Unit, invariants** — each check in isolation: exactly-at-limit
  boundaries, empty scopes, glob overlap matrix (prefix, mutual-match,
  disjoint), branch exact-match vs None.
- **Unit, queue** — FIFO order preserved, `expires_at` comparison,
  `expire_overdue` returns expired entries exactly once.
- **Unit, TaskRouter** — admit/queue/release lifecycle with a stub bus;
  slot accounting; `queue_ttl_seconds: 0` reject path; **race test**:
  two concurrent `admit()` at `max_concurrency: 1` — exactly one runs,
  one queues (per §Admission atomicity).
- **Integration** — real EventBus + WorkflowEngine with mocked LLM: two
  scope-conflicting tasks, second queues and starts after the first
  releases; TTL expiry produces FAILED + `routing.expired`; events
  observed by a `routing.*` subscriber.
- All async tests use anyio, per project convention.
