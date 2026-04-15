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
