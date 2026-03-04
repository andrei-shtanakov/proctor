"""Internal async pub/sub EventBus with wildcard pattern matching."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from uuid import uuid4

import anyio

from proctor.core.models import Event

logger = logging.getLogger(__name__)

Handler = Callable[[Event], Awaitable[None]]


@dataclass
class _Subscription:
    """Single subscription: pattern + async handler + unique ID."""

    pattern: str
    handler: Handler
    id: str = field(default_factory=lambda: str(uuid4()))


class EventBus:
    """Async pub/sub bus with fnmatch wildcard pattern matching.

    Handlers run concurrently via a task group. Exceptions in one
    handler do not affect others or the bus itself.
    """

    def __init__(self) -> None:
        self._subscriptions: list[_Subscription] = []

    def subscribe(self, pattern: str, handler: Handler) -> str:
        """Subscribe handler to events matching pattern.

        Args:
            pattern: fnmatch pattern (e.g. "trigger.*").
            handler: Async callable receiving an Event.

        Returns:
            Subscription ID for later unsubscribe.
        """
        sub = _Subscription(pattern=pattern, handler=handler)
        self._subscriptions.append(sub)
        return sub.id

    def unsubscribe(self, sub_id: str) -> None:
        """Remove a subscription by its ID."""
        self._subscriptions = [s for s in self._subscriptions if s.id != sub_id]

    async def publish(self, event: Event) -> None:
        """Publish event to all matching subscribers.

        Each matching handler runs concurrently in a task group.
        Exceptions are caught and logged per handler.
        """
        async with anyio.create_task_group() as tg:
            for sub in self._subscriptions:
                if fnmatch(event.type, sub.pattern):
                    tg.start_soon(self._safe_call, sub, event)

    async def _safe_call(self, sub: _Subscription, event: Event) -> None:
        """Invoke handler, catching and logging any exception."""
        try:
            await sub.handler(event)
        except Exception:
            logger.exception(
                "Handler error for pattern=%r event=%s",
                sub.pattern,
                event.type,
            )
