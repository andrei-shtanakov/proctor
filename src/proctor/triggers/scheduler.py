"""Scheduler trigger — fires events on cron or fixed-interval schedules."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from croniter import croniter

from proctor.core.bus import EventBus
from proctor.core.config import ScheduleItemConfig
from proctor.core.models import Event
from proctor.triggers.base import Trigger

logger = logging.getLogger(__name__)


class SchedulerTrigger(Trigger):
    """Publishes trigger.scheduler events based on cron/interval schedules.

    Each enabled schedule item gets its own asyncio task that sleeps
    until the next fire time and then publishes an event on the bus.
    """

    def __init__(self, schedules: list[ScheduleItemConfig]) -> None:
        self._schedules = schedules
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self, bus: EventBus) -> None:
        """Launch one asyncio task per enabled schedule item."""
        for item in self._schedules:
            if not item.enabled:
                logger.debug("Skipping disabled schedule: %s", item.name)
                continue
            if item.cron is not None:
                task = asyncio.create_task(self._run_cron(item, bus))
            else:
                task = asyncio.create_task(self._run_interval(item, bus))
            self._tasks.append(task)
        logger.info(
            "SchedulerTrigger started with %d active schedule(s)",
            len(self._tasks),
        )

    async def stop(self) -> None:
        """Cancel all running schedule tasks with proper cleanup."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        logger.info("SchedulerTrigger stopped")

    async def _run_cron(self, item: ScheduleItemConfig, bus: EventBus) -> None:
        """Loop using croniter to sleep until next fire, then publish."""
        assert item.cron is not None
        while True:
            now = datetime.now(UTC)
            cron = croniter(item.cron, now)
            next_fire = cron.get_next(datetime)
            delay = (next_fire - now).total_seconds()
            if delay <= 0:
                # Past time (e.g. after long sleep) — skip to next future
                next_fire = cron.get_next(datetime)
                delay = (next_fire - now).total_seconds()
            await asyncio.sleep(delay)
            await self._publish(item, bus)

    async def _run_interval(self, item: ScheduleItemConfig, bus: EventBus) -> None:
        """Loop with fixed interval sleep, then publish."""
        while True:
            await asyncio.sleep(item.interval_seconds)  # type: ignore[arg-type]
            await self._publish(item, bus)

    async def _publish(self, item: ScheduleItemConfig, bus: EventBus) -> None:
        """Publish a scheduler event for the given item."""
        event = Event(
            type="trigger.scheduler",
            source=f"scheduler:{item.name}",
            payload=item.payload,
        )
        await bus.publish(event)
        logger.debug("Scheduler fired: %s (event %s)", item.name, event.id)
