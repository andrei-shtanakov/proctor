"""Tests for TelegramTrigger: polling, filtering, event publishing."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proctor.core.bus import EventBus
from proctor.core.config import TelegramConfig
from proctor.core.models import Event
from proctor.triggers.telegram import (
    INITIAL_RETRY_DELAY,
    RETRY_BACKOFF_FACTOR,
    TelegramTrigger,
)


def _make_config(
    bot_token: str = "test-token",
    allowed_chat_ids: list[int] | None = None,
    poll_timeout: int = 1,
) -> TelegramConfig:
    return TelegramConfig(
        bot_token=bot_token,
        allowed_chat_ids=allowed_chat_ids or [],
        poll_timeout=poll_timeout,
    )


def _make_update(
    update_id: int,
    chat_id: int = 100,
    message_id: int = 1,
    text: str = "hello",
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def _make_update_no_message(update_id: int) -> dict[str, Any]:
    return {"update_id": update_id}


def _make_update_no_text(update_id: int, chat_id: int = 100) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id},
        },
    }


class TestTelegramTriggerInit:
    """Test initial state."""

    def test_initial_state(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)
        assert trigger._running is False
        assert trigger._task is None
        assert trigger._session is None
        assert trigger._offset == 0

    def test_is_trigger_subclass(self) -> None:
        from proctor.triggers.base import Trigger

        config = _make_config()
        trigger = TelegramTrigger(config)
        assert isinstance(trigger, Trigger)

    def test_api_url(self) -> None:
        config = _make_config(bot_token="abc123")
        trigger = TelegramTrigger(config)
        assert trigger._api_url.endswith("botabc123")


class TestHandleUpdate:
    """Test _handle_update logic: filtering and event publishing."""

    @pytest.mark.anyio
    async def test_publishes_text_message(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.telegram", handler)

        update = _make_update(update_id=1, chat_id=100, message_id=5, text="hi")
        await trigger._handle_update(update, bus)

        assert len(received) == 1
        assert received[0].type == "trigger.telegram"
        assert received[0].source == "telegram"
        assert received[0].payload == {
            "text": "hi",
            "chat_id": 100,
            "message_id": 5,
        }

    @pytest.mark.anyio
    async def test_updates_offset(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)
        bus = EventBus()

        update = _make_update(update_id=42)
        await trigger._handle_update(update, bus)

        assert trigger._offset == 43

    @pytest.mark.anyio
    async def test_skips_non_message_update(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.*", handler)

        update = _make_update_no_message(update_id=1)
        await trigger._handle_update(update, bus)

        assert len(received) == 0
        assert trigger._offset == 2

    @pytest.mark.anyio
    async def test_skips_non_text_message(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.*", handler)

        update = _make_update_no_text(update_id=1)
        await trigger._handle_update(update, bus)

        assert len(received) == 0

    @pytest.mark.anyio
    async def test_filters_by_allowed_chat_ids(self) -> None:
        config = _make_config(allowed_chat_ids=[200, 300])
        trigger = TelegramTrigger(config)
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.*", handler)

        # chat_id=100 not in allowed list
        update = _make_update(update_id=1, chat_id=100)
        await trigger._handle_update(update, bus)

        assert len(received) == 0

    @pytest.mark.anyio
    async def test_allows_matching_chat_id(self) -> None:
        config = _make_config(allowed_chat_ids=[100, 200])
        trigger = TelegramTrigger(config)
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.*", handler)

        update = _make_update(update_id=1, chat_id=100)
        await trigger._handle_update(update, bus)

        assert len(received) == 1

    @pytest.mark.anyio
    async def test_empty_allowed_list_accepts_all(self) -> None:
        config = _make_config(allowed_chat_ids=[])
        trigger = TelegramTrigger(config)
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.*", handler)

        update = _make_update(update_id=1, chat_id=999)
        await trigger._handle_update(update, bus)

        assert len(received) == 1

    @pytest.mark.anyio
    async def test_multiple_updates_advance_offset(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)
        bus = EventBus()

        for uid in [10, 11, 12]:
            await trigger._handle_update(_make_update(update_id=uid), bus)

        assert trigger._offset == 13


class TestGetUpdates:
    """Test _get_updates HTTP call."""

    @pytest.mark.anyio
    async def test_parses_ok_response(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(
            return_value={
                "ok": True,
                "result": [_make_update(1), _make_update(2)],
            }
        )

        @asynccontextmanager
        async def mock_get(
            url: str, params: dict[str, int]
        ) -> AsyncIterator[AsyncMock]:
            yield mock_resp

        mock_session = MagicMock()
        mock_session.get = mock_get
        trigger._session = mock_session

        updates = await trigger._get_updates()

        assert len(updates) == 2

    @pytest.mark.anyio
    async def test_returns_empty_on_not_ok(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(
            return_value={"ok": False, "description": "Unauthorized"}
        )

        @asynccontextmanager
        async def mock_get(
            url: str, params: dict[str, int]
        ) -> AsyncIterator[AsyncMock]:
            yield mock_resp

        mock_session = MagicMock()
        mock_session.get = mock_get
        trigger._session = mock_session

        updates = await trigger._get_updates()

        assert updates == []

    @pytest.mark.anyio
    async def test_includes_offset_when_set(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)
        trigger._offset = 42

        captured_params: dict[str, Any] = {}

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"ok": True, "result": []})

        @asynccontextmanager
        async def mock_get(
            url: str, params: dict[str, int]
        ) -> AsyncIterator[AsyncMock]:
            captured_params.update(params)
            yield mock_resp

        mock_session = MagicMock()
        mock_session.get = mock_get
        trigger._session = mock_session

        await trigger._get_updates()

        assert captured_params["offset"] == 42
        assert captured_params["timeout"] == 1

    @pytest.mark.anyio
    async def test_no_offset_param_when_zero(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)

        captured_params: dict[str, Any] = {}

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"ok": True, "result": []})

        @asynccontextmanager
        async def mock_get(
            url: str, params: dict[str, int]
        ) -> AsyncIterator[AsyncMock]:
            captured_params.update(params)
            yield mock_resp

        mock_session = MagicMock()
        mock_session.get = mock_get
        trigger._session = mock_session

        await trigger._get_updates()

        assert "offset" not in captured_params


class TestPollLoop:
    """Test _poll_loop behavior including retry/backoff."""

    @pytest.mark.anyio
    async def test_processes_updates_from_get_updates(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)
        bus = EventBus()
        received: list[Event] = []

        async def handler(e: Event) -> None:
            received.append(e)

        bus.subscribe("trigger.telegram", handler)

        call_count = 0

        async def mock_get_updates() -> list[dict]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [_make_update(1, text="first")]
            trigger._running = False
            return []

        trigger._running = True
        with patch.object(trigger, "_get_updates", side_effect=mock_get_updates):
            await trigger._poll_loop(bus)

        assert len(received) == 1
        assert received[0].payload["text"] == "first"

    @pytest.mark.anyio
    async def test_retries_on_client_error(self) -> None:
        import aiohttp

        config = _make_config()
        trigger = TelegramTrigger(config)
        bus = EventBus()

        call_count = 0

        async def mock_get_updates() -> list[dict]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientError("connection failed")
            trigger._running = False
            return []

        trigger._running = True
        with (
            patch.object(trigger, "_get_updates", side_effect=mock_get_updates),
            patch("proctor.triggers.telegram.asyncio.sleep") as mock_sleep,
        ):
            await trigger._poll_loop(bus)

        assert call_count == 2
        mock_sleep.assert_called_once_with(INITIAL_RETRY_DELAY)

    @pytest.mark.anyio
    async def test_backoff_increases_on_repeated_errors(self) -> None:
        import aiohttp

        config = _make_config()
        trigger = TelegramTrigger(config)
        bus = EventBus()

        call_count = 0

        async def mock_get_updates() -> list[dict]:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise aiohttp.ClientError("connection failed")
            trigger._running = False
            return []

        trigger._running = True
        with (
            patch.object(trigger, "_get_updates", side_effect=mock_get_updates),
            patch("proctor.triggers.telegram.asyncio.sleep") as mock_sleep,
        ):
            await trigger._poll_loop(bus)

        assert call_count == 3
        calls = mock_sleep.call_args_list
        assert calls[0].args[0] == INITIAL_RETRY_DELAY
        assert calls[1].args[0] == INITIAL_RETRY_DELAY * RETRY_BACKOFF_FACTOR

    @pytest.mark.anyio
    async def test_retry_resets_after_success(self) -> None:
        import aiohttp

        config = _make_config()
        trigger = TelegramTrigger(config)
        bus = EventBus()

        call_count = 0

        async def mock_get_updates() -> list[dict]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientError("fail")
            if call_count == 2:
                return []  # success, resets retry
            if call_count == 3:
                raise aiohttp.ClientError("fail again")
            trigger._running = False
            return []

        trigger._running = True
        with (
            patch.object(trigger, "_get_updates", side_effect=mock_get_updates),
            patch("proctor.triggers.telegram.asyncio.sleep") as mock_sleep,
        ):
            await trigger._poll_loop(bus)

        # Both errors should use INITIAL_RETRY_DELAY (reset after success)
        calls = mock_sleep.call_args_list
        assert calls[0].args[0] == INITIAL_RETRY_DELAY
        assert calls[1].args[0] == INITIAL_RETRY_DELAY


class TestStartStop:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_session_and_task(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)
        bus = EventBus()

        async def fake_poll(bus: EventBus) -> None:
            await asyncio.sleep(10)

        trigger._poll_loop = fake_poll  # type: ignore[assignment]

        with patch("proctor.triggers.telegram.aiohttp.ClientSession") as mock_cls:
            mock_session = AsyncMock()
            mock_cls.return_value = mock_session
            await trigger.start(bus)

            assert trigger._session is mock_session
            assert trigger._task is not None
            assert trigger._running is True

            await trigger.stop()

            assert trigger._session is None
            assert trigger._task is None
            assert trigger._running is False
            mock_session.close.assert_called_once()

    @pytest.mark.anyio
    async def test_stop_when_not_started(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)
        await trigger.stop()  # should not raise
        assert trigger._running is False

    @pytest.mark.anyio
    async def test_stop_closes_session(self) -> None:
        config = _make_config()
        trigger = TelegramTrigger(config)
        mock_session = AsyncMock()
        trigger._session = mock_session
        trigger._running = True

        await trigger.stop()

        mock_session.close.assert_called_once()
        assert trigger._session is None


class TestPublicExports:
    def test_import_from_triggers_package(self) -> None:
        from proctor.triggers import TelegramTrigger

        assert TelegramTrigger is not None
