# Docker Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Core-managed container fleet per `docs/superpowers/specs/2026-07-07-docker-worker-design.md`: an operator declares docker workers, the core starts them, restarts on container exit with backoff, and stops them on shutdown. Each container runs `python -m proctor` in worker role and self-registers over NATS.

**Architecture:** `infra/docker.py` is a runtime-agnostic async CLI wrapper (docker|podman) with an injectable exec function. `workers/docker.py`'s `DockerWorkerManager` owns a per-fleet slot table and a poll loop that mirrors the registry sweep loop's lifecycle discipline. Bootstrap wires one manager per fleet into the core/standalone role only. Reuses the PR #30 registry/dispatch/fencing unchanged.

**Tech Stack:** Python 3.12, pydantic 2.x, asyncio/anyio, the container CLI (no new Python dependency), existing EventBus.

## Global Constraints

- uv only (`uv run pytest`); line length 88; `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check` clean before every commit; type hints everywhere; async tests use anyio (asyncio backend where the bus/aiosqlite are involved); pydantic `BaseModel`.
- **Every container launched with `--restart=no`** — the manager owns restart; a runtime-level restart policy would revive the container under the same `container_id`/env → stale `PROCTOR_WORKER_ID` → registry rejection for ~`liveness_timeout`.
- **Fresh `worker_id` per container launch**: `{base}_{slot}_{suffix}`, `suffix = uuid4().hex[:12]` (lowercase hex — subject-safe under `^[a-z][a-z0-9_]*$`; ULID is NOT). `{base}_{slot}` is the stable observability key.
- **Restart trigger is container exit only** (poll `inspect`), never registry `worker.offline`.
- `worker_id` charset is `^[a-z][a-z0-9_]*$`; `base_worker_id` must satisfy it and be unique across fleets.
- Injected clock (`now_fn`) and injected jitter for all timing logic — no sleep-based timing assertions at unit level.
- No new Python dependency: talk to the runtime via `asyncio.create_subprocess_exec`, injectable for tests.
- Branch: `feat/docker-worker`. TDD per task; commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Config — DockerWorkerConfig + env-var overrides

**Files:**
- Modify: `src/proctor/core/config.py`
- Test: `tests/test_core/test_config.py` (append), `tests/test_core/test_env_overrides.py` (new)

