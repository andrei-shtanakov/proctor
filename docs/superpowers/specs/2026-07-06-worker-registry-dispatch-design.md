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
- The core's in-process runtime appears in the registry under the id
  from its own `worker:` config (`worker.id`, default `local`), bound
  once at startup as the inline-executor id — dispatch compares the
  scoring winner against that binding, never against a string literal.
  The registry is the only source of candidates — the static
  `agents=[AgentProfile("local")]` list in bootstrap is removed. The
  local profile is **seeded directly** into the registry from the
  `worker:` config (in-process ⇒ alive by definition, exempt from the
  heartbeat sweep); bus registration/heartbeat is for remote workers
  only.
- Real scoring: `WorkflowSpec.requires: list[str]`, capability filter +
  free-slot ranking in `scoring.py`; `AdmitDecision.agent_id`.
- Remote dispatch path in bootstrap: winner ≠ inline-executor id → publish
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
  **subject namespace and payload schema are preserved**
  (`task.assign.{id}`, `task.result`, including `dispatch_id` /
  `instance_id`, which JetStream deduplication will need anyway).
  Transport semantics WILL change: durable consumers, ack policy,
  redelivery and dedup all require handler changes — the seam promises
  a stable wire contract, not untouched handlers. Until then delivery
  is at-most-once and the deadline reaper is the safety net.
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
| `worker.registered` | worker → all | `worker_id`, `instance_id`, `capabilities: list[str]`, `max_slots: int` |
| `worker.heartbeat` | worker → all | **same full payload as `worker.registered`** (every `heartbeat_interval`) |
| `worker.offline` | registry or worker → all | `worker_id`, `instance_id`, `reason: "timeout" \| "shutdown"` |
| `task.assign.{worker_id}` | core → one worker | `dispatch_id`, `target_instance_id`, `task: Task.model_dump()`, `spec: WorkflowSpec.model_dump()` |
| `task.result` | worker → core | `task_id`, `dispatch_id`, `worker_id`, `instance_id`, `ok: bool`, `output: str \| None`, `error: str \| None` |

Constraints:

- `Event.type` segments must match `[a-z][a-z0-9_]*`, therefore
  **`worker_id` must be subject-safe**: `^[a-z][a-z0-9_]*$` (no
  hyphens). Enforced by a pydantic validator on `WorkerConfig.id`.
- **Incarnation fencing (`instance_id`)**: each WorkerNode process
  generates a UUID at start and stamps it on every message it sends.
  UUIDs carry no ordering, so "newest wins" is undecidable — the
  policy is **first-alive-owns**: the registry binds a worker_id to
  the first instance it sees and rejects (logs at WARNING) messages
  from any other instance while the owner is alive. The binding is
  released only when the owner goes offline (graceful `worker.offline`
  or liveness timeout); the next registration/heartbeat may then claim
  the id. Consequences, recorded deliberately: a worker that crashed
  without `worker.offline` cannot re-claim its id until
  `liveness_timeout` elapses (~90 s of downtime by default; a graceful
  restart takes over immediately), and two concurrently-live processes
  sharing an id never ping-pong — exactly one owns it at any moment.
  Events carrying a non-owner instance_id (late offline from a dead
  process, late heartbeat, foreign results) are logged and ignored.
- **Self-healing discovery**: heartbeats carry the full profile, so a
  restarted core rebuilds its registry within one
  `heartbeat_interval` with no extra protocol. `worker.registered` is
  kept as the immediate announce (same payload, prompt reaction);
  there is deliberately no periodic re-registration — the
  profile-carrying heartbeat already is one.
- **Subscription readiness barrier**: `NATSEventTransport.subscribe()`
  registers the subscription via a background task, so a worker MUST
  subscribe to its `task.assign.{id}` subject, then `await bus.flush()`
  (which by design completes pending subscribe registrations before
  flushing the wire), and only then publish `worker.registered`.
  Otherwise the core can assign to a worker that cannot hear yet.
- **Assign targeting**: a worker executes an assignment only if
  `target_instance_id` matches its own; anything else is logged and
  dropped. This closes duplicate-worker_id double execution (plain NATS
  delivers a subject to ALL subscribers — a queue group arrives only
  with the JetStream upgrade).
- A worker publishes `worker.offline` (`reason: "shutdown"`) on graceful
  stop; the registry publishes it (`reason: "timeout"`) when
  `now - last_seen > liveness_timeout`.

## Components

