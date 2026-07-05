"""Application bootstrap — lifecycle management and component wiring."""

import asyncio
import contextlib
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

from proctor.core.bus import EventBus
from proctor.core.config import (
    ProctorConfig,
    _resolve_transport_mode_static,
)
from proctor.core.memory import EpisodicMemory
from proctor.core.models import Episode, Event, Task, TaskStatus
from proctor.core.router import Router
from proctor.core.state import StateManager
from proctor.core.transport import EventTransport, LocalEventTransport
from proctor.core.transport.errors import TransportDrainingError
from proctor.router.models import AgentProfile, QueueEntry
from proctor.router.router import TaskRouter
from proctor.triggers.scheduler import SchedulerTrigger
from proctor.triggers.telegram import TelegramTrigger
from proctor.triggers.webhook import WebhookTrigger
from proctor.workers.llm import episode_id_ctx, task_id_ctx
from proctor.workflow.engine import WorkflowEngine
from proctor.workflow.spec import WorkflowSpec

logger = logging.getLogger(__name__)

LLMCall = Callable[[str], Awaitable[str]]


def _resolve_transport_mode(
    config: ProctorConfig,
) -> Literal["local", "nats"]:
    """Resolve config.transport to an effective mode."""
    return _resolve_transport_mode_static(config.transport, config.node_role)


def _build_event_transport(config: ProctorConfig) -> EventTransport:
    """Construct the EventTransport implementation selected by config."""
    mode = _resolve_transport_mode(config)
    if mode == "local":
        return LocalEventTransport(
            max_payload=config.events.max_payload,
        )

    from proctor.core.transport.nats import NATSEventTransport

    nats_cfg = config.nats
    hostname = socket.gethostname()
    if nats_cfg.name == f"proctor-{hostname}":
        nats_cfg = nats_cfg.model_copy(
            update={"name": f"proctor-{config.node_role}-{hostname}"}
        )
    return NATSEventTransport(nats_cfg, events_config=config.events)


