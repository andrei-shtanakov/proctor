"""Tests for WebhookTrigger and its helpers."""

import asyncio

import pytest

from proctor.triggers.webhook import InflightLimiter


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


pytestmark = pytest.mark.anyio


class TestInflightLimiter:
    async def test_acquire_under_limit(self) -> None:
        lim = InflightLimiter(limit=3)
        assert await lim.try_acquire() is True
        assert lim.in_flight == 1

    async def test_acquire_at_limit_returns_false(self) -> None:
        lim = InflightLimiter(limit=2)
        assert await lim.try_acquire() is True
        assert await lim.try_acquire() is True
        assert await lim.try_acquire() is False
        assert lim.in_flight == 2

    async def test_release_signals_idle(self) -> None:
        lim = InflightLimiter(limit=2)
        assert await lim.try_acquire() is True
        assert await lim.wait_idle(0.01) is False
        await lim.release()
        assert await lim.wait_idle(0.5) is True

    async def test_wait_idle_times_out_while_busy(self) -> None:
        lim = InflightLimiter(limit=1)
        assert await lim.try_acquire() is True
        assert await lim.wait_idle(0.05) is False

    async def test_concurrent_acquire_release(self) -> None:
        lim = InflightLimiter(limit=10)

        async def acquire_release() -> None:
            acquired = await lim.try_acquire()
            assert acquired is True
            await asyncio.sleep(0.01)
            await lim.release()

        await asyncio.gather(*[acquire_release() for _ in range(10)])
        assert lim.in_flight == 0
        assert await lim.wait_idle(0.5) is True