**Interfaces:**
- Produces: `DockerWorkerConfig` (fields below); `ProctorConfig.docker_workers: list[DockerWorkerConfig]` with a `base_worker_id`-uniqueness validator; `load_config` applies `PROCTOR_NATS_SERVERS` / `PROCTOR_WORKER_ID` / `PROCTOR_WORKER_CAPABILITIES` env overrides after YAML load. Tasks 3–5 consume `DockerWorkerConfig`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_core/test_config.py`:

```python
class TestDockerWorkerConfig:
    def test_defaults(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        fleet = DockerWorkerConfig(
            image="proctor:latest", base_worker_id="docker_py"
        )
        assert fleet.replicas == 1
        assert fleet.runtime == "docker"
        assert fleet.capabilities == []
        assert fleet.max_restarts == 5
        assert fleet.stop_timeout == 30.0
        assert fleet.nats_servers == ["nats://host.docker.internal:4222"]

    def test_base_worker_id_charset(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        with pytest.raises(ValidationError):
            DockerWorkerConfig(
                image="i", base_worker_id="docker-py"  # hyphen illegal
            )

    def test_duplicate_base_worker_id_rejected(self) -> None:
        from proctor.core.config import DockerWorkerConfig

        with pytest.raises(ValidationError, match="base_worker_id"):
            ProctorConfig(
                docker_workers=[
                    DockerWorkerConfig(image="a", base_worker_id="dup"),
                    DockerWorkerConfig(image="b", base_worker_id="dup"),
                ]
            )

    def test_empty_docker_workers_default(self) -> None:
        assert ProctorConfig().docker_workers == []
```

New file `tests/test_core/test_env_overrides.py`:

```python
"""Env-var overrides applied by load_config (container injection)."""

from pathlib import Path

import pytest
import yaml

from proctor.core.config import load_config


@pytest.fixture
def _yaml(tmp_path: Path) -> Path:
    p = tmp_path / "c.yaml"
    p.write_text(
        yaml.dump(
            {
                "node_role": "worker",
                "transport": "nats",
                "worker": {"id": "from_yaml", "capabilities": ["yaml_cap"]},
                "nats": {"servers": ["nats://yaml:4222"]},
            }
        )
    )
    return p


def test_no_env_keeps_yaml(_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("PROCTOR_WORKER_ID", "PROCTOR_WORKER_CAPABILITIES",
              "PROCTOR_NATS_SERVERS"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config(_yaml)
    assert cfg.worker.id == "from_yaml"
    assert cfg.worker.capabilities == ["yaml_cap"]
    assert cfg.nats.servers == ["nats://yaml:4222"]


def test_worker_id_override(_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCTOR_WORKER_ID", "from_env")
    assert load_config(_yaml).worker.id == "from_env"


def test_nats_servers_override(
    _yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROCTOR_NATS_SERVERS", "nats://a:4222,nats://b:4222")
    assert load_config(_yaml).nats.servers == ["nats://a:4222", "nats://b:4222"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", []),
        ("python", ["python"]),
        ("shell,python", ["shell", "python"]),
        (" shell , python ", ["shell", "python"]),
    ],
)
def test_capabilities_csv(
    _yaml: Path, monkeypatch: pytest.MonkeyPatch,
    raw: str, expected: list[str],
) -> None:
    monkeypatch.setenv("PROCTOR_WORKER_CAPABILITIES", raw)
    assert load_config(_yaml).worker.capabilities == expected
```

- [ ] **Step 2: Run to verify failures**

`uv run pytest tests/test_core/test_config.py::TestDockerWorkerConfig tests/test_core/test_env_overrides.py -q` — ImportError / attribute errors expected. (Ensure `ValidationError` is imported in `test_config.py`; it already is from prior tasks.)

- [ ] **Step 3: Implement**

In `src/proctor/core/config.py`, add near `WorkerConfig`:

```python
class DockerWorkerConfig(BaseModel):
    """One declared fleet of container-based workers."""

    image: str
    base_worker_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capabilities: list[str] = Field(default_factory=list)
    replicas: int = Field(default=1, ge=1)
    runtime: Literal["docker", "podman"] = "docker"
    nats_servers: list[str] = Field(
        default_factory=lambda: ["nats://host.docker.internal:4222"]
    )
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: list[str] = Field(default_factory=list)
    network: str | None = None
    poll_interval: float = Field(default=2.0, gt=0.0)
    stop_timeout: float = Field(default=30.0, gt=0.0)
    base_backoff: float = Field(default=1.0, gt=0.0)
    max_backoff: float = Field(default=60.0, gt=0.0)
    max_restarts: int = Field(default=5, ge=1)
    stability_window: float = Field(default=60.0, gt=0.0)
    log_tail: int = Field(default=50, ge=1)
```

In `ProctorConfig`, add the field after `routes`:

```python
    docker_workers: list[DockerWorkerConfig] = Field(default_factory=list)
```

and a validator:

```python
    @model_validator(mode="after")
    def _unique_docker_base_ids(self) -> Self:
        seen: set[str] = set()
        for fleet in self.docker_workers:
            if fleet.base_worker_id in seen:
                raise ValueError(
                    f"duplicate docker_workers base_worker_id "
                    f"{fleet.base_worker_id!r}; each fleet needs a unique base"
                )
            seen.add(fleet.base_worker_id)
        return self
```

Add env overrides to `load_config` (replace the final `return`):

```python
    config = ProctorConfig.model_validate(data)
    return _apply_env_overrides(config)
```

and a helper (module level, above `load_config`), plus `import os` at the top if absent:

```python
def _apply_env_overrides(config: ProctorConfig) -> ProctorConfig:
    """Apply the container-injection env overrides after YAML load.

    Only the three fields a containerized worker needs; deliberately not
    a general env layer (see the design's Approach A rationale).
    """
    updates: dict[str, object] = {}
    worker_updates: dict[str, object] = {}
    if (servers := os.environ.get("PROCTOR_NATS_SERVERS")) is not None:
        nats = config.nats.model_copy(
            update={"servers": [s.strip() for s in servers.split(",") if s.strip()]}
        )
        updates["nats"] = nats
    if (wid := os.environ.get("PROCTOR_WORKER_ID")) is not None:
        worker_updates["id"] = wid
    if (caps := os.environ.get("PROCTOR_WORKER_CAPABILITIES")) is not None:
        worker_updates["capabilities"] = [
            c.strip() for c in caps.split(",") if c.strip()
        ]
    if worker_updates:
        updates["worker"] = config.worker.model_copy(update=worker_updates)
    if not updates:
        return config
    return config.model_copy(update=updates)
```

Note: `load_config(None)` returns `ProctorConfig()` without env overrides today; apply overrides there too so `PROCTOR_*` works with default config — change the `path is None` branch to `return _apply_env_overrides(ProctorConfig())`, and the missing-file / empty-data branches likewise return `_apply_env_overrides(ProctorConfig())`.

- [ ] **Step 4: Run tests + full suite; gates; commit**

```bash
uv run pytest tests/test_core/ -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(config): DockerWorkerConfig + container env overrides"
```

---

### Task 2: infra/docker.py — ContainerRuntime

**Files:**
- Create: `src/proctor/infra/__init__.py`, `src/proctor/infra/docker.py`
- Test: `tests/test_infra/__init__.py`, `tests/test_infra/test_docker.py`, fixtures `tests/test_infra/fixtures/inspect_docker.json`, `tests/test_infra/fixtures/inspect_podman.json`

**Interfaces:**
- Produces: `ContainerSpec` (image, name, env: dict, env_file: str | None, labels: dict, network: str | None, restart_policy: str = "no"); `ContainerStatus` (id, state, exit_code, started_at) with classmethod `parse(raw: dict) -> ContainerStatus`; `RunCmd = Callable[[list[str]], Awaitable[tuple[int, str, str]]]`; `ContainerRuntime(binary: str, run_cmd: RunCmd | None = None)` with `async run(spec) -> str`, `async inspect(id) -> ContainerStatus`, `async stop(id, timeout: float) -> None`, `async remove(id) -> None`, `async logs(id, tail: int) -> str`. Task 3 consumes `ContainerRuntime`.

- [ ] **Step 1: Add JSON fixtures**

`tests/test_infra/fixtures/inspect_docker.json` (trimmed real `docker inspect --format '{{json .}}'` output):

```json
{"Id":"abc123def456","Name":"/docker_py_0_aabbccddeeff","State":{"Status":"exited","Running":false,"ExitCode":1,"StartedAt":"2026-07-07T10:00:00.000000000Z","FinishedAt":"2026-07-07T10:00:05.000000000Z"}}
```

`tests/test_infra/fixtures/inspect_podman.json` (podman shape — same key paths, extra fields):

```json
{"Id":"podman789aa","Name":"docker_py_0_112233445566","State":{"Status":"running","Running":true,"ExitCode":0,"StartedAt":"2026-07-07T11:00:00.000000000Z","FinishedAt":"0001-01-01T00:00:00Z"},"Config":{"Image":"proctor:latest"}}
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_infra/__init__.py  (empty)
```

```python
# tests/test_infra/test_docker.py
"""ContainerRuntime: argv construction and inspect parsing, no daemon."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from proctor.infra.docker import ContainerRuntime, ContainerSpec, ContainerStatus

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


FIXTURES = Path(__file__).parent / "fixtures"


def _fake(rc: int = 0, out: str = "", err: str = "") -> tuple[
    Callable[[list[str]], Awaitable[tuple[int, str, str]]], list[list[str]]
]:
    calls: list[list[str]] = []

    async def run_cmd(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        return rc, out, err

    return run_cmd, calls


async def test_run_builds_argv() -> None:
    run_cmd, calls = _fake(out="cid123\n")
    rt = ContainerRuntime("podman", run_cmd=run_cmd)
    spec = ContainerSpec(
        image="proctor:latest",
        name="docker_py_0_deadbeef",
        env={"PROCTOR_WORKER_ID": "docker_py_0_deadbeef"},
        env_file="/tmp/fleet.env",
        labels={"proctor.fleet": "docker_py"},
        network="proctor_net",
    )
    cid = await rt.run(spec)
    assert cid == "cid123"
    argv = calls[0]
    assert argv[:2] == ["podman", "run"]
    assert "-d" in argv
    assert "--restart" in argv and argv[argv.index("--restart") + 1] == "no"
    assert "--name" in argv
    assert "-e" in argv
    assert "--env-file" in argv
    assert argv[argv.index("--env-file") + 1] == "/tmp/fleet.env"
    assert "--network" in argv
    assert argv[-1] == "proctor:latest"


async def test_run_raises_on_nonzero() -> None:
    run_cmd, _ = _fake(rc=125, err="no such image")
    rt = ContainerRuntime("docker", run_cmd=run_cmd)
    with pytest.raises(RuntimeError, match="no such image"):
        await rt.run(ContainerSpec(image="missing", name="x"))


async def test_inspect_parses_docker_fixture() -> None:
    raw = (FIXTURES / "inspect_docker.json").read_text()
    run_cmd, calls = _fake(out=raw)
    rt = ContainerRuntime("docker", run_cmd=run_cmd)
    st = await rt.inspect("abc123def456")
    assert st.id == "abc123def456"
    assert st.state == "exited"
    assert st.exit_code == 1
    assert "inspect" in calls[0]
    assert "{{json .}}" in calls[0]


async def test_inspect_parses_podman_fixture() -> None:
    raw = (FIXTURES / "inspect_podman.json").read_text()
    run_cmd, _ = _fake(out=raw)
    rt = ContainerRuntime("podman", run_cmd=run_cmd)
    st = await rt.inspect("podman789aa")
    assert st.id == "podman789aa"
    assert st.state == "running"
    assert st.exit_code == 0


def test_status_parse_normalizes_both() -> None:
    d = json.loads((FIXTURES / "inspect_docker.json").read_text())
    p = json.loads((FIXTURES / "inspect_podman.json").read_text())
    assert ContainerStatus.parse(d).state == "exited"
    assert ContainerStatus.parse(p).state == "running"


async def test_stop_remove_logs_argv() -> None:
    run_cmd, calls = _fake(out="line1\nline2\n")
    rt = ContainerRuntime("docker", run_cmd=run_cmd)
    await rt.stop("cid", timeout=12.0)
    assert calls[-1][:2] == ["docker", "stop"]
    assert "-t" in calls[-1] and calls[-1][calls[-1].index("-t") + 1] == "12"
    await rt.remove("cid")
    assert calls[-1][:3] == ["docker", "rm", "-f"]
    out = await rt.logs("cid", tail=50)
    assert out == "line1\nline2\n"
    assert calls[-1][:2] == ["docker", "logs"]
    assert "--tail" in calls[-1] and calls[-1][calls[-1].index("--tail") + 1] == "50"
```

- [ ] **Step 3: Run to verify failure**, then **Step 4: Implement**

```python
# src/proctor/infra/__init__.py
"""Thin async wrappers over container/host CLIs (M9)."""
```

```python
# src/proctor/infra/docker.py
"""Runtime-agnostic async wrapper over the container CLI (docker|podman).

All operations shell out via an injected exec function so tests need no
daemon. inspect() reads structured `--format '{{json .}}'` output only —
never scraped human text — and ContainerStatus.parse normalizes the
docker-vs-podman JSON shape into one model.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

RunCmd = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


async def _default_run_cmd(argv: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(), err.decode()


class ContainerSpec(BaseModel):
    """Declarative inputs for `run`."""

    image: str
    name: str
    env: dict[str, str] = Field(default_factory=dict)
    env_file: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    network: str | None = None
    restart_policy: str = "no"


class ContainerStatus(BaseModel):
    """Normalized subset of `inspect` across docker and podman."""

    id: str
    state: str
    exit_code: int
    started_at: str

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "ContainerStatus":
        state = raw.get("State") or {}
        return cls(
            id=str(raw.get("Id", "")),
            state=str(state.get("Status", "unknown")),
            exit_code=int(state.get("ExitCode", 0) or 0),
            started_at=str(state.get("StartedAt", "")),
        )


class ContainerRuntime:
    """Async CLI wrapper; `binary` is `docker` or `podman`."""

    def __init__(self, binary: str, run_cmd: RunCmd | None = None) -> None:
        self._binary = binary
        self._run = run_cmd or _default_run_cmd

    async def _exec(self, args: list[str]) -> str:
        argv = [self._binary, *args]
        rc, out, err = await self._run(argv)
        if rc != 0:
            raise RuntimeError(
                f"{' '.join(argv)} exited {rc}: {err.strip() or out.strip()}"
            )
        return out

    async def run(self, spec: ContainerSpec) -> str:
        args = ["run", "-d", "--name", spec.name, "--restart", spec.restart_policy]
        for key, value in spec.env.items():
            args += ["-e", f"{key}={value}"]
        if spec.env_file is not None:
            args += ["--env-file", spec.env_file]
        for key, value in spec.labels.items():
            args += ["--label", f"{key}={value}"]
        if spec.network is not None:
            args += ["--network", spec.network]
        args.append(spec.image)
        return (await self._exec(args)).strip()

    async def inspect(self, container_id: str) -> ContainerStatus:
        out = await self._exec(
            ["inspect", "--format", "{{json .}}", container_id]
        )
        return ContainerStatus.parse(json.loads(out))

    async def stop(self, container_id: str, timeout: float) -> None:
        await self._exec(["stop", "-t", str(int(timeout)), container_id])

    async def remove(self, container_id: str) -> None:
        await self._exec(["rm", "-f", container_id])

    async def logs(self, container_id: str, tail: int) -> str:
        return await self._exec(["logs", "--tail", str(tail), container_id])
```

- [ ] **Step 5: Run tests; gates; commit**

```bash
uv run pytest tests/test_infra/ -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(infra): ContainerRuntime — async CLI wrapper, docker|podman"
```

---

### Task 3: DockerWorkerManager — start / stop / fresh-id / env-file (no restart yet)

**Files:**
- Create: `src/proctor/workers/docker.py`
- Test: `tests/test_workers/test_docker_manager.py`

**Interfaces:**
- Consumes: `ContainerRuntime`, `ContainerSpec` (Task 2); `DockerWorkerConfig` (Task 1); `EventBus`.
- Produces: `SlotState` (slot, worker_id, container_id, restarts, started_at, restart_at, state); `DockerWorkerManager(runtime, fleet: DockerWorkerConfig, bus, *, environ=None, tmp_dir=None, now_fn=None, jitter_fn=None)` with `async start()`, `async stop()`, `slots: dict[int, SlotState]`, and `_launch(slot) -> None`. Task 4 adds the poll loop onto this.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_workers/test_docker_manager.py
"""DockerWorkerManager lifecycle with a fake ContainerRuntime."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from proctor.core.bus import EventBus
from proctor.core.config import DockerWorkerConfig
from proctor.core.transport import LocalEventTransport
from proctor.infra.docker import ContainerSpec, ContainerStatus
from proctor.workers.docker import DockerWorkerManager

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def bus() -> AsyncGenerator[EventBus, None]:
    b = EventBus(LocalEventTransport())
    await b.start()
    yield b
    await b.stop()


class FakeRuntime:
    """Records ops; hands out sequential container ids; inspect scripted."""

    def __init__(self) -> None:
        self.runs: list[ContainerSpec] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.logged: list[str] = []
        self._n = 0
        self.status: dict[str, str] = {}  # container_id -> state

    async def run(self, spec: ContainerSpec) -> str:
        self.runs.append(spec)
        self._n += 1
        cid = f"cid{self._n}"
        self.status[cid] = "running"
        return cid

    async def inspect(self, container_id: str) -> ContainerStatus:
        return ContainerStatus(
            id=container_id,
            state=self.status.get(container_id, "running"),
            exit_code=0,
            started_at="2026-07-07T12:00:00Z",
        )

    async def stop(self, container_id: str, timeout: float) -> None:
        self.stopped.append(container_id)

    async def remove(self, container_id: str) -> None:
        self.removed.append(container_id)

    async def logs(self, container_id: str, tail: int) -> str:
        self.logged.append(container_id)
        return "crash tail\n"


def _fleet(**kw: object) -> DockerWorkerConfig:
    base = dict(image="proctor:latest", base_worker_id="docker_py",
                capabilities=["python"], replicas=2, runtime="podman",
                secret_env=["FAKE_KEY"])
    base.update(kw)
    return DockerWorkerConfig(**base)  # type: ignore[arg-type]


def _mgr(rt: FakeRuntime, bus: EventBus, tmp_path: Path,
         **kw: object) -> DockerWorkerManager:
    return DockerWorkerManager(
        rt, _fleet(**kw), bus,
        environ={"FAKE_KEY": "s3cr3t"},
        tmp_dir=tmp_path,
        now_fn=lambda: T0,
        jitter_fn=lambda d: d,  # deterministic: no jitter
    )


async def test_start_launches_replicas_distinct_ids(
    bus: EventBus, tmp_path: Path
) -> None:
    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path)
    await mgr.start()
    try:
        assert len(rt.runs) == 2
        ids = [s.worker_id for s in mgr.slots.values()]
        assert len(set(ids)) == 2
        for wid in ids:
            assert wid.startswith("docker_py_")  # {base}_{slot}_{suffix}
        # each container --name equals its worker_id, --restart=no
        for spec in rt.runs:
            assert spec.name in ids
            assert spec.restart_policy == "no"
            assert spec.env["PROCTOR_WORKER_ID"] == spec.name
            assert spec.env["PROCTOR_WORKER_CAPABILITIES"] == "python"
            assert spec.env["PROCTOR_NATS_SERVERS"]
    finally:
        await mgr.stop()


async def test_secret_env_file_written_once_and_removed(
    bus: EventBus, tmp_path: Path
) -> None:
    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path)
    await mgr.start()
    env_file = mgr.env_file_path
    assert env_file is not None and env_file.exists()
    assert "FAKE_KEY=s3cr3t" in env_file.read_text()
    # one fleet-level file reused by every replica
    assert all(s.env_file == str(env_file) for s in rt.runs)
    await mgr.stop()
    assert not env_file.exists()


async def test_stop_stops_and_removes_all(
    bus: EventBus, tmp_path: Path
) -> None:
    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path)
    await mgr.start()
    cids = [s.container_id for s in mgr.slots.values()]
    await mgr.stop()
    assert sorted(rt.stopped) == sorted(cids)
    assert sorted(rt.removed) == sorted(cids)
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: Implement** (no poll loop yet)

```python
# src/proctor/workers/docker.py
"""DockerWorkerManager — core-side lifecycle for a container worker fleet.

Owns one fleet (one DockerWorkerConfig): launches replicas as slots,
restarts exited containers (Task 4 poll loop), and stops+removes on
shutdown. Every launch gets a fresh worker_id so container restart never
collides with the registry's first-alive-owns fencing. The runtime and
clock are injected for tests.
"""

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from proctor.core.bus import EventBus
from proctor.core.config import DockerWorkerConfig
from proctor.infra.docker import ContainerRuntime, ContainerSpec

logger = logging.getLogger(__name__)

_SOURCE = "docker_worker_manager"


class SlotState(BaseModel):
    """One replica slot's current incarnation."""

    slot: int
    worker_id: str
    container_id: str
    restarts: int = 0
    started_at: datetime
    restart_at: datetime | None = None
    state: str = "running"  # running | backoff | failed


class DockerWorkerManager:
    """Lifecycle manager for one declared container-worker fleet."""

    def __init__(
        self,
        runtime: ContainerRuntime,
        fleet: DockerWorkerConfig,
        bus: EventBus,
        *,
        environ: dict[str, str] | None = None,
        tmp_dir: Path | None = None,
        now_fn: Callable[[], datetime] | None = None,
        jitter_fn: Callable[[float], float] | None = None,
    ) -> None:
        self._rt = runtime
        self._fleet = fleet
        self._bus = bus
        self._environ = environ if environ is not None else dict(os.environ)
        self._tmp_dir = tmp_dir or Path("/tmp")
        self._now = now_fn or (lambda: datetime.now(UTC))
        # full jitter by default; injected deterministic in tests
        self._jitter = jitter_fn or _full_jitter
        self.slots: dict[int, SlotState] = {}
        self.env_file_path: Path | None = None
        self._poll_task: asyncio.Task[None] | None = None
        # crash tail captured at exit, carried to the restart event payload
        self._pending_tail: dict[int, str] = {}

    def _new_worker_id(self, slot: int) -> str:
        return f"{self._fleet.base_worker_id}_{slot}_{uuid4().hex[:12]}"

    def _write_env_file(self) -> None:
        if not self._fleet.secret_env:
            return
        path = self._tmp_dir / f"proctor_{self._fleet.base_worker_id}.env"
        lines = [
            f"{name}={self._environ[name]}"
            for name in self._fleet.secret_env
            if name in self._environ
        ]
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)
        self.env_file_path = path

    async def _launch(self, slot: int, at: datetime | None = None) -> None:
        worker_id = self._new_worker_id(slot)
        env = {
            "PROCTOR_WORKER_ID": worker_id,
            "PROCTOR_WORKER_CAPABILITIES": ",".join(self._fleet.capabilities),
            "PROCTOR_NATS_SERVERS": ",".join(self._fleet.nats_servers),
            **self._fleet.env,
        }
        spec = ContainerSpec(
            image=self._fleet.image,
            name=worker_id,
            env=env,
            env_file=str(self.env_file_path) if self.env_file_path else None,
            labels={"proctor.fleet": self._fleet.base_worker_id},
            network=self._fleet.network,
            restart_policy="no",
        )
        container_id = await self._rt.run(spec)
        self.slots[slot] = SlotState(
            slot=slot,
            worker_id=worker_id,
            container_id=container_id,
            restarts=self.slots[slot].restarts if slot in self.slots else 0,
            started_at=at or self._now(),
            state="running",
        )
        logger.info(
            "Launched docker worker %s (slot %d, container %s)",
            worker_id, slot, container_id,
        )

    async def start(self) -> None:
        """Write the fleet env-file and launch all replicas."""
        self._write_env_file()
        for slot in range(self._fleet.replicas):
            await self._launch(slot)

    async def stop(self) -> None:
        """Stop+remove every container and delete the env-file."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            import contextlib

            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await self._poll_task
                except Exception:
                    logger.exception("Docker poll loop exited with error")
            self._poll_task = None
        for state in self.slots.values():
            try:
                await self._rt.stop(
                    state.container_id, timeout=self._fleet.stop_timeout
                )
                await self._rt.remove(state.container_id)
            except Exception:
                logger.exception(
                    "Error stopping docker worker %s", state.worker_id
                )
        self.slots.clear()
        if self.env_file_path is not None and self.env_file_path.exists():
            self.env_file_path.unlink()
            self.env_file_path = None


def _full_jitter(delay: float) -> float:
    import random

    return random.uniform(0, delay)
```

- [ ] **Step 4: Run tests; gates; commit**

```bash
uv run pytest tests/test_workers/test_docker_manager.py -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(workers): DockerWorkerManager start/stop, fresh id, fleet env-file"
```

---

### Task 4: DockerWorkerManager — poll loop, restart, backoff, ceiling

**Files:**
- Modify: `src/proctor/workers/docker.py`
- Test: `tests/test_workers/test_docker_manager.py` (append)

**Interfaces:**
- Consumes: everything from Task 3.
- Produces: `DockerWorkerManager.start()` now also launches the poll loop; `async _poll_once(now)` (the testable tick body); `docker_worker.restarted` / `docker_worker.failed` events on the bus.

- [ ] **Step 1: Write failing tests** (append)

```python
async def test_exit_captures_logs_then_removes_then_relaunches(
    bus: EventBus, tmp_path: Path
) -> None:
    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path, replicas=1)
    await mgr.start()
    try:
        old = mgr.slots[0]
        rt.status[old.container_id] = "exited"
        # backoff = base_backoff (1.0) with no jitter → restart_at = T0+1s
        await mgr._poll_once(T0)
        assert mgr.slots[0].state == "backoff"
        # order: logs captured, old removed, no relaunch yet
        assert rt.logged == [old.container_id]
        assert rt.removed == [old.container_id]
        assert len(rt.runs) == 1
        # before restart_at: still waiting
        from datetime import timedelta
        await mgr._poll_once(T0 + timedelta(seconds=0.5))
        assert len(rt.runs) == 1
        # at restart_at: fresh container, NEW worker_id
        await mgr._poll_once(T0 + timedelta(seconds=1.0))
        assert len(rt.runs) == 2
        assert mgr.slots[0].worker_id != old.worker_id
        assert mgr.slots[0].state == "running"
        assert mgr.slots[0].restarts == 1
    finally:
        await mgr.stop()


async def test_backoff_grows_and_uses_jitter(
    bus: EventBus, tmp_path: Path
) -> None:
    from datetime import timedelta

    seen: list[float] = []
    rt = FakeRuntime()
    mgr = DockerWorkerManager(
        rt, _fleet(replicas=1, base_backoff=1.0, max_backoff=100.0),
        bus, environ={"FAKE_KEY": "x"}, tmp_dir=tmp_path,
        now_fn=lambda: T0, jitter_fn=lambda d: (seen.append(d), d)[1],
    )
    await mgr.start()
    try:
        now = T0
        for expected in (1.0, 2.0, 4.0):
            rt.status[mgr.slots[0].container_id] = "exited"
            await mgr._poll_once(now)
            now = mgr.slots[0].restart_at or now
            await mgr._poll_once(now)  # relaunch
        assert seen == [1.0, 2.0, 4.0]  # exponential, jitter_fn saw each
    finally:
        await mgr.stop()


async def test_stability_window_resets_restart_count(
    bus: EventBus, tmp_path: Path
) -> None:
    from datetime import timedelta

    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path, replicas=1, stability_window=10.0)
    await mgr.start()
    try:
        rt.status[mgr.slots[0].container_id] = "exited"
        await mgr._poll_once(T0)
        await mgr._poll_once(mgr.slots[0].restart_at or T0)  # relaunch, restarts=1
        assert mgr.slots[0].restarts == 1
        # container stays up beyond stability_window → counter resets
        up = mgr.slots[0].started_at + timedelta(seconds=11)
        await mgr._poll_once(up)
        assert mgr.slots[0].restarts == 0
    finally:
        await mgr.stop()


async def test_ceiling_trips_failed_with_log_tail(
    bus: EventBus, tmp_path: Path
) -> None:
    from datetime import timedelta

    events: list[object] = []

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe("docker_worker.>", collect)
    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path, replicas=1, max_restarts=2)
    await mgr.start()
    try:
        now = T0
        for _ in range(3):  # exceed max_restarts=2
            rt.status[mgr.slots[0].container_id] = "exited"
            await mgr._poll_once(now)
            if mgr.slots[0].state == "failed":
                break
            now = mgr.slots[0].restart_at or now
            await mgr._poll_once(now)
        assert mgr.slots[0].state == "failed"
        await bus.flush()
        failed = [
            e for e in events
            if getattr(e, "type", "") == "docker_worker.failed"
        ]
        assert failed
        assert "crash tail" in str(getattr(failed[0], "payload", {}))
    finally:
        await mgr.stop()
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: Implement**

Add `Event` import at the top of `workers/docker.py`:

```python
from proctor.core.models import Event
```

Add the poll loop to `start()` — after the replica launch loop:

```python
        self._poll_task = asyncio.create_task(self._poll_loop())
```

Add these methods to `DockerWorkerManager`:

```python
    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._fleet.poll_interval)
            try:
                await self._poll_once(self._now())
            except Exception:
                logger.exception("Docker poll loop iteration failed")

    async def _poll_once(self, now: datetime) -> None:
        """One reconciliation pass over all slots (testable tick body)."""
        for slot, state in list(self.slots.items()):
            if state.state == "failed":
                continue
            if state.state == "backoff":
                if state.restart_at is not None and now >= state.restart_at:
                    await self._relaunch(slot, now)
                continue
            status = await self._rt.inspect(state.container_id)
            if status.state == "exited":
                await self._handle_exit(slot, now)
            elif (now - state.started_at).total_seconds() >= (
                self._fleet.stability_window
            ):
                state.restarts = 0

    async def _handle_exit(self, slot: int, now: datetime) -> None:
        """Capture logs, remove the container, schedule a backoff restart."""
        state = self.slots[slot]
        tail = ""
        try:
            tail = await self._rt.logs(
                state.container_id, tail=self._fleet.log_tail
            )
        except Exception:
            logger.exception("Failed to capture logs for %s", state.worker_id)
        try:
            await self._rt.remove(state.container_id)
        except Exception:
            logger.exception("Failed to remove %s", state.container_id)

        next_restarts = state.restarts + 1
        if next_restarts > self._fleet.max_restarts:
            state.state = "failed"
            state.restarts = next_restarts
            logger.error(
                "Docker worker slot %d exceeded max_restarts; last logs:\n%s",
                slot, tail,
            )
            await self._bus.publish(
                Event(
                    type="docker_worker.failed",
                    source=_SOURCE,
                    payload={
                        "base_worker_id": self._fleet.base_worker_id,
                        "slot": slot,
                        "restarts": next_restarts,
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

    async def _relaunch(self, slot: int, now: datetime) -> None:
        tail = self._pending_tail.pop(slot, "")
        await self._launch(slot, at=now)  # fresh worker_id; preserves restarts
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

Add `timedelta` to the datetime import: `from datetime import UTC, datetime, timedelta`.

`_launch` already takes `at: datetime | None` (Task 3) and stamps
`started_at = at or self._now()`; `_relaunch` passes `at=now` so the
restarted container's `started_at` is the injected clock's `now`, not the
constant `T0` — this is what makes the stability-window test observable.
The initial `start()` launch calls `_launch(slot)` (→ `self._now()`).

- [ ] **Step 4: Run tests; gates; commit**

```bash
uv run pytest tests/test_workers/test_docker_manager.py -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(workers): docker poll loop — restart, backoff+jitter, ceiling"
```

---

### Task 5: Bootstrap wiring + log sink + Dockerfile + config example

**Files:**
- Modify: `src/proctor/core/bootstrap.py`
- Create: `Dockerfile`, `docker/worker.yaml`
- Modify: `config/proctor.yaml`
- Test: `tests/test_workers/test_docker_bootstrap.py` (new)

**Interfaces:**
- Consumes: `DockerWorkerManager` (Tasks 3–4); `ContainerRuntime` (Task 2); `ProctorConfig.docker_workers` (Task 1).
- Produces: `Application` starts one manager per fleet in the core/standalone branch, subscribes a `docker_worker.>` log sink, and stops the managers before bus drain.

- [ ] **Step 1: Write failing test**

```python
# tests/test_workers/test_docker_bootstrap.py
"""node_role core/standalone wires a DockerWorkerManager per fleet."""

from pathlib import Path

import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import DockerWorkerConfig, ProctorConfig
from proctor.core.transport import LocalEventTransport

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_managers_started_and_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    stopped: list[str] = []

    class FakeManager:
        def __init__(self, runtime, fleet, bus, **kw):  # type: ignore[no-untyped-def]
            self.fleet = fleet

        async def start(self) -> None:
            started.append(self.fleet.base_worker_id)

        async def stop(self) -> None:
            stopped.append(self.fleet.base_worker_id)

    monkeypatch.setattr(
        "proctor.core.bootstrap.DockerWorkerManager", FakeManager
    )
    config = ProctorConfig(
        data_dir=tmp_path / "data",
        docker_workers=[
            DockerWorkerConfig(image="i", base_worker_id="fleet_a"),
            DockerWorkerConfig(image="i", base_worker_id="fleet_b"),
        ],
    )
    app = Application(config, event_transport=LocalEventTransport())

    async def llm(prompt: str) -> str:
        return "ok"

    app.set_llm_call(llm)
    await app.start()
    try:
        assert sorted(started) == ["fleet_a", "fleet_b"]
    finally:
        await app.stop()
    assert sorted(stopped) == ["fleet_a", "fleet_b"]


async def test_no_docker_workers_no_managers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[object] = []
    monkeypatch.setattr(
        "proctor.core.bootstrap.DockerWorkerManager",
        lambda *a, **k: made.append(1),
    )
    app = Application(
        ProctorConfig(data_dir=tmp_path / "d"),
        event_transport=LocalEventTransport(),
    )

    async def llm(prompt: str) -> str:
        return "ok"

    app.set_llm_call(llm)
    await app.start()
    await app.stop()
    assert made == []
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: Implement bootstrap wiring**

In `src/proctor/core/bootstrap.py` add imports:

```python
from proctor.infra.docker import ContainerRuntime
from proctor.workers.docker import DockerWorkerManager
```

In `Application.__init__`, add:

```python
        self._docker_managers: list[DockerWorkerManager] = []
```

and (next to other core-side subscriptions, gated on non-worker role — reuse the existing `if config.node_role != "worker":` block):

```python
            self.bus.subscribe("docker_worker.>", self._log_docker_event)
```

In `start()` (core/standalone branch), after the tick loop is created:

```python
        for fleet in self.config.docker_workers:
            manager = DockerWorkerManager(
                ContainerRuntime(fleet.runtime),
                fleet,
                self.bus,
            )
            await manager.start()
            self._docker_managers.append(manager)
```

In `stop()` (core branch), right after the tick-task teardown and before the registry stop (managers should stop before the registry so their `worker.offline(shutdown)` is still processed):

```python
        for manager in self._docker_managers:
            await manager.stop()
        self._docker_managers = []
```

Add the log-sink handler method:

```python
    async def _log_docker_event(self, event: Event) -> None:
        """Floor consumer for docker_worker.* so failures aren't silent."""
        if event.type == "docker_worker.failed":
            logger.error("Docker worker fleet failure: %s", event.payload)
        else:
            logger.info("Docker worker event %s: %s", event.type, event.payload)
```

- [ ] **Step 4: Dockerfile + base worker.yaml + config example**

`Dockerfile`:

```dockerfile
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 proctor
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[nats]"

COPY docker/worker.yaml /etc/proctor/worker.yaml

USER proctor
ENTRYPOINT ["python", "-m", "proctor", "--config", "/etc/proctor/worker.yaml"]
```

`docker/worker.yaml` (base image config; per-container fields arrive via `PROCTOR_*` env):

```yaml
# Base worker config baked into the image. worker.id, worker.capabilities,
# and nats.servers are overridden per container by PROCTOR_* env vars
# injected by the DockerWorkerManager. drain_timeout is deliberately
# below the manager's default stop_timeout (30s) so graceful SIGTERM
# drains fully before docker's SIGKILL.
node_role: worker
transport: nats
worker:
  id: placeholder_overridden_by_env
events:
  drain_timeout: 20.0
```

Append to `config/proctor.yaml`:

```yaml
# Container worker fleets managed by the core (Phase 3). Each fleet is a
# declared set of replicas; the core starts them, restarts on container
# exit, and stops them on shutdown. worker_id churns per launch.
# docker_workers:
#   - image: proctor:latest
#     base_worker_id: docker_py
#     capabilities: [python, shell]
#     replicas: 2
#     runtime: podman              # or docker
#     nats_servers: ["nats://host.docker.internal:4222"]  # container-facing
#     secret_env: [ANTHROPIC_API_KEY]   # forwarded via one fleet env-file
#     max_restarts: 5
#     stop_timeout: 30.0           # must be >= container's drain_timeout
```

- [ ] **Step 5: Run; gates; commit**

```bash
uv run pytest tests/test_workers/test_docker_bootstrap.py -q && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(core): wire docker managers + log sink; Dockerfile + base config"
```

---

### Task 6: Integration test (docker marker) + docs

**Files:**
- Create: `tests/integration/test_docker_worker.py` (marker `docker`)
- Modify: `pyproject.toml` (register `docker` marker + deselect), `CLAUDE.md`, `TODO.md`

**Interfaces:** consumes everything; no new production code (fix in the owning module if a test exposes a bug).

- [ ] **Step 1: Register the marker**

In `pyproject.toml`, in `[tool.pytest.ini_options].markers` add:

```toml
    "docker: requires a container runtime (docker/podman) present",
```

and extend the default deselect in `addopts` to also exclude `docker` (mirror how `nats` is excluded — read the existing `addopts` line and add `and not docker`).

- [ ] **Step 2: Write the integration test**

Read `tests/integration/test_distribution_nats.py` first and mirror its NATS/skip conventions. Then:

```python
# tests/integration/test_docker_worker.py
"""Real container worker managed by the core (podman/docker + NATS)."""

import os
import shutil

import anyio
import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.docker]

# ... (skip if no NATS_URL and no runtime binary; build/available image
# assumed present as `proctor:latest`). Shape:
#   - core Application on a NATSEventTransport (transport=nats)
#   - config.docker_workers = [one fleet, replicas=1, image proctor:latest,
#     nats_servers pointing at the test NATS reachable from the container]
#   - start core → manager launches the container → the container's worker
#     self-registers → wait for worker.registered in the core registry
#   - dispatch a requires-gated task via trigger.terminal → assert
#     task.completed with the container worker's output
#   - `podman kill` the container → assert the manager relaunches it under
#     a new worker_id and the fleet returns to one live worker
#   - stop core → assert container stopped+removed
```

Implement the full test following the sketch, choosing the runtime binary via `shutil.which("podman") or shutil.which("docker")`, skipping cleanly when neither the binary nor `NATS_URL`/testcontainers is available (mirror the NATS test's skip). Report in the task whether it ran live or skip-only.

- [ ] **Step 3: Docs**

- `CLAUDE.md`: module table `infra/` row → "Thin async CLI wrappers — `docker.py` (ContainerRuntime). Future: ssh.py, vagrant.py"; `workers/` row add "DockerWorkerManager (container fleet lifecycle)"; Implementation Status → add docker worker to Completed; **Next:** "Phase 3 continues — workers/remote.py (SSH), mcp/".
- `TODO.md`: current-state line mentions container workers.

- [ ] **Step 4: Final gates, push, PR**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git push -u origin feat/docker-worker
gh pr create --base master --title "feat(workers): docker worker — core-managed container fleet (Phase 3, part 3)" --body "..."
```

PR body: reference the spec, the fresh-id/`--restart=no` fencing rationale, restart trigger (container-exit poll), backoff+jitter+stability-reset+ceiling, log sink, and test evidence (unit + docker-marker integration).

---

## Self-Review Notes

- Spec coverage: config + env overrides (T1); infra/docker.py incl. `--restart=no`, `--format '{{json .}}'`, both-runtime fixtures (T2); manager start/stop + fresh id + fleet env-file (T3); poll loop + exit-order (logs→remove→relaunch) + backoff/jitter + stability reset + ceiling + docker_worker.* (T4); bootstrap wiring + log sink + Dockerfile + base config + config example (T5); docker-marker integration + docs (T6). Fencing/`--restart=no` invariant lives in T2 (spec) + T3/T4 (enforced); graceful-shutdown drain (stop_timeout ≥ drain_timeout) realized via T5's base worker.yaml drain_timeout=20 < stop_timeout=30.
- Type consistency: `ContainerSpec`/`ContainerStatus` (T2) consumed by the manager (T3/T4); `SlotState` fields written in T3 and mutated in T4; `_launch(slot, at=None)` threading of `now` into `started_at` is called out explicitly in T4 Step 3.
- Known judgment calls: the crash tail is carried between exit and restart in a manager-level side dict `self._pending_tail: dict[int, str]` (not on the pydantic `SlotState`), avoiding any extra-attribute typing issue. `stop()` imports `contextlib` locally inside the method (mirrors the pattern; move it to a module import if the implementer prefers — behavior identical).