class Application:
    """Main application container.

    Owns core components, manages their lifecycle, and wires
    event handlers. Entry point for ``python -m proctor``.
    """

    def __init__(
        self,
        config: ProctorConfig,
        *,
        event_transport: EventTransport | None = None,
    ) -> None:
        self.config = config
        transport = event_transport or _build_event_transport(config)
        self.bus = EventBus(transport)
        self.state = StateManager(config.data_dir / "state.db")
        self.memory = EpisodicMemory(config.data_dir / "episodes.db")
        self.is_running = False
        self._llm_call: LLMCall | None = None
        self._engine: WorkflowEngine | None = None
        self._router: Router | None = None
        self._task_router: TaskRouter | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._exec_tasks: set[asyncio.Task[None]] = set()
        self._telegram_trigger: TelegramTrigger | None = None
        self._scheduler: SchedulerTrigger | None = None
        self._webhook_trigger: WebhookTrigger | None = None
        # Subscribe in __init__ (ADR #19 — buffered until bus.start()).
        # Router and engine are nil until start(); _handle_trigger_event
        # checks before dispatching.
        self.bus.subscribe("trigger.>", self._handle_trigger_event)

    def set_llm_call(self, llm_call: LLMCall) -> None:
        """Inject LLM callable and create WorkflowEngine."""
        self._llm_call = llm_call
        self._engine = WorkflowEngine(llm_call)

    async def start(self) -> None:
        """Initialize state and memory, start bus and triggers, set running."""
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        await self.bus.start()
        await self.state.initialize()
        await self.memory.initialize()
        self._router = Router(
            bus=self.bus,
            routes=self.config.routes,
            workflows=self.config.workflows,
        )
        self._task_router = TaskRouter(
            bus=self.bus,
            config=self.config.router,
            agents=[
                AgentProfile(
                    id="local",
                    max_slots=self.config.router.agent.max_slots,
                )
            ],
        )
        self._tick_task = asyncio.create_task(self._queue_tick_loop())

        if self.config.telegram is not None:
            self._telegram_trigger = TelegramTrigger(self.config.telegram)
            await self._telegram_trigger.start(self.bus)
            logger.info("TelegramTrigger enabled")

        if self.config.scheduler.enabled and self.config.schedules:
            self._scheduler = SchedulerTrigger(self.config.schedules)
            await self._scheduler.start(self.bus)

        if self.config.webhook is not None:
            self._webhook_trigger = WebhookTrigger(self.config.webhook)
            await self._webhook_trigger.start(self.bus)
            logger.info("WebhookTrigger enabled")

        self.is_running = True
        logger.info("Application started (node=%s)", self.config.node_id)

    async def stop(self) -> None:
        """Close state and memory, stop triggers, unset running."""
        self.is_running = False
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await self._tick_task
                except Exception:
                    # The tick task may have already died from an
                    # exception before cancel() reached it; teardown
                    # must proceed regardless.
                    logger.exception("Queue tick task exited with an error")
            self._tick_task = None
        # Close inputs first — WebhookTrigger first so the HTTP endpoint
        # stops accepting new POSTs before other components tear down.
        if self._webhook_trigger is not None:
            await self._webhook_trigger.stop()
            self._webhook_trigger = None
        if self._telegram_trigger is not None:
            await self._telegram_trigger.stop()
            self._telegram_trigger = None
        if self._scheduler is not None:
            await self._scheduler.stop()
            self._scheduler = None
        # Drain in-flight handlers before transport shutdown.
        await self.bus.drain(timeout=self.config.events.drain_timeout)
        if self._exec_tasks:
            _, pending = await asyncio.wait(
                self._exec_tasks,
                timeout=self.config.events.drain_timeout,
            )
            # Executions that outlive the drain window must not keep
            # running into memory/state teardown below.
            for exec_task in pending:
                exec_task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await self.memory.close()
        await self.state.close()
        await self.bus.stop()
        logger.info("Application stopped")

    async def _handle_trigger_event(self, event: Event) -> None:
        """Route trigger.* events to workflows via the Router.

        On matched + bound rule, runs the standard task+episode+ctxvar
        lifecycle. On unmatched / binding-failed, the Router has already
        published routing.* observability events and logged a WARNING;
        we simply skip task creation.
        """
        if self._router is None or self._engine is None or self._task_router is None:
            await self.bus.publish(
                Event(
                    type="task.failed",
                    source="application",
                    payload={
                        "error": "Application not fully started "
                        "(router, engine, or task router missing)",
                    },
                )
            )
            return

        spec = await self._router.route(event)
        if spec is None:
            return  # router already emitted routing.*

        task = Task(trigger_event=event.id, spec=spec.model_dump())
        await self.state.save_task(task)  # persisted as PENDING

        assert self._task_router is not None  # created in start()
        decision = await self._task_router.admit(task, spec, event.source)
        if decision.verdict == "queued":
            return  # TaskRouter emitted routing.queued; tick/release will run it
        if decision.verdict == "rejected":
            task.status = TaskStatus.FAILED
            task.result = {"error": f"admission rejected: {decision.reason}"}
            task.updated_at = datetime.now(UTC)
            await self.state.save_task(task)
            await self.bus.publish(
                Event(
                    type="task.failed",
                    source="application",
                    payload=task.result,
                )
            )
            return

        await self._run_admitted(task, spec, event.source)

    async def _run_admitted(
        self, task: Task, spec: WorkflowSpec, trigger_source: str
    ) -> None:
        """Execute an admitted task; always release its slot at the end."""
        assert self._engine is not None and self._task_router is not None
        try:
            resolved_prompt = spec.prompt or ""
            task.status = TaskStatus.RUNNING
            task.updated_at = datetime.now(UTC)
            await self.state.save_task(task)

            episode = Episode(
                trigger_type=trigger_source,
                user_input=resolved_prompt,
                agent_response="",
            )
            await self.memory.save_episode(episode)

            task_token = task_id_ctx.set(task.id)
            episode_token = episode_id_ctx.set(episode.id)
            try:
                result = await self._engine.execute(spec)
            except Exception as exc:
                logger.exception("Workflow execution failed")
                task.status = TaskStatus.FAILED
                task.result = {"error": str(exc)}
                task.updated_at = datetime.now(UTC)
                await self.state.save_task(task)

                episode.workflow_result = task.result
                await self.memory.save_episode(episode)

                await self.bus.publish(
                    Event(
                        type="task.failed",
                        source="application",
                        payload={"error": str(exc)},
                    )
                )
                return
            finally:
                task_id_ctx.reset(task_token)
                episode_id_ctx.reset(episode_token)

            if result.error:
                task.status = TaskStatus.FAILED
                task.result = {"error": result.error}
            else:
                task.status = TaskStatus.COMPLETED
                task.result = {"output": result.output}

            task.updated_at = datetime.now(UTC)
            await self.state.save_task(task)

            episode.agent_response = result.output or ""
            episode.workflow_result = task.result
            await self.memory.save_episode(episode)

            await self.bus.publish(
                Event(
                    type=(
                        "task.completed"
                        if task.status == TaskStatus.COMPLETED
                        else "task.failed"
                    ),
                    source="application",
                    payload=task.result,
                )
            )
        finally:
            try:
                ready = await self._task_router.release(task.id)
                self._spawn_ready(ready)
            except TransportDrainingError:
                # release() frees the in-memory slot synchronously before
                # its first await; the raise below only means the follow-up
                # dequeue publish lost the race with shutdown. The slot is
                # freed either way — any entries it reserved stay PENDING
                # in SQLite and are covered by the restart-recovery gap.
                logger.debug(
                    "Skipping post-release dequeue for task %s: transport draining",
                    task.id,
                )

    async def _run_spawned(self, entry: QueueEntry) -> None:
        """Run a dequeued entry, logging (not raising) on crash.

        Guards against "Task exception was never retrieved" and a task
        left RUNNING in SQLite with no accompanying task.failed event;
        matches the logging-only contract used by ``_safe_invoke``.
        """
        try:
            await self._run_admitted(entry.task, entry.spec, entry.trigger_source)
        except Exception:
            logger.exception("Dequeued task %s crashed", entry.task.id)

    def _spawn_ready(self, entries: list[QueueEntry]) -> None:
        """Launch execution of dequeued entries as tracked asyncio tasks."""
        if not self.is_running:
            # Teardown in progress — don't start new executions; the
            # entries' tasks remain PENDING in SQLite (restart limitation).
            return
        for entry in entries:
            exec_task = asyncio.create_task(self._run_spawned(entry))
            self._exec_tasks.add(exec_task)
            exec_task.add_done_callback(self._exec_tasks.discard)

    async def _queue_tick_loop(self) -> None:
        """Expire overdue queue entries and re-check the queue."""
        assert self._task_router is not None
        while True:
            await asyncio.sleep(self.config.router.queue_tick_seconds)
            try:
                expired = await self._task_router.expire_overdue()
                for entry in expired:
                    entry.task.status = TaskStatus.FAILED
                    entry.task.result = {"error": f"queue TTL expired: {entry.reason}"}
                    entry.task.updated_at = datetime.now(UTC)
                    await self.state.save_task(entry.task)
                    await self.bus.publish(
                        Event(
                            type="task.failed",
                            source="application",
                            payload=entry.task.result,
                        )
                    )
                self._spawn_ready(await self._task_router.dequeue_ready())
            except Exception:
                logger.exception("Queue tick failed")
