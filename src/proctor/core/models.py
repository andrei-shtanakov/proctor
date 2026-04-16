"""Core data models: Event, Task, Envelope, TaskStatus."""

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter, field_validator


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

    _TYPE_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*"
    )

    id: str = Field(default_factory=_uuid)
    type: str
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utcnow)

    @field_validator("type")
    @classmethod
    def _type_charset(cls, v: str) -> str:
        if not cls._TYPE_RE.fullmatch(v):
            raise ValueError(
                f"Event.type {v!r} must match [a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*"
            )
        return v

    @field_validator("timestamp")
    @classmethod
    def _timestamp_must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("Event.timestamp must be timezone-aware (UTC)")
        if v.utcoffset() != timedelta(0):
            raise ValueError("Event.timestamp must be in UTC (offset=0)")
        return v

    @field_validator("payload")
    @classmethod
    def _payload_serializable(cls, v: dict[str, Any]) -> dict[str, Any]:
        try:
            TypeAdapter(dict[str, Any]).dump_json(v)
        except Exception as e:
            raise ValueError(f"Event.payload not serializable by pydantic: {e}") from e
        return v


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


class Episode(BaseModel):
    """Record of a single agent interaction."""

    id: str = Field(default_factory=_uuid)
    timestamp: datetime = Field(default_factory=_utcnow)
    trigger_type: str
    user_input: str
    agent_response: str
    workflow_result: dict[str, Any] | None = None


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


class LLMCallRecord(BaseModel):
    """Record of a single LLM API call attempt (success or failure)."""

    id: str = Field(default_factory=_uuid)
    episode_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    model: str
    fallback_used: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
