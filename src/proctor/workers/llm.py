"""LiteLLM-backed llm_call with retry + fallback + per-call persistence.

Exposes build_llm_call(config, memory) -> Callable[[str], Awaitable[str]]
compatible with the LLMCall alias in bootstrap.py. Reads contextvars
task_id_ctx / step_id_ctx / episode_id_ctx to tag persisted records.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from time import monotonic
from typing import Any

import litellm

from proctor.core.config import LLMConfig
from proctor.core.memory import EpisodicMemory
from proctor.core.models import LLMCallRecord

logger = logging.getLogger(__name__)

task_id_ctx: ContextVar[str | None] = ContextVar("task_id_ctx", default=None)
step_id_ctx: ContextVar[str | None] = ContextVar("step_id_ctx", default=None)
episode_id_ctx: ContextVar[str | None] = ContextVar("episode_id_ctx", default=None)

LLMCall = Callable[[str], Awaitable[str]]


def build_llm_call(config: LLMConfig, memory: EpisodicMemory) -> LLMCall:
    """Return a closure capturing config + memory.

    The returned callable has signature
    ``async def _call(prompt: str, model: str | None = None) -> str``.
    Because ``model`` has a default, it is compatible with the single-arg
    ``LLMCall = Callable[[str], Awaitable[str]]`` alias used by
    WorkflowEngine and bootstrap — no caller change required.
    """

    async def _call(prompt: str, model: str | None = None) -> str:
        chosen = model or config.default_model
        start = monotonic()
        # litellm.acompletion is declared as returning
        # Union[ModelResponse, CustomStreamWrapper]. We never pass stream=True,
        # so the result is always ModelResponse — pyrefly can't narrow this.
        resp = await litellm.acompletion(  # type: ignore[misc]
            model=chosen,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.request_timeout,
            num_retries=0,
        )
        latency_ms = int((monotonic() - start) * 1000)
        text, usage = _extract_text_and_usage(resp)
        await _persist(
            memory,
            _record(
                model=chosen,
                fallback_used=False,
                usage=usage,
                latency_ms=latency_ms,
            ),
        )
        return text

    return _call


def _extract_text_and_usage(resp: Any) -> tuple[str, Any]:
    """Pull choices[0].message.content and usage (may be None)."""
    text = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    return text, usage


def _record(
    *,
    model: str,
    fallback_used: bool,
    usage: Any = None,
    latency_ms: int | None = None,
    error: str | None = None,
) -> LLMCallRecord:
    """Construct an LLMCallRecord, extracting token fields from usage."""
    prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
    cache_write_tokens = (
        getattr(usage, "cache_creation_input_tokens", None) if usage else None
    )
    cache_read_tokens = (
        getattr(usage, "cache_read_input_tokens", None) if usage else None
    )
    return LLMCallRecord(
        episode_id=episode_id_ctx.get(),
        task_id=task_id_ctx.get(),
        step_id=step_id_ctx.get(),
        model=model,
        fallback_used=fallback_used,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        latency_ms=latency_ms,
        error=error,
    )


async def _persist(memory: EpisodicMemory, record: LLMCallRecord) -> None:
    """Save record; swallow errors so telemetry never blocks real work."""
    try:
        await memory.save_llm_call(record)
    except Exception:
        logger.warning(
            "Failed to persist LLM call record id=%s model=%s",
            record.id,
            record.model,
            exc_info=True,
        )
