"""Unit tests for PendingQueue — pure FIFO with injected clock."""

from datetime import UTC, datetime, timedelta

from proctor.core.models import Task
from proctor.router.models import QueueEntry
from proctor.router.queue import PendingQueue
from proctor.workflow.spec import WorkflowMode, WorkflowSpec

T0 = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _entry(name: str, ttl_seconds: float = 60.0) -> QueueEntry:
    return QueueEntry(
        task=Task(),
        spec=WorkflowSpec(workflow_id=name, mode=WorkflowMode.SIMPLE),
        trigger_source="test",
        enqueued_at=T0,
        expires_at=T0 + timedelta(seconds=ttl_seconds),
        reason="test block",
    )


def test_fifo_order_preserved() -> None:
    q = PendingQueue()
    first, second = _entry("first"), _entry("second")
    q.push(first)
    q.push(second)
    popped = q.pop_admissible(lambda e: True)
    assert [e.spec.workflow_id for e in popped] == ["first", "second"]
    assert len(q) == 0


def test_pop_admissible_keeps_blocked_entries() -> None:
    q = PendingQueue()
    q.push(_entry("blocked"))
    q.push(_entry("ready"))
    popped = q.pop_admissible(lambda e: e.spec.workflow_id == "ready")
    assert [e.spec.workflow_id for e in popped] == ["ready"]
    assert len(q) == 1


def test_pop_expired_returns_each_entry_once() -> None:
    q = PendingQueue()
    q.push(_entry("old", ttl_seconds=10))
    q.push(_entry("fresh", ttl_seconds=120))
    now = T0 + timedelta(seconds=60)
    expired = q.pop_expired(now)
    assert [e.spec.workflow_id for e in expired] == ["old"]
    assert q.pop_expired(now) == []
    assert len(q) == 1


def test_boundary_exactly_at_expiry_is_expired() -> None:
    q = PendingQueue()
    q.push(_entry("edge", ttl_seconds=60))
    expired = q.pop_expired(T0 + timedelta(seconds=60))
    assert len(expired) == 1


def test_empty_queue_pops_nothing() -> None:
    q = PendingQueue()
    assert q.pop_expired(T0) == []
    assert q.pop_admissible(lambda e: True) == []
    assert len(q) == 0


def test_all_entries_blocked_nothing_popped() -> None:
    q = PendingQueue()
    q.push(_entry("a"))
    q.push(_entry("b"))
    assert q.pop_admissible(lambda e: False) == []
    assert len(q) == 2
