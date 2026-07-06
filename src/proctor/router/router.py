"""TaskRouter — the M4 admission facade.

Decides whether an already-routed task may run *now*: reserves a slot
(admitted), parks it in the TTL queue (queued), or fails it fast
(rejected, when ``queue_ttl_seconds`` is 0). Never executes anything —
execution stays in bootstrap.

Atomicity: reservation mutates ``_running`` synchronously, before the
first ``await``. The local transport dispatches each handler as its own
asyncio task, so two ``admit()`` calls interleave only at await points;
mutating first makes the check-then-reserve step atomic. ``routing.*``
events are published only after the reservation is committed.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from proctor.core.bus import EventBus
from proctor.core.config import RouterConfig
from proctor.core.models import Event, Task
from proctor.router.invariants import (
    check_agent_available,
    check_branch_not_locked,
    check_concurrency_limit,
    check_scope_isolation,
)
from proctor.router.models import (
    AdmitDecision,
    AgentProfile,
    QueueEntry,
    RunningTask,
)
from proctor.router.queue import PendingQueue
from proctor.router.scoring import score_candidates
from proctor.workflow.spec import WorkflowSpec

logger = logging.getLogger(__name__)

_SOURCE = "task_router"


class TaskRouter:
    """Admission control: invariants + TTL queue + scoring seam."""

    def __init__(
        self,
        bus: EventBus,
        config: RouterConfig,
        agent_provider: Callable[[], list[AgentProfile]],
    ) -> None:
        self._bus = bus
        self._config = config
        self._agent_provider = agent_provider
        self._running: list[RunningTask] = []
        self._queue = PendingQueue()

    @property
    def running_count(self) -> int:
        """Number of currently reserved (admitted) tasks."""
        return len(self._running)

    def _check(self, spec: WorkflowSpec, profile: AgentProfile) -> str | None:
        return (
            check_concurrency_limit(self._running, self._config.max_concurrency)
            or check_agent_available(profile, self._running)
            or check_scope_isolation(spec.scope, self._running)
            or check_branch_not_locked(spec.branch, self._running)
        )

    def _try_reserve(
        self, task: Task, spec: WorkflowSpec
    ) -> tuple[str | None, str | None]:
        """Reserve a slot synchronously: (None, agent_id) on success,
        (reason, None) otherwise. MUST stay free of awaits."""
        agents = self._agent_provider()
        used: dict[str, int] = {}
        for r in self._running:
            used[r.agent_id] = used.get(r.agent_id, 0) + 1
        candidates = score_candidates(spec, agents, used)
        if not candidates:
            if spec.requires:
                return (
                    f"no_candidates: no live worker offers {sorted(spec.requires)}",
                    None,
                )
            return "no_candidates: no live workers", None
        reason = "no_candidates: no live workers"
        for candidate in candidates:
            reason = self._check(spec, candidate.profile)
            if reason is None:
                self._running.append(
                    RunningTask(
                        task_id=task.id,
                        agent_id=candidate.profile.id,
                        scope=spec.scope,
                        branch=spec.branch,
                    )
                )
                return None, candidate.profile.id
        return reason, None

    async def admit(
        self,
        task: Task,
        spec: WorkflowSpec,
        trigger_source: str,
        now: datetime | None = None,
    ) -> AdmitDecision:
        """Admit, queue, or reject a routed task."""
        now = now or datetime.now(UTC)
        reason, agent_id = self._try_reserve(task, spec)  # atomic: no await above
        if reason is None:
            return AdmitDecision(verdict="admitted", agent_id=agent_id)

        if self._config.queue_ttl_seconds <= 0:
            logger.warning("Task %s rejected: %s", task.id, reason)
            await self._bus.publish(
                Event(
                    type="routing.rejected",
                    source=_SOURCE,
                    payload={"task_id": task.id, "reason": reason},
                )
            )
            return AdmitDecision(verdict="rejected", reason=reason)

        entry = QueueEntry(
            task=task,
            spec=spec,
            trigger_source=trigger_source,
            enqueued_at=now,
            expires_at=now + timedelta(seconds=self._config.queue_ttl_seconds),
            reason=reason,
        )
        self._queue.push(entry)
        logger.info("Task %s queued: %s", task.id, reason)
        await self._bus.publish(
            Event(
                type="routing.queued",
                source=_SOURCE,
                payload={
                    "task_id": task.id,
                    "reason": reason,
                    "expires_at": entry.expires_at.isoformat(),
                },
            )
        )
        return AdmitDecision(verdict="queued", reason=reason)

    async def release(
        self, task_id: str, now: datetime | None = None
    ) -> list[QueueEntry]:
        """Free a finished task's slot, then re-check the queue."""
        self._running = [r for r in self._running if r.task_id != task_id]
        return await self.dequeue_ready(now)

    async def dequeue_ready(self, now: datetime | None = None) -> list[QueueEntry]:
        """Admit queued entries whose invariants now pass (FIFO scan).

        Returned entries already hold a reservation — the caller MUST
        run them and eventually call release().
        """
        now = now or datetime.now(UTC)

        def _try_admit(entry: QueueEntry) -> bool:
            if entry.not_before is not None and entry.not_before > now:
                return False
            reason, agent_id = self._try_reserve(entry.task, entry.spec)
            if reason is None:
                entry.agent_id = agent_id
                return True
            return False

        ready = self._queue.pop_admissible(_try_admit)
        for entry in ready:
            waited = (now - entry.enqueued_at).total_seconds()
            await self._bus.publish(
                Event(
                    type="routing.dequeued",
                    source=_SOURCE,
                    payload={
                        "task_id": entry.task.id,
                        "waited_seconds": waited,
                    },
                )
            )
        return ready

    async def retry(
        self,
        task: Task,
        spec: WorkflowSpec,
        trigger_source: str,
        not_before: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        """Park a task for re-dispatch. Never re-admits inline.

        TTL is anchored at the moment the entry becomes runnable, so a
        retry delay longer than the TTL still gets a full TTL window.
        """
        now = now or datetime.now(UTC)
        runnable_at = max(now, not_before) if not_before is not None else now
        entry = QueueEntry(
            task=task,
            spec=spec,
            trigger_source=trigger_source,
            enqueued_at=now,
            not_before=not_before,
            expires_at=runnable_at + timedelta(seconds=self._config.queue_ttl_seconds),
            reason="retry: worker lost",
        )
        self._queue.push(entry)
        await self._bus.publish(
            Event(
                type="routing.queued",
                source=_SOURCE,
                payload={
                    "task_id": task.id,
                    "reason": entry.reason,
                    "retry": True,
                    "expires_at": entry.expires_at.isoformat(),
                },
            )
        )

    async def expire_overdue(self, now: datetime | None = None) -> list[QueueEntry]:
        """Drop queue entries past their expires_at; caller fails them."""
        now = now or datetime.now(UTC)
        expired = self._queue.pop_expired(now)
        for entry in expired:
            logger.warning("Task %s expired in queue: %s", entry.task.id, entry.reason)
            await self._bus.publish(
                Event(
                    type="routing.expired",
                    source=_SOURCE,
                    payload={
                        "task_id": entry.task.id,
                        "reason": entry.reason,
                    },
                )
            )
        return expired
