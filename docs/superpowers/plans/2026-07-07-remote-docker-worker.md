# Remote Docker Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run container workers on remote hosts by pointing the existing docker worker at a remote socket over SSH (`DOCKER_HOST=ssh://` / `CONTAINER_HOST=ssh://`), per `docs/superpowers/specs/2026-07-07-remote-docker-worker-design.md`.

**Architecture:** A docker fleet gains an optional `ssh_host`; the fleet's `ContainerRuntime` is built with an env pointing the client at the remote socket, and every op runs under a deadline that kills+reaps the child. The manager gains a bounded, observable "unreachable" state and failure-tolerant launch. Reuses all of PR #32 (restart/backoff/ceiling, fresh-id, local secret env-file).

**Tech Stack:** Python 3.12+, pydantic 2.x, asyncio, the container CLI (no new Python dependency), existing EventBus.

## Global Constraints

- uv only (`uv run pytest`); line length 88; `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check` clean before every commit; type hints everywhere; async tests use anyio (asyncio backend where the bus/aiosqlite are involved); pydantic `BaseModel`.
- **`op_timeout` never wraps `stop`.** Fast ops (run/inspect/remove/logs/poll) run under `op_timeout`; `stop` runs under `stop_timeout + op_margin` so a container using its full SIGTERM grace is never cut mid-drain (a bug even locally — default vs default).
- **A timed-out op explicitly kills AND reaps the child** (`proc.kill()` then `await proc.wait()`) — a bare `asyncio.timeout` around `communicate()` leaks the ssh/docker child as a zombie.
- **Fresh `worker_id` per launch** and `--restart=no` are unchanged from PR #32.
- Secret `--env-file` stays local (docker client reads it locally, sends values over the tunneled API).
- `ssh_host` form is `[user@]host[:port]`; a value already starting with `ssh://` is rejected; `_ssh_env` prepends the scheme.
- NATS-reachability validator rejects a remote fleet whose `nats_servers` contains `host.docker.internal`, `localhost`, `127.0.0.1`, `::1`, or `172.17.0.1`.
- Deferred (do NOT build): `infra/ssh.py`, `asyncssh`, `SSHBackend`, `workers/remote.py`, `WorkerFleetManager` base extraction, `docker_worker.*`→`worker_fleet.*` rename.
- Injected clock (`now_fn`) for the unreachable timer — no sleep-based timing assertions at unit level.
- Branch: `feat/remote-docker-worker`. TDD per task; commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: ContainerRuntime — per-fleet env + per-op timeout with kill/reap

**Files:**
- Modify: `src/proctor/infra/docker.py`
- Test: `tests/test_infra/test_docker.py`

**Interfaces:**
- Changed: `RunCmd = Callable[[list[str], float | None], Awaitable[tuple[int, str, str]]]` (adds a per-call timeout arg). `ContainerRuntime(binary, run_cmd=None, env=None, op_timeout=30.0, op_margin=10.0)`. Fast ops use `op_timeout`; `stop(id, timeout)` uses `ceil(timeout) + op_margin` as its deadline. Task 4 constructs the runtime with these.

- [ ] **Step 1: Update the failing tests**

The existing `tests/test_infra/test_docker.py` `_fake` returns a `run_cmd(argv)`. Update it to the 2-arg signature and add new tests. Replace the `_fake` helper and add tests:

```python
def _fake(rc: int = 0, out: str = "", err: str = "") -> tuple[
    Callable[[list[str], float | None], Awaitable[tuple[int, str, str]]],
    list[tuple[list[str], float | None]],
]:
    calls: list[tuple[list[str], float | None]] = []

    async def run_cmd(argv: list[str], timeout: float | None) -> tuple[int, str, str]:
        calls.append((argv, timeout))
        return rc, out, err

    return run_cmd, calls
```

Every existing assertion that read `calls[0]` (the argv) now reads `calls[0][0]`. Update those (they are in `test_run_builds_argv`, `test_inspect_parses_*`, `test_stop_remove_logs_argv`, `test_stop_rounds_fractional_timeout_up` — change `calls[-1]` → `calls[-1][0]` for argv checks). Add:

