"""Scheduler trigger — fires events on cron or fixed-interval schedules."""

import contextlib
import logging
from datetime import UTC, datetime

import anyio
from anyio.abc import TaskGroup
from croniter import croniter

from proctor.core.bus import EventBus
from proctor.core.config import ScheduleItemConfig
from proctor.core.models import Event
from proctor.triggers.base import Trigger

logger = logging.getLogger(__name__)


class SchedulerTrigger(Trigger):
    """Publishes trigger.scheduler events based on cron/interval schedules.

    Each enabled schedule item gets its own task that sleeps
    until the next fire time and then publishes an event on the bus.
    """

    def __init__(self, schedules: list[ScheduleItemConfig]) -> None:
        self._schedules = schedules
        self._task_group: TaskGroup | None = None

    async def start(self, bus: EventBus) -> None:
        """Launch one task per enabled schedule item."""
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        count = 0
        for item in self._schedules:
            if not item.enabled:
                logger.debug("Skipping disabled schedule: %s", item.name)
                continue
            if item.cron is not None:
                self._task_group.start_soon(self._run_cron, item, bus)
            else:
                self._task_group.start_soon(self._run_interval, item, bus)
            count += 1
        logger.info(
            "SchedulerTrigger started with %d active schedule(s)",
            count,
        )

    async def stop(self) -> None:
        """Cancel all running schedule tasks with proper cleanup."""
        if self._task_group is not None:
            self._task_group.cancel_scope.cancel()
            with contextlib.suppress(BaseException):
                await self._task_group.__aexit__(None, None, None)
            self._task_group = None
        logger.info("SchedulerTrigger stopped")

    async def _run_cron(self, item: ScheduleItemConfig, bus: EventBus) -> None:
        """Loop using croniter to sleep until next fire, then publish."""
        if item.cron is None:
            return
        while True:
            now = datetime.now(UTC)
            cron = croniter(item.cron, now)
            next_fire = cron.get_next(datetime)
            delay = (next_fire - now).total_seconds()
            if delay <= 0:
                next_fire = cron.get_next(datetime)
                delay = (next_fire - now).total_seconds()
            await anyio.sleep(delay)
            await self._publish(item, bus)

    async def _run_interval(self, item: ScheduleItemConfig, bus: EventBus) -> None:
        """Loop with fixed interval sleep, then publish."""
        if item.interval_seconds is None:
            return
        while True:
            await anyio.sleep(item.interval_seconds)
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