| File | Contents |
|------|----------|
| `workers/registry.py` | `WorkerRegistry(bus, config)` — subscribes `worker.registered`/`worker.heartbeat`/`worker.offline`; dict `worker_id → WorkerEntry(profile, instance_id, last_seen)` with first-alive-owns fencing; `alive_profiles() -> list[AgentProfile]`; `sweep(now)` removes silent workers; **loss delivery is a callback, not the bus**: `add_loss_listener(cb: Callable[[str, str], Awaitable[None]])` — the registry awaits `cb(worker_id, instance_id)` exactly once per lost incarnation, atomically at the point it removes the entry. Bootstrap's worker-loss logic hangs off this callback; it never inspects the registry to judge staleness (bus handlers run concurrently — by the time a `worker.offline` event handler ran, the registry may already have mutated). **`worker.offline` publication is asymmetric**: on a *graceful* shutdown the WORKER published the event — the registry removes the entry and fires the callback but re-publishes nothing (no duplicate observability events, no recursive handling); on a *timeout* the registry removes the entry, fires the callback, and is itself the publisher of `worker.offline(reason="timeout")`. Own asyncio sweep loop with the same lifecycle guarantees as the router tick loop |
| `workers/node.py` | `WorkerNode(bus, config, engine)` — `start()`: subscribe `task.assign.{id}` → `await bus.flush()` (readiness barrier) → publish `worker.registered`, start heartbeat loop. `stop()` in strict order: (1) unsubscribe/stop accepting assignments, (2) cancel heartbeat loop and **await its completion** (a heartbeat escaping after offline would re-register the worker), (3) wait for running executions up to a drain window, cancel the rest (core reaps them via the loss path), (4) publish `worker.offline(shutdown)`, (5) `bus.flush()`. `_handle_assign(event)`: drop if `target_instance_id` mismatch, deserialize spec, enforce own `max_slots` (over-assignment → immediate `task.result` with `ok=False, error="worker_busy"`), execute via WorkflowEngine, publish `task.result` with `dispatch_id` echoed |
| `router/scoring.py` | Real implementation: candidates = agents where `set(spec.requires) <= set(profile.capabilities)`; score = free slots (caller passes per-agent used-slot counts); sorted descending, stable |
| `router/models.py` | `AdmitDecision` gains `agent_id: str \| None` (set when admitted); `QueueEntry` gains `not_before: datetime \| None` (delayed retry; `dequeue_ready` skips entries whose `not_before` is in the future) |
| `router/router.py` | API changes: `TaskRouter(bus, config, agent_provider)` where `agent_provider: Callable[[], list[AgentProfile]]` (bootstrap passes `registry.alive_profiles` — every admit/dequeue sees the live list); new public `retry(task, spec, trigger_source, not_before=None) -> None` that enqueues directly (never re-admits inline), used by the worker-loss path with `not_before = now + policies.retry_delay_seconds`. The retry TTL starts at the effective runnable moment: `expires_at = max(now, not_before or now) + queue_ttl_seconds` — otherwise `retry_delay_seconds > queue_ttl_seconds` would expire the entry before it ever became runnable |
| `core/bootstrap.py` | Role branch in `Application`; registry wiring; dispatch layer: in-flight map `task_id → InflightDispatch(task, spec, agent_id, instance_id, dispatch_id, trigger_source)` (`trigger_source` is required downstream: Episode creation on remote result and `task_router.retry(..., trigger_source, ...)` — `Task.trigger_event` stores an event ID, not a source), `_dispatch_remote`, `task.result` handler, worker-loss handling via the registry's loss callback (`worker.offline` bus events are observability only), deadline reaper folded into the existing tick loop; the inline executor's id comes from `worker.id` in the core's own config (bound once as `self._local_worker_id` — no literal `"local"` comparisons) |

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
  ├─ W == self._local_worker_id → inline _run_admitted (unchanged)
  └─ remote W:
       dispatch_id = uuid4()
       task.status = ASSIGNED, task.worker_id = W
       task.deadline = now + spec.policies.max_runtime_seconds
       inflight[task.id] = InflightDispatch(task, spec, W, instance,
                                            dispatch_id, trigger_source)
       save_task; publish task.assign.{W} (dispatch_id, target_instance_id)
       (slot already reserved by admit)

  dispatch rollback (both cleanups idempotent via the same pop-if-current):
       save_task raises  → pop inflight entry, release(task.id);
                           the task never left the core — surface the
                           error as a normal task.failed
       publish raises    → pop inflight entry, then run the worker-loss
                           policy branch immediately (retry or fail) —
                           never wait out max_runtime_seconds for a
                           dispatch that provably never departed

worker: _handle_assign → fence on target_instance_id → execute
        → publish task.result (dispatch_id echoed)

core on task.result:
  pop-if-current — SYNCHRONOUS critical section, before the first await:
      entry = inflight.get(task_id)
      accept only if entry and entry.dispatch_id == result.dispatch_id
                    and entry.instance_id == result.instance_id
      on accept: del inflight[task_id]   # atomic wrt the event loop
      else: log "stale/unknown result" and return (no state change)
  then (async): task → COMPLETED/FAILED, episode recorded,
  release(task_id) → dequeue_ready → spawn (existing path)