```python
async def test_fast_ops_use_op_timeout() -> None:
    run_cmd, calls = _fake(out="{}")
    rt = ContainerRuntime("docker", run_cmd=run_cmd, op_timeout=7.0, op_margin=3.0)
    await rt.remove("cid")
    assert calls[-1][1] == 7.0  # op_timeout


async def test_stop_uses_stop_timeout_plus_margin() -> None:
    run_cmd, calls = _fake()
    rt = ContainerRuntime("docker", run_cmd=run_cmd, op_timeout=7.0, op_margin=3.0)
    await rt.stop("cid", timeout=30.0)
    # deadline is the -t grace (30) + margin (3), NOT op_timeout (7)
    assert calls[-1][1] == 33.0


async def test_default_run_cmd_kills_and_reaps_on_timeout() -> None:
    # A real slow command under a tiny timeout must raise AND leave no child.
    from proctor.infra.docker import _make_default_run_cmd

    run_cmd = _make_default_run_cmd(env=None)
    with pytest.raises(RuntimeError, match="timed out"):
        # `sleep 30` under a 0.3s deadline
        await run_cmd(["sleep", "30"], 0.3)


async def test_env_merged_into_subprocess() -> None:
    from proctor.infra.docker import _make_default_run_cmd

    run_cmd = _make_default_run_cmd(env={"PROCTOR_TEST_MARKER": "xyz"})
    # `env` prints the environment; assert our marker is present
    rc, out, err = await run_cmd(
        ["sh", "-c", "echo $PROCTOR_TEST_MARKER"], 5.0
    )
    assert rc == 0
    assert "xyz" in out
```

(`Callable`/`Awaitable` are imported in the test file already; if not, add `from collections.abc import Awaitable, Callable`.)

- [ ] **Step 2: Run to verify failures**

`uv run pytest tests/test_infra/test_docker.py -q` — signature mismatches + missing `_make_default_run_cmd`/env/timeout behavior.

- [ ] **Step 3: Implement**

In `src/proctor/infra/docker.py`:

Change the `RunCmd` alias and replace `_default_run_cmd` with a factory:

```python
RunCmd = Callable[[list[str], float | None], Awaitable[tuple[int, str, str]]]


def _make_default_run_cmd(env: dict[str, str] | None) -> RunCmd:
    """Build a subprocess runner with `env` merged over os.environ.

    Applies a per-call deadline and, on expiry, explicitly kills and
    reaps the child (a bare asyncio.timeout only cancels the await and
    would leak the ssh/docker process as a zombie).
    """
    merged = {**os.environ, **(env or {})}

    async def run_cmd(
        argv: list[str], timeout: float | None
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged,
        )
        try:
            async with asyncio.timeout(timeout):
                out, err = await proc.communicate()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"{' '.join(argv)} timed out after {timeout}s"
            ) from None
        return (
            proc.returncode or 0,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )

    return run_cmd
```

Add `import os` at the top if absent (it is not currently imported in this file). `math` and `asyncio` are already imported.

Update `ContainerRuntime`:

```python
    def __init__(
        self,
        binary: str,
        run_cmd: RunCmd | None = None,
        *,
        env: dict[str, str] | None = None,
        op_timeout: float = 30.0,
        op_margin: float = 10.0,
    ) -> None:
        self._binary = binary
        self._run = run_cmd or _make_default_run_cmd(env)
        self._op_timeout = op_timeout
        self._op_margin = op_margin

    async def _exec(self, args: list[str], timeout: float | None) -> str:
        argv = [self._binary, *args]
        rc, out, err = await self._run(argv, timeout)
        if rc != 0:
            raise RuntimeError(
                f"{' '.join(argv)} exited {rc}: {err.strip() or out.strip()}"
            )
        return out
```

Thread the timeout through every op. `run`, `inspect`, `remove`, `logs` pass `self._op_timeout`; `stop` passes `secs + self._op_margin`:

```python
    async def run(self, spec: ContainerSpec) -> str:
        args = [ ... unchanged ... ]
        return (await self._exec(args, self._op_timeout)).strip()

    async def inspect(self, container_id: str) -> ContainerStatus:
        out = await self._exec(
            ["inspect", "--format", "{{json .}}", container_id], self._op_timeout
        )
        return ContainerStatus.parse(json.loads(out))

    async def stop(self, container_id: str, timeout: float) -> None:
        secs = max(1, math.ceil(timeout)) if timeout > 0 else 0
        await self._exec(
            ["stop", "-t", str(secs), container_id],
            float(secs) + self._op_margin,
        )

    async def remove(self, container_id: str) -> None:
        await self._exec(["rm", "-f", container_id], self._op_timeout)

    async def logs(self, container_id: str, tail: int) -> str:
        return await self._exec(
            ["logs", "--tail", str(tail), container_id], self._op_timeout
        )
```

