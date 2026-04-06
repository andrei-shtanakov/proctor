"""Telegram trigger — polls Telegram Bot API and publishes events."""

import asyncio
import contextlib
import logging

import aiohttp

from proctor.core.bus import EventBus
from proctor.core.config import TelegramConfig
from proctor.core.models import Event
from proctor.triggers.base import Trigger

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
INITIAL_RETRY_DELAY = 1.0
MAX_RETRY_DELAY = 60.0
RETRY_BACKOFF_FACTOR = 2.0


class TelegramTrigger(Trigger):
    """Polls Telegram Bot API getUpdates and publishes trigger.telegram events.

    Messages from chats not in allowed_chat_ids are silently dropped
    (when the list is non-empty). Tracks offset to avoid reprocessing.
    """

    def __init__(self, config: TelegramConfig) -> None:
        self._config = config
        self._offset: int = 0
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def _api_url(self) -> str:
        return f"{TELEGRAM_API_BASE}{self._config.bot_token}"

    async def start(self, bus: EventBus) -> None:
        """Create aiohttp session and launch polling task."""
        self._session = aiohttp.ClientSession()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(bus))
        logger.info("TelegramTrigger started")

    async def stop(self) -> None:
        """Cancel polling task and close aiohttp session."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        logger.info("TelegramTrigger stopped")

    async def _poll_loop(self, bus: EventBus) -> None:
        """Long-poll getUpdates, dispatch messages, retry on errors."""
        retry_delay = INITIAL_RETRY_DELAY
        while self._running:
            try:
                updates = await self._get_updates()
                retry_delay = INITIAL_RETRY_DELAY
                for update in updates:
                    await self._handle_update(update, bus)
            except asyncio.CancelledError:
                break
            except aiohttp.ClientError as exc:
                logger.error("Telegram API HTTP error: %s", exc)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * RETRY_BACKOFF_FACTOR, MAX_RETRY_DELAY)
            except Exception:
                logger.exception("Unexpected error in Telegram poll loop")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * RETRY_BACKOFF_FACTOR, MAX_RETRY_DELAY)

    async def _get_updates(self) -> list[dict]:
        """Call Telegram getUpdates endpoint."""
        assert self._session is not None
        url = f"{self._api_url}/getUpdates"
        params: dict[str, int] = {
            "timeout": self._config.poll_timeout,
        }
        if self._offset:
            params["offset"] = self._offset
        async with self._session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if not data.get("ok"):
            logger.error("Telegram API returned ok=false: %s", data)
            return []
        result: list[dict] = data.get("result", [])
        return result

    async def _handle_update(self, update: dict, bus: EventBus) -> None:
        """Process a single update: filter, extract, publish."""
        update_id: int = update["update_id"]
        self._offset = update_id + 1

        message: dict | None = update.get("message")
        if message is None:
            logger.debug("Skipping non-message update %d", update_id)
            return

        chat: dict = message.get("chat", {})
        chat_id: int = chat.get("id", 0)

        if (
            self._config.allowed_chat_ids
            and chat_id not in self._config.allowed_chat_ids
        ):
            logger.debug("Skipping message from disallowed chat %d", chat_id)
            return

        text: str | None = message.get("text")
        if text is None:
            logger.debug("Skipping non-text message in chat %d", chat_id)
            return

        message_id: int = message.get("message_id", 0)

        event = Event(
            type="trigger.telegram",
            source="telegram",
            payload={
                "text": text,
                "chat_id": chat_id,
                "message_id": message_id,
            },
        )
        await bus.publish(event)
        logger.debug("Published telegram event: %s (chat=%d)", event.id, chat_id)
