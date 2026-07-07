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
        self.stop_timeouts: list[float] = []
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
        self.stop_timeouts.append(timeout)

    async def remove(self, container_id: str) -> None:
        self.removed.append(container_id)

    async def logs(self, container_id: str, tail: int) -> str:
        self.logged.append(container_id)
        return "crash tail\n"


def _fleet(**kw: object) -> DockerWorkerConfig:
    base: dict[str, object] = dict(
        image="proctor:latest",
        base_worker_id="docker_py",
        capabilities=["python"],
        replicas=2,
        runtime="podman",
        secret_env=["FAKE_KEY"],
    )
    base.update(kw)
    return DockerWorkerConfig(**base)  # type: ignore[arg-type]


def _mgr(
    rt: FakeRuntime, bus: EventBus, tmp_path: Path, **kw: object
) -> DockerWorkerManager:
    return DockerWorkerManager(
        rt,  # type: ignore[arg-type]
        _fleet(**kw),
        bus,
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
    # owner-only file inside an owner-only private directory
    assert oct(env_file.stat().st_mode)[-3:] == "600"
    env_dir = env_file.parent
    assert oct(env_dir.stat().st_mode)[-3:] == "700"
    await mgr.stop()
    assert not env_file.exists()
    assert not env_dir.exists()


async def test_stop_stops_and_removes_all(bus: EventBus, tmp_path: Path) -> None:
    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path)
    await mgr.start()
    cids = [s.container_id for s in mgr.slots.values()]
    await mgr.stop()
    assert sorted(rt.stopped) == sorted(cids)
    assert sorted(rt.removed) == sorted(cids)
    # default fleet.stop_timeout (30.0) is passed through to runtime.stop
    assert rt.stop_timeouts == [30.0] * len(cids)


async def test_stop_passes_fleet_stop_timeout(bus: EventBus, tmp_path: Path) -> None:
    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path, stop_timeout=5.0)
    await mgr.start()
    cids = [s.container_id for s in mgr.slots.values()]
    await mgr.stop()
    assert rt.stop_timeouts == [5.0] * len(cids)


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


async def test_backoff_grows_and_uses_jitter(bus: EventBus, tmp_path: Path) -> None:
    seen: list[float] = []
    rt = FakeRuntime()
    mgr = DockerWorkerManager(
        rt,  # type: ignore[arg-type]
        _fleet(replicas=1, base_backoff=1.0, max_backoff=100.0),
        bus,
        environ={"FAKE_KEY": "x"},
        tmp_dir=tmp_path,
        now_fn=lambda: T0,
        jitter_fn=lambda d: (seen.append(d), d)[1],
    )
    await mgr.start()
    try:
        now = T0
        for _expected in (1.0, 2.0, 4.0):
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
        failed = [e for e in events if getattr(e, "type", "") == "docker_worker.failed"]
        assert failed
        assert "crash tail" in str(getattr(failed[0], "payload", {}))
    finally:
        await mgr.stop()
