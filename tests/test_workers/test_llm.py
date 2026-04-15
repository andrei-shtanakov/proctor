"""Tests for llm_call (LiteLLM-backed with retry + fallback)."""

from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
