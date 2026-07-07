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
    await mgr.stop()
    assert not env_file.exists()


async def test_stop_stops_and_removes_all(bus: EventBus, tmp_path: Path) -> None:
    rt = FakeRuntime()
    mgr = _mgr(rt, bus, tmp_path)
    await mgr.start()
    cids = [s.container_id for s in mgr.slots.values()]
    await mgr.stop()
    assert sorted(rt.stopped) == sorted(cids)
    assert sorted(rt.removed) == sorted(cids)
