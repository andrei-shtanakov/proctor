# WebhookTrigger (LABS-66) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an HTTP webhook trigger — aiohttp server with per-path HMAC / Bearer / none authentication — that publishes `trigger.webhook.<source_name>` events on the bus with fire-and-forget semantics.

**Architecture:** `WebhookTrigger(Trigger)` hosts a per-path-registered aiohttp application. Handler runs `try_acquire → read body → verify auth → parse JSON → _safe_headers whitelist → Event → bus.publish → 202 Accepted` inside an outer try/except that turns any unexpected exception into 503 (never 500). `InflightLimiter` provides memory-safe admission control; a graceful `stop()` drains HTTP handlers with a timeout. LABS-65 Router routes `trigger.webhook.*` events to catalog workflows via YAML — no code coupling between this trigger and routing.

**Tech Stack:** Python 3.12, pydantic 2.x (discriminated unions, `extra="forbid"`), aiohttp 3.x (`web.Application`, `TCPSite`, `AppRunner`), asyncio (Event, Lock, wait_for — intentionally not anyio because Proctor de facto runs on asyncio), pytest + pytest-anyio, `hmac.compare_digest` for constant-time comparison.

**Spec:** [`docs/superpowers/specs/2026-04-15-webhook-trigger-design.md`](../specs/2026-04-15-webhook-trigger-design.md)

---

## File Structure

### New files

- `src/proctor/triggers/webhook.py` — `WebhookTrigger`, `InflightLimiter`, `_safe_headers`, `_verify_auth`, `_AUTH_REASONS`, `_SAFE_HEADER_NAMES`, `_SAFE_HEADER_PREFIXES`.
- `tests/test_triggers/test_webhook.py` — ~40 tests.

### Modified files

- `src/proctor/core/config.py` — new `HMACAuthConfig`, `BearerAuthConfig`, `NoneAuthConfig`, `AuthConfig` (discriminated union), `WebhookPathConfig`, `WebhookConfig`. Add `webhook: WebhookConfig | None = None` to `ProctorConfig`.
- `src/proctor/core/bootstrap.py` — `self._webhook_trigger: WebhookTrigger | None = None`. Start if `config.webhook is not None`. Stop **first** in `Application.stop()`.
- `src/proctor/triggers/__init__.py` — re-export `WebhookTrigger`.
- `tests/test_core/test_config.py` — `TestWebhookConfigValidation` with 8 tests.
- `tests/test_integration.py` — `TestWebhookIntegration` with 2 tests.
- `config/proctor.yaml` — example `webhook:` section.
- `README.md` — `## Webhook trigger`, `## Deployment topologies` subsections, Phase 2 roadmap row update.

---

## Task 1: AuthConfig discriminated union

**Files:**
- Modify: `src/proctor/core/config.py`
- Modify: `tests/test_core/test_config.py`

- [ ] **Step 1.1: Write failing tests**

Append to `tests/test_core/test_config.py`:

```python
from typing import get_args

from proctor.core.config import (
    AuthConfig,
    BearerAuthConfig,
    HMACAuthConfig,
    NoneAuthConfig,
)


class TestAuthConfig:
    def test_hmac_defaults(self) -> None:
        cfg = HMACAuthConfig(secret_env="TEST_SECRET")
        assert cfg.type == "hmac"
        assert cfg.header == "X-Hub-Signature-256"
        assert cfg.prefix == "sha256="

    def test_bearer_defaults(self) -> None:
        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        assert cfg.type == "bearer"
        assert cfg.header == "Authorization"

    def test_none_has_no_fields(self) -> None:
        cfg = NoneAuthConfig()
        assert cfg.type == "none"

    def test_secret_env_format_rejected(self) -> None:
        with pytest.raises(ValueError):
            HMACAuthConfig(secret_env="lowercase")
        with pytest.raises(ValueError):
            BearerAuthConfig(secret_env="")
        with pytest.raises(ValueError):
            HMACAuthConfig(secret_env="123_BAD_START")

    def test_hmac_extra_field_forbidden(self) -> None:
        with pytest.raises(ValueError):
            HMACAuthConfig(secret_env="X", extra_field="bad")  # type: ignore[call-arg]

    def test_bearer_extra_field_forbidden(self) -> None:
        with pytest.raises(ValueError):
            BearerAuthConfig(secret_env="X", header_wrong="bad")  # type: ignore[call-arg]

    def test_none_extra_field_forbidden(self) -> None:
        with pytest.raises(ValueError):
            NoneAuthConfig(secret_env="X")  # type: ignore[call-arg]
```

- [ ] **Step 1.2: Run, confirm failure**

Run: `uv run pytest tests/test_core/test_config.py::TestAuthConfig -v`
Expected: `ImportError` on the new classes.

- [ ] **Step 1.3: Implement auth config classes**

Edit `src/proctor/core/config.py`. Add imports (if `Literal`, `Annotated` not already present):

```python
from typing import Annotated, Literal
```

Check if `ConfigDict` is imported from pydantic; if not, add it:

```python
from pydantic import BaseModel, ConfigDict, Field, model_validator
```

Add classes (a reasonable placement is next to `TelegramConfig`, before `ProctorConfig`):

```python
class HMACAuthConfig(BaseModel):
    """HMAC-SHA256 auth for a webhook path."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["hmac"] = "hmac"
    secret_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    header: str = "X-Hub-Signature-256"
    # Header-value prefix stripped before hex comparison. Configures
    # header SHAPE only, not the HMAC base string — Slack and Stripe
    # sign a constructed string (e.g. "v0:{timestamp}:{body}"), which
    # this implementation does NOT produce. Real Slack/Stripe support
    # needs dedicated auth.type: "slack" / "stripe" — see Risks in
    # docs/superpowers/specs/2026-04-15-webhook-trigger-design.md.
    prefix: str = "sha256="


class BearerAuthConfig(BaseModel):
    """Bearer-token auth for a webhook path (RFC 6750)."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["bearer"] = "bearer"
    secret_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    header: str = "Authorization"


class NoneAuthConfig(BaseModel):
    """No authentication. Explicit opt-in. Triggers startup WARNING."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["none"] = "none"


AuthConfig = Annotated[
    HMACAuthConfig | BearerAuthConfig | NoneAuthConfig,
    Field(discriminator="type"),
]
```

- [ ] **Step 1.4: Run tests, confirm pass**

Run: `uv run pytest tests/test_core/test_config.py::TestAuthConfig -v`
Expected: 7 passed.

- [ ] **Step 1.5: Format + lint + type check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 1.6: Commit**

```bash
git add src/proctor/core/config.py tests/test_core/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): add AuthConfig discriminated union (HMAC/Bearer/none)

Three pydantic variants with extra="forbid" prevent nonsense
cross-type fields. secret_env constrained to ^[A-Z][A-Z0-9_]*$.
NoneAuthConfig has no fields — explicit opt-in to unauthenticated
paths. LABS-66 groundwork.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: WebhookPathConfig + WebhookConfig + validator

**Files:**
- Modify: `src/proctor/core/config.py`
- Modify: `tests/test_core/test_config.py`

- [ ] **Step 2.1: Write failing tests**

Append to `tests/test_core/test_config.py`:

```python
from proctor.core.config import (
    ProctorConfig,
    WebhookConfig,
    WebhookPathConfig,
)