core on registry loss callback (W, instance):
  # invoked by WorkerRegistry exactly once per lost incarnation, at the
  # moment it removes the entry — the worker.offline bus event is
  # observability only and is never used for staleness decisions
  for each inflight entry with agent_id == W and instance_id == instance
      (popped synchronously):
      if spec.policies.retry_on_worker_loss and task.retries < max_retries:
          task.retries += 1
          # explicit state reset — SQLite must not keep showing the
          # task ASSIGNED to a dead worker:
          task.status = PENDING; task.worker_id = None
          task.deadline = None; task.updated_at = now
          save_task
          task_router.retry(task, spec, entry.trigger_source,
                            not_before=now + policies.retry_delay_seconds)
      else:
          task → FAILED {"error": "worker_lost: ..."} + task.failed event
      release(task_id) either way

tick loop (existing) additionally reaps deadline-overdue ASSIGNED tasks:
  pop from inflight synchronously, then the same policy branch as
  worker loss. A reaped-then-arriving result is stale by construction
  (its entry is gone) and is ignored by pop-if-current.
```

Fencing rationale: with retries, the same task_id can be dispatched
twice (attempt A to worker_1, attempt B to worker_2 after A is presumed
lost). A late result from attempt A must not complete attempt B —
results are matched on `(task_id, dispatch_id, instance_id)`, and each
dispatch gets a fresh `dispatch_id`. The in-flight map is owned by the
dispatch layer; entries are removed exactly once (result, loss, or
reap — whichever pops first wins, the others become no-ops). The
registry never touches task state; the router never touches the bus
subjects — boundaries stay as in Phase 2.

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
- Duplicate result / result racing the reaper: pop-if-current accepts
  exactly one outcome per dispatch; everything else is logged and
  ignored — no double episode, double release, or terminal-status
  overwrite.
- Core restart: the registry refills within one `heartbeat_interval`
  (profile-carrying heartbeats). The in-flight map is lost; ASSIGNED
  rows survive in SQLite but v1 does not reconcile them on boot (same
  recorded limitation as the Phase 2 pending queue; both revisit
  together with JetStream).

## Config

```yaml
node_role: worker             # existing field: standalone | core | worker

worker:                       # who am I as an executor
  id: worker_a                # subject-safe: ^[a-z][a-z0-9_]*$
  capabilities: []            # e.g. [shell, python]
  max_slots: 4                # replaces router.agent.max_slots

# Validation: for node_role: worker the id MUST be set explicitly and
# must not be "local" — that value is reserved for the core's inline
# executor; a silently-defaulted worker.id would collide with it.

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

- At-most-once delivery **per dispatch**; with
  `retry_on_worker_loss: true` one logical task may execute more than
  once (a lost result triggers a re-dispatch of already-done work).
  The deadline reaper converts silent loss into the worker-loss
  policy. JetStream upgrade keeps the same subjects.
- **Exactly one active core is supported.** The bus has no leader
  election; two cores would independently reserve capacity and race on
  the same triggers. Multi-core is out of scope for the whole phase.
- No reconciliation of ASSIGNED tasks after core restart.
- Worker executes with its own LLM config; capability strings are
  free-form labels matched by set inclusion — no versioning/semantics
  yet.
- `worker.heartbeat` traffic is O(workers), fine for a single-operator
  fleet; no sharding considerations.

## Testing

- **Unit, registry** — register/heartbeat/refresh with injected `now`;
  sweep removes silent workers exactly once at
  `last_seen + liveness_timeout`; re-registration after offline
  revives; `alive_profiles` excludes offline; a heartbeat alone (no
  prior registered) creates the entry — core restart recovery.
  **First-alive-owns fencing**: while an owner is alive, a second
  instance's registered/heartbeat/offline are rejected (no ping-pong —
  alternating heartbeats from two instances leave ownership stable);
  after graceful offline the next heartbeat claims immediately; after
  a liveness timeout the id is claimable. **Loss callback**: invoked
  exactly once per lost incarnation with (worker_id, instance_id); on
  timeout the registry then publishes `worker.offline(timeout)`, on
  graceful shutdown it publishes nothing (the worker already did); a
  stale offline event triggers no callback and no publication.
- **Unit, scoring** — capability filter (subset, missing, empty
  requires), free-slot ranking, stable order, zero-free-slots kept.
- **Unit, node** — assign→result round-trip with stub engine
  (dispatch_id echoed); assign with foreign `target_instance_id`
  dropped without execution; over-assignment beyond max_slots →
  `worker_busy` result; start order is subscribe → flush → registered;
  **stop order**: no heartbeat is published after
  `worker.offline(shutdown)` (heartbeat loop cancelled and awaited
  first), offline is the last worker.* message on the bus.
- **Unit, dispatch fencing** — late result with the old dispatch_id
  after a retry re-dispatch is ignored (task stays with attempt B);
  result after deadline reap is ignored; exactly one of
  {result, loss, reap} wins per dispatch.
- **Unit, retry** — `TaskRouter.retry` enqueues (never runs inline);
  entry with future `not_before` is skipped by `dequeue_ready` until
  due; `expires_at = max(now, not_before) + queue_ttl` (a delay longer
  than the TTL still leaves a full TTL window after becoming
  runnable); the worker-loss path resets task state
  (PENDING, worker_id=None, deadline=None) and persists it before
  enqueueing.
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
