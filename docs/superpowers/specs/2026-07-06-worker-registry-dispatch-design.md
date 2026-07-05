# Worker Registry + NATS Dispatch (Phase 3, part 1) — Design

**Status:** Draft
**Date:** 2026-07-06
**Phase:** 3 (Distribution) — first sub-project

## Goal

Turn proctor from a single-process system into a distributed one: worker
nodes register themselves over the bus, the core's TaskRouter scores
real candidates from a live registry, and admitted tasks are dispatched
to the winning worker over the same bus. Fills the `score_candidates`
seam left by the TaskRouter (PR #28) with real candidates and makes the
`agent_available` invariant reflect actual live workers.

## Scope

In scope:

- `worker.*` / `task.assign.*` / `task.result` event protocol on the
  existing EventBus (works identically over Local and NATS transports).
- `workers/registry.py` — `WorkerRegistry` on the core: live catalog of
  workers with heartbeat-based liveness and a sweep loop.
- `workers/node.py` — `WorkerNode`: the worker-role runtime (register,
  heartbeat, execute assigned specs, publish results).
- Role-dependent bootstrap: `node_role: worker` starts a WorkerNode (no
  triggers, no Router/TaskRouter, no SQLite); `standalone`/`core` starts
  everything plus the registry.
- The core's in-process runtime appears in the registry as worker
  `local`; the registry is the only source of candidates — the static
  `agents=[AgentProfile("local")]` list in bootstrap is removed. The
  local profile is **seeded directly** into the registry from the
  `worker:` config (in-process ⇒ alive by definition, exempt from the
  heartbeat sweep); bus registration/heartbeat is for remote workers
  only.
- Real scoring: `WorkflowSpec.requires: list[str]`, capability filter +
  free-slot ranking in `scoring.py`; `AdmitDecision.agent_id`.
- Remote dispatch path in bootstrap: winner ≠ `local` → publish
  `task.assign.{agent_id}`, status ASSIGNED, deadline set; on
  `task.result` → status transitions, episode, slot release.
- Worker-loss policy: `WorkflowPolicies.retry_on_worker_loss: bool`
  (default False) — requeue vs fail, plus a deadline reaper for lost
  assign/result messages.
- Config: `worker:` section (id, capabilities, max_slots), `registry:`
  section (heartbeat_interval, liveness_timeout). `router.agent` is
  replaced by `worker` (see Migration).

Out of scope (deferred):

- JetStream at-least-once delivery — the upgrade seam is that the
  **same subjects** (`task.assign.{id}`, `task.result`) become JetStream
  streams later; nothing else in the protocol changes. Until then
  delivery is at-most-once and the deadline reaper is the safety net.
- Docker/SSH worker processes (`workers/docker.py`, `workers/remote.py`)
  — next Phase 3 sub-projects; they will reuse WorkerNode unchanged and
  only differ in how the process is started.
- MCP proxying (`mcp/`), agent-to-agent inbox (`agents.{id}.inbox`).
- Worker-side episodic memory; episodes stay core-owned.

## Protocol

All messages are plain `Event`s on the existing bus. Subjects follow the
existing dot-namespace convention (no `events.` prefix — same as
`trigger.*`, `routing.*`, `task.*`):

| Subject | Direction | Payload |
|---------|-----------|---------|
| `worker.registered` | worker → all | `worker_id`, `capabilities: list[str]`, `max_slots: int` |
| `worker.heartbeat` | worker → all | `worker_id` (every `heartbeat_interval`) |
| `worker.offline` | registry or worker → all | `worker_id`, `reason: "timeout" \| "shutdown"` |
| `task.assign.{worker_id}` | core → one worker | `task: Task.model_dump()`, `spec: WorkflowSpec.model_dump()` |
| `task.result` | worker → core | `task_id`, `ok: bool`, `output: str \| None`, `error: str \| None`, `worker_id` |

Constraints:

- `Event.type` segments must match `[a-z][a-z0-9_]*`, therefore
  **`worker_id` must be subject-safe**: `^[a-z][a-z0-9_]*$` (no
  hyphens). Enforced by a pydantic validator on `WorkerConfig.id`.
- Re-registration is idempotent: a `worker.registered` for a known id
  refreshes its profile and `last_seen`. After a NATS reconnect workers
  simply keep heartbeating; the registry needs no reconnect handling of
  its own.
- A worker publishes `worker.offline` (`reason: "shutdown"`) on graceful
  stop; the registry publishes it (`reason: "timeout"`) when
  `now - last_seen > liveness_timeout`.

## Components

| File | Contents |
|------|----------|
| `workers/registry.py` | `WorkerRegistry(bus, config)` — subscribes `worker.registered`/`worker.heartbeat`/`worker.offline`; dict `worker_id → WorkerEntry(profile, last_seen)`; `alive_profiles() -> list[AgentProfile]`; `sweep(now)` marks silent workers offline and publishes `worker.offline`; own asyncio sweep loop with the same lifecycle guarantees as the router tick loop (started in `Application.start()`, body exception-guarded, cancelled in `stop()` before drain) |
| `workers/node.py` | `WorkerNode(bus, config, engine)` — `start()`: publish `worker.registered`, start heartbeat loop, subscribe `task.assign.{id}`; `stop()`: publish `worker.offline(shutdown)`, cancel loops; `_handle_assign(event)`: deserialize spec, enforce own `max_slots` (over-assignment → immediate `task.result` with `ok=False, error="worker_busy"`), execute via WorkflowEngine, publish `task.result` |
| `router/scoring.py` | Real implementation: candidates = agents where `set(spec.requires) <= set(profile.capabilities)`; score = free slots (caller passes per-agent used-slot counts); sorted descending, stable |
| `router/models.py` | `AdmitDecision` gains `agent_id: str \| None` (set when admitted) |
| `core/bootstrap.py` | Role branch in `Application`; registry wiring; dispatch path (`_dispatch_remote`), `task.result` handler, worker-loss handler on `worker.offline`, deadline reaper folded into the existing tick loop |

## Scoring

`score_candidates(spec, agents, used_slots)`:

1. Filter: `set(spec.requires) <= set(profile.capabilities)`; workers
   with zero free slots stay in the list (the `agent_available`
   invariant rejects them — one place decides, not two).
2. Score = `max_slots - used_slots[profile.id]` (free slots).
3. Sort by score descending; stable, so equal-score candidates keep
   registry order.

`TaskRouter._try_reserve` already loops candidates in order — the
existing loop IS the arch plan's "up to 3 candidates" fallback (it tries
every candidate; capping at 3 is pointless at this scale and is dropped
deliberately).

