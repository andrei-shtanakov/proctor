"""Tests for core data models: Event, Task, Envelope, TaskStatus."""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from proctor.core.models import (
    Envelope,
    Event,
    LLMCallRecord,
    Task,
    TaskStatus,
)


class TestTaskStatus:
    def test_values(self) -> None:
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.ASSIGNED == "assigned"
        assert TaskStatus.RUNNING == "running"
        assert TaskStatus.COMPLETED == "completed"
        assert TaskStatus.FAILED == "failed"

    def test_all_statuses_are_strings(self) -> None:
        for status in TaskStatus:
            assert isinstance(status, str)

    def test_member_count(self) -> None:
        assert len(TaskStatus) == 5


class TestEvent:
    def test_creation_with_required_fields(self) -> None:
        event = Event(type="trigger.terminal", source="terminal")
        assert event.type == "trigger.terminal"
        assert event.source == "terminal"
        assert event.payload == {}

    def test_auto_generated_id(self) -> None:
        e1 = Event(type="test", source="test")
        e2 = Event(type="test", source="test")
        assert e1.id != e2.id
        assert len(e1.id) == 36  # UUID format

    def test_auto_generated_timestamp(self) -> None:
        before = datetime.now(UTC)
        event = Event(type="test", source="test")
        after = datetime.now(UTC)
        assert before <= event.timestamp <= after

    def test_payload(self) -> None:
        event = Event(
            type="trigger.terminal",
            source="terminal",
            payload={"text": "hello"},
        )
        assert event.payload == {"text": "hello"}

    def test_serialization_roundtrip(self) -> None:
        event = Event(
            type="trigger.terminal",
            source="terminal",
            payload={"text": "search RISC-V"},
        )
        data = json.loads(event.model_dump_json())
        restored = Event.model_validate(data)
        assert restored.id == event.id
        assert restored.type == event.type
        assert restored.source == event.source
        assert restored.payload == event.payload

    def test_uniqueness_across_many(self) -> None:
        ids = {Event(type="t", source="s").id for _ in range(100)}
        assert len(ids) == 100


class TestTask:
    def test_defaults(self) -> None:
        task = Task(spec={"prompt": "do something"})
        assert task.status == TaskStatus.PENDING
        assert task.retries == 0
        assert task.trigger_event is None
        assert task.worker_id is None
        assert task.result is None
        assert task.deadline is None

    def test_auto_generated_id(self) -> None:
        t1 = Task()
        t2 = Task()
        assert t1.id != t2.id

    def test_auto_generated_timestamps(self) -> None:
        before = datetime.now(UTC)
        task = Task()
        after = datetime.now(UTC)
        assert before <= task.created_at <= after
        assert before <= task.updated_at <= after

    def test_spec_dict(self) -> None:
        spec = {"prompt": "analyze", "model": "gpt-4"}
        task = Task(spec=spec)
        assert task.spec == spec

    def test_status_values(self) -> None:
        for status in TaskStatus:
            task = Task(status=status)
            assert task.status == status

    def test_optional_fields(self) -> None:
        task = Task(
            trigger_event="evt-123",
            worker_id="worker-1",
            result={"output": "done"},
            deadline=datetime(2026, 12, 31, tzinfo=UTC),
        )
        assert task.trigger_event == "evt-123"
        assert task.worker_id == "worker-1"
        assert task.result == {"output": "done"}
        assert task.deadline is not None

    def test_serialization_roundtrip(self) -> None:
        task = Task(
            spec={"prompt": "test"},
            status=TaskStatus.RUNNING,
            retries=2,
        )
        data = json.loads(task.model_dump_json())
        restored = Task.model_validate(data)
        assert restored.id == task.id
        assert restored.status == TaskStatus.RUNNING
        assert restored.retries == 2
        assert restored.spec == {"prompt": "test"}


