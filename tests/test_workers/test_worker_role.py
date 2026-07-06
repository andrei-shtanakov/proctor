"""node_role: worker wires a WorkerNode and nothing core-side."""

from pathlib import Path

import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import ProctorConfig, WorkerConfig
from proctor.core.transport import LocalEventTransport

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _worker_config(tmp_path: Path) -> ProctorConfig:
    return ProctorConfig(
        node_role="worker",
        transport="local",  # keep the unit test off real NATS
        data_dir=tmp_path / "data",
        worker=WorkerConfig(id="worker_a", capabilities=["python"]),
    )


async def test_worker_role_starts_node_not_core(tmp_path: Path) -> None:
    app = Application(_worker_config(tmp_path), event_transport=LocalEventTransport())

    async def llm(prompt: str) -> str:
        return "ok"

    app.set_llm_call(llm)
    await app.start()
    try:
        assert app._worker_node is not None
        assert app._router is None
        assert app._task_router is None
        assert app._registry is None
        assert app._tick_task is None
        # worker must not listen to core-side subjects
        subjects = {
            sub.subject
            for sub in app.bus._transport._subscriptions  # type: ignore[attr-defined]
        }
        assert "trigger.>" not in subjects
        assert "task.result" not in subjects
        assert any(s.startswith("task.assign.worker_a") for s in subjects)
    finally:
        await app.stop()
