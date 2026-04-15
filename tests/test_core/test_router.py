"""Tests for the Router component."""

from typing import Any

from proctor.core.router import _resolve_path


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
