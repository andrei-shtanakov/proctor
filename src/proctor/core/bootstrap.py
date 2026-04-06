"""Application bootstrap — lifecycle management and component wiring."""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from proctor.core.bus import EventBus
from proctor.core.config import ProctorConfig
from proctor.core.memory import EpisodicMemory
from proctor.core.models import Event, Task, TaskStatus
from proctor.core.state import StateManager
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
        self.is_running = True
        logger.info("Application started (node=%s)", self.config.node_id)

    async def stop(self) -> None:
        """Close state and memory, unset running."""
        self.is_running = False
        await self.memory.close()
        await self.state.close()
        logger.info("Application stopped")

    async def _handle_terminal(self, event: Event) -> None:
        """Handle terminal trigger events.

        Creates a Task, builds a simple WorkflowSpec, executes via
        WorkflowEngine, persists status transitions, and publishes
        the result as task.completed or task.failed.
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

        # Create and persist task
        task = Task(
            trigger_event=event.id,
            spec={"prompt": text},
        )
        await self.state.save_task(task)

        # Transition to RUNNING
        task.status = TaskStatus.RUNNING
        task.updated_at = datetime.now(UTC)
        await self.state.save_task(task)

        # Build workflow and execute
        spec = WorkflowSpec(
            workflow_id=task.id,
            mode=WorkflowMode.SIMPLE,
            prompt=text,
        )

        try:
            result = await self._engine.execute(spec)

            if result.error:
                task.status = TaskStatus.FAILED
                task.result = {"error": result.error}
            else:
                task.status = TaskStatus.COMPLETED
                task.result = {"output": result.output}

            task.updated_at = datetime.now(UTC)
            await self.state.save_task(task)

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
        except Exception as exc:
            logger.exception("Workflow execution failed")
            task.status = TaskStatus.FAILED
            task.result = {"error": str(exc)}
            task.updated_at = datetime.now(UTC)
            await self.state.save_task(task)

            await self.bus.publish(
                Event(
                    type="task.failed",
                    source="application",
                    payload={"error": str(exc)},
                )
            )
