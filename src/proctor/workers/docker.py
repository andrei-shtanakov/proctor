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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from proctor.core.bus import EventBus
from proctor.core.config import DockerWorkerConfig
from proctor.core.models import Event
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
    unreachable_since: datetime | None = None


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
            await self._launch_slot(slot, self._now())
        self._poll_task = asyncio.create_task(self._poll_loop())

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
                    slot=slot,
                    worker_id="",
                    container_id="",
                    restarts=0,
                    started_at=now,
                    state="backoff",
                )
            await self._backoff_or_fail(slot, now, tail="", reason="launch_failed")
            return False

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
                slot,
                reason,
                tail,
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
            logger.error("Docker worker slot %d unreachable past ceiling", slot)
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

    async def _relaunch(self, slot: int, now: datetime) -> None:
        tail = self._pending_tail.pop(slot, "")
        if not await self._launch_slot(slot, now):
            return  # launch failed; backoff already rescheduled
        logger.info(
            "Restarted docker worker slot %d (restart #%d)",
            slot,
            self.slots[slot].restarts,
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
            if not state.container_id:
                continue  # launch-failed slot never created a container
            # stop and remove independently: if the container is already
            # gone (e.g. it exited between poll and teardown), stop() may
            # raise, but remove() must still run so nothing is leaked.
            try:
                await self._rt.stop(
                    state.container_id, timeout=self._fleet.stop_timeout
                )
            except Exception:
                logger.exception("Error stopping docker worker %s", state.worker_id)
            try:
                await self._rt.remove(state.container_id)
            except Exception:
                logger.exception("Error removing docker worker %s", state.worker_id)
        self.slots.clear()
        if self._env_dir is not None:
            shutil.rmtree(self._env_dir, ignore_errors=True)
            self._env_dir = None
        self.env_file_path = None


def _full_jitter(delay: float) -> float:
    import random

    return random.uniform(0, delay)
