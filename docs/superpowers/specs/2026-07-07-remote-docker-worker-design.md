# Remote Docker Worker (Phase 3, part 3) — Design

**Status:** Draft
**Date:** 2026-07-07
**Phase:** 3 (Distribution) — third sub-project

## Goal

Run container workers on **remote hosts** by pointing the existing docker
worker at a remote container socket over SSH (`DOCKER_HOST=ssh://…` /
podman `CONTAINER_HOST=ssh://…`). A docker fleet gains an optional
`ssh_host`; when set, every runtime operation for that fleet transparently
targets the remote host. This reuses the entire PR #32 machinery
(`DockerWorkerManager`, restart/backoff/ceiling, fresh-id, secret
env-file) unchanged — the "remote worker" is a docker worker whose
runtime socket is remote.

## Scope

In scope:

- `DockerWorkerConfig.ssh_host: str | None` and a `_ssh_env(fleet)` helper
  mapping `runtime` → the right transport env var.
- Per-instance `env` and a per-operation `op_timeout` on `ContainerRuntime`.
- `_poll_once` hardening: a hung/failed runtime op becomes a bounded,
  observable "unreachable" state with a ceiling (finding #1), instead of
  a silent stall of the whole poll loop.
- `_launch` failures (at start and on relaunch) are caught and retried
  via backoff, so a remote host down at core startup does not crash the
  manager.
- Config validators: `ssh_host` form, and a NATS-reachability check for
  remote fleets.
- Remote-setup docs (operational preconditions).

Out of scope / **deferred** (recorded so the door stays open):

- **Bare hosts without a container runtime** — the full SSHBackend
  (`infra/ssh.py` over asyncssh, `nohup`+pidfile, three-state poll). Not
  built now (rule of three: only one real backend exists). If the driving
  use case turns out to be bare/blocked hosts, this design is a detour and
  we build the SSHBackend instead.
- `WorkerFleetManager` / `WorkerBackend` base extraction — premature at
  N=1 real backend; revisit when the bare-SSH backend is actually needed.
- `workers/remote.py`, `infra/ssh.py`, the `asyncssh` dependency.
- Renaming `docker_worker.*` events to a generic `worker_fleet.*` — that
  was part of the base extraction; events stay `docker_worker.*`.

**Assumption (flag on review):** target remote hosts have a container
runtime (docker/podman) and are hosts we control. proctor-a is a 0.1.0
dogfooding lab with no production fleet; the driving case is spreading
load across controlled machines, which can run containers. If instead the
real need is hosts that *cannot* run containers, stop and build the
deferred SSHBackend.

## Mechanism

`DockerWorkerConfig.ssh_host: str | None = None`. When set, the manager's
`ContainerRuntime` is constructed with an env that points the client at
the remote socket; the manager is otherwise **unchanged** — it builds
`ContainerSpec`s and calls `run/inspect/stop/remove/logs`, all of which
transparently execute against the remote host.

`_ssh_env(fleet) -> dict[str, str]`:

- `runtime == "docker"` → `{"DOCKER_HOST": f"ssh://{ssh_host}"}`
- `runtime == "podman"` → `{"CONTAINER_HOST": f"ssh://{ssh_host}"}`
- `ssh_host is None` → `{}` (local, current behavior).

The secret `--env-file` stays **local**: the docker client reads it
locally and sends the values over the tunneled API, so the existing
mkdtemp-0600 fleet env-file works for remote fleets with no change (the
"secret on remote disk" concern does not arise on this path).

## Per-fleet env + per-op timeout on ContainerRuntime

Two changes to `ContainerRuntime` (both needed for remote correctness):

1. **Per-fleet env (not global).** `ContainerRuntime(binary, run_cmd=None,
   env=None, op_timeout=None)`. `_default_run_cmd` passes
   `env={**os.environ, **(env or {})}` to `create_subprocess_exec`.
   Bootstrap builds one runtime per fleet with that fleet's `_ssh_env`, so
   two fleets targeting two hosts never clobber each other (today no `env`
   is passed at all — a global var would collide).

2. **Per-op timeout (the load-bearing fix for hangs).** `DOCKER_HOST=ssh://`
   shells the docker client into the `ssh` binary, which does **not**
   crash but **hangs** on: a host key not in `known_hosts` (interactive
   prompt with no tty), or a black-holed TCP (firewalled port → kernel
   timeout of tens of seconds to minutes). Today `_default_run_cmd` awaits
   `communicate()` with no deadline and `_poll_once` awaits `inspect`
   unwrapped, so one hung op stalls the **entire poll loop for every slot**
   — worse than the silent-stuck-slot it was meant to fix. Each runtime op
   runs under a deadline: on expiry `_default_run_cmd` **explicitly kills
   the subprocess and reaps it** (`proc.kill()` then `await proc.wait()`)
   before raising `RuntimeError` — a bare `asyncio.timeout` around
   `communicate()` only cancels the await and would leak the ssh/docker
   child as a zombie (and fail the "process is gone" test). The raised
   timeout feeds the unreachable logic below.

   **`stop` gets its own budget, not `op_timeout` (finding A).** `stop` is
   `docker stop -t <stop_timeout>`; a container that ignores SIGTERM for
   the full grace window makes `stop` legitimately take ~`stop_timeout`
   (+ ssh latency). If it ran under the same `op_timeout` (both default
   30s), the deadline would kill the client **mid-drain** — breaking the
   graceful shutdown it exists to protect, even locally (default vs
   default). So poll/inspect/run/remove/logs use `op_timeout` (they must
   be fast); `stop` uses `stop_timeout + op_margin` (ssh/latency headroom),
   so it can never be cut before its own grace completes. `op_timeout`
   (`op_timeout: float`, e.g. 30s) and `op_margin` (e.g. 10s) are fleet
   config fields.

The op-timeout is the code-side **guarantee** that a hang is bounded.
Fail-fast SSH options (below) are an operational optimization that turns
a hang into a fast failure rather than a full-timeout wait.

## Poll-loop hardening: bounded, observable unreachability (finding #1)

`_poll_once` currently calls `inspect` unwrapped and treats only `exited`
specially. New behavior, per slot:

- `inspect` succeeds → clear `unreachable_since` (recovery reset — a
  flapping tunnel that recovers must not accumulate unreachable time);
  then the existing `exited` → restart / stability-window logic runs.
- `inspect` raises (timeout or transport error) → set `unreachable_since`
  to `now` if unset; if `now - unreachable_since >= max_unreachable_duration`,
  trip the slot to `failed` and publish `docker_worker.failed`
  (`reason="unreachable"`, with any diagnostics available). Otherwise
  leave the slot as-is and move on — the loop is **not** stalled (the op
  is bounded by `op_timeout`), and other slots are still polled.

`SlotState` gains `unreachable_since: datetime | None = None`.
`DockerWorkerConfig` gains `max_unreachable_duration: float` (e.g. 120s).

**`_launch` is failure-tolerant** at start and on relaunch: a failed or
timed-out launch (remote host down at core start) is caught and treated
like an exit — increment `restarts`, schedule a backoff retry, and trip
the slot to `failed` past `max_restarts` — rather than propagating. Start
therefore never crashes because a remote host is unreachable, and a
permanently-down-at-start host eventually trips the crash-loop ceiling
instead of retrying forever.

## Config

`DockerWorkerConfig` additions:

- `ssh_host: str | None = None` — form `[user@]host[:port]` (the manager
  prepends `ssh://`). A validator **rejects** a value already starting
  with `ssh://` (single owner of the scheme prefix; prevents
  `ssh://ssh://box`).
- `op_timeout: float = Field(default=30.0, gt=0.0)` — deadline for
  poll/inspect/run/remove/logs (fast ops).
- `op_margin: float = Field(default=10.0, gt=0.0)` — extra headroom on
  `stop`'s deadline over its `-t` grace, so drain is never cut short.
- `max_unreachable_duration: float = Field(default=120.0, gt=0.0)`.

**NATS-reachability validator (semantic, not "non-default"):** if
`ssh_host` is set AND any entry in `nats_servers` contains
`host.docker.internal`, `localhost`, `127.0.0.1`, `::1`, or `172.17.0.1`
(the default docker bridge IP — the next-most-common footgun after
`host.docker.internal`), raise ValidationError — those addresses never
resolve to the core from a remote host. This catches the misconfiguration
by value (a loopback/docker-internal address) rather than by comparing
against the default object, which pydantic cannot distinguish from a user
re-typing the same value. It cannot catch every unroutable private IP, but
covers the common footguns.

## Operational preconditions (docs, not code)

`DOCKER_HOST=ssh://` runs the system `ssh` client from **inside the core's
runtime**. If the core runs in a container (the project Dockerfile), that
runtime must carry: the `ssh` binary, a usable private key (agent or
mounted), and a `known_hosts` entry for each remote host. The current
image ships none of these — documented as a hard precondition in a
CLAUDE.md / README remote section, and exercised by the integration smoke.

**Podman remote is heavier than docker (finding D):** `CONTAINER_HOST=ssh://`
requires the remote host to run the `podman system service` (socket-activated)
— not just an installed binary. The remote-setup docs must call this out
alongside the ssh/key/known_hosts preconditions.

Recommended per-host `~/.ssh/config` (turns missing-key/host-key and dead
hosts from hangs into fast failures, complementing the op-timeout):

```
Host <remote>
    BatchMode yes
    ConnectTimeout 10
    StrictHostKeyChecking yes
```

docker does not forward `-o` ssh flags, so these live in the operator's
ssh config; the code-side `op_timeout` is the backstop for any not set.

## Limitations (v1, recorded)

- **Transport flap delays recovery.** On a transport failure the manager
  cannot tell "container exited" from "socket unreachable" — both become
  `unreachable` and wait up to `max_unreachable_duration` rather than
  restarting immediately. Acceptable (better than the prior
  stuck-forever), but recorded.
- **Orphaned remote container after unreachable-failure.** If a slot trips
  to `failed(reason="unreachable")` and the host later returns, the remote
  container may still be alive; the failed slot does not reap it. Low
  priority for v1; noted.
- **Untracked container from a timed-out `run` (finding C).** If a `run`
  is killed by `op_timeout` but the container actually started on the
  remote host, the manager never recorded its id, so the next `_launch`
  proceeds with a fresh worker_id (no name collision) while the orphan
  keeps running and heartbeating to NATS — worse than the tracked
  orphan above, because nothing knows it exists. Bounded by the crash-loop
  ceiling on the slot, but the stray container persists until an operator
  reaps it. Recorded; a future reconcile-by-label sweep would close it.
- Sequential per-tick polling: a dead host's replicas each cost up to
  `op_timeout` per tick (they share the down host). Fine for small fleets;
  parallelizing inspects is a future optimization.
- Single active core; image build/publish out of band (inherited).

## Testing

- **Unit, config** — `ssh_host="ssh://box"` rejected; `_ssh_env` maps
  docker→`DOCKER_HOST`, podman→`CONTAINER_HOST`, None→`{}`, all with the
  `ssh://` prefix; NATS validator rejects a remote fleet whose
  `nats_servers` contains loopback/`host.docker.internal`, accepts a
  routable address.
- **Unit, ContainerRuntime** — `env` is merged into the subprocess
  environment (two runtimes with different env don't clobber; verified via
  a fake run_cmd capturing the env it was given — extend the fake to
  receive env); `op_timeout` kills a hanging op and **reaps** it: a focused
  test runs a real trivial `sleep`-style command via the default run_cmd
  with a tiny `op_timeout` and asserts it raises **and the child process is
  gone** (pid not alive — proves kill+reap, not just await-cancel; no
  docker/ssh needed); `stop`'s deadline is `stop_timeout + op_margin`, not
  `op_timeout` — a fake whose `stop` sleeps longer than `op_timeout` but
  within `stop_timeout + op_margin` completes rather than being cut
  (guards finding A: drain isn't killed mid-grace).
- **Unit, manager unreachable** (fake runtime whose `inspect` raises):
  a single failure does not stall the loop or trip failure immediately;
  after `max_unreachable_duration` the slot is `failed` with
  `docker_worker.failed(reason="unreachable")`; a successful inspect
  **resets** `unreachable_since` (recovery test right beside the ceiling
  test); other slots keep being polled while one is unreachable.
- **Unit, launch tolerance** — `_launch` raising at start puts the slot in
  `backoff` and does not propagate; the poll loop later retries it.
- **Integration, `docker` marker** (deselected by default) — remote path
  needs a reachable remote socket + ssh key; collect+skip here. Where a
  local socket stands in, assert a fleet with `ssh_host=None` is
  unaffected (regression: local docker still works).
- All async tests anyio; injected clock for the unreachable timer.