- [ ] **Step 4: Run tests + full suite; gates; commit**

```bash
uv run pytest tests/test_infra/ -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(infra): ContainerRuntime per-fleet env + per-op timeout with kill/reap"
```

---

### Task 2: Config — ssh_host, timing fields, validators, _ssh_env

**Files:**
- Modify: `src/proctor/core/config.py`
- Test: `tests/test_core/test_config.py` (append)

**Interfaces:**
- Produces: `DockerWorkerConfig.ssh_host: str | None`, `.op_timeout: float`, `.op_margin: float`, `.max_unreachable_duration: float`; validators (ssh_host form, NATS reachability); module function `docker_ssh_env(fleet: DockerWorkerConfig) -> dict[str, str]`. Tasks 3–4 consume these.

- [ ] **Step 1: Write failing tests** (append to `tests/test_core/test_config.py`)

```python
class TestRemoteDockerConfig:
    def test_ssh_host_defaults_none(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        fleet = DockerWorkerConfig(image="i", base_worker_id="d")
        assert fleet.ssh_host is None
        assert fleet.op_timeout == 30.0
        assert fleet.op_margin == 10.0
        assert fleet.max_unreachable_duration == 120.0

    def test_ssh_host_with_scheme_rejected(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        with pytest.raises(ValidationError, match="ssh://"):
            DockerWorkerConfig(
                image="i", base_worker_id="d", ssh_host="ssh://box"
            )

    def test_ssh_env_docker(self) -> None:
        from proctor.core.config import DockerWorkerConfig, docker_ssh_env

        fleet = DockerWorkerConfig(
            image="i", base_worker_id="d", runtime="docker",
            ssh_host="user@box:2222",
            nats_servers=["nats://10.0.0.1:4222"],
        )
        assert docker_ssh_env(fleet) == {"DOCKER_HOST": "ssh://user@box:2222"}

    def test_ssh_env_podman(self) -> None:
        from proctor.core.config import DockerWorkerConfig, docker_ssh_env

        fleet = DockerWorkerConfig(
            image="i", base_worker_id="d", runtime="podman",
            ssh_host="box", nats_servers=["nats://10.0.0.1:4222"],
        )
        assert docker_ssh_env(fleet) == {"CONTAINER_HOST": "ssh://box"}

    def test_ssh_env_local_empty(self) -> None:
        from proctor.core.config import DockerWorkerConfig, docker_ssh_env

        fleet = DockerWorkerConfig(image="i", base_worker_id="d")
        assert docker_ssh_env(fleet) == {}

    @pytest.mark.parametrize(
        "server",
        [
            "nats://host.docker.internal:4222",
            "nats://localhost:4222",
            "nats://127.0.0.1:4222",
            "nats://[::1]:4222",
            "nats://172.17.0.1:4222",
        ],
    )
    def test_remote_fleet_rejects_unroutable_nats(self, server: str) -> None:
        from proctor.core.config import DockerWorkerConfig

        with pytest.raises(ValidationError, match="nats_servers"):
            DockerWorkerConfig(
                image="i", base_worker_id="d", ssh_host="box",
                nats_servers=[server],
            )

    def test_remote_fleet_routable_nats_ok(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        fleet = DockerWorkerConfig(
            image="i", base_worker_id="d", ssh_host="box",
            nats_servers=["nats://10.0.0.1:4222"],
        )
        assert fleet.ssh_host == "box"

    def test_local_fleet_keeps_default_nats(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        # no ssh_host → the host.docker.internal default is fine
        fleet = DockerWorkerConfig(image="i", base_worker_id="d")
        assert "host.docker.internal" in fleet.nats_servers[0]
```

- [ ] **Step 2: Run to verify failures**, then **Step 3: Implement**

In `DockerWorkerConfig`, add the fields (after `network`):

