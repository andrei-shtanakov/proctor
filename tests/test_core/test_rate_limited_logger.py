"""Tests for _RateLimitedLogger."""

import logging

import pytest

from proctor.core.transport.local import _RateLimitedLogger

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestRateLimitedLogger:
    async def test_first_log_emits(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = logging.getLogger("test.rl")
        rl = _RateLimitedLogger(logger, interval=10.0)
        with caplog.at_level(logging.WARNING, logger="test.rl"):
            await rl.warn("key1", "first message")
        assert any("first message" in r.message for r in caplog.records)

    async def test_suppresses_within_interval(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = logging.getLogger("test.rl2")
        rl = _RateLimitedLogger(logger, interval=10.0)
        with caplog.at_level(logging.WARNING, logger="test.rl2"):
            await rl.warn("key2", "msg")
            await rl.warn("key2", "msg")
            await rl.warn("key2", "msg")
        records = [r for r in caplog.records if "msg" in r.message]
        assert len(records) == 1

    async def test_emits_after_interval(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fake_time = [0.0]

        def fake_monotonic() -> float:
            return fake_time[0]

        logger = logging.getLogger("test.rl3")
        rl = _RateLimitedLogger(logger, interval=10.0, time_fn=fake_monotonic)
        with caplog.at_level(logging.WARNING, logger="test.rl3"):
            await rl.warn("key3", "msg")
            fake_time[0] = 11.0
            await rl.warn("key3", "msg")
        records = [r for r in caplog.records if "msg" in r.message]
        assert len(records) == 2

    async def test_aggregates_count(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fake_time = [0.0]

        def fake_monotonic() -> float:
            return fake_time[0]

        logger = logging.getLogger("test.rl4")
        rl = _RateLimitedLogger(logger, interval=10.0, time_fn=fake_monotonic)
        with caplog.at_level(logging.WARNING, logger="test.rl4"):
            for _ in range(5):
                await rl.warn("key4", "m")
            fake_time[0] = 11.0
            await rl.warn("key4", "m")
        emitted = [r for r in caplog.records if "m" in r.message]
        assert len(emitted) == 2
        # Second emission includes suppressed count
        assert "5" in emitted[1].message

    async def test_different_keys_independent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = logging.getLogger("test.rl5")
        rl = _RateLimitedLogger(logger, interval=10.0)
        with caplog.at_level(logging.WARNING, logger="test.rl5"):
            await rl.warn("a", "msg_a")
            await rl.warn("b", "msg_b")
        assert any("msg_a" in r.message for r in caplog.records)
        assert any("msg_b" in r.message for r in caplog.records)
