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
import shutil
import tempfile
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
        self._env_dir: Path | None = None
        self._poll_task: asyncio.Task[None] | None = None
        # crash tail captured at exit, carried to the restart event payload
        self._pending_tail: dict[int, str] = {}

    def _new_worker_id(self, slot: int) -> str:
        return f"{self._fleet.base_worker_id}_{slot}_{uuid4().hex[:12]}"

    def _write_env_file(self) -> None:
        if not self._fleet.secret_env:
            return
        env_dir = tempfile.mkdtemp(
            prefix=f"proctor_{self._fleet.base_worker_id}_",
            dir=str(self._tmp_dir),
        )
        os.chmod(env_dir, 0o700)
        self._env_dir = Path(env_dir)
        path = self._env_dir / "fleet.env"
        lines = [
            f"{name}={self._environ[name]}"
            for name in self._fleet.secret_env
            if name in self._environ
        ]
        fd = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
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
            worker_id,
            slot,
            container_id,
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
                logger.exception("Error stopping docker worker %s", state.worker_id)
        self.slots.clear()
        if self._env_dir is not None:
            shutil.rmtree(self._env_dir, ignore_errors=True)
            self._env_dir = None
        self.env_file_path = None


def _full_jitter(delay: float) -> float:
    import random

    return random.uniform(0, delay)
