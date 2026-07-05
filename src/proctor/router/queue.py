"""PendingQueue — FIFO of blocked tasks with injected clock.

Pure data structure: no I/O, no clock reads. The caller passes ``now``
and the admission predicate; TaskRouter owns all side effects.
"""

from collections.abc import Callable
from datetime import datetime

from proctor.router.models import QueueEntry


class PendingQueue:
    """FIFO queue of QueueEntry with TTL expiry."""

    def __init__(self) -> None:
        self._entries: list[QueueEntry] = []

    def __len__(self) -> int:
        return len(self._entries)

    def push(self, entry: QueueEntry) -> None:
        """Append an entry at the tail."""
        self._entries.append(entry)

    def pop_expired(self, now: datetime) -> list[QueueEntry]:
        """Remove and return entries whose ``expires_at`` <= now."""
        expired = [e for e in self._entries if e.expires_at <= now]
        self._entries = [e for e in self._entries if e.expires_at > now]
        return expired

    def pop_admissible(
        self, try_admit: Callable[[QueueEntry], bool]
    ) -> list[QueueEntry]:
        """Scan FIFO; remove and return entries ``try_admit`` accepts.

        ``try_admit`` is expected to commit a reservation when it
        returns True (TaskRouter passes a reserving closure), so
        later entries see the effect of earlier admissions.
        """
        admitted: list[QueueEntry] = []
        remaining: list[QueueEntry] = []
        for entry in self._entries:
            if try_admit(entry):
                admitted.append(entry)
            else:
                remaining.append(entry)
        self._entries = remaining
        return admitted