```python
    ssh_host: str | None = None
    op_timeout: float = Field(default=30.0, gt=0.0)
    op_margin: float = Field(default=10.0, gt=0.0)
    max_unreachable_duration: float = Field(default=120.0, gt=0.0)
```

Add validators to `DockerWorkerConfig` (`model_validator`/`Self` already imported):

```python
    @model_validator(mode="after")
    def _validate_ssh_host(self) -> Self:
        if self.ssh_host is not None and self.ssh_host.startswith("ssh://"):
            raise ValueError(
                "ssh_host must be [user@]host[:port] without a scheme; "
                "the 'ssh://' prefix is added automatically"
            )
        return self

    @model_validator(mode="after")
    def _validate_remote_nats_reachable(self) -> Self:
        if self.ssh_host is None:
            return self
        unroutable = (
            "host.docker.internal", "localhost", "127.0.0.1", "::1", "172.17.0.1",
        )
        for server in self.nats_servers:
            if any(bad in server for bad in unroutable):
                raise ValueError(
                    f"remote fleet nats_servers {server!r} is unroutable from "
                    "the remote host; set nats_servers to a core address "
                    "reachable from there"
                )
        return self
```

Add the module function (near the class):

```python
def docker_ssh_env(fleet: DockerWorkerConfig) -> dict[str, str]:
    """Env that points a fleet's container client at its remote socket.

    docker → DOCKER_HOST, podman → CONTAINER_HOST; local (no ssh_host) → {}.
    """
    if fleet.ssh_host is None:
        return {}
    url = f"ssh://{fleet.ssh_host}"
    if fleet.runtime == "podman":
        return {"CONTAINER_HOST": url}
    return {"DOCKER_HOST": url}
```

- [ ] **Step 4: Run tests + full suite; gates; commit**

```bash
uv run pytest tests/test_core/test_config.py -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(config): remote docker fleet — ssh_host, timeouts, validators, _ssh_env"
```

---

### Task 3: Manager — unreachable ceiling, recovery reset, launch tolerance

**Files:**
- Modify: `src/proctor/workers/docker.py`
- Test: `tests/test_workers/test_docker_manager.py` (append)

**Interfaces:**
- Consumes: `DockerWorkerConfig.max_unreachable_duration` (Task 2).
- Produces: `SlotState.unreachable_since: datetime | None`; `_poll_once` inspect-fault-tolerant; `_handle_unreachable(slot, now)`; `_backoff_or_fail(slot, now, tail, reason)`; `_launch_slot(slot, now) -> bool`. `docker_worker.failed` payload gains `reason`.

- [ ] **Step 1: Write failing tests** (append)

