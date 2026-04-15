"""Tests for the Router component."""

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from proctor.core.bus import EventBus
from proctor.core.config import RouteRule
from proctor.core.models import Event
from proctor.core.router import Router, _resolve_path
from proctor.workflow.spec import WorkflowMode, WorkflowSpec


class TestResolvePath:
    def test_top_level_str(self) -> None:
        value, reason = _resolve_path({"text": "hi"}, "text")
        assert value == "hi"
        assert reason is None

    def test_top_level_missing(self) -> None:
        value, reason = _resolve_path({}, "text")
        assert value is None
        assert reason is not None
        assert "top-level key 'text' missing" in reason

    def test_nested_str(self) -> None:
        payload: dict[str, Any] = {"message": {"text": "hi"}}
        value, reason = _resolve_path(payload, "message.text")
        assert value == "hi"
        assert reason is None

    def test_intermediate_missing(self) -> None:
        value, reason = _resolve_path({"other": {}}, "message.text")
        assert value is None
        assert reason is not None
        assert "'message'" in reason
        assert "missing" in reason

    def test_intermediate_not_dict(self) -> None:
        value, reason = _resolve_path({"message": "hi"}, "message.text")
        assert value is None
        assert reason is not None
        assert "not a dict" in reason
        assert "'message'" in reason

    def test_terminal_non_string(self) -> None:
        value, reason = _resolve_path({"chat_id": 123}, "chat_id")
        assert value is None
        assert reason is not None
        assert "int" in reason
        assert "expected str" in reason

    def test_terminal_none(self) -> None:
        value, reason = _resolve_path({"text": None}, "text")
        assert value is None
        assert reason is not None
        assert "expected str" in reason


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def bus() -> AsyncGenerator[EventBus]:
    b = EventBus()
    yield b


class TestRouterHappyPath:
    @pytest.mark.anyio
    async def test_matched_literal_prompt(self, bus: EventBus) -> None:
        workflows = {
            "heartbeat": WorkflowSpec(
                workflow_id="heartbeat", mode=WorkflowMode.SIMPLE
            ),
        }
        routes = [
            RouteRule(
                event_pattern="trigger.scheduler",
                workflow_id="heartbeat",
                prompt="Check system status",
            ),
        ]
        router = Router(bus=bus, routes=routes, workflows=workflows)

        event = Event(
            type="trigger.scheduler",
            source="scheduler:heartbeat",
            payload={},
        )
        spec = await router.route(event)

        assert spec is not None
        assert spec.workflow_id == "heartbeat"
        assert spec.mode == WorkflowMode.SIMPLE
        assert spec.prompt == "Check system status"

    @pytest.mark.anyio
    async def test_prompt_from_payload_resolves(self, bus: EventBus) -> None:
        workflows = {
            "chat": WorkflowSpec(workflow_id="chat", mode=WorkflowMode.SIMPLE),
        }
        routes = [
            RouteRule(
                event_pattern="trigger.telegram",
                workflow_id="chat",
                prompt_from_payload="text",
            ),
        ]
        router = Router(bus=bus, routes=routes, workflows=workflows)

        event = Event(
            type="trigger.telegram",
            source="telegram",
            payload={"text": "hello", "chat_id": 1},
        )
        spec = await router.route(event)

        assert spec is not None
        assert spec.prompt == "hello"

    @pytest.mark.anyio
    async def test_prompt_from_payload_nested(self, bus: EventBus) -> None:
        workflows = {
            "chat": WorkflowSpec(workflow_id="chat", mode=WorkflowMode.SIMPLE),
        }
        routes = [
            RouteRule(
                event_pattern="trigger.telegram",
                workflow_id="chat",
                prompt_from_payload="message.text",
            ),
        ]
        router = Router(bus=bus, routes=routes, workflows=workflows)

        event = Event(
            type="trigger.telegram",
            source="telegram",
            payload={"message": {"text": "nested hello"}},
        )
        spec = await router.route(event)

        assert spec is not None
        assert spec.prompt == "nested hello"

    @pytest.mark.anyio
    async def test_first_match_wins(self, bus: EventBus) -> None:
        workflows = {
            "first": WorkflowSpec(workflow_id="first", mode=WorkflowMode.SIMPLE),
            "second": WorkflowSpec(workflow_id="second", mode=WorkflowMode.SIMPLE),
        }
        routes = [
            RouteRule(
                event_pattern="trigger.telegram",
                workflow_id="first",
                prompt_from_payload="text",
            ),
            RouteRule(
                event_pattern="trigger.*",
                workflow_id="second",
                prompt_from_payload="text",
            ),
        ]
        router = Router(bus=bus, routes=routes, workflows=workflows)

        event = Event(
            type="trigger.telegram",
            source="telegram",
            payload={"text": "hi"},
        )
        spec = await router.route(event)

        assert spec is not None
        assert spec.workflow_id == "first"

    @pytest.mark.anyio
    async def test_model_copy_preserves_other_fields(self, bus: EventBus) -> None:
        """Cloning via model_copy preserves mode, steps, and other
        catalog fields — only prompt is overridden."""
        from proctor.workflow.spec import Step

        workflows = {
            "pipeline": WorkflowSpec(
                workflow_id="pipeline",
                mode=WorkflowMode.DAG,
                description="original description",
                steps=[Step(id="a")],
            ),
        }
        routes = [
            RouteRule(
                event_pattern="trigger.terminal",
                workflow_id="pipeline",
                prompt="go",
            ),
        ]
        router = Router(bus=bus, routes=routes, workflows=workflows)

        event = Event(type="trigger.terminal", source="terminal", payload={})
        spec = await router.route(event)

        assert spec is not None
        assert spec.mode == WorkflowMode.DAG
        assert spec.description == "original description"
        assert len(spec.steps) == 1
        assert spec.prompt == "go"


class TestRouterUnmatched:
    @pytest.mark.anyio
    async def test_unmatched_publishes_routing_unmatched(
        self,
        bus: EventBus,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        captured: list[Event] = []

        async def capture(event: Event) -> None:
            captured.append(event)

        bus.subscribe("routing.*", capture)

        workflows = {
            "chat": WorkflowSpec(workflow_id="chat", mode=WorkflowMode.SIMPLE),
        }
        routes = [
            RouteRule(
                event_pattern="trigger.terminal",
                workflow_id="chat",
                prompt_from_payload="text",
            ),
        ]
        router = Router(bus=bus, routes=routes, workflows=workflows)

        original = Event(
            type="trigger.webhook",
            source="webhook",
            payload={"body": "x"},
        )

        with caplog.at_level(logging.WARNING, logger="proctor.core.router"):
            spec = await router.route(original)

        assert spec is None
        assert len(captured) == 1
        rout = captured[0]
        assert rout.type == "routing.unmatched"
        assert rout.source == "router"
        assert rout.payload["original_event_id"] == original.id
        assert rout.payload["original_type"] == "trigger.webhook"
        assert rout.payload["original_source"] == "webhook"
        assert rout.payload["original_payload"] == {"body": "x"}
        assert "original_timestamp" in rout.payload

        # WARNING log includes tried patterns
        assert any(
            "trigger.terminal" in r.message
            for r in caplog.records
            if r.levelname == "WARNING"
        )
