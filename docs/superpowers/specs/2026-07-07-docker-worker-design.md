# Docker Worker (Phase 3, part 2) — Design

**Status:** Draft
**Date:** 2026-07-07
**Phase:** 3 (Distribution) — second sub-project

## Goal

Let the core run worker nodes inside containers: an operator declares a
fleet of docker workers, and the core starts them, keeps them running
(restart on container exit), and stops them on shutdown. Each container
runs `python -m proctor` in worker role and self-registers over NATS —
so this reuses the entire PR #30 distribution machinery (registry,
dispatch, fencing) unchanged. The only new capability is **container
lifecycle management** on the core.

## Scope

In scope:

- `infra/docker.py` — a thin async wrapper over the container CLI
  (`docker` or `podman`), runtime-agnostic, no new Python dependency.
- `workers/docker.py` — `DockerWorkerManager`: declarative fleet →
  running containers, poll-based restart with backoff, lifecycle tied
  to the core.
- A `Dockerfile` producing an image that runs proctor in worker role.
- Env-var config overrides in `load_config` for the handful of fields a
  container worker needs.
- Config: a `docker_workers:` section on the core.

Out of scope (deferred):

- SSH workers (`workers/remote.py`) — next sub-project; reuses
  WorkerNode identically, differs only in how the process is launched.
- `infra/ssh.py`, `infra/vagrant.py`, etc. (M9 beyond docker).
- Image build/publish pipeline (the Dockerfile is provided; building and
  pushing it is an operator/CI concern, not core runtime).
- Autoscaling, bin-packing, GPU/resource scheduling.
- Restarting a *hung but alive* container (heartbeat lost, container up)
  — see Restart trigger below; the registry marks it offline so the core
  stops dispatching, and it is left for the operator. Recorded limitation.

## Why fresh worker_id per launch (interaction with PR #30 fencing)

The registry uses **first-alive-owns** incarnation fencing: a crashed
container does not publish `worker.offline(shutdown)`, so the registry
holds its `worker_id` bound to the dead instance until `liveness_timeout`
(~90 s default). Restarting a container under the **same** `worker_id`
would therefore be **rejected** by the registry for up to
`liveness_timeout`.

**Decision:** every container launch gets a **fresh** `worker_id`. The
dead id sits in the registry as an offline entry until the next
liveness sweep GCs it (verified: `WorkerRegistry.sweep` does
`del self._entries[wid]` — no tombstone accumulation), while the new id
registers immediately under a different key. This touches the
ownership/fencing protocol **not at all** — deliberately.

Rejected alternative — manager publishes `worker.offline` for the dead
container to free the id and keep `worker_id` stable: a third party
declaring offline for another incarnation reaches straight into PR #30's
ownership semantics (the publisher would need a fencing token), and a
manager↔registry partition (worker alive, manager thinks it dead) would
false-evict a live worker, violating first-alive-owns. The fresh-id
approach has neither problem, at the cost of id churn.

**Id shape:** `{base}_{slot}_{suffix}` where `suffix` is a
`uuid4().hex[:12]` fragment. The hard requirement is **uniqueness**, not
monotonicity — a fresh uuid avoids collision with a still-hanging
offline id even across a manager/core restart, without a persistent
counter. (ULID was considered and rejected: Crockford base32 is
uppercase, violating the `^[a-z][a-z0-9_]*$` subject-safe constraint on
`worker_id`.) `{base}_{slot}` is the **stable observability key** —
logs, metrics, and the manager's slot table are keyed on it, so a crash
does not fragment observability across churning ids.

Registry GC behavior, confirmed and relied upon: timed-out entries are
removed at the sweep; stale-offline-ignored and reclaim-after-timeout
are already covered by `test_stale_offline_ignored`,
`test_id_claimable_after_liveness_timeout`, and
`test_graceful_offline_releases_then_next_claims`.

## Components

