"""Tests for llm_call (LiteLLM-backed with retry + fallback)."""

from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from proctor.core.config import LLMConfig
from proctor.core.memory import EpisodicMemory
from proctor.workers.llm import build_llm_call

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """aiosqlite only supports asyncio."""
    return "asyncio"


@pytest.fixture
async def memory(tmp_path: Path) -> AsyncGenerator[EpisodicMemory]:
    mem = EpisodicMemory(tmp_path / "episodes.db")
    await mem.initialize()
    yield mem
    await mem.close()


def _make_response(
    content: str = "hi",
    prompt_tokens: int | None = 10,
    completion_tokens: int | None = 5,
    cache_creation: int | None = None,
    cache_read: int | None = None,
) -> SimpleNamespace:
    """Build a SimpleNamespace mimicking litellm's ModelResponse."""
    usage_fields: dict[str, Any] = {}
    if prompt_tokens is not None:
        usage_fields["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        usage_fields["completion_tokens"] = completion_tokens
    if cache_creation is not None:
        usage_fields["cache_creation_input_tokens"] = cache_creation
    if cache_read is not None:
        usage_fields["cache_read_input_tokens"] = cache_read
    usage = SimpleNamespace(**usage_fields) if usage_fields else None
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage)


async def _fetch_rows(memory: EpisodicMemory) -> list[dict[str, Any]]:
    assert memory._db is not None
    cursor = await memory._db.execute("SELECT * FROM llm_calls ORDER BY created_at ASC")
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


