"""Tests for WebhookTrigger and its helpers."""

import asyncio

import pytest

from proctor.triggers.webhook import InflightLimiter, _safe_headers


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


class TestSafeHeaders:
    def test_auth_headers_excluded(self) -> None:
        result = _safe_headers(
            {
                "Authorization": "Bearer xyz",
                "X-Hub-Signature-256": "sha256=abc",
                "Stripe-Signature": "t=1,v1=x",
                "Cookie": "sid=1",
                "Proxy-Authorization": "Basic xxx",
            }
        )
        assert result == {}

    def test_safe_headers_preserved(self) -> None:
        result = _safe_headers(
            {
                "Content-Type": "application/json",
                "User-Agent": "test/1.0",
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "abc-123",
                "X-GitHub-Hook-Id": "42",
                "X-Forwarded-For": "1.2.3.4",
                "X-Forwarded-Proto": "https",
                "X-Real-IP": "1.2.3.4",
                "X-Request-Id": "req-1",
                "X-GitLab-Event": "Push Hook",
                "X-Gitlab-Event-UUID": "uuid-1",
            }
        )
        assert result["Content-Type"] == "application/json"
        assert result["X-GitHub-Event"] == "push"
        assert result["X-Forwarded-For"] == "1.2.3.4"
        assert result["X-GitLab-Event"] == "Push Hook"
        assert len(result) == 11

    def test_unknown_header_dropped(self) -> None:
        result = _safe_headers({"X-Custom-Header": "data"})
        assert result == {}

    def test_case_insensitive_match(self) -> None:
        result = _safe_headers({"x-github-event": "push"})
        assert result == {"x-github-event": "push"}
