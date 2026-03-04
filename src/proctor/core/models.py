"""Core data models: Event, Task, Envelope, TaskStatus."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    """Status machine for task lifecycle."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def _uuid() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    """Typed event for inter-component communication."""

    id: str = Field(default_factory=_uuid)
    type: str
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utcnow)


class Task(BaseModel):
    """Work unit with status machine."""

    id: str = Field(default_factory=_uuid)
    status: TaskStatus = TaskStatus.PENDING
    spec: dict[str, Any] = Field(default_factory=dict)
    trigger_event: str | None = None
    worker_id: str | None = None
    result: Any | None = None
    retries: int = 0
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    deadline: datetime | None = None


class Envelope(BaseModel):
    """NATS message wrapper with routing metadata."""

    id: str = Field(default_factory=_uuid)
    type: str
    source: str
    target: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    reply_to: str | None = None
    correlation_id: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)
    ttl_seconds: int | None = None
