"""Terminal trigger — reads stdin lines and publishes events."""

import asyncio
import contextlib
import logging
import sys

from proctor.core.bus import EventBus
from proctor.core.models import Event
from proctor.triggers.base import Trigger

logger = logging.getLogger(__name__)

QUIT_COMMANDS = frozenset({"/quit", "/exit", "/q"})


class TerminalTrigger(Trigger):
    """Reads lines from stdin and publishes trigger.terminal events.

    Empty/whitespace lines are ignored. Quit commands (/quit, /exit, /q)
    signal the trigger to stop.
    """

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self, bus: EventBus) -> None:
        """Start reading stdin and publishing events."""
        self._running = True
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            sys.stdin,
        )
        self._task = asyncio.create_task(self._read_loop(reader, bus))

    async def stop(self) -> None:
        """Stop the read loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _read_loop(self, reader: asyncio.StreamReader, bus: EventBus) -> None:
        """Read lines from stdin until quit or stopped."""
        while self._running:
            try:
                raw = await reader.readline()
            except asyncio.CancelledError:
                break
            if not raw:  # EOF
                break
            line = raw.decode().rstrip("\n\r")
            result = await self._process_line(line, bus)
            if result == "quit":
                self._running = False
                break

    async def _process_line(self, line: str, bus: EventBus) -> str | None:
        """Process a single input line.

        Returns:
            "quit" if the line is a quit command, None otherwise.
        """
        stripped = line.strip()
        if not stripped:
            return None
        if stripped.lower() in QUIT_COMMANDS:
            logger.info("Quit command received: %s", stripped)
            return "quit"
        event = Event(
            type="trigger.terminal",
            source="terminal",
            payload={"text": stripped},
        )
        await bus.publish(event)
        logger.debug("Published terminal event: %s", event.id)
        return None