```python
async def test_inspect_failure_does_not_stall_other_slots(
    bus: EventBus, tmp_path: Path
) -> None:
    """One unreachable slot must not block polling the others."""
    from datetime import timedelta

    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path, replicas=2)
    await mgr.start()
    try:
        good = mgr.slots[1].container_id

        async def flaky_inspect(container_id: str):  # type: ignore[no-untyped-def]
            if container_id == mgr.slots[0].container_id:
                raise RuntimeError("unreachable")
            return ContainerStatus(
                id=container_id, state="running", exit_code=0,
                started_at="2026-07-07T12:00:00Z",
            )

        rt.inspect = flaky_inspect  # type: ignore[method-assign]
        await mgr._poll_once(T0)
        # slot 0 marked unreachable, slot 1 still inspected (unchanged)
        assert mgr.slots[0].unreachable_since == T0
        assert mgr.slots[1].container_id == good
        assert mgr.slots[1].state == "running"
    finally:
        await mgr.stop()


async def test_unreachable_ceiling_fails_slot(
    bus: EventBus, tmp_path: Path
) -> None:
    from datetime import timedelta

    events: list[object] = []

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe("docker_worker.>", collect)
    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path, replicas=1, max_unreachable_duration=60.0)
    await mgr.start()
    try:
        async def dead_inspect(container_id: str):  # type: ignore[no-untyped-def]
            raise RuntimeError("host down")

        rt.inspect = dead_inspect  # type: ignore[method-assign]
        await mgr._poll_once(T0)                       # marks unreachable_since
        assert mgr.slots[0].state == "running"
        await mgr._poll_once(T0 + timedelta(seconds=59))  # still under ceiling
        assert mgr.slots[0].state == "running"
        await mgr._poll_once(T0 + timedelta(seconds=60))  # ceiling
        assert mgr.slots[0].state == "failed"
        await bus.flush()
        failed = [
            e for e in events
            if getattr(e, "type", "") == "docker_worker.failed"
        ]
        assert failed
        assert failed[0].payload["reason"] == "unreachable"
    finally:
        await mgr.stop()


async def test_recovery_resets_unreachable_since(
    bus: EventBus, tmp_path: Path
) -> None:
    from datetime import timedelta

    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path, replicas=1, max_unreachable_duration=60.0)
    await mgr.start()
    try:
        cid = mgr.slots[0].container_id
        fail = {"on": True}

        async def flapping_inspect(container_id: str):  # type: ignore[no-untyped-def]
            if fail["on"]:
                raise RuntimeError("blip")
            return ContainerStatus(
                id=container_id, state="running", exit_code=0,
                started_at="2026-07-07T12:00:00Z",
            )

        rt.inspect = flapping_inspect  # type: ignore[method-assign]
        await mgr._poll_once(T0)
        assert mgr.slots[0].unreachable_since == T0
        fail["on"] = False  # tunnel recovers
        await mgr._poll_once(T0 + timedelta(seconds=30))
        assert mgr.slots[0].unreachable_since is None  # reset
    finally:
        await mgr.stop()


async def test_launch_failure_at_start_backs_off_not_crash(
    bus: EventBus, tmp_path: Path
) -> None:
    rt = FakeRuntime()

    async def boom_run(spec: object) -> str:
        raise RuntimeError("remote host down")

    rt.run = boom_run  # type: ignore[method-assign]
    mgr = _mgr(rt, bus, tmp_path, replicas=1)
    await mgr.start()  # must NOT raise
    try:
        assert mgr.slots[0].state == "backoff"
        assert mgr.slots[0].restarts == 1
    finally:
        await mgr.stop()
```

(`ContainerStatus` is imported in this test file from Task 3-of-the-docker-plan setup; if not, add `from proctor.infra.docker import ContainerStatus`.)

- [ ] **Step 2: Run to verify failures**, then **Step 3: Implement**

In `src/proctor/workers/docker.py`:

Add `unreachable_since` to `SlotState`:

```python
    unreachable_since: datetime | None = None
```

Replace `start()`'s launch loop to tolerate failure:

```python
    async def start(self) -> None:
        """Write the fleet env-file and launch all replicas."""
        self._write_env_file()
        for slot in range(self._fleet.replicas):
            await self._launch_slot(slot, self._now())
        self._poll_task = asyncio.create_task(self._poll_loop())
```

Add `_launch_slot` (wraps `_launch`, routes failure to backoff/ceiling):

```python
    async def _launch_slot(self, slot: int, now: datetime) -> bool:
        """Launch a slot; on failure schedule a backoff retry (or fail).

        Returns True on success. Ensures a SlotState exists so a launch
        that fails before any container is created still carries restart
        bookkeeping and is retried by the poll loop.
        """
        try:
            await self._launch(slot, at=now)
            return True
        except Exception:
            logger.exception("Launch failed for docker slot %d", slot)
            if slot not in self.slots:
                self.slots[slot] = SlotState(
                    slot=slot, worker_id="", container_id="",
                    restarts=0, started_at=now, state="backoff",
                )
            await self._backoff_or_fail(slot, now, tail="", reason="launch_failed")
            return False
```

Extract `_backoff_or_fail` from the tail of `_handle_exit`:

```python
    async def _backoff_or_fail(
        self, slot: int, now: datetime, tail: str, reason: str
    ) -> None:
        """Increment restarts; schedule backoff, or trip the ceiling."""
        state = self.slots[slot]
        next_restarts = state.restarts + 1
        if next_restarts > self._fleet.max_restarts:
            state.state = "failed"
            state.restarts = next_restarts
            logger.error(
                "Docker worker slot %d exceeded max_restarts (%s); last logs:\n%s",
                slot, reason, tail,
            )
            await self._bus.publish(
                Event(
                    type="docker_worker.failed",
                    source=_SOURCE,
                    payload={
                        "base_worker_id": self._fleet.base_worker_id,
                        "slot": slot,
                        "restarts": next_restarts,
                        "reason": reason,
                        "log_tail": tail,
                    },
                )
            )
            return
        delay = min(
            self._fleet.base_backoff * (2 ** (next_restarts - 1)),
            self._fleet.max_backoff,
        )
        state.restarts = next_restarts
        state.state = "backoff"
        state.restart_at = now + timedelta(seconds=self._jitter(delay))
        self._pending_tail[slot] = tail
```

