"""Application bootstrap — lifecycle management and component wiring."""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from proctor.core.bus import EventBus
from proctor.core.config import ProctorConfig
from proctor.core.memory import EpisodicMemory
from proctor.core.models import Episode, Event, Task, TaskStatus
from proctor.core.state import StateManager
from proctor.triggers.scheduler import SchedulerTrigger
from proctor.triggers.telegram import TelegramTrigger
from proctor.workers.llm import episode_id_ctx, task_id_ctx
from proctor.workflow.engine import WorkflowEngine
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

logger = logging.getLogger(__name__)

LLMCall = Callable[[str], Awaitable[str]]


class Application:
    """Main application container.

    Owns core components, manages their lifecycle, and wires
    event handlers. Entry point for ``python -m proctor``.
    """

    def __init__(self, config: ProctorConfig) -> None:
        self.config = config
        self.bus = EventBus()
        self.state = StateManager(config.data_dir / "state.db")
        self.memory = EpisodicMemory(config.data_dir / "episodes.db")
        self.is_running = False
        self._llm_call: LLMCall | None = None
        self._engine: WorkflowEngine | None = None
        self._telegram_trigger: TelegramTrigger | None = None
        self._scheduler: SchedulerTrigger | None = None

    def set_llm_call(self, llm_call: LLMCall) -> None:
        """Inject LLM callable and create WorkflowEngine."""
        self._llm_call = llm_call
        self._engine = WorkflowEngine(llm_call)

    async def start(self) -> None:
        """Initialize state and memory, subscribe handlers, set running."""
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        await self.state.initialize()
        await self.memory.initialize()
        self.bus.subscribe("trigger.terminal", self._handle_terminal)

        if self.config.telegram is not None:
            self._telegram_trigger = TelegramTrigger(self.config.telegram)
            await self._telegram_trigger.start(self.bus)
            logger.info("TelegramTrigger enabled")

        if self.config.scheduler.enabled and self.config.schedules:
            self._scheduler = SchedulerTrigger(self.config.schedules)
            await self._scheduler.start(self.bus)

        self.is_running = True
        logger.info("Application started (node=%s)", self.config.node_id)

    async def stop(self) -> None:
        """Close state and memory, stop triggers, unset running."""
        self.is_running = False
        if self._telegram_trigger is not None:
            await self._telegram_trigger.stop()
            self._telegram_trigger = None
        if self._scheduler is not None:
            await self._scheduler.stop()
            self._scheduler = None
        await self.memory.close()
        await self.state.close()
        logger.info("Application stopped")

    async def _handle_terminal(self, event: Event) -> None:
        """Handle terminal trigger events.

        Creates a Task and a pre-execution Episode, sets
        ``task_id_ctx``/``episode_id_ctx``, runs the workflow, then
        updates the Episode row with the real agent_response.
        """
        text = event.payload.get("text", "")
        if not text:
            return

        if self._engine is None:
            await self.bus.publish(
                Event(
                    type="task.failed",
                    source="application",
                    payload={"error": "No LLM configured"},
                )
            )
            return

        task = Task(trigger_event=event.id, spec={"prompt": text})
        await self.state.save_task(task)
        task.status = TaskStatus.RUNNING
        task.updated_at = datetime.now(UTC)
        await self.state.save_task(task)

        # Pre-execution Episode so llm_calls rows can reference episode_id.
        # We update agent_response after execute (save_episode is
        # idempotent via ON CONFLICT(id) DO UPDATE).
        episode = Episode(
            trigger_type=event.source,
            user_input=text,
            agent_response="",
        )
        await self.memory.save_episode(episode)

        spec = WorkflowSpec(
            workflow_id=task.id,
            mode=WorkflowMode.SIMPLE,
            prompt=text,
        )

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