`spec.requires` that no live worker satisfies → admit blocks with reason
`no_candidates: no live worker offers {missing}` → normal queue/TTL
path.

## Dispatch flow

```
admit() → AdmitDecision(admitted, agent_id=W)
  ├─ W == local worker id → inline _run_admitted (unchanged)
  └─ W != local:
       task.status = ASSIGNED, task.worker_id = W
       task.deadline = now + spec.policies.max_runtime_seconds
       save_task; publish task.assign.{W}
       (slot already reserved by admit)

worker: _handle_assign → execute → publish task.result

core on task.result:
  task → COMPLETED/FAILED (+ result payload), episode recorded,
  release(task_id) → dequeue_ready → spawn (existing path)

core on worker.offline(W):
  registry drops W
  for each in-flight task assigned to W:
      if spec.policies.retry_on_worker_loss and task.retries < max_retries:
          task.retries += 1; back to pending queue (fresh TTL)
      else:
          task → FAILED {"error": "worker_lost: ..."} + task.failed event
      release(task_id) either way

tick loop (existing) additionally reaps deadline-overdue ASSIGNED tasks:
  treated exactly like worker loss (same policy branch)
```

The core tracks in-flight remote tasks in a small
`task_id → (task, spec, agent_id)` map owned by the dispatch layer;
entries are removed on result/loss/reap. The registry never touches
task state; the router never touches the bus subjects — boundaries stay
as in Phase 2.

## Failure semantics (at-most-once, stated honestly)

- Lost `task.assign`: worker never starts; deadline reaper fires →
  worker-loss policy branch.
- Lost `task.result`: work happened, core times out → policy branch. A
  successfully completed task can be marked FAILED (or re-run, if the
  workflow opted into `retry_on_worker_loss`). This is the price of
  at-most-once and the reason `retry_on_worker_loss` defaults to False —
  side-effecting workflows must not silently re-run.
- Worker crash mid-task: heartbeat stops → registry sweep → offline →
  policy branch.
- Core restart: in-flight map is lost; ASSIGNED rows survive in SQLite
  but v1 does not reconcile them on boot (same recorded limitation as
  the Phase 2 pending queue; both revisit together with JetStream).

## Config

```yaml
node_role: worker             # existing field: standalone | core | worker

worker:                       # who am I as an executor
  id: worker_a                # subject-safe: ^[a-z][a-z0-9_]*$
  capabilities: []            # e.g. [shell, python]
  max_slots: 4                # replaces router.agent.max_slots

registry:                     # core/standalone only
  heartbeat_interval: 30.0    # seconds, gt=0
  liveness_timeout: 90.0      # seconds; must be > heartbeat_interval
```

Migration: `router.agent` (RouterAgentConfig) is **removed**; the
core's local agent is described by the same `worker:` section (default
id `local`). A config still containing `router.agent` fails validation
with a message pointing to `worker.max_slots`. WorkflowSpec gains
`requires: list[str] = []`; WorkflowPolicies gains
`retry_on_worker_loss: bool = False`.

## Limitations (v1, recorded deliberately)

- Delivery is at-most-once end-to-end; the deadline reaper converts
  silent loss into the worker-loss policy. JetStream upgrade keeps the
  same subjects.
- No reconciliation of ASSIGNED tasks after core restart.
- Worker executes with its own LLM config; capability strings are
  free-form labels matched by set inclusion — no versioning/semantics
  yet.
- `worker.heartbeat` traffic is O(workers), fine for a single-operator
  fleet; no sharding considerations.

## Testing

- **Unit, registry** — register/heartbeat/refresh with injected `now`;
  sweep marks offline exactly once at `last_seen + liveness_timeout`;
  re-registration after offline revives; `alive_profiles` excludes
  offline.
- **Unit, scoring** — capability filter (subset, missing, empty
  requires), free-slot ranking, stable order, zero-free-slots kept.
- **Unit, node** — assign→result round-trip with stub engine;
  over-assignment beyond max_slots → `worker_busy` result; graceful
  stop publishes offline(shutdown).
- **Integration, LocalEventTransport** (no NATS needed): core
  Application + WorkerNode on one bus — full loop trigger → admit →
  assign → execute (mock LLM) → result → COMPLETED with episode;
  worker silence → sweep → offline → both policy branches
  (fail-by-default, opt-in requeue → re-dispatch to a second worker);
  deadline reap of a lost result.
- **Integration, `-m nats`** — the same full-loop contract over two
  `NATSEventTransport` instances (core + worker) against one NATS
  server: real multi-node in one test process.
- All async tests anyio; injected clocks for liveness/deadline logic —
  no sleep-based liveness tests at unit level.
