"""Integration test against a locally running Ollama server.

Skipped automatically when Ollama is not reachable at localhost:11434.
Run explicitly with: uv run pytest -m integration
"""

import asyncio
from pathlib import Path

import aiohttp
import pytest

from proctor.core.config import LLMConfig
from proctor.core.memory import EpisodicMemory
from proctor.workers.llm import build_llm_call

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ollama_up_sync() -> bool:
    """Check if Ollama is reachable (synchronous check)."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:

            async def check() -> bool:
                try:
                    async with (
                        aiohttp.ClientSession() as session,
                        session.get(
                            "http://localhost:11434/api/tags",
                            timeout=aiohttp.ClientTimeout(total=2),
                        ) as resp,
                    ):
                        return resp.status == 200
                except Exception:
                    return False

            return loop.run_until_complete(check())
        finally:
            loop.close()
    except Exception:
        return False


@pytest.mark.skipif(
    not _ollama_up_sync(),
    reason="Ollama not running at localhost:11434",
)
async def test_ollama_real_call(tmp_path: Path) -> None:
    """Test LLM call against real Ollama server.

    Note: Requires Ollama to be running AND have at least one model loaded.
    Use `ollama pull llama3.2` to load a model if needed.
    """
    cfg = LLMConfig(
        default_model="ollama/llama3.2",
        fallback_model=None,
        max_retries=0,
        request_timeout=30.0,
    )
    memory = EpisodicMemory(tmp_path / "episodes.db")
    await memory.initialize()
    try:
        call = build_llm_call(cfg, memory)
        result = await call("Say 'hello' and nothing else.")
        assert result.strip()  # non-empty response
    except RuntimeError as e:
        if "failed with transient error" in str(e):
            pytest.skip(f"Ollama model unavailable: {e}")
        raise
    finally:
        await memory.close()
