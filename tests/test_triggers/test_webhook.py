"""Tests for WebhookTrigger and its helpers."""

import asyncio
import hashlib
import hmac
import logging
from unittest.mock import MagicMock

import pytest

from proctor.core.bus import EventBus
from proctor.core.config import (
    BearerAuthConfig,
    HMACAuthConfig,
    NoneAuthConfig,
    WebhookConfig,
    WebhookPathConfig,
)
from proctor.triggers.webhook import (
    InflightLimiter,
    WebhookTrigger,
    _safe_headers,
    _verify_auth,
)


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


def _sign_hmac(
    body: bytes,
    secret: str,
    *,
    prefix: str = "sha256=",
) -> str:
    """Build HMAC header value: '<prefix><hex>'."""
    return prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _mock_request(headers: dict[str, str]) -> MagicMock:
    req = MagicMock()
    req.headers = headers
    return req


class TestVerifyAuthHMAC:
    def test_valid_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SECRET", "topsecret")
        body = b'{"x": 1}'
        cfg = HMACAuthConfig(secret_env="TEST_SECRET")
        sig = _sign_hmac(body, "topsecret")
        req = _mock_request({"X-Hub-Signature-256": sig})
        assert _verify_auth(cfg, req, body) is True

    def test_missing_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SECRET", "topsecret")
        cfg = HMACAuthConfig(secret_env="TEST_SECRET")
        req = _mock_request({})
        assert _verify_auth(cfg, req, b"") is False

    def test_bad_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SECRET", "topsecret")
        body = b'{"x": 1}'
        cfg = HMACAuthConfig(secret_env="TEST_SECRET", prefix="v0=")
        sig = _sign_hmac(body, "topsecret", prefix="sha256=")
        req = _mock_request({"X-Hub-Signature-256": sig})
        assert _verify_auth(cfg, req, body) is False

    def test_bad_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SECRET", "topsecret")
        cfg = HMACAuthConfig(secret_env="TEST_SECRET")
        req = _mock_request({"X-Hub-Signature-256": "sha256=" + "a" * 64})
        assert _verify_auth(cfg, req, b"body") is False

    def test_custom_header_and_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-GitHub shape: header name + prefix parameterizable.

        Does NOT make Proctor Slack-compatible: real Slack signs
        'v0:{timestamp}:{body}', not just the body.
        """
        monkeypatch.setenv("TEST_SECRET", "topsecret")
        body = b'{"x": 1}'
        cfg = HMACAuthConfig(
            secret_env="TEST_SECRET",
            header="X-Slack-Signature",
            prefix="v0=",
        )
        sig = _sign_hmac(body, "topsecret", prefix="v0=")
        req = _mock_request({"X-Slack-Signature": sig})
        assert _verify_auth(cfg, req, body) is True

    def test_empty_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SECRET", "topsecret")
        cfg = HMACAuthConfig(secret_env="TEST_SECRET")
        sig = _sign_hmac(b"", "topsecret")
        req = _mock_request({"X-Hub-Signature-256": sig})
        assert _verify_auth(cfg, req, b"") is True


class TestVerifyAuthBearer:
    def test_valid_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_TOKEN", "alpha-beta-gamma")
        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        req = _mock_request({"Authorization": "Bearer alpha-beta-gamma"})
        assert _verify_auth(cfg, req, b"") is True

    def test_case_insensitive_scheme(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RFC 6750 §2.1 — scheme name case-insensitive."""
        monkeypatch.setenv("TEST_TOKEN", "alpha-beta-gamma")
        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        req = _mock_request({"Authorization": "bearer alpha-beta-gamma"})
        assert _verify_auth(cfg, req, b"") is True

    def test_missing_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_TOKEN", "alpha-beta-gamma")
        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        req = _mock_request({})
        assert _verify_auth(cfg, req, b"") is False

    def test_wrong_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_TOKEN", "alpha-beta-gamma")
        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        req = _mock_request({"Authorization": "Bearer wrong"})
        assert _verify_auth(cfg, req, b"") is False

    def test_non_bearer_scheme(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_TOKEN", "alpha-beta-gamma")
        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        req = _mock_request({"Authorization": "Basic xxx"})
        assert _verify_auth(cfg, req, b"") is False


class TestVerifyAuthNone:
    def test_always_true(self) -> None:
        cfg = NoneAuthConfig()
        req = _mock_request({})
        assert _verify_auth(cfg, req, b"") is True


class TestLifecycle:
    async def test_missing_env_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """start() fails fast with list of all missing env vars."""
        monkeypatch.delenv("MISSING_ONE", raising=False)
        monkeypatch.delenv("MISSING_TWO", raising=False)
        cfg = WebhookConfig(
            paths={
                "/webhook/a": WebhookPathConfig(
                    auth=HMACAuthConfig(secret_env="MISSING_ONE"),
                ),
                "/webhook/b": WebhookPathConfig(
                    auth=BearerAuthConfig(secret_env="MISSING_TWO"),
                ),
            },
        )
        trigger = WebhookTrigger(cfg)
        with pytest.raises(RuntimeError, match="MISSING_ONE.*MISSING_TWO"):
            await trigger.start(EventBus())

    async def test_unauthenticated_path_logs_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = WebhookConfig(
            port=0,
            paths={
                "/webhook/open": WebhookPathConfig(auth=NoneAuthConfig()),
            },
        )
        trigger = WebhookTrigger(cfg)
        with caplog.at_level(logging.WARNING, logger="proctor.triggers.webhook"):
            await trigger.start(EventBus())
        try:
            assert any("NO AUTHENTICATION" in r.message for r in caplog.records)
        finally:
            await trigger.stop()

    async def test_idempotent_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = WebhookConfig(
            port=0,
            paths={
                "/webhook/open": WebhookPathConfig(auth=NoneAuthConfig()),
            },
        )
        trigger = WebhookTrigger(cfg)
        await trigger.start(EventBus())
        await trigger.stop()
        await trigger.stop()  # second call must be a no-op

    async def test_bound_port_none_before_start(self) -> None:
        cfg = WebhookConfig(
            port=0,
            paths={
                "/webhook/open": WebhookPathConfig(auth=NoneAuthConfig()),
            },
        )
        trigger = WebhookTrigger(cfg)
        assert trigger.bound_port is None
