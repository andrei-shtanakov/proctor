"""Application bootstrap — lifecycle management and component wiring."""

import logging
from collections.abc import Awaitable, Callable

from proctor.core.bus import EventBus
from proctor.core.config import ProctorConfig
from proctor.core.models import Event
from proctor.core.state import StateManager

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
        self.is_running = False
        self._llm_call: LLMCall | None = None

    def set_llm_call(self, llm_call: LLMCall) -> None:
        """Inject LLM callable for workflow execution."""
        self._llm_call = llm_call

    async def start(self) -> None:
        """Initialize state, subscribe handlers, set running."""
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        await self.state.initialize()
        self.bus.subscribe("trigger.terminal", self._handle_terminal)
        self.is_running = True
        logger.info("Application started (node=%s)", self.config.node_id)

    async def stop(self) -> None:
        """Close state, unset running."""
        self.is_running = False
        await self.state.close()
        logger.info("Application stopped")

    async def _handle_terminal(self, event: Event) -> None:
        """Handle terminal trigger events.

        Creates a simple workflow from terminal text and publishes
        the result as task.completed or task.failed.
        """
        text = event.payload.get("text", "")
        if not text:
            return

        if self._llm_call is None:
            await self.bus.publish(
                Event(
                    type="task.failed",
                    source="application",
                    payload={"error": "No LLM configured"},
                )
            )
            return

        try:
            result = await self._llm_call(text)
            await self.bus.publish(
                Event(
                    type="task.completed",
                    source="application",
                    payload={"output": result},
                )
            )
        except Exception as exc:
            logger.exception("LLM call failed")
            await self.bus.publish(
                Event(
                    type="task.failed",
                    source="application",
                    payload={"error": str(exc)},
                )
            )