| File | Contents |
|------|----------|
| `infra/docker.py` | `ContainerRuntime(binary, run_cmd=...)` — async CLI wrapper. `run(spec) -> container_id`, `inspect(id) -> ContainerStatus`, `stop(id, timeout)`, `remove(id)`, `logs(id) -> str`. Every call shells out via `asyncio.create_subprocess_exec`; the exec function is injected (`run_cmd`) so unit tests fake subprocess with zero daemon. Pydantic `ContainerSpec` (image, name, env, env_file, labels, network) and `ContainerStatus` (id, state, exit_code, started_at). `inspect` uses `--format '{{json .}}'` — structured only, never scrape human output. `ContainerStatus.parse` normalizes the docker-vs-podman JSON shape divergence into one model. |
| `workers/docker.py` | `DockerWorkerManager(runtime, config, bus, *, now_fn=None)` — owns a slot table `slot -> SlotState(worker_id, container_id, restarts, started_at)`. `start()` launches every declared replica; a poll loop (`poll_interval`) inspects containers, restarts exited ones with jittered backoff, resets the restart count after a stability window, and trips a slot to `failed` (publishing `docker_worker.failed`) past `max_restarts`. `stop()` gracefully stops+removes all containers. Same lifecycle discipline as the registry sweep / router tick loop: loop body exception-guarded, cancelled in `Application.stop()` before bus drain. |
| `Dockerfile` | Slim Python 3.12 base, installs proctor (with the `nats` extra), non-root user, entrypoint `python -m proctor --config /etc/proctor/worker.yaml`. The image ships a minimal base `worker.yaml` (`node_role: worker`, `transport: nats`); per-container specifics arrive via env overrides (below), so the same image serves every slot. |
| `core/config.py` | `DockerWorkerConfig` (image, capabilities, replicas, runtime, env, network, base_worker_id, `nats_servers` — the **container-facing** NATS address) and `ProctorConfig.docker_workers: list[DockerWorkerConfig]`; env-var overrides in `load_config` (below). |
| `core/bootstrap.py` | Wire `DockerWorkerManager` into the core/standalone branch (not worker role): construct after the registry, `start()` it, `stop()` it before drain. |

## Config injection into containers (Approach A)

The container worker needs: `node_role=worker`, `transport=nats`,
`nats.servers` (a core-reachable NATS address), its `worker.id`,
`capabilities`, and an LLM key (litellm reads that from its own env).
`load_config` gains explicit env overrides for exactly these fields,
applied after YAML load:

- `PROCTOR_NATS_SERVERS` — comma-separated, overrides `nats.servers`.
  The manager passes the fleet's `nats_servers` field here, which is the
  **container-facing** address (a container cannot reach the core's own
  `localhost:4222` — it needs e.g. `host.docker.internal:4222` or a
  compose/pod service name). It is a distinct config field precisely
  because it differs from the core's own `nats.servers`.
- `PROCTOR_WORKER_ID` — overrides `worker.id`.
- `PROCTOR_WORKER_CAPABILITIES` — **CSV** (`shell,python`), whitespace
  around items trimmed; empty string → `[]`. Overrides
  `worker.capabilities`.

Approach A (explicit, ~10 lines) over A′ (pydantic-settings nested
override): `pydantic-settings` is only a transitive dep and
`ProctorConfig` is a plain `BaseModel`, so A′ would mean a new direct
dependency, converting the model to `BaseSettings`, and making *every*
field env-overridable (a footgun). A keeps the surface to the three
fields a container actually needs.

Mechanism per container: the manager passes non-secret overrides via
`docker run -e PROCTOR_WORKER_ID=... -e PROCTOR_WORKER_CAPABILITIES=... -e PROCTOR_NATS_SERVERS=...`.
`PROCTOR_WORKER_ID` is injected by the manager (it generates the
per-incarnation id) — never baked into the image. The **LLM key is
passed via `--env-file`** (a temp file the manager writes `chmod 600`,
removes on stop), not `-e`, because `-e` secrets are visible in
`docker inspect` / process argv.

## Restart policy

- **Trigger:** container exit only, detected by the poll loop
  (`inspect` state `exited`). The manager owns *container* liveness; the
  registry independently owns *worker* liveness (heartbeat). These are
  different facts, not two sources of one fact — a crashed container
  stops heartbeating too, but the registry would only notice ~1
  `liveness_timeout` later and cannot restart anything. Restarting on
  registry `worker.offline` was rejected: slower, and it would also fire
  for a merely-slow worker (flap).
