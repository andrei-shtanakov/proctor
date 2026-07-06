"""WorkerRegistry — live worker catalog with heartbeat liveness.

Fencing policy is first-alive-owns: a worker_id is bound to the first
instance seen and released only on offline (graceful or timeout).
Worker loss is delivered to listeners via an awaited callback, exactly
once per lost incarnation, at the moment the entry is removed — bus
``worker.offline`` events are observability only. Publication is
asymmetric: on graceful shutdown the worker already published the
event, so the registry publishes nothing; on timeout the registry is
the publisher.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from proctor.core.bus import EventBus
from proctor.core.config import RegistryConfig
from proctor.core.models import Event
from proctor.router.models import AgentProfile

logger = logging.getLogger(__name__)

_SOURCE = "worker_registry"

LossListener = Callable[[str, str], Awaitable[None]]


class WorkerEntry(BaseModel):
    """Registry bookkeeping for one live remote worker."""

    profile: AgentProfile
    instance_id: str
    last_seen: datetime


class WorkerRegistry:
    """Live catalog of workers; the only source of scoring candidates."""

    def __init__(
        self,
        bus: EventBus,
        config: RegistryConfig,
        *,
        local_profile: AgentProfile | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._bus = bus
        self._config = config
        self._local = local_profile
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._entries: dict[str, WorkerEntry] = {}
        self._loss_listeners: list[LossListener] = []
        self._sweep_task: asyncio.Task[None] | None = None
        bus.subscribe("worker.registered", self._handle_alive)
        bus.subscribe("worker.heartbeat", self._handle_alive)
        bus.subscribe("worker.offline", self._handle_offline)

    def add_loss_listener(self, cb: LossListener) -> None:
        """Register a callback awaited once per lost incarnation."""
        self._loss_listeners.append(cb)

    def alive_profiles(self) -> list[AgentProfile]:
        """Current candidates: seeded local profile plus live remotes."""
        remotes = [e.profile for e in self._entries.values()]
        return ([self._local] if self._local is not None else []) + remotes

    def instance_of(self, worker_id: str) -> str | None:
        """Current owning instance of a remote worker id, if any."""
        entry = self._entries.get(worker_id)
        return entry.instance_id if entry is not None else None

    async def start(self) -> None:
        """Start the periodic liveness sweep."""
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        """Cancel the sweep loop (idempotent)."""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await self._sweep_task
                except Exception:
                    logger.exception("Registry sweep task exited with error")
            self._sweep_task = None

    async def sweep(self, now: datetime | None = None) -> None:
        """Remove workers silent past liveness_timeout; notify + publish."""
        now = now or self._now()
        cutoff = timedelta(seconds=self._config.liveness_timeout)
        dead = [
            (wid, entry)
            for wid, entry in self._entries.items()
            if now - entry.last_seen >= cutoff
        ]
        for wid, _ in dead:
            del self._entries[wid]  # sync removal before any await
        for wid, entry in dead:
            logger.warning("Worker %s timed out (instance %s)", wid, entry.instance_id)
            await self._notify_loss(wid, entry.instance_id)
            await self._bus.publish(
                Event(
                    type="worker.offline",
                    source=_SOURCE,
                    payload={
                        "worker_id": wid,
                        "instance_id": entry.instance_id,
                        "reason": "timeout",
                    },
                )
            )

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.heartbeat_interval)
            try:
                await self.sweep()
            except Exception:
                logger.exception("Registry sweep failed")

    async def _handle_alive(self, event: Event) -> None:
        wid = event.payload.get("worker_id")
        iid = event.payload.get("instance_id")
        if not isinstance(wid, str) or not isinstance(iid, str):
            logger.warning("Malformed %s payload: %s", event.type, event.payload)
            return
        if self._local is not None and wid == self._local.id:
            logger.warning("Rejecting remote claim on reserved local id %r", wid)
            return
        entry = self._entries.get(wid)
        if entry is not None and entry.instance_id != iid:
            logger.warning(
                "Worker %s owned by instance %s; rejecting %s",
                wid,
                entry.instance_id,
                iid,
            )
            return
        capabilities = event.payload.get("capabilities", [])
        max_slots = event.payload.get("max_slots", 1)
        if (
            not isinstance(capabilities, list)
            or not all(isinstance(c, str) for c in capabilities)
            or isinstance(max_slots, bool)
            or not isinstance(max_slots, int)
            or max_slots < 1
        ):
            logger.warning(
                "Rejecting malformed %s profile from %s: %s",
                event.type,
                wid,
                event.payload,
            )
            return
        self._entries[wid] = WorkerEntry(
            profile=AgentProfile(
                id=wid, capabilities=capabilities, max_slots=max_slots
            ),
            instance_id=iid,
            last_seen=self._now(),
        )

    async def _handle_offline(self, event: Event) -> None:
        wid = event.payload.get("worker_id")
        iid = event.payload.get("instance_id")
        if not isinstance(wid, str) or not isinstance(iid, str):
            return
        if event.source == _SOURCE:
            return  # our own timeout publication — already handled
        entry = self._entries.get(wid)
        if entry is None or entry.instance_id != iid:
            logger.info("Ignoring stale worker.offline for %s/%s", wid, iid)
            return
        del self._entries[wid]  # sync removal before any await
        await self._notify_loss(wid, iid)
        # Graceful path: the worker already published the event —
        # re-publishing would duplicate observability signals.

    async def _notify_loss(self, worker_id: str, instance_id: str) -> None:
        for cb in self._loss_listeners:
            try:
                await cb(worker_id, instance_id)
            except Exception:
                logger.exception("Worker-loss listener failed for %s", worker_id)