Rewrite `_handle_exit` to reuse it:

```python
    async def _handle_exit(self, slot: int, now: datetime) -> None:
        """Capture logs, remove the container, schedule a backoff restart."""
        state = self.slots[slot]
        tail = ""
        try:
            tail = await self._rt.logs(state.container_id, tail=self._fleet.log_tail)
        except Exception:
            logger.exception("Failed to capture logs for %s", state.worker_id)
        try:
            await self._rt.remove(state.container_id)
        except Exception:
            logger.exception("Failed to remove %s", state.container_id)
        await self._backoff_or_fail(slot, now, tail, reason="exited")
```

Rewrite `_relaunch` to tolerate launch failure:

```python
    async def _relaunch(self, slot: int, now: datetime) -> None:
        tail = self._pending_tail.pop(slot, "")
        if not await self._launch_slot(slot, now):
            return  # launch failed; backoff already rescheduled
        logger.info(
            "Restarted docker worker slot %d (restart #%d)",
            slot, self.slots[slot].restarts,
        )
        await self._bus.publish(
            Event(
                type="docker_worker.restarted",
                source=_SOURCE,
                payload={
                    "base_worker_id": self._fleet.base_worker_id,
                    "slot": slot,
                    "restarts": self.slots[slot].restarts,
                    "log_tail": tail,
                },
            )
        )
```

Rewrite `_poll_once` to be inspect-fault-tolerant with the unreachable timer:

```python
    async def _poll_once(self, now: datetime) -> None:
        """One reconciliation pass over all slots (testable tick body)."""
        for slot, state in list(self.slots.items()):
            if state.state == "failed":
                continue
            if state.state == "backoff":
                if state.restart_at is not None and now >= state.restart_at:
                    await self._relaunch(slot, now)
                continue
            try:
                status = await self._rt.inspect(state.container_id)
            except Exception:
                await self._handle_unreachable(slot, now)
                continue
            state.unreachable_since = None  # recovery reset
            if status.state == "exited":
                await self._handle_exit(slot, now)
            elif (now - state.started_at).total_seconds() >= (
                self._fleet.stability_window
            ):
                state.restarts = 0
```

Guard `stop()` against a launch-failed slot's empty container_id: at the
top of `stop()`'s per-slot loop (the `for state in self.slots.values():`
block), add:

```python
            if not state.container_id:
                continue  # launch-failed slot never created a container
```

Add `_handle_unreachable`:

```python
    async def _handle_unreachable(self, slot: int, now: datetime) -> None:
        """A failed inspect: start/continue the unreachable timer, or fail."""
        state = self.slots[slot]
        if state.unreachable_since is None:
            state.unreachable_since = now
            logger.warning("Docker worker slot %d unreachable", slot)
            return
        if (now - state.unreachable_since).total_seconds() >= (
            self._fleet.max_unreachable_duration
        ):
            state.state = "failed"
            logger.error(
                "Docker worker slot %d unreachable past ceiling", slot
            )
            await self._bus.publish(
                Event(
                    type="docker_worker.failed",
                    source=_SOURCE,
                    payload={
                        "base_worker_id": self._fleet.base_worker_id,
                        "slot": slot,
                        "reason": "unreachable",
                    },
                )
            )
```

- [ ] **Step 4: Run tests + full suite; gates; commit**

```bash
uv run pytest tests/test_workers/test_docker_manager.py -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(workers): docker manager — unreachable ceiling, recovery reset, launch tolerance"
```

---

### Task 4: Bootstrap wiring + config example + remote docs

**Files:**
- Modify: `src/proctor/core/bootstrap.py`, `config/proctor.yaml`, `CLAUDE.md`
- Create: `docs/remote-workers.md`
- Test: `tests/test_workers/test_docker_bootstrap.py` (append)

