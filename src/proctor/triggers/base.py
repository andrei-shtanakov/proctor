"""Trigger ABC — base interface for all input adapters."""

from abc import ABC, abstractmethod

from proctor.core.bus import EventBus


class Trigger(ABC):
    """Base class for input adapters that produce events.

    Each trigger reads from an external source (terminal, webhook,
    filesystem, etc.) and publishes Event objects on the EventBus.
    """

    @abstractmethod
    async def start(self, bus: EventBus) -> None:
        """Start listening and publishing events to the bus."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop listening and clean up resources."""