- **Backoff with jitter:** exponential base delay with **full jitter**,
  so a core-NATS blip that kills every slot at once does not produce a
  synchronized thundering-herd of restarts.
- **Stability-window reset:** a slot's restart counter resets to 0 once
  its current container has been up longer than `stability_window`.
  Without this, a slot that restarts rarely-but-regularly over weeks
  accumulates unrelated restarts and falsely trips the ceiling.
- **Crash-loop ceiling:** past `max_restarts` (within the window) the
  slot is marked `failed`, `docker_worker.failed` is published for the
  operator, and the manager stops restarting that slot.
- **Fresh id on every (re)start**, per the fencing section above.

## Graceful shutdown (drain vs abandon, made explicit)

`Application.stop()` (core) → `DockerWorkerManager.stop()` →
`runtime.stop(id, timeout=T)`, which issues `docker stop -t T`: SIGTERM,
then SIGKILL after `T`. Inside the container SIGTERM reaches
`python -m proctor` → `app.stop()` → worker-role stop → `WorkerNode.stop()`,
which **drains in-flight executions up to `drain_timeout`, then cancels
(abandons) the rest** and publishes `worker.offline(shutdown)`.

Invariant: the manager's stop timeout `T` must be **≥ the worker's
`drain_timeout`**, otherwise SIGKILL cuts the drain short. Abandoned
in-flight tasks are handled by existing core machinery — the graceful
`worker.offline(shutdown)` fires the registry loss callback → worker-loss
policy (retry-if-opted-in, else fail); a task killed by SIGKILL with no
offline is caught by the deadline reaper. No new task-recovery logic.

## Limitations (v1, recorded deliberately)

- A hung-but-alive container (worker process up, heartbeat stopped) is
  not restarted: the registry marks it offline so the core stops
  dispatching, but the container lingers until the operator intervenes.
  Restart is container-exit-triggered only.
- `worker_id` is not stable across restarts (id churn); `{base}_{slot}`
  is the stable key for observability.
- Single active core (inherited from PR #30) — two cores would both try
  to manage the same fleet.
- The image build/publish is out of band; the manager assumes the image
  is present to the container runtime.

## Testing

- **Unit, `infra/docker.py`** — fake `run_cmd` returning canned
  stdout/rc: `run` builds correct argv (env, env_file, labels, network),
  `stop`/`remove` argv, `inspect` parses `--format '{{json .}}'`.
  **Both runtimes**: `ContainerStatus.parse` against a captured
  **docker** JSON fixture and a captured **podman** JSON fixture (CI
  likely has only podman, so docker parsing is otherwise uncovered).
- **Unit, `DockerWorkerManager`** with a fake `ContainerRuntime`: launches
  `replicas` containers with distinct ids; a fresh id per (re)start;
  exited container → restart; backoff increases and carries jitter;
  stability-window resets the counter; ceiling trips `failed` +
  `docker_worker.failed`; `stop()` stops+removes all.
  **Collision-on-manager-restart**: a new manager instance starting
  against a slot whose previous id is still "offline" picks a
  non-colliding fresh id (the argument for a unique, not sequential,
  suffix).
- **Unit, `load_config` overrides** — `PROCTOR_NATS_SERVERS`,
  `PROCTOR_WORKER_ID`, `PROCTOR_WORKER_CAPABILITIES` (CSV boundaries:
  empty / one / many / surrounding whitespace); absence leaves YAML
  values intact.
- **Integration, `docker` marker** (deselected by default, run via
  podman here): a real core `Application` + a real container worker;
  wait for `worker.registered` in the core registry; dispatch a
  `requires`-gated task and assert `task.completed` with the container's
  output; `docker kill` the container and assert the manager restarts it
  under a **new** worker_id and the fleet returns to full strength.
- All async tests anyio; injected clock (`now_fn`) for backoff/stability
  timing — no sleep-based timing assertions at unit level.