**Interfaces:**
- Consumes: `docker_ssh_env` (Task 2), `DockerWorkerConfig.op_timeout/op_margin` (Task 2), `ContainerRuntime(..., env=, op_timeout=, op_margin=)` (Task 1).

- [ ] **Step 1: Write the failing test** (append to `tests/test_workers/test_docker_bootstrap.py`)

```python
async def test_remote_fleet_builds_runtime_with_ssh_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fleet with ssh_host gets a ContainerRuntime whose env targets it."""
    captured: list[dict[str, str] | None] = []

    class FakeRuntime:
        def __init__(self, binary, run_cmd=None, *, env=None,
                     op_timeout=30.0, op_margin=10.0):  # type: ignore[no-untyped-def]
            captured.append(env)

    class NoopManager:
        def __init__(self, runtime, fleet, bus, **kw):  # type: ignore[no-untyped-def]
            pass

        async def start(self) -> None: ...
        async def stop(self) -> None: ...

    monkeypatch.setattr("proctor.core.bootstrap.ContainerRuntime", FakeRuntime)
    monkeypatch.setattr(
        "proctor.core.bootstrap.DockerWorkerManager", NoopManager
    )
    config = ProctorConfig(
        data_dir=tmp_path / "d",
        docker_workers=[
            DockerWorkerConfig(
                image="i", base_worker_id="rem", ssh_host="user@box",
                nats_servers=["nats://10.0.0.1:4222"],
            )
        ],
    )
    app = Application(config, event_transport=LocalEventTransport())

    async def llm(prompt: str) -> str:
        return "ok"

    app.set_llm_call(llm)
    await app.start()
    await app.stop()
    assert {"DOCKER_HOST": "ssh://user@box"} in captured
```