class TestWebhookConfigValidation:
    def test_empty_paths_raises(self) -> None:
        with pytest.raises(ValueError):
            WebhookConfig(paths={})

    def test_valid_single_path(self) -> None:
        cfg = WebhookConfig(
            paths={
                "/webhook/github": WebhookPathConfig(
                    source_name="github",
                    auth=HMACAuthConfig(secret_env="X"),
                ),
            }
        )
        assert "/webhook/github" in cfg.paths
        assert cfg.port == 8080
        assert cfg.max_in_flight == 20
        assert cfg.max_body_bytes == 1_048_576
        assert cfg.shutdown_timeout == 30.0
        assert cfg.keepalive_timeout == 75.0
        assert cfg.host == "127.0.0.1"

    def test_path_must_start_with_slash(self) -> None:
        with pytest.raises(ValueError, match="must start with"):
            WebhookConfig(
                paths={
                    "webhook/github": WebhookPathConfig(
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )

    def test_path_fnmatch_meta_rejected(self) -> None:
        with pytest.raises(ValueError, match="metacharacter"):
            WebhookConfig(
                paths={
                    "/webhook/*": WebhookPathConfig(
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )

    def test_trailing_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="trailing"):
            WebhookConfig(
                paths={
                    "/webhook/github/": WebhookPathConfig(
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )

    def test_source_name_uniqueness_enforced(self) -> None:
        with pytest.raises(ValueError, match="uniqueness"):
            WebhookConfig(
                paths={
                    "/webhook/gh-prod": WebhookPathConfig(
                        source_name="github",
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                    "/webhook/gh-stage": WebhookPathConfig(
                        source_name="github",
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )

    def test_source_name_format_rejected(self) -> None:
        with pytest.raises(ValueError, match="source_name"):
            WebhookConfig(
                paths={
                    "/webhook/GH": WebhookPathConfig(
                        source_name="GitHub",  # uppercase
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                }
            )

    def test_source_name_derived_from_basename(self) -> None:
        cfg = WebhookConfig(
            paths={
                "/webhook/ci": WebhookPathConfig(
                    auth=BearerAuthConfig(secret_env="T"),
                ),
            }
        )
        assert cfg.paths["/webhook/ci"].source_name == "ci"

    def test_port_range_enforced(self) -> None:
        WebhookConfig(
            paths={
                "/webhook/x": WebhookPathConfig(
                    auth=HMACAuthConfig(secret_env="X"),
                ),
            },
            port=0,  # ephemeral — test allowance
        )
        with pytest.raises(ValueError):
            WebhookConfig(
                paths={
                    "/webhook/x": WebhookPathConfig(
                        auth=HMACAuthConfig(secret_env="X"),
                    ),
                },
                port=70000,
            )

    def test_proctor_config_webhook_optional(self) -> None:
        cfg = ProctorConfig()
        assert cfg.webhook is None
```

- [ ] **Step 2.2: Run, confirm failure**

Run: `uv run pytest tests/test_core/test_config.py::TestWebhookConfigValidation -v`
Expected: fails — classes don't exist yet.

- [ ] **Step 2.3: Implement WebhookPathConfig + WebhookConfig**

Add below `AuthConfig` alias:

```python
class WebhookPathConfig(BaseModel):
    """Per-path webhook configuration."""

    model_config = ConfigDict(extra="forbid")
    source_name: str | None = None   # derived from path basename if omitted
    auth: AuthConfig


class WebhookConfig(BaseModel):
    """Webhook trigger configuration (aiohttp server)."""

    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=0, le=65535)
    paths: dict[str, WebhookPathConfig] = Field(min_length=1)
    max_in_flight: int = 20
    max_body_bytes: int = 1_048_576
    shutdown_timeout: float = 30.0
    keepalive_timeout: float = 75.0

    @model_validator(mode="after")
    def _validate_paths(self) -> "WebhookConfig":
        """Validate path format; derive + uniqueness-check source_name."""
        seen_names: dict[str, str] = {}  # source_name → first path that used it
        for path, path_cfg in self.paths.items():
            if not path.startswith("/"):
                raise ValueError(
                    f"webhook path {path!r} must start with '/'"
                )
            if path == "/":
                raise ValueError(
                    f"webhook path {path!r} is not allowed"
                )
            if path.endswith("/"):
                raise ValueError(
                    f"webhook path {path!r} has a trailing '/'"
                )
            if any(c in path for c in "*?["):
                raise ValueError(
                    f"webhook path {path!r} contains a fnmatch "
                    f"metacharacter (*, ?, [)"
                )
            effective = path_cfg.source_name or path.rsplit("/", 1)[-1]
            import re
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", effective):
                raise ValueError(
                    f"webhook path {path!r}: source_name {effective!r} "
                    f"must match ^[a-z][a-z0-9_-]*$"
                )
            if effective in {"", "*", "?"}:
                raise ValueError(
                    f"webhook path {path!r}: source_name {effective!r} "
                    f"is reserved"
                )
            if effective in seen_names:
                raise ValueError(
                    f"webhook source_name uniqueness: {effective!r} "
                    f"used by both {seen_names[effective]!r} and {path!r}"
                )
            seen_names[effective] = path
            # Persist derived source_name for the handler.
            path_cfg.source_name = effective
        return self
```

Add to `ProctorConfig`:

```python
    webhook: WebhookConfig | None = None
```

- [ ] **Step 2.4: Run tests, confirm pass**

Run: `uv run pytest tests/test_core/test_config.py -v`
Expected: all pass.

- [ ] **Step 2.5: Format + lint + type check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean. The `import re` inside a method is unusual — move it to module-level if ruff/pyrefly flags it.

- [ ] **Step 2.6: Commit**

```bash
git add src/proctor/core/config.py tests/test_core/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): add WebhookConfig and WebhookPathConfig

paths: dict[str, WebhookPathConfig] with min_length=1. Validator:
- Paths start with '/', not '/', no trailing '/', no fnmatch metachars.
- source_name defaults to path basename, matches ^[a-z][a-z0-9_-]*$,
  not reserved, unique across paths.
- Derived source_name is persisted on WebhookPathConfig so the
  handler can rely on a non-None value at runtime.
port: Field(ge=0, le=65535). ProctorConfig.webhook optional.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `InflightLimiter` with asyncio primitives

**Files:**
- Create: `src/proctor/triggers/webhook.py`
- Create: `tests/test_triggers/test_webhook.py`

- [ ] **Step 3.1: Write failing unit tests**

Create `tests/test_triggers/test_webhook.py`:

```python
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
        # Not idle while busy.
        assert await lim.wait_idle(0.01) is False
        await lim.release()
        # Idle after release.
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
```

- [ ] **Step 3.2: Run, confirm failure**

Run: `uv run pytest tests/test_triggers/test_webhook.py -v`
Expected: `ImportError` on `proctor.triggers.webhook`.

- [ ] **Step 3.3: Create module with `InflightLimiter`**

Create `src/proctor/triggers/webhook.py`:

```python
"""WebhookTrigger — aiohttp-based HTTP endpoint that publishes
trigger.webhook.<source_name> events on the bus.

Fire-and-forget semantics (202 Accepted), per-path auth
(HMAC / Bearer / none), in-flight admission cap, graceful drain on
stop(). See docs/superpowers/specs/2026-04-15-webhook-trigger-design.md
for the full design.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class InflightLimiter:
    """Counter-based in-flight cap with event-driven idle signalling.

    Uses asyncio primitives (not anyio) because Proctor de facto runs
    on asyncio (aiosqlite, litellm are asyncio-only) and asyncio.Event
    has clear(), which anyio.Event lacks — yielding a simpler,
    race-free reusable idle signal.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._count = 0
        self._lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def in_flight(self) -> int:
        return self._count

    @property
    def limit(self) -> int:
        return self._limit

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self._count >= self._limit:
                return False
            self._count += 1
            self._idle.clear()
            return True

    async def release(self) -> None:
        async with self._lock:
            self._count -= 1
            if self._count == 0:
                self._idle.set()

    async def wait_idle(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False
```

- [ ] **Step 3.4: Run tests, confirm pass**

Run: `uv run pytest tests/test_triggers/test_webhook.py -v`
Expected: 5 passed.

- [ ] **Step 3.5: Format + lint + type check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 3.6: Commit**

```bash
git add src/proctor/triggers/webhook.py tests/test_triggers/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(webhook): add InflightLimiter for admission-cap + drain

asyncio.Event + clear() gives a race-free reusable idle signal.
try_acquire returns False at limit (non-blocking); wait_idle waits
for count to hit zero with a timeout — used by the forthcoming
graceful stop() path.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `_safe_headers` whitelist helper

**Files:**
- Modify: `src/proctor/triggers/webhook.py`
- Modify: `tests/test_triggers/test_webhook.py`

- [ ] **Step 4.1: Write failing tests**

Append to `tests/test_triggers/test_webhook.py`:

```python
from proctor.triggers.webhook import _safe_headers


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
        # Client sends non-standard capitalization; whitelist still matches.
        result = _safe_headers({"x-github-event": "push"})
        assert result == {"x-github-event": "push"}
```

- [ ] **Step 4.2: Run, confirm failure**

Run: `uv run pytest tests/test_triggers/test_webhook.py::TestSafeHeaders -v`
Expected: fails — `_safe_headers` not exported.

- [ ] **Step 4.3: Implement `_safe_headers`**

Edit `src/proctor/triggers/webhook.py`. Add imports at top:

```python
from collections.abc import Mapping
```

Add below `logger` (before `InflightLimiter`):

```python
_SAFE_HEADER_NAMES: frozenset[str] = frozenset({
    "content-type",
    "user-agent",
    "x-real-ip",
    "x-request-id",
    "x-github-event",
    "x-github-delivery",
    "x-github-hook-id",
    "x-gitlab-event",
    "x-gitlab-event-uuid",
})
_SAFE_HEADER_PREFIXES: tuple[str, ...] = ("x-forwarded-",)


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Whitelist-filter request headers before publishing to the bus.

    Auth headers (Authorization, X-Hub-Signature-256, Stripe-Signature,
    Cookie, etc.) MUST be excluded: event.payload is persisted to
    episodes.db; leaking credentials there is a security incident.

    Multi-value headers: last-wins via dict conversion. Duplicate
    headers are rare in webhook traffic; if a future use case needs
    them preserved, switch to list[tuple[str, str]].

    Header casing: keys preserve whatever the client sent (HTTP
    headers are case-insensitive, but Python dicts are not).
    Downstream consumers should use case-insensitive lookup if
    portability across clients matters.
    """
    result: dict[str, str] = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in _SAFE_HEADER_NAMES or kl.startswith(_SAFE_HEADER_PREFIXES):
            result[k] = v
    return result
```

- [ ] **Step 4.4: Run tests, confirm pass**

Run: `uv run pytest tests/test_triggers/test_webhook.py::TestSafeHeaders -v`
Expected: 4 passed.

- [ ] **Step 4.5: Format + lint + type check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 4.6: Commit**

```bash
git add src/proctor/triggers/webhook.py tests/test_triggers/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(webhook): add _safe_headers whitelist filter

Prevents credential leak (Authorization, X-Hub-Signature-256,
Stripe-Signature, Cookie) into event.payload and thus episodes.db.
Whitelist covers Content-Type, User-Agent, X-Forwarded-*, and common
identifier headers from GitHub/GitLab.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `_verify_auth` dispatch (HMAC + Bearer + none)

**Files:**
- Modify: `src/proctor/triggers/webhook.py`
- Modify: `tests/test_triggers/test_webhook.py`

- [ ] **Step 5.1: Write failing tests**

Append to `tests/test_triggers/test_webhook.py`:

```python
import hashlib
import hmac
from unittest.mock import MagicMock

from proctor.core.config import (
    BearerAuthConfig,
    HMACAuthConfig,
    NoneAuthConfig,
)
from proctor.triggers.webhook import _verify_auth


def _sign_hmac(
    body: bytes,
    secret: str,
    *,
    prefix: str = "sha256=",
) -> str:
    """Build HMAC header value: '<prefix><hex>'."""
    return prefix + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


def _mock_request(headers: dict[str, str]) -> MagicMock:
    req = MagicMock()
    req.headers = headers
    return req


class TestVerifyAuthHMAC:
    def test_valid_signature(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_SECRET", "topsecret")
        body = b'{"x": 1}'
        cfg = HMACAuthConfig(secret_env="TEST_SECRET")
        sig = _sign_hmac(body, "topsecret")
        req = _mock_request({"X-Hub-Signature-256": sig})
        assert _verify_auth(cfg, req, body) is True

    def test_missing_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_SECRET", "topsecret")
        cfg = HMACAuthConfig(secret_env="TEST_SECRET")
        req = _mock_request({})
        assert _verify_auth(cfg, req, b"") is False

    def test_bad_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_SECRET", "topsecret")
        body = b'{"x": 1}'
        cfg = HMACAuthConfig(
            secret_env="TEST_SECRET", prefix="v0="
        )
        # Client sends sha256=, config expects v0=.
        sig = _sign_hmac(body, "topsecret", prefix="sha256=")
        req = _mock_request({"X-Hub-Signature-256": sig})
        assert _verify_auth(cfg, req, body) is False

    def test_bad_signature(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_SECRET", "topsecret")
        cfg = HMACAuthConfig(secret_env="TEST_SECRET")
        req = _mock_request(
            {"X-Hub-Signature-256": "sha256=" + "a" * 64}
        )
        assert _verify_auth(cfg, req, b"body") is False

    def test_custom_header_and_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_empty_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_SECRET", "topsecret")
        cfg = HMACAuthConfig(secret_env="TEST_SECRET")
        sig = _sign_hmac(b"", "topsecret")
        req = _mock_request({"X-Hub-Signature-256": sig})
        assert _verify_auth(cfg, req, b"") is True


class TestVerifyAuthBearer:
    def test_valid_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_TOKEN", "alpha-beta-gamma")
        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        req = _mock_request({"Authorization": "Bearer alpha-beta-gamma"})
        assert _verify_auth(cfg, req, b"") is True

    def test_case_insensitive_scheme(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RFC 6750 §2.1 — scheme name case-insensitive."""
        monkeypatch.setenv("TEST_TOKEN", "alpha-beta-gamma")
        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        req = _mock_request({"Authorization": "bearer alpha-beta-gamma"})
        assert _verify_auth(cfg, req, b"") is True

    def test_missing_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_TOKEN", "alpha-beta-gamma")
        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        req = _mock_request({})
        assert _verify_auth(cfg, req, b"") is False

    def test_wrong_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_TOKEN", "alpha-beta-gamma")
        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        req = _mock_request({"Authorization": "Bearer wrong"})
        assert _verify_auth(cfg, req, b"") is False

    def test_non_bearer_scheme(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_TOKEN", "alpha-beta-gamma")
        cfg = BearerAuthConfig(secret_env="TEST_TOKEN")
        req = _mock_request({"Authorization": "Basic xxx"})
        assert _verify_auth(cfg, req, b"") is False


class TestVerifyAuthNone:
    def test_always_true(self) -> None:
        cfg = NoneAuthConfig()
        req = _mock_request({})
        assert _verify_auth(cfg, req, b"") is True
```

- [ ] **Step 5.2: Run, confirm failure**

Run: `uv run pytest tests/test_triggers/test_webhook.py::TestVerifyAuthHMAC tests/test_triggers/test_webhook.py::TestVerifyAuthBearer tests/test_triggers/test_webhook.py::TestVerifyAuthNone -v`
Expected: fails — `_verify_auth` not exported.

- [ ] **Step 5.3: Implement `_verify_auth` and reason codes**

Edit `src/proctor/triggers/webhook.py`. Add imports:

```python
import hashlib
import hmac
import os
```

Add after `_safe_headers` (before `InflightLimiter`):

```python
# Auth failure reason codes. Never include raw header values, signatures,
# or tokens in any log line — logs often have broader read access than
# the SQLite DBs.
_AUTH_REASONS = frozenset({
    "missing_header",
    "bad_prefix",
    "bad_signature",
    "non_bearer_scheme",
    "wrong_token",
})


def _verify_auth(
    auth_cfg: object,  # HMACAuthConfig | BearerAuthConfig | NoneAuthConfig
    request: object,    # aiohttp.web.Request (duck-typed for unit tests)
    raw_body: bytes,
) -> bool:
    """Per-request auth verification.

    Secrets are re-read from os.environ on every call; rotation via
    os.environ[...] = new_value takes effect immediately without
    restart.
    """
    # Type narrowing via attribute access (duck typing to avoid
    # importing config classes into this module's type hints).
    kind = getattr(auth_cfg, "type", None)
    if kind == "none":
        return True
    if kind == "hmac":
        header_name: str = auth_cfg.header  # type: ignore[attr-defined]
        prefix: str = auth_cfg.prefix         # type: ignore[attr-defined]
        secret_env: str = auth_cfg.secret_env # type: ignore[attr-defined]
        header_val = request.headers.get(header_name)  # type: ignore[attr-defined]
        if header_val is None or not header_val.startswith(prefix):
            return False
        sig_hex = header_val[len(prefix):]
        secret = os.environ[secret_env].encode()
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig_hex, expected)
    if kind == "bearer":
        header_name = auth_cfg.header  # type: ignore[attr-defined]
        secret_env = auth_cfg.secret_env  # type: ignore[attr-defined]
        header_val = request.headers.get(header_name, "")  # type: ignore[attr-defined]
        parts = header_val.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        token = parts[1]
        secret = os.environ[secret_env]
        return hmac.compare_digest(token, secret)
    return False  # unreachable with discriminated union
```

- [ ] **Step 5.4: Run tests, confirm pass**

Run: `uv run pytest tests/test_triggers/test_webhook.py -v`
Expected: all pass.

- [ ] **Step 5.5: Format + lint + type check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 5.6: Commit**

```bash
git add src/proctor/triggers/webhook.py tests/test_triggers/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(webhook): add _verify_auth dispatch and _AUTH_REASONS codes

HMAC-SHA256 with configurable header + prefix (covers GitHub, Slack-ish
header shape — but NOT Slack's v0:timestamp:body base string).
Bearer is RFC 6750 case-insensitive. None always True. All three use
hmac.compare_digest for constant-time comparison. Secrets re-read
from os.environ on every request.

_AUTH_REASONS enumerates failure codes the handler may log; raw
values never appear in logs.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `WebhookTrigger` skeleton + start/stop lifecycle

**Files:**
- Modify: `src/proctor/triggers/webhook.py`
- Modify: `tests/test_triggers/test_webhook.py`

- [ ] **Step 6.1: Write failing tests**

Append to `tests/test_triggers/test_webhook.py`:

```python
from collections.abc import AsyncGenerator
from pathlib import Path

import aiohttp

from proctor.core.bus import EventBus
from proctor.core.config import WebhookConfig, WebhookPathConfig
from proctor.triggers.webhook import WebhookTrigger


@pytest.fixture(scope="module")
async def webhook_env():
    """Module-scoped fixture: env vars + a started WebhookTrigger on
    an ephemeral port. Uses pytest.MonkeyPatch() directly because
    the built-in monkeypatch fixture is function-scoped.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("TEST_HMAC_SECRET", "topsecret")
    mp.setenv("TEST_BEARER_TOKEN", "alpha-beta-gamma")
    try:
        cfg = WebhookConfig(
            host="127.0.0.1",
            port=0,  # OS assigns
            paths={
                "/webhook/hmac": WebhookPathConfig(
                    auth=HMACAuthConfig(secret_env="TEST_HMAC_SECRET"),
                ),
                "/webhook/bearer": WebhookPathConfig(
                    auth=BearerAuthConfig(secret_env="TEST_BEARER_TOKEN"),
                ),
                "/webhook/open": WebhookPathConfig(
                    auth=NoneAuthConfig(),
                ),
            },
        )
        bus = EventBus()
        trigger = WebhookTrigger(cfg)
        await trigger.start(bus)
        yield trigger, bus, f"http://127.0.0.1:{trigger.bound_port}"
        await trigger.stop()
    finally:
        mp.undo()


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
        import logging

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
            assert any(
                "NO AUTHENTICATION" in r.message
                for r in caplog.records
            )
        finally:
            await trigger.stop()

    async def test_idempotent_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
```

- [ ] **Step 6.2: Run, confirm failure**

Run: `uv run pytest tests/test_triggers/test_webhook.py::TestLifecycle -v`
Expected: fails — `WebhookTrigger` not exported.

- [ ] **Step 6.3: Implement `WebhookTrigger`**

Edit `src/proctor/triggers/webhook.py`. Add imports:

```python
from aiohttp import web

from proctor.core.bus import EventBus
from proctor.core.config import WebhookConfig
from proctor.core.models import Event
from proctor.triggers.base import Trigger
```

Add below `InflightLimiter`:

```python
class WebhookTrigger(Trigger):
    """aiohttp-based HTTP server that publishes trigger.webhook.<source_name>
    events on the bus. Per-path auth (HMAC/Bearer/none), fire-and-forget
    semantics (202 Accepted), graceful drain on stop().
    """

    def __init__(self, config: WebhookConfig) -> None:
        self._config = config
        self._paths = config.paths
        self._limiter = InflightLimiter(config.max_in_flight)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._bus: EventBus | None = None
        self._stopped = False

    @property
    def bound_port(self) -> int | None:
        """Actual bound port (useful when config.port=0). None until
        started, also None after stop() cleanup.
        """
        if self._site is None or self._site._server is None:
            return None
        sockets = self._site._server.sockets
        return sockets[0].getsockname()[1] if sockets else None

    async def start(self, bus: EventBus) -> None:
        # Fail fast: every required env secret must be present.
        missing = sorted({
            cfg.auth.secret_env
            for cfg in self._paths.values()
            if cfg.auth.type != "none"
            and cfg.auth.secret_env not in os.environ
        })
        if missing:
            raise RuntimeError(
                f"Missing required webhook secrets in env: {missing}"
            )

        # Loud WARNING for any unauthenticated path.
        for path, cfg in self._paths.items():
            if cfg.auth.type == "none":
                logger.warning(
                    "Webhook path %r has NO AUTHENTICATION "
                    "(auth.type: none). Do not use in production.",
                    path,
                )

        self._bus = bus
        app = web.Application(client_max_size=self._config.max_body_bytes)
        for path in self._paths:
            app.router.add_post(path, self._handle_webhook)
        self._runner = web.AppRunner(
            app,
            keepalive_timeout=self._config.keepalive_timeout,
        )
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, self._config.host, self._config.port
        )
        await self._site.start()
        logger.info(
            "WebhookTrigger started on %s:%d with %d path(s)",
            self._config.host,
            self._config.port,
            len(self._paths),
        )

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True

        if self._site is not None:
            await self._site.stop()

        drained = await self._limiter.wait_idle(
            self._config.shutdown_timeout
        )
        if not drained:
            logger.warning(
                "Webhook shutdown timed out with %d in-flight requests",
                self._limiter.in_flight,
            )

        if self._runner is not None:
            await self._runner.cleanup()
        logger.info("WebhookTrigger stopped")

    async def _handle_webhook(
        self, request: web.Request
    ) -> web.Response:
        # Placeholder — full implementation in Task 7.
        return web.json_response({"stub": True}, status=501)
```

- [ ] **Step 6.4: Run tests, confirm pass**

Run: `uv run pytest tests/test_triggers/test_webhook.py::TestLifecycle -v`
Expected: 4 passed.

- [ ] **Step 6.5: Format + lint + type check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 6.6: Commit**

```bash
git add src/proctor/triggers/webhook.py tests/test_triggers/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(webhook): add WebhookTrigger lifecycle (start/stop/bound_port)

- start(): verify all required env secrets present; loud WARNING for
  auth.type: none paths; register POST per path; bind aiohttp site.
- stop(): idempotent; stop accepting → wait_idle(timeout) → force
  cleanup; WARNING if drain timed out.
- bound_port property for test ephemeral-port discovery.

_handle_webhook is a 501 stub; full logic lands in the next commit.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `_handle_webhook` happy path + status-code matrix

**Files:**
- Modify: `src/proctor/triggers/webhook.py`
- Modify: `tests/test_triggers/test_webhook.py`

- [ ] **Step 7.1: Write failing tests**

Append to `tests/test_triggers/test_webhook.py`:

```python
import json as _json


async def _post(
    session: aiohttp.ClientSession,
    url: str,
    body: bytes,
    headers: dict[str, str],
) -> tuple[int, dict[str, str]]:
    async with session.post(url, data=body, headers=headers) as resp:
        try:
            data = await resp.json()
        except aiohttp.ContentTypeError:
            data = {}
        return resp.status, data


@pytest.fixture
def captured(webhook_env):
    trigger, bus, _ = webhook_env
    events: list[Event] = []
    arrived = asyncio.Event()

    async def _collect(e: Event) -> None:
        events.append(e)
        arrived.set()

    bus.subscribe("trigger.webhook.*", _collect)
    return events, arrived


class TestHappyPathHMAC:
    async def test_valid_signature_returns_202_and_event(
        self, webhook_env, captured
    ) -> None:
        _, _, url = webhook_env
        events, arrived = captured

        body = b'{"hello": "world"}'
        sig = _sign_hmac(body, "topsecret")

        async with aiohttp.ClientSession() as s:
            status, data = await _post(
                s,
                f"{url}/webhook/hmac",
                body,
                {
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "push",
                    "X-GitHub-Delivery": "del-1",
                },
            )

        assert status == 202
        assert data["accepted"] is True
        assert "correlation_id" in data

        await arrived.wait()
        assert len(events) == 1
        ev = events[0]
        assert ev.type == "trigger.webhook.hmac"
        assert ev.source == "webhook"
        assert ev.payload["path"] == "/webhook/hmac"
        assert ev.payload["body"] == {"hello": "world"}
        assert ev.payload["headers"]["X-GitHub-Event"] == "push"
        assert "X-Hub-Signature-256" not in ev.payload["headers"]
        assert data["correlation_id"] == ev.id


class TestStatusCodes:
    async def test_unregistered_path_404(self, webhook_env) -> None:
        _, _, url = webhook_env
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{url}/webhook/nope", data=b"{}") as r:
                assert r.status == 404

    async def test_wrong_method_405(self, webhook_env) -> None:
        _, _, url = webhook_env
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{url}/webhook/open") as r:
                assert r.status == 405

    async def test_bad_signature_401(self, webhook_env) -> None:
        _, _, url = webhook_env
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{url}/webhook/hmac",
                data=b'{"x": 1}',
                headers={"X-Hub-Signature-256": "sha256=" + "a" * 64},
            ) as r:
                assert r.status == 401
                data = await r.json()
                assert data == {"error": "unauthorized"}

    async def test_missing_auth_header_401(self, webhook_env) -> None:
        _, _, url = webhook_env
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{url}/webhook/bearer", data=b"{}"
            ) as r:
                assert r.status == 401

    async def test_malformed_json_400(self, webhook_env) -> None:
        _, _, url = webhook_env
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{url}/webhook/open", data=b"{not json"
            ) as r:
                assert r.status == 400

    async def test_empty_body_accepted(
        self, webhook_env, captured
    ) -> None:
        _, _, url = webhook_env
        events, arrived = captured
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{url}/webhook/open", data=b""
            ) as r:
                assert r.status == 202
        await arrived.wait()
        assert events[-1].payload["body"] == {}

    async def test_body_too_large_413(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dedicated webhook with small max_body_bytes so the test
        doesn't need to send 1MB."""
        mp = pytest.MonkeyPatch()
        mp.setenv("TINY_SECRET", "s")
        try:
            cfg = WebhookConfig(
                host="127.0.0.1",
                port=0,
                max_body_bytes=100,
                paths={
                    "/webhook/tiny": WebhookPathConfig(
                        auth=NoneAuthConfig(),
                    ),
                },
            )
            trigger = WebhookTrigger(cfg)
            await trigger.start(EventBus())
            try:
                url = f"http://127.0.0.1:{trigger.bound_port}"
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        f"{url}/webhook/tiny", data=b"x" * 500
                    ) as r:
                        assert r.status == 413
            finally:
                await trigger.stop()
        finally:
            mp.undo()
```

- [ ] **Step 7.2: Run, confirm failure**

Run: `uv run pytest tests/test_triggers/test_webhook.py::TestHappyPathHMAC tests/test_triggers/test_webhook.py::TestStatusCodes -v`
Expected: fails — handler returns 501 stub.

- [ ] **Step 7.3: Implement full `_handle_webhook`**

Replace the stub `_handle_webhook` in `src/proctor/triggers/webhook.py`. Add `import asyncio`, `import json` at top if not present:

```python
import json
```

Replace the method:

```python
    async def _handle_webhook(
        self, request: web.Request
    ) -> web.Response:
        try:
            cfg = self._paths[request.path]

            if not await self._limiter.try_acquire():
                logger.warning(
                    "webhook overloaded: rejected %s (in_flight=%d/%d)",
                    request.path,
                    self._limiter.in_flight,
                    self._limiter.limit,
                )
                return web.json_response(
                    {"error": "overloaded"},
                    status=503,
                    headers={"Retry-After": "1"},
                )
            try:
                raw_body = await request.read()

                if not _verify_auth(cfg.auth, request, raw_body):
                    logger.info(
                        "webhook auth failed: path=%s", request.path,
                    )
                    return web.json_response(
                        {"error": "unauthorized"}, status=401,
                    )

                try:
                    body = json.loads(raw_body) if raw_body else {}
                except json.JSONDecodeError:
                    return web.json_response(
                        {"error": "bad request"}, status=400,
                    )

                headers = _safe_headers(request.headers)
                assert cfg.source_name is not None  # guaranteed by validator
                event = Event(
                    type=f"trigger.webhook.{cfg.source_name}",
                    source="webhook",
                    payload={
                        "path": request.path,
                        "headers": headers,
                        "body": body,
                    },
                )
                assert self._bus is not None  # set in start()
                try:
                    await self._bus.publish(event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "webhook failed to publish event for %s",
                        request.path,
                    )
                    return web.json_response(
                        {"error": "service unavailable"},
                        status=503,
                        headers={"Retry-After": "5"},
                    )

                logger.debug(
                    "webhook accepted: path=%s source=%s event_id=%s",
                    request.path, cfg.source_name, event.id,
                )
                return web.json_response(
                    {"accepted": True, "correlation_id": event.id},
                    status=202,
                )
            finally:
                await self._limiter.release()
        except asyncio.CancelledError:
            raise
        except web.HTTPException:
            raise
        except Exception:
            logger.exception(
                "webhook handler unexpected error for %s",
                request.path,
            )
            return web.json_response(
                {"error": "service unavailable"},
                status=503,
                headers={"Retry-After": "5"},
            )
```

- [ ] **Step 7.4: Run tests, confirm pass**

Run: `uv run pytest tests/test_triggers/test_webhook.py -v`
Expected: all pass.

- [ ] **Step 7.5: Format + lint + type check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 7.6: Commit**

```bash
git add src/proctor/triggers/webhook.py tests/test_triggers/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(webhook): full _handle_webhook with status-code matrix

- 202 + correlation_id on successful publish (fire-and-forget).
- 503 + Retry-After: 1 when in-flight cap saturated.
- 503 + Retry-After: 5 on bus.publish failure.
- 401 + {"error": "unauthorized"} (generic, no oracle).
- 400 on malformed JSON body.
- 404/405/413 handled automatically by aiohttp routing + size limit.
- Outer try/except guard: any unexpected exception → 503, never 500.
  web.HTTPException + asyncio.CancelledError re-raised correctly.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Bearer path + auth edge cases + header whitelist proof

**Files:**
- Modify: `tests/test_triggers/test_webhook.py`

- [ ] **Step 8.1: Append tests — Bearer happy path + whitelist proof**

```python
class TestHappyPathBearer:
    async def test_valid_bearer_returns_202(
        self, webhook_env, captured
    ) -> None:
        _, _, url = webhook_env
        events, arrived = captured
        async with aiohttp.ClientSession() as s:
            status, data = await _post(
                s,
                f"{url}/webhook/bearer",
                b'{"x": 1}',
                {"Authorization": "Bearer alpha-beta-gamma"},
            )
        assert status == 202
        await arrived.wait()
        assert events[-1].type == "trigger.webhook.bearer"


class TestHeaderWhitelistE2E:
    async def test_auth_headers_never_in_payload(
        self, webhook_env, captured
    ) -> None:
        _, _, url = webhook_env
        events, arrived = captured
        async with aiohttp.ClientSession() as s:
            status, _ = await _post(
                s,
                f"{url}/webhook/bearer",
                b'{"x": 1}',
                {
                    "Authorization": "Bearer alpha-beta-gamma",
                    "Cookie": "sid=secret",
                    "Stripe-Signature": "t=1,v1=x",
                    "X-GitHub-Event": "push",
                    "Content-Type": "application/json",
                },
            )
        assert status == 202
        await arrived.wait()
        headers = events[-1].payload["headers"]
        assert "Authorization" not in headers
        assert "Cookie" not in headers
        assert "Stripe-Signature" not in headers
        assert headers.get("X-GitHub-Event") == "push"
        assert headers.get("Content-Type") == "application/json"


class TestUnauthenticatedOpenPath:
    async def test_open_path_accepts_anything(
        self, webhook_env, captured
    ) -> None:
        _, _, url = webhook_env
        events, arrived = captured
        async with aiohttp.ClientSession() as s:
            status, _ = await _post(
                s, f"{url}/webhook/open", b'{"x": 1}', {}
            )
        assert status == 202
        await arrived.wait()
        assert events[-1].type == "trigger.webhook.open"
```

- [ ] **Step 8.2: Run, confirm pass**

Run: `uv run pytest tests/test_triggers/test_webhook.py::TestHappyPathBearer tests/test_triggers/test_webhook.py::TestHeaderWhitelistE2E tests/test_triggers/test_webhook.py::TestUnauthenticatedOpenPath -v`
Expected: all pass (impl supports these already).

- [ ] **Step 8.3: Commit**

```bash
git add tests/test_triggers/test_webhook.py
git commit -m "$(cat <<'EOF'
test(webhook): bearer + header-whitelist + open-path e2e

Locks in: Bearer auth happy path, header whitelist eliminates
Authorization/Cookie/Stripe-Signature from the payload, and
NoneAuthConfig paths accept unauthenticated POSTs.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: In-flight cap + bus-failure + CancelledError + outer-guard tests

**Files:**
- Modify: `tests/test_triggers/test_webhook.py`

- [ ] **Step 9.1: Append tests**

```python
class TestInflightCap:
    async def test_21st_request_returns_503_retry_after_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mp = pytest.MonkeyPatch()
        mp.setenv("X", "x")
        try:
            cfg = WebhookConfig(
                port=0,
                max_in_flight=2,
                paths={
                    "/webhook/slow": WebhookPathConfig(
                        auth=NoneAuthConfig(),
                    ),
                },
            )
            bus = EventBus()
            # Block the bus publish so in-flight cap saturates.
            hold = asyncio.Event()

            async def blocking_publish(_evt: Event) -> None:
                await hold.wait()

            trigger = WebhookTrigger(cfg)
            trigger._bus = None  # filled by start()
            await trigger.start(bus)

            # Monkey-patch bus.publish on the instance.
            monkeypatch.setattr(trigger, "_bus", bus)
            monkeypatch.setattr(bus, "publish", blocking_publish)

            url = f"http://127.0.0.1:{trigger.bound_port}"
            try:
                async with aiohttp.ClientSession() as s:
                    # Fire 2 requests that will block inside publish.
                    task1 = asyncio.create_task(
                        s.post(f"{url}/webhook/slow", data=b"{}").__aenter__()
                    )
                    task2 = asyncio.create_task(
                        s.post(f"{url}/webhook/slow", data=b"{}").__aenter__()
                    )
                    # Give them a tick to enter _handle_webhook.
                    await asyncio.sleep(0.05)
                    # 3rd request — should be rejected immediately.
                    async with s.post(
                        f"{url}/webhook/slow", data=b"{}"
                    ) as r:
                        assert r.status == 503
                        assert r.headers["Retry-After"] == "1"
                    # Release.
                    hold.set()
                    r1 = await task1
                    r2 = await task2
                    await r1.__aexit__(None, None, None)
                    await r2.__aexit__(None, None, None)
            finally:
                await trigger.stop()
        finally:
            mp.undo()


class TestBusPublishFailure:
    async def test_bus_exception_returns_503_retry_after_5(
        self,
        webhook_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        trigger, bus, url = webhook_env

        async def failing_publish(_evt: Event) -> None:
            raise RuntimeError("bus down")

        monkeypatch.setattr(bus, "publish", failing_publish)

        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{url}/webhook/open", data=b"{}"
            ) as r:
                assert r.status == 503
                assert r.headers["Retry-After"] == "5"


class TestCancelledErrorPassthrough:
    async def test_cancelled_error_not_swallowed(
        self,
        webhook_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A CancelledError from bus.publish must propagate, not
        be caught by the outer try/except that produces 503.
        """
        _, bus, url = webhook_env

        async def raise_cancelled(_evt: Event) -> None:
            raise asyncio.CancelledError("shutdown")

        monkeypatch.setattr(bus, "publish", raise_cancelled)

        async with aiohttp.ClientSession() as s:
            # The POST will be cancelled mid-handler → connection
            # reset. We only assert that it doesn't return a 503 body.
            try:
                async with s.post(
                    f"{url}/webhook/open", data=b"{}"
                ) as r:
                    # If we got a response, it must NOT be 503; a
                    # well-behaved cancel closes the connection
                    # before any response body.
                    assert r.status != 503
            except aiohttp.ClientError:
                pass  # connection reset is the expected signal


class TestOuterErrorGuard:
    async def test_unexpected_exception_returns_503_not_500(
        self,
        webhook_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Something deep in the handler raises an unexpected
        exception (simulated by breaking _safe_headers) — the outer
        guard must return 503, never let 500 escape.
        """
        _, _, url = webhook_env

        def broken_safe_headers(_h):
            raise ValueError("intentional test failure")

        monkeypatch.setattr(
            "proctor.triggers.webhook._safe_headers",
            broken_safe_headers,
        )

        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{url}/webhook/open", data=b"{}"
            ) as r:
                assert r.status == 503
                assert r.headers["Retry-After"] == "5"
```

- [ ] **Step 9.2: Run, confirm pass**

Run: `uv run pytest tests/test_triggers/test_webhook.py::TestInflightCap tests/test_triggers/test_webhook.py::TestBusPublishFailure tests/test_triggers/test_webhook.py::TestCancelledErrorPassthrough tests/test_triggers/test_webhook.py::TestOuterErrorGuard -v`
Expected: all pass (impl already handles these).

- [ ] **Step 9.3: Format + lint + type check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 9.4: Commit**

```bash
git add tests/test_triggers/test_webhook.py
git commit -m "$(cat <<'EOF'
test(webhook): in-flight cap, bus failure, cancellation, outer guard

- 21st request to a saturated limiter → 503 + Retry-After: 1.
- bus.publish raising → 503 + Retry-After: 5.
- asyncio.CancelledError from bus.publish propagates; no 503 body.
- Unexpected exception inside handler (e.g. _safe_headers bug) →
  503, never 500.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Drain-with-timeout integration test

**Files:**
- Modify: `tests/test_triggers/test_webhook.py`

- [ ] **Step 10.1: Append test**

```python
class TestDrainOnStop:
    async def test_in_flight_handlers_drain_before_stop_returns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inject a slow bus.publish — stop() must wait for in-flight
        handlers to complete (within shutdown_timeout) before
        returning. Each client must still get 202.
        """
        mp = pytest.MonkeyPatch()
        mp.setenv("X", "x")
        try:
            cfg = WebhookConfig(
                port=0,
                shutdown_timeout=2.0,
                paths={
                    "/webhook/slow": WebhookPathConfig(
                        auth=NoneAuthConfig(),
                    ),
                },
            )
            bus = EventBus()
            published = asyncio.Event()
            proceed = asyncio.Event()

            async def slow_publish(_evt: Event) -> None:
                published.set()
                await proceed.wait()

            monkeypatch.setattr(bus, "publish", slow_publish)

            trigger = WebhookTrigger(cfg)
            await trigger.start(bus)
            url = f"http://127.0.0.1:{trigger.bound_port}"
            try:
                async with aiohttp.ClientSession() as s:
                    # Kick off a request that will block in publish.
                    post_task = asyncio.create_task(
                        s.post(f"{url}/webhook/slow", data=b"{}").__aenter__()
                    )
                    await published.wait()
                    # Now in-flight = 1. Start stop() concurrently.
                    stop_task = asyncio.create_task(trigger.stop())
                    # stop() must not finish while handler is still
                    # in-flight (publish blocked).
                    await asyncio.sleep(0.1)
                    assert not stop_task.done()
                    # Release publish → handler completes → drain fires.
                    proceed.set()
                    r = await post_task
                    async with r:
                        assert r.status == 202
                    await asyncio.wait_for(stop_task, timeout=2.0)
            finally:
                if not trigger._stopped:
                    await trigger.stop()
        finally:
            mp.undo()
```

- [ ] **Step 10.2: Run, confirm pass**

Run: `uv run pytest tests/test_triggers/test_webhook.py::TestDrainOnStop -v`
Expected: 1 passed.

- [ ] **Step 10.3: Commit**

```bash
git add tests/test_triggers/test_webhook.py
git commit -m "$(cat <<'EOF'
test(webhook): stop() drains in-flight handlers before returning

Injects a slow bus.publish to force an in-flight handler, then starts
stop() and verifies it does not return until the handler completes.
Client gets 202 before the shutdown completes.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Wire into `Application` (bootstrap)

**Files:**
- Modify: `src/proctor/core/bootstrap.py`
- Modify: `tests/test_core/test_bootstrap.py`

- [ ] **Step 11.1: Write failing test**

Append to `tests/test_core/test_bootstrap.py`:

```python
from proctor.core.config import (
    BearerAuthConfig,
    NoneAuthConfig,
    WebhookConfig,
    WebhookPathConfig,
)


class TestWebhookBootstrap:
    async def test_webhook_trigger_started_when_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from proctor.core.bootstrap import Application
        from proctor.core.config import ProctorConfig

        monkeypatch.setenv("CI_TOKEN", "x")
        cfg = ProctorConfig(
            data_dir=tmp_path,
            webhook=WebhookConfig(
                port=0,
                paths={
                    "/webhook/ci": WebhookPathConfig(
                        auth=BearerAuthConfig(secret_env="CI_TOKEN"),
                    ),
                },
            ),
        )
        app = Application(cfg)
        await app.start()
        try:
            assert app._webhook_trigger is not None
            assert app._webhook_trigger.bound_port is not None
        finally:
            await app.stop()

    async def test_no_webhook_when_config_missing(
        self, tmp_path: Path
    ) -> None:
        from proctor.core.bootstrap import Application
        from proctor.core.config import ProctorConfig

        cfg = ProctorConfig(data_dir=tmp_path)
        app = Application(cfg)
        await app.start()
        try:
            assert app._webhook_trigger is None
        finally:
            await app.stop()

    async def test_webhook_stopped_first_in_application_stop(
        self, tmp_path: Path
    ) -> None:
        """Application.stop() must close WebhookTrigger before other
        triggers/state. Verified by monkey-patching stop order
        instrumentation.
        """
        from proctor.core.bootstrap import Application
        from proctor.core.config import ProctorConfig

        cfg = ProctorConfig(
            data_dir=tmp_path,
            webhook=WebhookConfig(
                port=0,
                paths={
                    "/webhook/open": WebhookPathConfig(
                        auth=NoneAuthConfig(),
                    ),
                },
            ),
        )
        app = Application(cfg)
        await app.start()

        order: list[str] = []
        real_webhook_stop = app._webhook_trigger.stop  # type: ignore[union-attr]
        real_memory_close = app.memory.close

        async def tagged_webhook_stop() -> None:
            order.append("webhook")
            await real_webhook_stop()

        async def tagged_memory_close() -> None:
            order.append("memory")
            await real_memory_close()

        assert app._webhook_trigger is not None
        app._webhook_trigger.stop = tagged_webhook_stop  # type: ignore[method-assign]
        app.memory.close = tagged_memory_close  # type: ignore[method-assign]

        await app.stop()
        assert order.index("webhook") < order.index("memory")
```

- [ ] **Step 11.2: Run, confirm failure**

Run: `uv run pytest tests/test_core/test_bootstrap.py::TestWebhookBootstrap -v`
Expected: fails — `app._webhook_trigger` does not exist.

- [ ] **Step 11.3: Wire `WebhookTrigger` into `Application`**

Edit `src/proctor/core/bootstrap.py`. Add import (next to existing trigger imports):

```python
from proctor.triggers.webhook import WebhookTrigger
```

In `Application.__init__`, after `self._scheduler: SchedulerTrigger | None = None`, add:

```python
        self._webhook_trigger: WebhookTrigger | None = None
```

In `Application.start`, after Telegram/Scheduler start logic (before the closing of the method), add:

```python
        if self.config.webhook is not None:
            self._webhook_trigger = WebhookTrigger(self.config.webhook)
            await self._webhook_trigger.start(self.bus)
            logger.info("WebhookTrigger enabled")
```

In `Application.stop`, modify the stop order. The current body (simplified):

```python
async def stop(self) -> None:
    self.is_running = False
    if self._telegram_trigger is not None:
        await self._telegram_trigger.stop()
    if self._scheduler is not None:
        await self._scheduler.stop()
    await self.memory.close()
    await self.state.close()
```

Replace with:

```python
async def stop(self) -> None:
    self.is_running = False
    # Close inputs first — WebhookTrigger first so the HTTP endpoint
    # stops accepting new POSTs before other components tear down.
    if self._webhook_trigger is not None:
        await self._webhook_trigger.stop()
        self._webhook_trigger = None
    if self._telegram_trigger is not None:
        await self._telegram_trigger.stop()
        self._telegram_trigger = None
    if self._scheduler is not None:
        await self._scheduler.stop()
        self._scheduler = None
    await self.memory.close()
    await self.state.close()
```

(Keep the existing logger calls if the current code has them.)

- [ ] **Step 11.4: Run tests, confirm pass**

Run: `uv run pytest tests/test_core/test_bootstrap.py -v`
Expected: all pass (existing + new class).

- [ ] **Step 11.5: Format + lint + type check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 11.6: Commit**

```bash
git add src/proctor/core/bootstrap.py tests/test_core/test_bootstrap.py
git commit -m "$(cat <<'EOF'
feat(bootstrap): wire WebhookTrigger (stop first in shutdown)

Application instantiates WebhookTrigger when config.webhook is not
None. In Application.stop(), WebhookTrigger.stop() is called BEFORE
other triggers and state tear-down, so the HTTP endpoint stops
accepting new POSTs before dependencies it relies on disappear.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Integration tests (e2e through Router to workflow)

**Files:**
- Modify: `tests/test_integration.py`

- [ ] **Step 12.1: Append `TestWebhookIntegration`**

```python
import asyncio
import hashlib
import hmac
from pathlib import Path

import aiohttp
import pytest

from proctor.core.bootstrap import Application
from proctor.core.config import (
    HMACAuthConfig,
    ProctorConfig,
    RouteRule,
    WebhookConfig,
    WebhookPathConfig,
)
from proctor.workflow.spec import WorkflowMode, WorkflowSpec


class TestWebhookIntegration:
    @pytest.mark.anyio
    async def test_webhook_routes_to_workflow_end_to_end(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def llm_echo(prompt: str) -> str:
            return f"echo: {prompt}"

        monkeypatch.setenv("GITHUB_TEST_SECRET", "topsecret")
        cfg = ProctorConfig(
            data_dir=tmp_path,
            workflows={
                "gh": WorkflowSpec(
                    workflow_id="gh", mode=WorkflowMode.SIMPLE,
                ),
            },
            routes=[
                RouteRule(
                    event_pattern="trigger.webhook.github",
                    workflow_id="gh",
                    prompt_from_payload="body.message",
                ),
            ],
            webhook=WebhookConfig(
                port=0,
                paths={
                    "/webhook/github": WebhookPathConfig(
                        source_name="github",
                        auth=HMACAuthConfig(
                            secret_env="GITHUB_TEST_SECRET",
                        ),
                    ),
                },
            ),
        )
        app = Application(cfg)
        app.set_llm_call(llm_echo)
        await app.start()
        try:
            port = app._webhook_trigger.bound_port  # type: ignore[union-attr]
            body = b'{"message": "hello from github"}'
            sig = "sha256=" + hmac.new(
                b"topsecret", body, hashlib.sha256
            ).hexdigest()
            url = f"http://127.0.0.1:{port}/webhook/github"
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url,
                    data=body,
                    headers={"X-Hub-Signature-256": sig},
                ) as r:
                    assert r.status == 202
            # Give the workflow a moment to complete.
            await asyncio.sleep(0.1)
            episodes = await app.memory.list_episodes(limit=10)
            assert any(
                "echo: hello from github" == ep.agent_response
                for ep in episodes
            )
        finally:
            await app.stop()

    @pytest.mark.anyio
    async def test_unmatched_webhook_creates_no_task(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def llm_unused(prompt: str) -> str:
            return "should not be called"

        monkeypatch.setenv("GH_SECRET", "s")
        cfg = ProctorConfig(
            data_dir=tmp_path,
            webhook=WebhookConfig(
                port=0,
                paths={
                    "/webhook/lonely": WebhookPathConfig(
                        source_name="lonely",
                        auth=HMACAuthConfig(secret_env="GH_SECRET"),
                    ),
                },
            ),
        )
        app = Application(cfg)
        app.set_llm_call(llm_unused)
        await app.start()
        try:
            port = app._webhook_trigger.bound_port  # type: ignore[union-attr]
            body = b'{"x": 1}'
            sig = "sha256=" + hmac.new(
                b"s", body, hashlib.sha256
            ).hexdigest()
            url = f"http://127.0.0.1:{port}/webhook/lonely"
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url,
                    data=body,
                    headers={"X-Hub-Signature-256": sig},
                ) as r:
                    assert r.status == 202  # webhook accepted
            await asyncio.sleep(0.1)
            episodes = await app.memory.list_episodes(limit=10)
            assert episodes == []  # no route → no task/episode
        finally:
            await app.stop()
```

- [ ] **Step 12.2: Run, confirm pass**

Run: `uv run pytest tests/test_integration.py::TestWebhookIntegration -v`
Expected: 2 passed.

- [ ] **Step 12.3: Full suite**

Run: `uv run pytest 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 12.4: Commit**

```bash
git add tests/test_integration.py
git commit -m "$(cat <<'EOF'
test(integration): webhook → Router → workflow end-to-end

Two scenarios:
- POST with valid HMAC → 202 → Router routes to "gh" workflow →
  mock LLM echoes prompt → episode persisted with expected output.
- POST to a path that has no matching route rule → 202 (webhook
  accepted delivery) → Router publishes routing.unmatched → no
  task, no episode.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Re-export + example YAML

**Files:**
- Modify: `src/proctor/triggers/__init__.py`
- Modify: `config/proctor.yaml`

- [ ] **Step 13.1: Add re-export**

Edit `src/proctor/triggers/__init__.py`:

```python
"""Triggers module — input adapters that produce events."""

from proctor.triggers.base import Trigger
from proctor.triggers.scheduler import SchedulerTrigger
from proctor.triggers.telegram import TelegramTrigger
from proctor.triggers.terminal import TerminalTrigger
from proctor.triggers.webhook import WebhookTrigger

__all__ = [
    "SchedulerTrigger",
    "TelegramTrigger",
    "TerminalTrigger",
    "Trigger",
    "WebhookTrigger",
]
```

- [ ] **Step 13.2: Smoke-test import**

Run: `uv run python -c "from proctor.triggers import WebhookTrigger; print(WebhookTrigger)"`
Expected: class prints.

- [ ] **Step 13.3: Add example to `config/proctor.yaml`**

Append (or merge with existing `schedules:` block):

```yaml
webhook:
  host: 127.0.0.1
  port: 8080
  max_in_flight: 20
  max_body_bytes: 1048576
  shutdown_timeout: 30.0
  paths:
    /webhook/github:
      source_name: github           # → trigger.webhook.github
      auth:
        type: hmac
        secret_env: GITHUB_WEBHOOK_SECRET
        header: X-Hub-Signature-256
        prefix: "sha256="
    /webhook/ci:
      # source_name derived = "ci"
      auth:
        type: bearer
        secret_env: PROCTOR_CI_TOKEN
```

Also add corresponding route rules in the existing `routes:` block:

```yaml
routes:
  # ... existing rules ...
  - event_pattern: "trigger.webhook.github"
    workflow_id: chat               # reuses existing chat workflow
    prompt_from_payload: body.head_commit.message
  - event_pattern: "trigger.webhook.ci"
    workflow_id: chat
    prompt_from_payload: body.message
```

- [ ] **Step 13.4: Verify config loads (without env vars set the secret-env presence check lives in start(), not load_config)**

Run: `uv run python -c "from proctor.core.config import load_config; cfg = load_config('config/proctor.yaml'); print(len(cfg.webhook.paths) if cfg.webhook else 0, 'webhook paths')"`
Expected: `2 webhook paths`.

- [ ] **Step 13.5: Format + lint + type check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 13.6: Commit**

```bash
git add src/proctor/triggers/__init__.py config/proctor.yaml
git commit -m "$(cat <<'EOF'
feat(webhook): re-export + config/proctor.yaml example

triggers/__init__.py re-exports WebhookTrigger alongside the other
triggers. The example YAML now demonstrates GitHub (HMAC) and CI
(Bearer) webhook paths with matching route rules.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: README — `## Webhook trigger`, `## Deployment topologies`, Phase 2 roadmap

**Files:**
- Modify: `README.md`

- [ ] **Step 14.1: Add `## Webhook trigger` subsection**

Insert after the existing `## Routing` section (from LABS-65):

````markdown
## Webhook trigger

HTTP endpoint that receives POSTs from external systems (GitHub,
Stripe, CI pipelines, internal services), authenticates them, and
publishes `trigger.webhook.<source_name>` events on the bus. LABS-65
Router then dispatches those events to catalog workflows via the
existing YAML rules.

Example config:

```yaml
webhook:
  host: 127.0.0.1
  port: 8080
  paths:
    /webhook/github:
      source_name: github
      auth:
        type: hmac
        secret_env: GITHUB_WEBHOOK_SECRET
        header: X-Hub-Signature-256
        prefix: "sha256="
    /webhook/ci:
      auth:
        type: bearer
        secret_env: PROCTOR_CI_TOKEN
```

The path `/webhook/github` → event `trigger.webhook.github`. Routing:

```yaml
routes:
  - event_pattern: "trigger.webhook.github"
    workflow_id: ci-reviewer
    prompt_from_payload: body.head_commit.message
```

### Response semantics

Webhook handlers return **202 Accepted** immediately after publishing
the event. The handler does **not** wait for the downstream workflow —
HTTP response times are milliseconds, not seconds, matching how
GitHub, Stripe, Slack, and the rest of the industry expect webhook
receivers to behave. The response body is:

```json
{ "accepted": true, "correlation_id": "<event uuid>" }
```

The `correlation_id` equals `event.id` and can be used to trace the
event through bus logs, task state, and episodic memory.

### At-least-once delivery

Webhook events are delivered **at least once**. Client retries, proxy
retries, or server crashes after publish can produce duplicate events.
Workflow authors should treat duplicates as normal and use dedup keys:

- GitHub: `payload.headers["X-GitHub-Delivery"]` (always present).
- Internal clients: arrange for `X-Request-Id`.
- `correlation_id` returned in 202 response.

A dedicated dedup layer is a separate issue.

### Authentication

Three `auth.type` variants:

- **`hmac`** — HMAC-SHA256. Header + prefix configurable. Covers
  GitHub (`X-Hub-Signature-256` + `sha256=`) and Slack-style header
  shape. Note: this verifies `HMAC(secret, body)`. Real Slack signing
  uses `HMAC(secret, "v0:{timestamp}:{body}")`; Stripe likewise signs
  `{timestamp}.{body}`. These platforms need a dedicated `auth.type:
  "slack"` / `"stripe"` implementation — out of scope today.
- **`bearer`** — `Authorization: Bearer <token>` (RFC 6750,
  case-insensitive scheme).
- **`none`** — no auth. Explicit opt-in (`auth: {type: none}`).
  Triggers a startup WARNING per such path. **Do not use in
  production.**

Secrets live in env vars via `secret_env`. `WebhookTrigger.start()`
fails fast with `RuntimeError` listing all missing vars. Rotation via
`os.environ[...] = new_value` takes effect on the next request.

All auth failures return an identical `401 {"error": "unauthorized"}`
body — never leak what went wrong to the caller.

### Capacity and shutdown

- `max_in_flight` (default **20**) — concurrent handler cap. 21st
  request returns `503 + Retry-After: 1`.
- `max_body_bytes` (default **1048576** — 1 MB) — enforced by aiohttp;
  excess returns `413`.
- `shutdown_timeout` (default **30s**) — maximum time `stop()` waits
  for in-flight handlers to drain before force-closing.

Fire-and-forget means handlers are measured in milliseconds; the
timeouts above guard against misconfigured reverse-proxies or bus
stalls, not against long-running workflows (those live outside the
HTTP cycle).

## Deployment topologies

The default `host: 127.0.0.1` means Proctor's webhook endpoint is
reachable only from localhost. **A reverse-proxy in front of Proctor
is required for public exposure** — Proctor itself does not do TLS
termination, per-IP rate limiting, or IP-level abuse detection. The
in-flight cap is a memory-footprint guardrail, not a DDoS defense; an
attacker with invalid credentials can briefly saturate slots until
auth rejects them.

### Sidecar nginx

```nginx
server {
    listen 443 ssl;
    server_name proctor.example.com;
    ssl_certificate /etc/letsencrypt/live/proctor.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/proctor.example.com/privkey.pem;

    # Per-IP rate limit: 10 req/s burst 20, delay excess.
    limit_req_zone $binary_remote_addr zone=webhook:10m rate=10r/s;

    location /webhook/ {
        limit_req zone=webhook burst=20 nodelay;
        proxy_pass http://127.0.0.1:8080;
        proxy_read_timeout 10s;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

### Kubernetes (Traefik ingress)

In Kubernetes, the Proctor pod exposes webhook on `0.0.0.0:8080` (to
be reachable from the ingress controller in a different pod) but is
protected by `NetworkPolicy`:

```yaml
# proctor.yaml excerpt
webhook:
  host: 0.0.0.0        # reachable from ingress
  port: 8080
```

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: proctor-webhook }
spec:
  podSelector: { matchLabels: { app: proctor } }
  ingress:
  - from:
    - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: ingress-nginx } }
    ports:
    - port: 8080
```

Traefik IngressRoute with rate limiting:

```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata: { name: proctor-webhook }
spec:
  entryPoints: [websecure]
  routes:
  - match: Host(`proctor.example.com`) && PathPrefix(`/webhook/`)
    services: [{ name: proctor, port: 8080 }]
    middlewares: [{ name: rate-limit }]
```

Set `terminationGracePeriodSeconds` on the Proctor Deployment to at
least `shutdown_timeout + 15s` (default 45s) so the pod has time to
drain HTTP handlers before SIGKILL.
````

- [ ] **Step 14.2: Update Phase 2 roadmap row**

Find the line:

```
| 2 | Proactivity (scheduler, Telegram trigger, router, episodic memory) | Partial (scheduler, Telegram, episodic memory, router, LiteLLM done; webhook, NATS pending) |
```

Replace with:

```
| 2 | Proactivity (scheduler, Telegram trigger, router, episodic memory, webhook) | Done (scheduler, Telegram, episodic memory, router, LiteLLM, webhook). NATS transport deferred to Phase 3. |
```

- [ ] **Step 14.3: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): document WebhookTrigger and deployment topologies

New ## Webhook trigger subsection covers config, 202 semantics,
at-least-once delivery, auth schemes, capacity/shutdown defaults.
New ## Deployment topologies subsection documents sidecar-nginx and
Kubernetes/Traefik patterns with concrete configs, and states that
a reverse-proxy is a production requirement. Phase 2 roadmap row
updated: all Phase 2 triggers done.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **Step F.1: Full test suite**

Run: `uv run pytest 2>&1 | tail -3`
Expected: all pass (previous count + ~40 new tests).

- [ ] **Step F.2: Format + lint + types**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step F.3: CLI smoke test**

Run: `uv run python -m proctor --config config/proctor.yaml --help 2>&1 | head -3`
Expected: CLI help renders.

- [ ] **Step F.4: Spec AC coverage**

Cross-check the Acceptance criteria block in
`docs/superpowers/specs/2026-04-15-webhook-trigger-design.md`:

- `WebhookTrigger` with `start(bus)`, `stop()`, `bound_port` → Task 6.
- `InflightLimiter` unit-tested → Task 3.
- All auth configs with `extra="forbid"` → Task 1.
- `_validate_paths` → Task 2.
- Secret env presence verified in `start()` → Task 6 (lifecycle).
- Loud WARNING per unauth path → Task 6.
- Header whitelist → Task 4, Task 8.
- In-flight cap 503 + `Retry-After: 1` → Task 9.
- CancelledError re-raised → Task 9.
- Outer guard → 503 not 500 → Task 7 + Task 9.
- Graceful drain + idempotent stop → Task 6 + Task 10.
- Application.stop WebhookTrigger first → Task 11.
- ~40 tests → Tasks 3-12.
- README subsections → Task 14.
- `config/proctor.yaml` example → Task 13.

- [ ] **Step F.5: Push branch and open PR**

```bash
git push -u origin <current-branch>
gh pr create --title "feat: WebhookTrigger (LABS-66)" --body "$(cat <<'EOF'
## Summary
- New HTTP webhook trigger publishing `trigger.webhook.<source_name>` events on the bus with fire-and-forget 202 Accepted semantics
- Per-path auth: HMAC-SHA256 (GitHub-compatible), Bearer (RFC 6750), or explicit `none` with loud WARNING
- `InflightLimiter` for memory-safe admission control, 503 + `Retry-After: 1` at capacity
- Outer try/except guard around the handler body: unexpected errors → 503, never 500
- Graceful drain on `stop()` via `asyncio.Event`-based `wait_idle`, WebhookTrigger stopped **first** in `Application.stop()`
- Header whitelist prevents credential leak (Authorization, X-Hub-Signature-256, Stripe-Signature) into episodes.db

## Test plan
- [x] ~40 unit + integration tests (AuthConfig, WebhookConfig, InflightLimiter, _safe_headers, _verify_auth, full HTTP happy paths, status-code matrix, in-flight cap, bus failure, CancelledError, outer guard, drain, bootstrap wiring, e2e through Router)
- [x] Spec AC checklist — all boxes checked (see F.4)
- [ ] Manual smoke: `uv run python -m proctor --config config/proctor.yaml` with `GITHUB_WEBHOOK_SECRET` set, POST with HMAC, verify event flows to workflow

Related spec: `docs/superpowers/specs/2026-04-15-webhook-trigger-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review notes

- **Spec coverage:** Every AC checkbox maps to at least one task — see F.4.
- **Placeholder scan:** No TBDs, no "similar to". The validator stub in the spec (`@model_validator(...) ...`) is fully expanded in Task 2 Step 2.3.
- **Type consistency:** `InflightLimiter` signature (`__init__(limit)`, `try_acquire()`, `release()`, `wait_idle(timeout)`, `in_flight`, `limit`) identical across Tasks 3, 6, 9, 10. `_verify_auth(auth_cfg, request, raw_body)` identical across Tasks 5, 7. `_safe_headers(headers)` identical across Tasks 4, 8. `WebhookTrigger` `bound_port` property first in Task 6, used in Tasks 7-12.
- **Task ordering:** config (Tasks 1-2) → helpers (Tasks 3-5) → trigger (Tasks 6-10) → wiring (Tasks 11-13) → docs (Task 14). Tasks 8 and 9 are test-only locks on behavior already implemented in Task 7.
- **Scope:** Out-of-scope items from the spec (Stripe auth, Slack signing, replay protection, multi-token, app-layer rate limit, dedup, list-indexing in prompt_from_payload) stayed deferred.
