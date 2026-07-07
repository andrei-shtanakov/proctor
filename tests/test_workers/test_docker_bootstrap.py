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

    monkeypatch.setattr("proctor.core.bootstrap.DockerWorkerManager", FakeManager)
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