(Add `DockerWorkerConfig` to the test file's imports if absent.)

- [ ] **Step 2: Run to verify failure**, then **Step 3: Implement bootstrap**

In `src/proctor/core/bootstrap.py`, import `docker_ssh_env`:

```python
from proctor.core.config import (
    ProctorConfig,
    _resolve_transport_mode_static,
    docker_ssh_env,
)
```

(Merge into the existing config import block.) Replace the docker manager construction in `start()`:

```python
        for fleet in self.config.docker_workers:
            manager = DockerWorkerManager(
                ContainerRuntime(
                    fleet.runtime,
                    env=docker_ssh_env(fleet),
                    op_timeout=fleet.op_timeout,
                    op_margin=fleet.op_margin,
                ),
                fleet,
                self.bus,
            )
            await manager.start()
            self._docker_managers.append(manager)
```

- [ ] **Step 4: Config example + docs**

Append to the `docker_workers:` example in `config/proctor.yaml`:

```yaml
#   - image: proctor:latest
#     base_worker_id: remote_gpu
#     capabilities: [python]
#     replicas: 1
#     runtime: docker
#     ssh_host: user@gpu-box:22        # [user@]host[:port], no ssh:// prefix
#     nats_servers: ["nats://10.0.0.5:4222"]  # core address routable FROM the host
#     secret_env: [ANTHROPIC_API_KEY]
#     op_timeout: 30.0                 # deadline for fast ops (kills a hung ssh)
#     max_unreachable_duration: 120.0  # then the slot is failed
```

Create `docs/remote-workers.md`:

```markdown
# Remote container workers (DOCKER_HOST=ssh://)

A docker fleet with `ssh_host` set runs its containers on a remote host by
pointing the container client at the remote socket over SSH. It reuses the
entire docker-worker lifecycle; only the socket is remote.

## Preconditions (the core's runtime must satisfy these)

`DOCKER_HOST=ssh://` runs the system `ssh` client from **inside the core's
runtime**. That runtime (host or the proctor image) must have:

- the `ssh` binary,
- a usable private key (ssh-agent or a mounted key),
- a `known_hosts` entry for each remote host,
- for **podman** remotes: a running `podman system service` (socket-activated)
  on the remote host — an installed binary is not enough.

Recommended per-host `~/.ssh/config` so a bad host key or dead host fails
fast instead of hanging (the code's `op_timeout` is the backstop):

    Host <remote>
        BatchMode yes
        ConnectTimeout 10
        StrictHostKeyChecking yes

## Config

`ssh_host` is `[user@]host[:port]` (no `ssh://` prefix — it is added
automatically). `nats_servers` must be a core address **routable from the
remote host** — `host.docker.internal`, `localhost`, `127.0.0.1`, `::1`,
and `172.17.0.1` are rejected because they never resolve to the core from
there.

## Known limitations

- A transport failure cannot be told apart from a container exit; the slot
  waits up to `max_unreachable_duration` before failing, rather than
  restarting immediately.
- A `run` killed by `op_timeout` that actually started the container on the
  remote host leaves an untracked container there; reap it manually.
```

Also add a one-line pointer in `CLAUDE.md`'s module table `infra/` / worker section and set **Next:** to "Phase 3 — mcp/ (SSH bare-host worker deferred)".

- [ ] **Step 5: Run; gates; commit**

```bash
uv run pytest tests/test_workers/ -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(core): wire remote docker fleets (ssh_host) + remote docs"
```

---

### Task 5: Integration regression + docs sync

**Files:**
- Modify: `tests/integration/test_docker_worker.py`, `TODO.md`
- (No new production code; fix in the owning module if a test exposes a bug.)

**Interfaces:** consumes everything.

- [ ] **Step 1: Add the regression assertion**

Read `tests/integration/test_docker_worker.py` first (the existing `docker`-marker test). Add one test (marker `docker`) that a fleet with `ssh_host=None` behaves exactly as before (local) — this is the regression guard that the env/timeout changes didn't break the local path. If a real local runtime is available it runs; otherwise it skips with the existing skip mechanism. Do NOT add a live remote test (needs a remote socket + key — collect+skip only). Add a comment block sketching the remote flow (fleet with `ssh_host`, wait for `worker.registered`, dispatch, then `docker kill` on the remote → manager restarts under a new worker_id), marked skip-unless-`PROCTOR_TEST_SSH_HOST` is set.

- [ ] **Step 2: Run + verify default suite excludes it**

```bash
uv run pytest -m docker tests/integration/test_docker_worker.py -v   # collects, runs-or-skips
uv run pytest -q                                                     # does NOT collect docker
```

- [ ] **Step 3: Docs sync**

In `TODO.md`, add a current-state bullet: Phase 3 (часть 3) — remote docker workers via `DOCKER_HOST=ssh://` (ssh_host on the docker fleet, per-op timeout with kill/reap, unreachable ceiling); bare-SSH backend deferred.

- [ ] **Step 4: Final gates, push, PR**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git push -u origin feat/remote-docker-worker
gh pr create --base master --title "feat(workers): remote docker workers via DOCKER_HOST=ssh:// (Phase 3, part 3)" --body "..."
```

PR body: reference the spec; the DOCKER_HOST=ssh:// approach and why (reuse over new SSHBackend, rule-of-three deferral); the op-timeout-with-kill/reap and stop-budget correctness fixes; unreachable ceiling + recovery reset + launch tolerance; the NATS-reachability and ssh_host validators; remote preconditions; test evidence (unit + docker-marker regression).

---

## Self-Review Notes

- Spec coverage: env + per-op timeout with kill/reap + stop-budget (T1); ssh_host/_ssh_env/timing fields/both validators incl. 172.17.0.1 (T2); unreachable ceiling + recovery reset + launch tolerance (T3); bootstrap wiring + config example + remote preconditions incl. podman-service note (T4); regression + docs (T5). Limitations (transport-flap delay, untracked timed-out run, orphan-after-unreachable) are documented in T4's `docs/remote-workers.md`.
- Type consistency: `RunCmd` 2-arg signature defined in T1 and used by the T1 fake; `docker_ssh_env` produced in T2, consumed in T4; `_backoff_or_fail(slot, now, tail, reason)` defined and called in T3 (`_handle_exit` reason="exited", `_launch_slot` reason="launch_failed", unreachable path publishes reason="unreachable" directly); `SlotState.unreachable_since` written in T3's `_poll_once`/`_handle_unreachable`.
- Known judgment calls: a launch-failed slot is seeded with `worker_id=""`/`container_id=""` in `backoff`; `_poll_once` only inspects `running` slots, so an empty container_id is never inspected, and `stop()` now skips empty-container_id slots explicitly (the guard added in T3). The `docker_worker.failed` payload gains `reason`; the existing ceiling test asserts `log_tail` which is still present, and `reason` is additive.
