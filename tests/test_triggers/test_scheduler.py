"""Tests for SchedulerTrigger: cron, interval, start/stop, edge cases."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import anyio
import pytest

from proctor.core.bus import EventBus
from proctor.core.config import ScheduleItemConfig
from proctor.core.models import Event
from proctor.core.transport import LocalEventTransport
from proctor.triggers.scheduler import SchedulerTrigger


@pytest.fixture
def anyio_backend() -> str:
    """LocalEventTransport uses asyncio primitives."""
    return "asyncio"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cron_item(
    name: str = "cron-job",
    cron: str = "*/5 * * * *",
    payload: dict | None = None,
    enabled: bool = True,
) -> ScheduleItemConfig:
    return ScheduleItemConfig(
        name=name,
        cron=cron,
        payload=payload or {"action": "cron"},
        enabled=enabled,
    )


def _interval_item(
    name: str = "interval-job",
    interval_seconds: float = 0.05,
    payload: dict | None = None,
    enabled: bool = True,
) -> ScheduleItemConfig:
    return ScheduleItemConfig(
        name=name,
        interval_seconds=interval_seconds,
        payload=payload or {"action": "interval"},
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# ScheduleItemConfig validation (TASK-005 items included here)
# ---------------------------------------------------------------------------


class TestScheduleItemConfigValidation:
    def test_valid_cron_config(self) -> None:
        item = ScheduleItemConfig(name="test", cron="*/5 * * * *")
        assert item.cron == "*/5 * * * *"
        assert item.interval_seconds is None

    def test_valid_interval_config(self) -> None:
        item = ScheduleItemConfig(name="test", interval_seconds=60)
        assert item.interval_seconds == 60
        assert item.cron is None

    def test_both_set_raises(self) -> None:
        with pytest.raises(ValueError, match="Exactly one"):
            ScheduleItemConfig(
                name="bad",
                cron="* * * * *",
                interval_seconds=60,
            )

    def test_neither_set_raises(self) -> None:
        with pytest.raises(ValueError, match="Exactly one"):
            ScheduleItemConfig(name="bad")

    def test_disabled_accepted(self) -> None:
        item = ScheduleItemConfig(name="off", cron="0 * * * *", enabled=False)
        assert item.enabled is False

    def test_default_payload_empty(self) -> None:
        item = ScheduleItemConfig(name="test", interval_seconds=10)
        assert item.payload == {}

    def test_invalid_cron_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid cron"):
            ScheduleItemConfig(name="bad", cron="not-a-cron")

    def test_zero_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="greater than 0"):
            ScheduleItemConfig(name="bad", interval_seconds=0)

    def test_negative_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="greater than 0"):
            ScheduleItemConfig(name="bad", interval_seconds=-1)


# ---------------------------------------------------------------------------
# SchedulerTrigger — init & ABC compliance
# ---------------------------------------------------------------------------


class TestSchedulerTriggerInit:
    def test_is_trigger_subclass(self) -> None:
        from proctor.triggers.base import Trigger

        trigger = SchedulerTrigger(schedules=[])
        assert isinstance(trigger, Trigger)

    def test_stores_schedules(self) -> None:
        items = [_interval_item()]
        trigger = SchedulerTrigger(schedules=items)
        assert trigger._schedules is items

    def test_initial_task_group_none(self) -> None:
        trigger = SchedulerTrigger(schedules=[])
        assert trigger._task_group is None


# ---------------------------------------------------------------------------
# SchedulerTrigger — interval scheduling
# ---------------------------------------------------------------------------


class TestIntervalScheduling:
    @pytest.mark.anyio
    async def test_interval_fires_events(self) -> None:
        bus = EventBus(LocalEventTransport())
        await bus.start()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.scheduler", handler)

        item = _interval_item(interval_seconds=0.01)
        trigger = SchedulerTrigger(schedules=[item])
        await trigger.start(bus)

        await anyio.sleep(0.1)
        await trigger.stop()

        assert len(received) >= 2
        for ev in received:
            assert ev.type == "trigger.scheduler"
            assert ev.source == "scheduler:interval-job"
            assert ev.payload == {"action": "interval"}

    @pytest.mark.anyio
    async def test_multiple_interval_items(self) -> None:
        bus = EventBus(LocalEventTransport())
        await bus.start()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.scheduler", handler)

        items = [
            _interval_item(name="a", interval_seconds=0.01),
            _interval_item(name="b", interval_seconds=0.01),
        ]
        trigger = SchedulerTrigger(schedules=items)
        await trigger.start(bus)

        await anyio.sleep(0.1)
        await trigger.stop()

        sources = {e.source for e in received}
        assert "scheduler:a" in sources
        assert "scheduler:b" in sources


# ---------------------------------------------------------------------------
# SchedulerTrigger — cron scheduling
# ---------------------------------------------------------------------------


class TestCronScheduling:
    @pytest.mark.anyio
    async def test_cron_publishes_correct_event_type(self) -> None:
        """Verify _publish produces the right event shape."""
        bus = EventBus(LocalEventTransport())
        await bus.start()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.scheduler", handler)

        item = _cron_item(payload={"key": "val"})
        trigger = SchedulerTrigger(schedules=[item])

        # Call _publish directly to avoid waiting for real cron delay
        await trigger._publish(item, bus)
        await bus.flush()

        assert len(received) == 1
        assert received[0].type == "trigger.scheduler"
        assert received[0].source == "scheduler:cron-job"
        assert received[0].payload == {"key": "val"}

    @pytest.mark.anyio
    async def test_cron_fires_event_via_start(self) -> None:
        """Cron schedule publishes an event on the bus after firing."""
        bus = EventBus(LocalEventTransport())
        await bus.start()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.scheduler", handler)

        item = _cron_item(
            name="fast-cron",
            payload={"action": "cron-fired"},
        )
        trigger = SchedulerTrigger(schedules=[item])

        # Patch croniter.get_next to return a time 0.01s in the
        # future so the cron loop fires almost immediately.
        def _fake_get_next(self: object, ret_type: type) -> datetime:
            return datetime.now(UTC) + timedelta(seconds=0.01)

        with patch(
            "proctor.triggers.scheduler.croniter.get_next",
            _fake_get_next,
        ):
            await trigger.start(bus)
            await anyio.sleep(0.08)
            await trigger.stop()

        assert len(received) >= 1
        ev = received[0]
        assert ev.type == "trigger.scheduler"
        assert ev.source == "scheduler:fast-cron"
        assert ev.payload == {"action": "cron-fired"}

    @pytest.mark.anyio
    async def test_cron_event_has_correct_fields(self) -> None:
        """Event from cron has correct type, source, and payload."""
        bus = EventBus(LocalEventTransport())
        await bus.start()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.scheduler", handler)

        item = _cron_item(
            name="field-check",
            payload={"env": "test", "tier": 1},
        )
        trigger = SchedulerTrigger(schedules=[item])

        def _fake_get_next(self: object, ret_type: type) -> datetime:
            return datetime.now(UTC) + timedelta(seconds=0.01)

        with patch(
            "proctor.triggers.scheduler.croniter.get_next",
            _fake_get_next,
        ):
            await trigger.start(bus)
            await anyio.sleep(0.05)
            await trigger.stop()

        assert len(received) >= 1
        ev = received[0]
        assert ev.type == "trigger.scheduler"
        assert ev.source == "scheduler:field-check"
        assert ev.payload == {"env": "test", "tier": 1}
        # Event should have auto-generated id and timestamp
        assert ev.id
        assert ev.timestamp


# ---------------------------------------------------------------------------
# SchedulerTrigger — disabled items
# ---------------------------------------------------------------------------


class TestDisabledSchedules:
    @pytest.mark.anyio
    async def test_disabled_items_not_started(self) -> None:
        bus = EventBus(LocalEventTransport())
        await bus.start()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.scheduler", handler)

        items = [
            _interval_item(name="on", interval_seconds=0.01),
            _interval_item(
                name="off",
                interval_seconds=0.01,
                enabled=False,
            ),
        ]
        trigger = SchedulerTrigger(schedules=items)
        await trigger.start(bus)
        await anyio.sleep(0.05)
        await trigger.stop()

        sources = {e.source for e in received}
        assert "scheduler:on" in sources
        assert "scheduler:off" not in sources

    @pytest.mark.anyio
    async def test_all_disabled_no_tasks(self) -> None:
        bus = EventBus(LocalEventTransport())
        await bus.start()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.scheduler", handler)

        items = [
            _interval_item(name="off1", enabled=False),
            _cron_item(name="off2", enabled=False),
        ]
        trigger = SchedulerTrigger(schedules=items)
        await trigger.start(bus)
        await anyio.sleep(0.05)
        await trigger.stop()

        assert len(received) == 0


# ---------------------------------------------------------------------------
# SchedulerTrigger — start / stop lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.anyio
    async def test_stop_cancels_all_tasks(self) -> None:
        bus = EventBus(LocalEventTransport())
        await bus.start()
        items = [
            _interval_item(name="a", interval_seconds=0.02),
            _interval_item(name="b", interval_seconds=0.02),
        ]
        trigger = SchedulerTrigger(schedules=items)
        await trigger.start(bus)
        assert trigger._task_group is not None
        await trigger.stop()
        assert trigger._task_group is None

    @pytest.mark.anyio
    async def test_stop_cancels_cron_tasks_without_errors(
        self,
    ) -> None:
        """stop() cleanly cancels cron tasks without errors."""
        bus = EventBus(LocalEventTransport())
        await bus.start()
        items = [
            _cron_item(name="cron-a"),
            _cron_item(name="cron-b"),
        ]
        trigger = SchedulerTrigger(schedules=items)
        await trigger.start(bus)
        assert trigger._task_group is not None
        await trigger.stop()
        assert trigger._task_group is None

    @pytest.mark.anyio
    async def test_stop_cancels_mixed_tasks(self) -> None:
        """stop() cleanly cancels a mix of cron and interval tasks."""
        bus = EventBus(LocalEventTransport())
        await bus.start()
        items = [
            _cron_item(name="cron-mix"),
            _interval_item(name="interval-mix", interval_seconds=10),
        ]
        trigger = SchedulerTrigger(schedules=items)
        await trigger.start(bus)
        assert trigger._task_group is not None
        await trigger.stop()
        assert trigger._task_group is None

    @pytest.mark.anyio
    async def test_stop_when_not_started(self) -> None:
        trigger = SchedulerTrigger(schedules=[])
        await trigger.stop()
        assert trigger._task_group is None

    @pytest.mark.anyio
    async def test_empty_schedules(self) -> None:
        bus = EventBus(LocalEventTransport())
        await bus.start()
        trigger = SchedulerTrigger(schedules=[])
        await trigger.start(bus)
        assert trigger._task_group is not None
        await trigger.stop()
        assert trigger._task_group is None


# ---------------------------------------------------------------------------
# SchedulerTrigger — public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_import_from_triggers_package(self) -> None:
        from proctor.triggers import SchedulerTrigger as ST

        assert ST is SchedulerTrigger