class TestEnvelope:
    def test_creation_with_required_fields(self) -> None:
        env = Envelope(type="task.submit", source="core")
        assert env.type == "task.submit"
        assert env.source == "core"
        assert env.payload == {}

    def test_auto_generated_id(self) -> None:
        e1 = Envelope(type="t", source="s")
        e2 = Envelope(type="t", source="s")
        assert e1.id != e2.id

    def test_auto_generated_timestamp(self) -> None:
        before = datetime.now(UTC)
        env = Envelope(type="t", source="s")
        after = datetime.now(UTC)
        assert before <= env.timestamp <= after

    def test_all_optional_fields(self) -> None:
        env = Envelope(
            type="task.submit",
            source="core",
            target="worker-1",
            payload={"task_id": "abc"},
            reply_to="proctor.reply.xyz",
            correlation_id="corr-123",
            ttl_seconds=30,
        )
        assert env.target == "worker-1"
        assert env.reply_to == "proctor.reply.xyz"
        assert env.correlation_id == "corr-123"
        assert env.ttl_seconds == 30
        assert env.payload == {"task_id": "abc"}

    def test_optional_fields_default_none(self) -> None:
        env = Envelope(type="t", source="s")
        assert env.target is None
        assert env.reply_to is None
        assert env.correlation_id is None
        assert env.ttl_seconds is None

    def test_serialization_roundtrip(self) -> None:
        env = Envelope(
            type="mcp.call",
            source="agent-1",
            target="tool-server",
            correlation_id="c-456",
            ttl_seconds=60,
        )
        data = json.loads(env.model_dump_json())
        restored = Envelope.model_validate(data)
        assert restored.id == env.id
        assert restored.type == env.type
        assert restored.correlation_id == "c-456"
        assert restored.ttl_seconds == 60


class TestLLMCallRecord:
    def test_defaults(self) -> None:
        rec = LLMCallRecord(model="claude-sonnet-4-20250514")
        assert rec.id
        assert rec.created_at <= datetime.now(UTC)
        assert rec.model == "claude-sonnet-4-20250514"
        assert rec.fallback_used is False
        assert rec.prompt_tokens is None
        assert rec.completion_tokens is None
        assert rec.cache_read_tokens is None
        assert rec.cache_write_tokens is None
        assert rec.latency_ms is None
        assert rec.error is None
        assert rec.episode_id is None
        assert rec.task_id is None
        assert rec.step_id is None

    def test_all_fields_set(self) -> None:
        rec = LLMCallRecord(
            id="llm-1",
            episode_id="ep-1",
            task_id="task-1",
            step_id="step-a",
            model="ollama/llama3.2",
            fallback_used=True,
            prompt_tokens=120,
            completion_tokens=45,
            cache_read_tokens=10,
            cache_write_tokens=20,
            latency_ms=350,
            error=None,
        )
        assert rec.fallback_used is True
        assert rec.prompt_tokens == 120
        assert rec.cache_write_tokens == 20


class TestPublicExports:
    def test_import_from_core(self) -> None:
        from proctor.core import Envelope, Event, Task, TaskStatus

        assert TaskStatus.PENDING == "pending"
        assert Event is not None
        assert Task is not None
        assert Envelope is not None


class TestEventValidators:
    def test_type_charset_underscores_ok(self) -> None:
        Event(type="trigger.webhook.github", source="x", payload={})
        Event(type="task.completed", source="x", payload={})
        Event(type="routing.binding_failed", source="x", payload={})

    def test_type_charset_uppercase_rejected(self) -> None:
        with pytest.raises(ValueError, match="must match"):
            Event(type="Task.Completed", source="x", payload={})

    def test_type_charset_dash_rejected(self) -> None:
        # After LABS-68 tightening: dashes no longer allowed
        with pytest.raises(ValueError, match="must match"):
            Event(type="my-event.foo", source="x", payload={})

    def test_type_charset_leading_digit_rejected(self) -> None:
        with pytest.raises(ValueError, match="must match"):
            Event(type="9task.completed", source="x", payload={})

    def test_type_charset_wildcards_rejected(self) -> None:
        # Event.type is concrete, not a pattern
        with pytest.raises(ValueError):
            Event(type="trigger.*", source="x", payload={})

    def test_timestamp_must_be_tz_aware(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Event(
                type="test.ok",
                source="x",
                payload={},
                timestamp=datetime.now(),  # naive
            )

    def test_timestamp_must_be_utc(self) -> None:
        non_utc = timezone(timedelta(hours=3))
        with pytest.raises(ValueError, match="UTC"):
            Event(
                type="test.ok",
                source="x",
                payload={},
                timestamp=datetime.now(non_utc),
            )

    def test_payload_simple_dict_ok(self) -> None:
        Event(type="test.ok", source="x", payload={"key": "val", "n": 1})

    def test_payload_datetime_ok_via_pydantic(self) -> None:
        # pydantic handles datetime → iso natively
        Event(
            type="test.ok",
            source="x",
            payload={"ts": datetime.now(UTC)},
        )

    def test_payload_non_serializable_rejected(self) -> None:
        class CustomObj:
            def __init__(self) -> None:
                pass

        with pytest.raises(ValueError, match="not serializable"):
            Event(
                type="test.ok",
                source="x",
                payload={"obj": CustomObj()},
            )