class TestHappyPath:
    async def test_happy_path_returns_text_and_writes_row(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
            return _make_response(content="hello world")

        monkeypatch.setattr("proctor.workers.llm.litellm.acompletion", fake_acompletion)

        cfg = LLMConfig()
        call = build_llm_call(cfg, memory)
        result = await call("Say hi")

        assert result == "hello world"
        rows = await _fetch_rows(memory)
        assert len(rows) == 1
        row = rows[0]
        assert row["model"] == cfg.default_model
        assert row["fallback_used"] == 0
        assert row["error"] is None
        assert row["prompt_tokens"] == 10
        assert row["completion_tokens"] == 5

    async def test_explicit_model_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        seen_models: list[str] = []

        async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
            seen_models.append(kwargs["model"])
            return _make_response()

        monkeypatch.setattr("proctor.workers.llm.litellm.acompletion", fake_acompletion)

        cfg = LLMConfig(default_model="claude-sonnet-4-20250514")
        call = build_llm_call(cfg, memory)
        # LLMCall alias is single-arg (Callable[[str], Awaitable[str]]).
        # The closure's optional `model` kwarg exists at runtime but isn't
        # in the declared type. A Protocol alias would remove this ignore.
        await call("hi", model="gpt-4o")  # type: ignore[call-arg]

        assert seen_models == ["gpt-4o"]
        rows = await _fetch_rows(memory)
        assert rows[0]["model"] == "gpt-4o"


class TestRetry:
    async def test_retry_then_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        calls: list[int] = []

        async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
            calls.append(1)
            if len(calls) == 1:
                raise litellm.RateLimitError("429", model="m", llm_provider="test")
            return _make_response(content="ok after retry")

        monkeypatch.setattr("proctor.workers.llm.litellm.acompletion", fake_acompletion)

        # Use a no-op sleep to keep the test fast
        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("proctor.workers.llm.anyio.sleep", no_sleep)

        cfg = LLMConfig(max_retries=1, fallback_model=None)
        call = build_llm_call(cfg, memory)
        result = await call("hi")

        assert result == "ok after retry"
        assert len(calls) == 2
        rows = await _fetch_rows(memory)
        assert len(rows) == 2
        assert rows[0]["error"] is not None  # first attempt failed
        assert rows[0]["fallback_used"] == 0
        assert rows[1]["error"] is None  # second attempt succeeded
        assert rows[1]["fallback_used"] == 0
        assert rows[1]["model"] == cfg.default_model


class TestFallback:
    async def test_fallback_transient_primary_fails_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        models_called: list[str] = []

        async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
            models_called.append(kwargs["model"])
            if kwargs["model"] == "primary":
                raise litellm.RateLimitError(
                    "429", model="primary", llm_provider="test"
                )
            return _make_response(content="fallback ok")

        monkeypatch.setattr("proctor.workers.llm.litellm.acompletion", fake_acompletion)

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("proctor.workers.llm.anyio.sleep", no_sleep)

        cfg = LLMConfig(
            default_model="primary",
            fallback_model="fallback",
            max_retries=0,
        )
        call = build_llm_call(cfg, memory)
        result = await call("hi")

        assert result == "fallback ok"
        assert models_called == ["primary", "fallback"]
        rows = await _fetch_rows(memory)
        assert len(rows) == 2
        assert rows[0]["error"] is not None
        assert rows[0]["fallback_used"] == 0
        assert rows[0]["model"] == "primary"
        assert rows[1]["error"] is None
        assert rows[1]["fallback_used"] == 1
        assert rows[1]["model"] == "fallback"

    async def test_retries_exhausted_then_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        primary_attempts = 0

        async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
            nonlocal primary_attempts
            if kwargs["model"] == "primary":
                primary_attempts += 1
                raise litellm.Timeout("slow", model="primary", llm_provider="test")
            return _make_response(content="fallback result")

        monkeypatch.setattr("proctor.workers.llm.litellm.acompletion", fake_acompletion)

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("proctor.workers.llm.anyio.sleep", no_sleep)

        cfg = LLMConfig(
            default_model="primary",
            fallback_model="fallback",
            max_retries=1,
        )
        call = build_llm_call(cfg, memory)
        result = await call("hi")

        assert result == "fallback result"
        assert primary_attempts == 2
        rows = await _fetch_rows(memory)
        assert len(rows) == 3
        assert [r["model"] for r in rows] == ["primary", "primary", "fallback"]
        assert [r["error"] is None for r in rows] == [False, False, True]
        assert [r["fallback_used"] for r in rows] == [0, 0, 1]

    async def test_fallback_itself_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
            if kwargs["model"] == "primary":
                raise litellm.RateLimitError(
                    "429", model="primary", llm_provider="test"
                )
            raise litellm.APIConnectionError(
                "down", model="fallback", llm_provider="test"
            )

        monkeypatch.setattr("proctor.workers.llm.litellm.acompletion", fake_acompletion)

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("proctor.workers.llm.anyio.sleep", no_sleep)

        cfg = LLMConfig(
            default_model="primary",
            fallback_model="fallback",
            max_retries=0,
        )
        call = build_llm_call(cfg, memory)

        with pytest.raises(litellm.APIConnectionError):
            await call("hi")

        rows = await _fetch_rows(memory)
        assert [r["model"] for r in rows] == ["primary", "fallback"]
        assert [r["error"] is not None for r in rows] == [True, True]
        assert [r["fallback_used"] for r in rows] == [0, 1]

    async def test_fallback_model_none_raises_runtime_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        memory: EpisodicMemory,
    ) -> None:
        fallback_calls = 0

        async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
            nonlocal fallback_calls
            if kwargs["model"] != "primary":
                fallback_calls += 1
            raise litellm.RateLimitError(
                "429", model=kwargs["model"], llm_provider="test"
            )

        monkeypatch.setattr("proctor.workers.llm.litellm.acompletion", fake_acompletion)

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("proctor.workers.llm.anyio.sleep", no_sleep)

        cfg = LLMConfig(default_model="primary", fallback_model=None, max_retries=1)
        call = build_llm_call(cfg, memory)

        with pytest.raises(RuntimeError, match="fallback_model is not configured"):
            await call("hi")

        assert fallback_calls == 0
        rows = await _fetch_rows(memory)
        assert len(rows) == 2  # both primary attempts recorded
        assert all(r["model"] == "primary" for r in rows)
