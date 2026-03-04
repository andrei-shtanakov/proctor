"""Tests for TerminalTrigger: stdin reading, event publishing, quit."""

import pytest

from proctor.core.bus import EventBus
from proctor.core.models import Event
from proctor.triggers.terminal import QUIT_COMMANDS, TerminalTrigger


class TestProcessLine:
    """Tests for _process_line — the core logic of TerminalTrigger."""

    @pytest.mark.anyio
    async def test_non_empty_line_publishes_event(self) -> None:
        bus = EventBus()
        trigger = TerminalTrigger()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.terminal", handler)

        result = await trigger._process_line("hello world", bus)

        assert result is None
        assert len(received) == 1
        assert received[0].type == "trigger.terminal"
        assert received[0].source == "terminal"
        assert received[0].payload == {"text": "hello world"}

    @pytest.mark.anyio
    async def test_empty_line_ignored(self) -> None:
        bus = EventBus()
        trigger = TerminalTrigger()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.*", handler)

        result = await trigger._process_line("", bus)

        assert result is None
        assert len(received) == 0

    @pytest.mark.anyio
    async def test_whitespace_only_ignored(self) -> None:
        bus = EventBus()
        trigger = TerminalTrigger()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.*", handler)

        result = await trigger._process_line("   \t  ", bus)

        assert result is None
        assert len(received) == 0

    @pytest.mark.anyio
    async def test_quit_command(self) -> None:
        bus = EventBus()
        trigger = TerminalTrigger()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.*", handler)

        result = await trigger._process_line("/quit", bus)

        assert result == "quit"
        assert len(received) == 0  # quit does not publish

    @pytest.mark.anyio
    async def test_exit_command(self) -> None:
        bus = EventBus()
        trigger = TerminalTrigger()

        result = await trigger._process_line("/exit", bus)

        assert result == "quit"

    @pytest.mark.anyio
    async def test_q_command(self) -> None:
        bus = EventBus()
        trigger = TerminalTrigger()

        result = await trigger._process_line("/q", bus)

        assert result == "quit"

    @pytest.mark.anyio
    async def test_quit_case_insensitive(self) -> None:
        bus = EventBus()
        trigger = TerminalTrigger()

        result = await trigger._process_line("/QUIT", bus)

        assert result == "quit"

    @pytest.mark.anyio
    async def test_quit_with_surrounding_whitespace(self) -> None:
        bus = EventBus()
        trigger = TerminalTrigger()

        result = await trigger._process_line("  /quit  ", bus)

        assert result == "quit"

    @pytest.mark.anyio
    async def test_line_stripped_before_publish(self) -> None:
        bus = EventBus()
        trigger = TerminalTrigger()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.terminal", handler)

        await trigger._process_line("  hello  ", bus)

        assert received[0].payload == {"text": "hello"}

    @pytest.mark.anyio
    async def test_multiple_lines_publish_multiple_events(
        self,
    ) -> None:
        bus = EventBus()
        trigger = TerminalTrigger()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.terminal", handler)

        await trigger._process_line("first", bus)
        await trigger._process_line("second", bus)

        assert len(received) == 2
        assert received[0].payload == {"text": "first"}
        assert received[1].payload == {"text": "second"}


class TestQuitCommands:
    """Verify the QUIT_COMMANDS constant."""

    def test_quit_commands_contains_expected(self) -> None:
        assert "/quit" in QUIT_COMMANDS
        assert "/exit" in QUIT_COMMANDS
        assert "/q" in QUIT_COMMANDS

    def test_quit_commands_is_frozenset(self) -> None:
        assert isinstance(QUIT_COMMANDS, frozenset)


class TestTriggerABC:
    """Test Trigger ABC interface compliance."""

    def test_terminal_trigger_is_trigger(self) -> None:
        from proctor.triggers.base import Trigger

        trigger = TerminalTrigger()
        assert isinstance(trigger, Trigger)

    def test_cannot_instantiate_abc(self) -> None:
        from proctor.triggers.base import Trigger

        with pytest.raises(TypeError):
            Trigger()  # type: ignore[abstract]


class TestTerminalTriggerState:
    """Test initial state and stop behavior."""

    def test_initial_state(self) -> None:
        trigger = TerminalTrigger()
        assert trigger._running is False
        assert trigger._task is None

    @pytest.mark.anyio
    async def test_stop_when_not_started(self) -> None:
        trigger = TerminalTrigger()
        await trigger.stop()  # should not raise
        assert trigger._running is False


class TestPublicExports:
    def test_import_from_triggers_package(self) -> None:
        from proctor.triggers import TerminalTrigger, Trigger

        assert TerminalTrigger is not None
        assert Trigger is not None
