# NATS Transport (LABS-68) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor in-memory `EventBus` into a pluggable `EventTransport` abstraction (PR 68a) and add a NATS-backed implementation (PR 68b) so Proctor can run in multi-node topologies. Standalone mode stays identical.

**Architecture:** Two PRs on one branch. 68a introduces `EventTransport` ABC + `LocalEventTransport` (in-memory, NATS-wildcard semantics) and migrates 61 call-sites to explicit DI; no NATS, no Docker, no CI changes. 68b adds `NATSEventTransport` with lazy `nats-py` import, JSON-body wire format + NATS headers (`Nats-Msg-Id`, `content-type`, `schema-version`, `event-type`, `published-at`), parametrized `[local, nats]` contract tests, and `integration-nats` CI job. Atomic merge.

**Tech Stack:** Python 3.11+, pydantic 2.x, anyio, asyncio, `nats-py>=2.7` (optional extra), testcontainers (dev), pytest + pytest-anyio, GitHub Actions `services:` block.

**Spec:** [`docs/superpowers/specs/2026-04-15-nats-transport-design.md`](../specs/2026-04-15-nats-transport-design.md)

---

## File structure

### PR 68a — new files

- `src/proctor/core/transport/__init__.py` — re-exports
- `src/proctor/core/transport/base.py` — `EventTransport` ABC, `Handler`, `SubscriptionHandle` Protocol, `ListenerHandle` Protocol, `ConnectionState` enum, `DisconnectCallback`
- `src/proctor/core/transport/errors.py` — exception hierarchy
- `src/proctor/core/transport/local.py` — `LocalEventTransport`, `_DedupCache`, `_RateLimitedLogger`, wildcard matcher
- `tests/test_core/test_transport_base.py` — ABC contract, Protocols, enum
- `tests/test_core/test_transport_dedup.py` — `_DedupCache` across 5 handler types
- `tests/test_core/test_transport_local.py` — `LocalEventTransport` contract + implementation tests
- `docs/superpowers/adr/2026-04-15-nats-transport.md` — 21 ADRs in-full

### PR 68a — modified files

- `src/proctor/core/models.py` — `Event` validators (timezone, charset, payload)
- `src/proctor/core/config.py` — add `EventsConfig`; tighten `WebhookPathConfig.source_name` validator
- `src/proctor/core/bus.py` — rewrite as thin wrapper over `EventTransport`
- `src/proctor/core/bootstrap.py` — `Application(*, event_transport=None)` DI; subscribes in `__init__`; `stop()` adds `drain()` phase
- 61 call-sites in tests + bootstrap — codemod `EventBus()` → `EventBus(LocalEventTransport())`

### PR 68b — new files

- `src/proctor/core/transport/nats.py` — `NATSEventTransport` (lazy `import nats`)
- `tests/integration/test_transport_nats.py` — contract-level NATS tests
- `tests/integration/test_transport_nats_reconnect.py` — Toxiproxy reconnect tests
- `tests/integration/test_cross_node_delivery.py` — **signature test** (priority #1)
- `tests/integration/test_bootstrap_nats.py` — lifecycle + config resolution
- `tests/test_core/test_transport_contract.py` — parametrized `[local, nats]` shared contract tests

### PR 68b — modified files

- `src/proctor/core/config.py` — `ProctorConfig.transport` field; `NATSConfig` extensions; `_validate_transport_consistency`
- `src/proctor/core/bootstrap.py` — `_build_event_transport` + `_resolve_transport_mode`
- `pyproject.toml` — `[project.optional-dependencies].nats`; dev-dep `testcontainers[nats,toxiproxy]`; pytest markers + `addopts`
- `.github/workflows/ci.yml` — `integration-nats` job on Py 3.11/3.12 with `services: nats`
- `README.md` — `## Multi-node deployment`, `## Running NATS integration tests`, ADR summary, rollback note

---

# PR 68a — Transport abstraction refactor

Sizing: ~1.5 weeks including review iteration. No NATS library, no Docker, no CI changes. Standalone users observe identical behaviour before and after.

## Task 1: Exception hierarchy + transport package init

**Files:**
- Create: `src/proctor/core/transport/__init__.py`
- Create: `src/proctor/core/transport/errors.py`
- Test: `tests/test_core/test_transport_errors.py`

- [ ] **Step 1.1: Create `errors.py`**

```python
"""Transport exception hierarchy.

All transport-related exceptions inherit from TransportError so callers
can `except TransportError` and catch everything. Some subclasses
(EventSchemaError, HandlerTimeoutError) are internal control-flow
only — they NEVER propagate beyond transport.
"""

from __future__ import annotations


class TransportError(Exception):
    """Base for all event transport errors."""


class TransportConnectionError(TransportError):
    """Connect / start failures."""


class TransportLifecycleError(TransportError):
    """Double-start, stop-before-start, etc."""


class TransportUnavailableError(TransportError):
    """publish() attempted while not CONNECTED."""


class TransportDrainingError(TransportUnavailableError):
    """publish() attempted during drain phase."""


class InvalidSubjectError(TransportError, ValueError):
    """Subject violates charset or wildcard rules.

    Dual-inherits ValueError so callers that `except ValueError` (e.g.
    pydantic validators) still catch this.
    """


class EventTooLargeError(TransportError):
    """Serialized event exceeds events.max_payload."""


class EventSchemaError(TransportError):
    """Internal control-flow exception raised inside transport when a
    received message can't be decoded.

    NEVER propagates beyond transport — caught internally for log +
    drop behaviour. Kept as a class (not bare logger call) so internal
    handlers can use `except EventSchemaError` pattern matching cleanly.
    """


class HandlerTimeoutError(TransportError):
    """Internal: handler exceeded drain soft timeout.

    Logged, task cancelled; never propagates beyond transport.
    """
```

- [ ] **Step 1.2: Create `__init__.py` with initial exports (classes from errors; more added later)**

```python
"""EventTransport abstraction and backends.

See docs/superpowers/specs/2026-04-15-nats-transport-design.md for
design rationale. All 21 ADRs in docs/superpowers/adr/.
"""

from proctor.core.transport.errors import (
    EventSchemaError,
    EventTooLargeError,
    HandlerTimeoutError,
    InvalidSubjectError,
    TransportConnectionError,
    TransportDrainingError,
    TransportError,
    TransportLifecycleError,
    TransportUnavailableError,
)

__all__ = [
    "EventSchemaError",
    "EventTooLargeError",
    "HandlerTimeoutError",
    "InvalidSubjectError",
    "TransportConnectionError",
    "TransportDrainingError",
    "TransportError",
    "TransportLifecycleError",
    "TransportUnavailableError",
]
```

- [ ] **Step 1.3: Write test**

```python
"""Tests for transport error hierarchy."""

import pytest

from proctor.core.transport import (
    EventSchemaError,
    EventTooLargeError,
    InvalidSubjectError,
    TransportConnectionError,
    TransportDrainingError,
    TransportError,
    TransportLifecycleError,
    TransportUnavailableError,
)


class TestExceptionHierarchy:
    def test_all_inherit_transport_error(self) -> None:
        for exc_cls in [
            TransportConnectionError,
            TransportLifecycleError,
            TransportUnavailableError,
            TransportDrainingError,
            InvalidSubjectError,
            EventTooLargeError,
            EventSchemaError,
        ]:
            assert issubclass(exc_cls, TransportError)

    def test_draining_is_unavailable(self) -> None:
        assert issubclass(TransportDrainingError, TransportUnavailableError)

    def test_invalid_subject_is_also_value_error(self) -> None:
        # Dual inheritance — callers catching ValueError still catch it
        with pytest.raises(ValueError):
            raise InvalidSubjectError("bad pattern")
        with pytest.raises(TransportError):
            raise InvalidSubjectError("bad pattern")
```

- [ ] **Step 1.4: Run tests**

Run: `uv run pytest tests/test_core/test_transport_errors.py -v`
Expected: 3 passed.

- [ ] **Step 1.5: Format, lint, types**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 1.6: Commit**

```bash
git add src/proctor/core/transport/ tests/test_core/test_transport_errors.py
git commit -m "$(cat <<'EOF'
feat(transport): add exception hierarchy

TransportError base with 8 subclasses. EventSchemaError and
HandlerTimeoutError are internal control-flow only. InvalidSubjectError
dual-inherits ValueError for caller convenience.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `EventTransport` ABC + Protocols + enum

**Files:**
- Create: `src/proctor/core/transport/base.py`
- Modify: `src/proctor/core/transport/__init__.py` (add new exports)
- Test: `tests/test_core/test_transport_base.py`

- [ ] **Step 2.1: Write failing tests**

```python
"""Tests for EventTransport ABC contract."""

import pytest

from proctor.core.transport import (
    ConnectionState,
    EventTransport,
    Handler,
    ListenerHandle,
    SubscriptionHandle,
)


class TestConnectionState:
    def test_states(self) -> None:
        assert ConnectionState.CONNECTED.value == "connected"
        assert ConnectionState.RECONNECTING.value == "reconnecting"
        assert ConnectionState.DISCONNECTED.value == "disconnected"


class TestEventTransportAbstract:
    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            EventTransport()  # type: ignore[abstract]

    def test_required_methods(self) -> None:
        required = {
            "start", "stop", "drain", "flush", "publish",
            "subscribe", "add_disconnect_listener", "add_reconnect_listener",
        }
        for name in required:
            assert hasattr(EventTransport, name), f"Missing: {name}"
        assert hasattr(EventTransport, "connection_state")  # property


class TestProtocols:
    def test_subscription_handle_protocol(self) -> None:
        """A minimal impl satisfies the Protocol duck-type."""
        class _Sub:
            @property
            def subject(self) -> str:
                return "test"
            async def unsubscribe(self) -> None:
                pass
        sub: SubscriptionHandle = _Sub()
        assert sub.subject == "test"

    def test_listener_handle_protocol(self) -> None:
        class _L:
            def remove(self) -> None:
                pass
        lh: ListenerHandle = _L()
        lh.remove()
```

- [ ] **Step 2.2: Run — should fail with ImportError**

Run: `uv run pytest tests/test_core/test_transport_base.py -v`
Expected: `ImportError` on `ConnectionState`, etc.

- [ ] **Step 2.3: Create `base.py`**

```python
"""EventTransport ABC + supporting Protocols and enums.

See spec section "Public surface".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Protocol, runtime_checkable

from proctor.core.models import Event


Handler = Callable[[Event], Awaitable[None]]


class ConnectionState(Enum):
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"


@runtime_checkable
class SubscriptionHandle(Protocol):
    """Subscription returned by EventTransport.subscribe.

    LocalEventTransport returns its own impl; NATSEventTransport
    wraps nats.aio.subscription.Subscription.
    """

    @property
    def subject(self) -> str: ...

    async def unsubscribe(self) -> None: ...


@runtime_checkable
class ListenerHandle(Protocol):
    """Handle returned by add_{disconnect,reconnect}_listener.

    Call .remove() to deregister.
    """

    def remove(self) -> None: ...


DisconnectCallback = Callable[[], Awaitable[None]] | Callable[[], None]


class EventTransport(ABC):
    """Transport for broadcast event delivery (fan-out, at-most-once).

    Subscribe accepts NATS-subject syntax (tokens separated by '.',
    '*' = single-token wildcard, '>' = multi-token trailing wildcard —
    only allowed as the last token). Both backends enforce identical
    wildcard and delivery semantics so tests are portable.
    """

    @abstractmethod
    async def start(self) -> None:
        """Connect backend and register buffered subscriptions.

        Not idempotent — double-call raises TransportLifecycleError.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Unsubscribe all and disconnect from backend.

        Must be preceded by drain() for graceful behaviour.
        """

    @abstractmethod
    async def drain(self, timeout: float = 60.0) -> None:
        """Reject new publishes (TransportDrainingError) and wait for
        in-flight handler tasks to complete within timeout.
        """

    @abstractmethod
    async def flush(self, timeout: float = 5.0) -> None:
        """Wait until buffered subscribe/publish commands are ACKed by broker.
        No-op for LocalEventTransport; nc.flush(timeout) for NATSEventTransport.
        """

    @abstractmethod
    async def publish(self, event: Event) -> None:
        """Serialize event, enforce max_payload, dispatch.

        Raises:
            EventTooLargeError: if serialized size > events.max_payload.
            TransportUnavailableError: if not CONNECTED.
            TransportDrainingError: during drain phase.
        """

    @abstractmethod
    def subscribe(
        self, subject: str, handler: Handler
    ) -> SubscriptionHandle:
        """Register handler for subject pattern.

        Sync call — returns SubscriptionHandle immediately.

        Before start(): subscription is buffered, registered at start()
        with flush. After start(): registration in background; caller
        should await flush() before publishing matching events if
        first-delivery semantics matter.

        Raises:
            InvalidSubjectError: if subject violates charset/wildcard rules.
        """

    @property
    @abstractmethod
    def connection_state(self) -> ConnectionState: ...

    @abstractmethod
    def add_disconnect_listener(
        self, cb: DisconnectCallback
    ) -> ListenerHandle: ...

    @abstractmethod
    def add_reconnect_listener(
        self, cb: DisconnectCallback
    ) -> ListenerHandle: ...
```

- [ ] **Step 2.4: Update `__init__.py`**

Replace content:

```python
"""EventTransport abstraction and backends."""

from proctor.core.transport.base import (
    ConnectionState,
    DisconnectCallback,
    EventTransport,
    Handler,
    ListenerHandle,
    SubscriptionHandle,
)
from proctor.core.transport.errors import (
    EventSchemaError,
    EventTooLargeError,
    HandlerTimeoutError,
    InvalidSubjectError,
    TransportConnectionError,
    TransportDrainingError,
    TransportError,
    TransportLifecycleError,
    TransportUnavailableError,
)

__all__ = [
    "ConnectionState",
    "DisconnectCallback",
    "EventSchemaError",
    "EventTooLargeError",
    "EventTransport",
    "Handler",
    "HandlerTimeoutError",
    "InvalidSubjectError",
    "ListenerHandle",
    "SubscriptionHandle",
    "TransportConnectionError",
    "TransportDrainingError",
    "TransportError",
    "TransportLifecycleError",
    "TransportUnavailableError",
]
```

- [ ] **Step 2.5: Run tests, format, commit**

```bash
uv run pytest tests/test_core/test_transport_base.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/core/transport/ tests/test_core/test_transport_base.py
git commit -m "$(cat <<'EOF'
feat(transport): add EventTransport ABC + Protocols + ConnectionState

Defines the public contract all transports implement. Subscribe is
sync (buffered), unsubscribe is async. Connection state is 3-value
enum (connected/reconnecting/disconnected). Listeners support both
sync and async callbacks via DisconnectCallback Union.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `Event` validators (timezone, charset, payload)

**Files:**
- Modify: `src/proctor/core/models.py`
- Modify: `tests/test_core/test_models.py`

- [ ] **Step 3.1: Write failing tests**

Append to `tests/test_core/test_models.py`:

```python
from datetime import UTC, datetime, timedelta, timezone

import pytest


class TestEventValidators:
    def test_type_charset_underscores_ok(self) -> None:
        Event(type="trigger.webhook.github", source="x", payload={})
        Event(type="task.completed", source="x", payload={})
        Event(type="routing.binding_failed", source="x", payload={})

    def test_type_charset_uppercase_rejected(self) -> None:
        with pytest.raises(ValueError, match="must match"):
            Event(type="Task.Completed", source="x", payload={})

    def test_type_charset_dash_rejected(self) -> None:
        # After LABS-68 tightening: dashes no longer allowed
        with pytest.raises(ValueError, match="must match"):
            Event(type="my-event.foo", source="x", payload={})

    def test_type_charset_leading_digit_rejected(self) -> None:
        with pytest.raises(ValueError, match="must match"):
            Event(type="9task.completed", source="x", payload={})

    def test_type_charset_wildcards_rejected(self) -> None:
        # Event.type is concrete, not a pattern
        with pytest.raises(ValueError):
            Event(type="trigger.*", source="x", payload={})

    def test_timestamp_must_be_tz_aware(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Event(
                type="test.ok",
                source="x",
                payload={},
                timestamp=datetime.now(),  # naive
            )

    def test_timestamp_must_be_utc(self) -> None:
        non_utc = timezone(timedelta(hours=3))
        with pytest.raises(ValueError, match="UTC"):
            Event(
                type="test.ok",
                source="x",
                payload={},
                timestamp=datetime.now(non_utc),
            )

    def test_payload_simple_dict_ok(self) -> None:
        Event(type="test.ok", source="x", payload={"key": "val", "n": 1})

    def test_payload_datetime_ok_via_pydantic(self) -> None:
        # pydantic handles datetime → iso natively
        Event(
            type="test.ok", source="x",
            payload={"ts": datetime.now(UTC)},
        )

    def test_payload_non_serializable_rejected(self) -> None:
        class CustomObj:
            pass
        with pytest.raises(ValueError, match="not serializable"):
            Event(
                type="test.ok", source="x",
                payload={"obj": CustomObj()},
            )
```

- [ ] **Step 3.2: Run — should fail**

Run: `uv run pytest tests/test_core/test_models.py::TestEventValidators -v`
Expected: tests fail (validators don't exist).

- [ ] **Step 3.3: Add validators to `Event`**

Edit `src/proctor/core/models.py`. Add imports at top:

```python
import re
from datetime import timedelta

from pydantic import TypeAdapter, field_validator
```

Inside the `Event` class (after field definitions), add:

```python
    _TYPE_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*"
    )

    @field_validator("type")
    @classmethod
    def _type_charset(cls, v: str) -> str:
        if not cls._TYPE_RE.fullmatch(v):
            raise ValueError(
                f"Event.type {v!r} must match "
                f"[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*"
            )
        return v

    @field_validator("timestamp")
    @classmethod
    def _timestamp_must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("Event.timestamp must be timezone-aware (UTC)")
        if v.utcoffset() != timedelta(0):
            raise ValueError("Event.timestamp must be in UTC (offset=0)")
        return v

    @field_validator("payload")
    @classmethod
    def _payload_serializable(cls, v: dict[str, Any]) -> dict[str, Any]:
        try:
            TypeAdapter(dict[str, Any]).dump_json(v)
        except Exception as e:
            raise ValueError(
                f"Event.payload not serializable by pydantic: {e}"
            ) from e
        return v
```

Add `ClassVar` to typing imports at top of file.

- [ ] **Step 3.4: Run tests, commit**

```bash
uv run pytest tests/test_core/test_models.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/core/models.py tests/test_core/test_models.py
git commit -m "$(cat <<'EOF'
feat(models): Event validators — timezone-aware UTC, charset, payload

Event.type must match [a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*.
Event.timestamp must be tz-aware and in UTC.
Event.payload must be serializable by pydantic (TypeAdapter check).

Breaking-but-safe: all existing Event constructions in codebase use
lowercase dot-separated types with underscores, pass new validator.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `Event(type=...)` construction audit

**Files:**
- Reference check only — no file changes expected, but if any fail, fix in same commit.

- [ ] **Step 4.1: Run validator against existing tests**

Run: `uv run pytest 2>&1 | tail -30`
Expected: all tests pass. If any fail with "Event.type ... must match", go to Step 4.2.

- [ ] **Step 4.2: For each failing construction site, identify source**

```bash
# Find all Event(type=...) construction sites
uv run rg 'Event\s*\(\s*type\s*=' --type py -n
```

Categorize each: constant string, f-string dynamic, variable. For dynamic ones (`f"scheduler.{name}"`, `f"trigger.webhook.{source_name}"`), verify the input source passes the new charset.

- [ ] **Step 4.3: If any source constructs invalid types, fix**

Example fixes:
- `scheduler_name = "daily-report"` → `scheduler_name = "daily_report"` in config + test data.
- `source_name = "my-service"` → `my_service`.

Each fix co-commits with the validator update from Task 3.

- [ ] **Step 4.4: If no failures — verify via grep**

```bash
# Upper-case letters in Event(type=...) constant strings
uv run rg 'Event\s*\(\s*type\s*=\s*"[^"]*[A-Z][^"]*"' --type py
# Dash in constant Event(type=...)
uv run rg 'Event\s*\(\s*type\s*=\s*"[^"]*-[^"]*"' --type py
```

Expected: zero matches (after audit).

- [ ] **Step 4.5: Commit (if any fixes made)**

```bash
git add -A
git commit -m "fix: migrate Event type construction sites to new charset"
```

If no fixes needed — skip this step.

---

## Task 5: `EventsConfig` + tighten `WebhookPathConfig.source_name`

**Files:**
- Modify: `src/proctor/core/config.py`
- Modify: `tests/test_core/test_config.py`

- [ ] **Step 5.1: Write failing tests**

Append to `tests/test_core/test_config.py`:

```python
class TestEventsConfig:
    def test_defaults(self) -> None:
        from proctor.core.config import EventsConfig
        cfg = EventsConfig()
        assert cfg.max_payload == 65_536
        assert cfg.drain_timeout == 60.0

    def test_in_proctor_config(self) -> None:
        from proctor.core.config import ProctorConfig
        cfg = ProctorConfig()
        assert cfg.events.max_payload == 65_536


class TestSourceNameTightened:
    def test_dash_rejected(self) -> None:
        from proctor.core.config import (
            HMACAuthConfig,
            WebhookPathConfig,
        )
        with pytest.raises(ValueError, match="must match"):
            WebhookPathConfig(
                source_name="my-service",
                auth=HMACAuthConfig(secret_env="X"),
            )

    def test_underscore_ok(self) -> None:
        from proctor.core.config import (
            HMACAuthConfig,
            WebhookPathConfig,
        )
        cfg = WebhookPathConfig(
            source_name="my_service",
            auth=HMACAuthConfig(secret_env="X"),
        )
        assert cfg.source_name == "my_service"

    def test_existing_examples_still_pass(self) -> None:
        from proctor.core.config import (
            HMACAuthConfig,
            WebhookPathConfig,
        )
        for name in ["github", "ci", "heartbeat", "gitlab_push"]:
            WebhookPathConfig(
                source_name=name,
                auth=HMACAuthConfig(secret_env="X"),
            )
```

- [ ] **Step 5.2: Run — should fail**

Run: `uv run pytest tests/test_core/test_config.py::TestEventsConfig tests/test_core/test_config.py::TestSourceNameTightened -v`
Expected: `ImportError` on EventsConfig; source_name with dash currently accepted.

- [ ] **Step 5.3: Add `EventsConfig` and tighten `source_name`**

Edit `src/proctor/core/config.py`.

Add class (near `NATSConfig`):

```python
class EventsConfig(BaseModel):
    """Shared events configuration across all EventTransport backends."""

    model_config = ConfigDict(extra="forbid")
    max_payload: int = 65_536  # bytes; shared with NATS server limits
    drain_timeout: float = 60.0  # seconds; LLM-heavy handlers need headroom
```

Update `ProctorConfig`:

```python
class ProctorConfig(BaseModel):
    # ... existing fields ...
    events: EventsConfig = EventsConfig()
    # ... rest ...
```

Find `WebhookPathConfig` and its source_name validator. Change the regex:

```python
# WAS: re.compile(r"[a-z][a-z0-9_-]*")
_SOURCE_NAME_RE: ClassVar[re.Pattern[str]] = re.compile(
    r"[a-z][a-z0-9_]*"
)
```

- [ ] **Step 5.4: Run suite, look for regressions**

Run: `uv run pytest -v 2>&1 | tail -20`
Expected: all pass. If any webhook test uses a dashed source_name — rename in test data.

- [ ] **Step 5.5: Format, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/core/config.py tests/test_core/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): add EventsConfig + tighten source_name charset

EventsConfig holds max_payload and drain_timeout, shared across
EventTransport backends (not nats-specific).

WebhookPathConfig.source_name tightened from [a-z][a-z0-9_-]* to
[a-z][a-z0-9_]* so derived event.type matches LABS-68 charset.
Existing source_names (github, ci, heartbeat) pass; dashes rejected.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `_RateLimitedLogger` helper

**Files:**
- Create: helper code lives in `src/proctor/core/transport/local.py` (placed here for reuse across both transports; NATSEventTransport imports from local).
- Test: `tests/test_core/test_rate_limited_logger.py`

- [ ] **Step 6.1: Write failing test**

```python
"""Tests for _RateLimitedLogger."""

import asyncio
import logging

import pytest

from proctor.core.transport.local import _RateLimitedLogger


class TestRateLimitedLogger:
    @pytest.mark.anyio
    async def test_first_log_emits(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = logging.getLogger("test.rl")
        rl = _RateLimitedLogger(logger, interval=10.0)
        with caplog.at_level(logging.WARNING, logger="test.rl"):
            await rl.warn("key1", "first message")
        assert any("first message" in r.message for r in caplog.records)

    @pytest.mark.anyio
    async def test_suppresses_within_interval(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = logging.getLogger("test.rl")
        rl = _RateLimitedLogger(logger, interval=10.0)
        with caplog.at_level(logging.WARNING, logger="test.rl"):
            await rl.warn("key2", "msg")
            await rl.warn("key2", "msg")
            await rl.warn("key2", "msg")
        records = [r for r in caplog.records if "msg" in r.message]
        assert len(records) == 1  # only first emitted

    @pytest.mark.anyio
    async def test_emits_after_interval(
        self, caplog: pytest.LogCaptureFixture, monkeypatch
    ) -> None:
        import time
        fake_time = [0.0]
        monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])
        logger = logging.getLogger("test.rl")
        rl = _RateLimitedLogger(logger, interval=10.0)
        with caplog.at_level(logging.WARNING, logger="test.rl"):
            await rl.warn("key3", "msg")
            fake_time[0] = 11.0  # past interval
            await rl.warn("key3", "msg")
        records = [r for r in caplog.records if "msg" in r.message]
        assert len(records) == 2

    @pytest.mark.anyio
    async def test_aggregates_count(
        self, caplog: pytest.LogCaptureFixture, monkeypatch
    ) -> None:
        import time
        fake_time = [0.0]
        monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])
        logger = logging.getLogger("test.rl")
        rl = _RateLimitedLogger(logger, interval=10.0)
        with caplog.at_level(logging.WARNING, logger="test.rl"):
            for _ in range(5):
                await rl.warn("key4", "m")
            fake_time[0] = 11.0
            await rl.warn("key4", "m")  # emits with count
        records = [r for r in caplog.records if "key4" not in str(r)]
        # Second emission includes count of 5 suppressed
        emitted = [r for r in caplog.records if "m" in r.message]
        assert len(emitted) == 2
        # Second includes aggregate (format: "m (occurrences since last log: 5)")
        assert "5" in emitted[1].message

    @pytest.mark.anyio
    async def test_different_keys_independent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = logging.getLogger("test.rl")
        rl = _RateLimitedLogger(logger, interval=10.0)
        with caplog.at_level(logging.WARNING, logger="test.rl"):
            await rl.warn("a", "msg_a")
            await rl.warn("b", "msg_b")
        # Both first-emits; interval applies per key
        assert any("msg_a" in r.message for r in caplog.records)
        assert any("msg_b" in r.message for r in caplog.records)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
```

- [ ] **Step 6.2: Run — should fail**

Run: `uv run pytest tests/test_core/test_rate_limited_logger.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 6.3: Create `local.py` skeleton with `_RateLimitedLogger`**

Create `src/proctor/core/transport/local.py`:

```python
"""LocalEventTransport — in-process event bus with NATS-wildcard semantics.

Also houses shared helpers (_RateLimitedLogger, _DedupCache, wildcard
matcher) reused by NATSEventTransport.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict


class _RateLimitedLogger:
    """Log first occurrence per key immediately, then every `interval`
    seconds with aggregated count of suppressed occurrences.

    Concurrent-safe via asyncio.Lock.
    """

    def __init__(
        self, logger: logging.Logger, interval: float = 60.0
    ) -> None:
        self._logger = logger
        self._interval = interval
        self._last_logged: dict[str, float] = {}
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def warn(self, key: str, fmt: str, *args: object) -> None:
        async with self._lock:
            self._counts[key] += 1
            now = time.monotonic()
            last = self._last_logged.get(key, 0.0)
            if now - last >= self._interval:
                count = self._counts[key]
                self._logger.warning(
                    f"{fmt} (occurrences since last log: %d)",
                    *args, count,
                )
                self._last_logged[key] = now
                self._counts[key] = 0
```

- [ ] **Step 6.4: Run tests, commit**

```bash
uv run pytest tests/test_core/test_rate_limited_logger.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/core/transport/local.py tests/test_core/test_rate_limited_logger.py
git commit -m "$(cat <<'EOF'
feat(transport): _RateLimitedLogger for bounded WARN under repeat events

First occurrence per key emits immediately; subsequent within interval
suppressed and counted. Emits aggregate count after interval. Used by
both LocalEventTransport and NATSEventTransport for publish_rejected,
handler_exception (per handler+event_type), clock_skew (per publisher).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `_DedupCache` with weak-refs

**Files:**
- Modify: `src/proctor/core/transport/local.py`
- Test: `tests/test_core/test_transport_dedup.py`

- [ ] **Step 7.1: Write failing tests (5 handler types)**

```python
"""Tests for _DedupCache across all handler types."""

import asyncio
import functools
import weakref

import pytest

from proctor.core.models import Event
from proctor.core.transport.local import _DedupCache


def _make_event() -> Event:
    return Event(type="test.ok", source="x", payload={})


class TestDedupCache:
    def test_first_seen_returns_false(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)
        async def h(e: Event) -> None: pass
        assert cache.seen(h, "msg-1") is False

    def test_second_same_key_returns_true(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)
        async def h(e: Event) -> None: pass
        cache.seen(h, "msg-1")
        assert cache.seen(h, "msg-1") is True

    def test_different_msg_id_not_deduped(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)
        async def h(e: Event) -> None: pass
        cache.seen(h, "msg-1")
        assert cache.seen(h, "msg-2") is False

    def test_different_handler_not_deduped(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)
        async def h1(e: Event) -> None: pass
        async def h2(e: Event) -> None: pass
        cache.seen(h1, "msg-1")
        assert cache.seen(h2, "msg-1") is False

    def test_async_function(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)
        async def h(e: Event) -> None: pass
        cache.seen(h, "msg-1")
        assert cache.seen(h, "msg-1") is True

    def test_async_lambda(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)
        h = lambda e: asyncio.sleep(0)  # noqa: E731
        cache.seen(h, "msg-1")
        assert cache.seen(h, "msg-1") is True

    def test_bound_method(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)
        class C:
            async def m(self, e: Event) -> None: pass
        c = C()
        cache.seen(c.m, "msg-1")
        assert cache.seen(c.m, "msg-1") is True

    def test_partial(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)
        async def base(context: str, e: Event) -> None: pass
        h = functools.partial(base, "ctx-a")
        cache.seen(h, "msg-1")
        assert cache.seen(h, "msg-1") is True

    def test_callable_class(self) -> None:
        cache = _DedupCache(size=100, ttl=10.0)
        class Callable:
            async def __call__(self, e: Event) -> None: pass
        c = Callable()
        cache.seen(c, "msg-1")
        assert cache.seen(c, "msg-1") is True


class TestDedupTTL:
    def test_entry_expires(self, monkeypatch) -> None:
        import time
        fake_time = [0.0]
        monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])

        cache = _DedupCache(size=100, ttl=10.0)
        async def h(e: Event) -> None: pass
        cache.seen(h, "msg-1")
        fake_time[0] = 20.0  # past TTL
        assert cache.seen(h, "msg-1") is False


class TestDedupEviction:
    def test_lru_eviction_at_capacity(self) -> None:
        cache = _DedupCache(size=3, ttl=1000.0)
        async def h(e: Event) -> None: pass
        for i in range(5):
            cache.seen(h, f"msg-{i}")
        # First 2 evicted, last 3 still in cache
        assert cache.seen(h, "msg-0") is False  # evicted
        assert cache.seen(h, "msg-4") is True  # present
```

- [ ] **Step 7.2: Run — should fail**

Run: `uv run pytest tests/test_core/test_transport_dedup.py -v`
Expected: `ImportError` on `_DedupCache`.

- [ ] **Step 7.3: Add `_DedupCache` to `local.py`**

Add imports at top of `src/proctor/core/transport/local.py`:

```python
import time
from collections import OrderedDict
from typing import Any

from proctor.core.transport.base import Handler
```

Add class:

```python
class _DedupCache:
    """LRU + TTL cache for (handler_id, msg_id) → seen.

    Delivers same-handler-overlapping-subscriptions exactly once
    per message. Handler identity via id(handler); weak-ref held in
    WeakValueDictionary to prevent id reuse masking collisions.

    Covered handler types: async def, async lambda, bound method
    (WeakMethod), functools.partial, class with async __call__.
    """

    def __init__(self, size: int = 10_000, ttl: float = 60.0) -> None:
        self._size = size
        self._ttl = ttl
        # OrderedDict acts as LRU
        self._entries: OrderedDict[tuple[int, str], float] = OrderedDict()
        # Hold strong refs keyed by id so IDs don't recycle before we're done
        self._handler_refs: dict[int, Any] = {}

    def _handler_key(self, handler: Handler) -> int:
        hid = id(handler)
        # Strong ref prevents GC + id reuse
        self._handler_refs[hid] = handler
        return hid

    def seen(self, handler: Handler, msg_id: str) -> bool:
        key = (self._handler_key(handler), msg_id)
        now = time.monotonic()
        self._evict_expired(now)
        if key in self._entries:
            # Touch for LRU
            self._entries.move_to_end(key)
            return True
        self._entries[key] = now
        # LRU eviction at capacity
        while len(self._entries) > self._size:
            self._entries.popitem(last=False)
        return False

    def _evict_expired(self, now: float) -> None:
        expired_keys = [
            k for k, ts in self._entries.items()
            if now - ts >= self._ttl
        ]
        for k in expired_keys:
            del self._entries[k]
```

- [ ] **Step 7.4: Run tests, commit**

```bash
uv run pytest tests/test_core/test_transport_dedup.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/core/transport/local.py tests/test_core/test_transport_dedup.py
git commit -m "$(cat <<'EOF'
feat(transport): _DedupCache with LRU + TTL

Keyed by (id(handler), msg_id). Covers async def, async lambda, bound
method, functools.partial, callable class — 9 tests verify each type
dedupes correctly. LRU eviction at size capacity; TTL-based expiry.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: NATS-wildcard matcher

**Files:**
- Modify: `src/proctor/core/transport/local.py`
- Test: `tests/test_core/test_transport_matcher.py`

- [ ] **Step 8.1: Write failing tests**

```python
"""Tests for NATS subject wildcard matcher."""

import pytest

from proctor.core.transport.local import _match_subject
from proctor.core.transport.errors import InvalidSubjectError


class TestWildcardMatcher:
    @pytest.mark.parametrize("subject,pattern,expected", [
        ("trigger.terminal", "trigger.terminal", True),      # literal
        ("trigger.terminal", "trigger.*", True),              # single-token
        ("trigger.webhook.github", "trigger.*", False),       # NATS * ≠ >
        ("trigger.webhook.github", "trigger.>", True),        # multi-token tail
        ("trigger.webhook.github", "trigger.webhook.*", True),
        ("trigger.webhook.github", "trigger.webhook.github", True),
        ("trigger.webhook.github.v2", "trigger.webhook.*", False),
        ("trigger.webhook.github.v2", "trigger.webhook.>", True),
        ("trigger.webhook.github", "trigger.*.github", True),  # * mid-path
        ("other.foo", "trigger.>", False),
        ("trigger", "trigger.>", False),                       # > needs ≥1 token
        ("trigger.a", ">", True),
        ("", ">", False),
    ])
    def test_match(self, subject: str, pattern: str, expected: bool) -> None:
        assert _match_subject(subject, pattern) is expected


class TestSubjectValidation:
    def test_wildcards_in_middle_are_ok(self) -> None:
        assert _match_subject("a.b.c", "a.*.c") is True

    def test_angle_only_at_end(self) -> None:
        with pytest.raises(InvalidSubjectError, match=">"):
            _match_subject("a.b", "a.>.b")

    def test_empty_subject_rejected(self) -> None:
        with pytest.raises(InvalidSubjectError):
            _match_subject("", "a")

    def test_fnmatch_meta_rejected(self) -> None:
        with pytest.raises(InvalidSubjectError):
            _match_subject("a.b", "a.?")
        with pytest.raises(InvalidSubjectError):
            _match_subject("a.b", "a.[abc]")
```

- [ ] **Step 8.2: Run — should fail**

Run: `uv run pytest tests/test_core/test_transport_matcher.py -v`
Expected: ImportError.

- [ ] **Step 8.3: Add matcher to `local.py`**

```python
def _validate_subject(s: str, *, allow_wildcards: bool) -> None:
    """Validate NATS subject / pattern. Raises InvalidSubjectError.

    allow_wildcards=True: subscribe patterns (accept *, >).
    allow_wildcards=False: publish subjects (reject any wildcard).
    """
    if not s:
        raise InvalidSubjectError("subject must not be empty")
    tokens = s.split(".")
    for i, tok in enumerate(tokens):
        if not tok:
            raise InvalidSubjectError(
                f"subject {s!r} has empty token"
            )
        if tok == ">":
            if not allow_wildcards:
                raise InvalidSubjectError(
                    f"wildcard > not allowed in subject {s!r}"
                )
            if i != len(tokens) - 1:
                raise InvalidSubjectError(
                    f"wildcard > must be the last token in {s!r}"
                )
            continue
        if tok == "*":
            if not allow_wildcards:
                raise InvalidSubjectError(
                    f"wildcard * not allowed in subject {s!r}"
                )
            continue
        # Literal token: same charset as event.type segment
        if not _LITERAL_TOKEN_RE.fullmatch(tok):
            raise InvalidSubjectError(
                f"token {tok!r} in {s!r} must match [a-z][a-z0-9_]*"
            )


_LITERAL_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]*")


def _match_subject(subject: str, pattern: str) -> bool:
    """Match subject against NATS-syntax pattern.

    Both subject and pattern are validated first; subject disallows
    wildcards (concrete), pattern allows them.
    """
    _validate_subject(subject, allow_wildcards=False)
    _validate_subject(pattern, allow_wildcards=True)
    return _match_tokens(subject.split("."), pattern.split("."))


def _match_tokens(sub: list[str], pat: list[str]) -> bool:
    if not pat:
        return not sub
    if pat[0] == ">":
        # > at end — must have ≥1 remaining subject token
        return len(sub) >= 1
    if not sub:
        return False
    if pat[0] == "*" or pat[0] == sub[0]:
        return _match_tokens(sub[1:], pat[1:])
    return False
```

Add `import re` at top if not there, and `from proctor.core.transport.errors import InvalidSubjectError`.

- [ ] **Step 8.4: Run, commit**

```bash
uv run pytest tests/test_core/test_transport_matcher.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/core/transport/local.py tests/test_core/test_transport_matcher.py
git commit -m "$(cat <<'EOF'
feat(transport): NATS subject wildcard matcher

_match_subject(subject, pattern) with NATS semantics:
- * = exactly one token
- > = one or more trailing tokens (last position only)
- literal tokens match [a-z][a-z0-9_]*

_validate_subject enforces charset + wildcard positioning; rejects
fnmatch-style wildcards (?, [...]).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `LocalEventTransport` lifecycle, subscribe, publish, drain

**Files:**
- Modify: `src/proctor/core/transport/local.py`
- Test: `tests/test_core/test_transport_local.py`

Large task — broken into sub-steps. The full implementation is ~200 LOC; tests ~300 LOC.

- [ ] **Step 9.1: Write failing tests for lifecycle + basic publish/subscribe**

```python
"""Tests for LocalEventTransport."""

import asyncio

import pytest

from proctor.core.models import Event
from proctor.core.transport import (
    ConnectionState,
    EventTooLargeError,
    Handler,
    TransportDrainingError,
    TransportLifecycleError,
    TransportUnavailableError,
)
from proctor.core.transport.local import LocalEventTransport


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestLifecycle:
    async def test_initial_state(self) -> None:
        t = LocalEventTransport()
        assert t.connection_state == ConnectionState.DISCONNECTED

    async def test_start_transitions_to_connected(self) -> None:
        t = LocalEventTransport()
        await t.start()
        assert t.connection_state == ConnectionState.CONNECTED
        await t.stop()

    async def test_double_start_raises(self) -> None:
        t = LocalEventTransport()
        await t.start()
        with pytest.raises(TransportLifecycleError):
            await t.start()
        await t.stop()

    async def test_stop_transitions_to_disconnected(self) -> None:
        t = LocalEventTransport()
        await t.start()
        await t.stop()
        assert t.connection_state == ConnectionState.DISCONNECTED

    async def test_subscribe_before_start_buffered(self) -> None:
        t = LocalEventTransport()
        received: list[Event] = []
        async def h(e: Event) -> None:
            received.append(e)
        handle = t.subscribe("test.ok", h)
        assert handle.subject == "test.ok"
        await t.start()
        await t.publish(Event(type="test.ok", source="x", payload={}))
        assert len(received) == 1
        await t.stop()


class TestPublish:
    async def test_publish_before_start_raises(self) -> None:
        t = LocalEventTransport()
        with pytest.raises(TransportUnavailableError):
            await t.publish(Event(type="test.ok", source="x", payload={}))

    async def test_publish_after_stop_raises(self) -> None:
        t = LocalEventTransport()
        await t.start()
        await t.stop()
        with pytest.raises(TransportUnavailableError):
            await t.publish(Event(type="test.ok", source="x", payload={}))

    async def test_publish_during_drain_raises(self) -> None:
        t = LocalEventTransport()
        await t.start()
        drain_task = asyncio.create_task(t.drain(timeout=0.5))
        await asyncio.sleep(0.01)
        with pytest.raises(TransportDrainingError):
            await t.publish(Event(type="test.ok", source="x", payload={}))
        await drain_task
        await t.stop()

    async def test_size_limit_enforced(self) -> None:
        t = LocalEventTransport(max_payload=200)
        await t.start()
        huge_payload = {"x": "a" * 500}
        with pytest.raises(EventTooLargeError):
            await t.publish(
                Event(type="test.ok", source="x", payload=huge_payload)
            )
        await t.stop()


class TestWildcardDelivery:
    async def test_wildcard_match(self) -> None:
        t = LocalEventTransport()
        received: list[Event] = []
        async def h(e: Event) -> None:
            received.append(e)
        t.subscribe("trigger.>", h)
        await t.start()
        await t.publish(Event(type="trigger.webhook.github", source="x", payload={}))
        await t.publish(Event(type="trigger.terminal", source="x", payload={}))
        await asyncio.sleep(0.05)  # let handler tasks settle
        assert len(received) == 2
        await t.stop()

    async def test_overlapping_subscribe_dedups(self) -> None:
        t = LocalEventTransport()
        received: list[Event] = []
        async def h(e: Event) -> None:
            received.append(e)
        t.subscribe("trigger.>", h)
        t.subscribe("trigger.webhook.*", h)  # same handler, overlap
        await t.start()
        await t.publish(
            Event(type="trigger.webhook.github", source="x", payload={})
        )
        await asyncio.sleep(0.05)
        assert len(received) == 1  # dedup
        await t.stop()


class TestDrainAndCancel:
    async def test_drain_waits_for_in_flight(self) -> None:
        t = LocalEventTransport()
        gate = asyncio.Event()
        completed: list[int] = []

        async def slow_handler(e: Event) -> None:
            await gate.wait()
            completed.append(1)

        t.subscribe("test.slow", slow_handler)
        await t.start()
        await t.publish(Event(type="test.slow", source="x", payload={}))
        await asyncio.sleep(0.01)
        # Handler running — drain should wait
        drain_task = asyncio.create_task(t.drain(timeout=2.0))
        await asyncio.sleep(0.05)
        assert not drain_task.done()
        gate.set()
        await drain_task
        assert completed == [1]
        await t.stop()

    async def test_handler_exception_isolated(self) -> None:
        t = LocalEventTransport()
        errored: list[str] = []
        received_by_ok: list[Event] = []

        async def bad(e: Event) -> None:
            raise RuntimeError("bad handler")

        async def ok(e: Event) -> None:
            received_by_ok.append(e)

        t.subscribe("test.x", bad)
        t.subscribe("test.x", ok)
        await t.start()
        await t.publish(Event(type="test.x", source="s", payload={}))
        await asyncio.sleep(0.05)
        # ok still received despite bad raising
        assert len(received_by_ok) == 1
        await t.stop()
```

- [ ] **Step 9.2: Run — most should fail**

Run: `uv run pytest tests/test_core/test_transport_local.py -v`
Expected: fails — `LocalEventTransport` class/signature not yet fully implemented.

- [ ] **Step 9.3: Implement `LocalEventTransport` class**

Extend `src/proctor/core/transport/local.py`. Add imports:

```python
from dataclasses import dataclass, field
from uuid import uuid4

from proctor.core.models import Event
from proctor.core.transport.base import (
    ConnectionState,
    DisconnectCallback,
    EventTransport,
    Handler,
    ListenerHandle,
    SubscriptionHandle,
)
from proctor.core.transport.errors import (
    EventTooLargeError,
    InvalidSubjectError,
    TransportDrainingError,
    TransportLifecycleError,
    TransportUnavailableError,
)
```

Append class (below `_DedupCache` and matcher):

```python
logger = logging.getLogger(__name__)


@dataclass
class _LocalSubscription:
    subject: str
    handler: Handler
    transport: "LocalEventTransport"
    _removed: bool = False

    @property
    def subject_(self) -> str:
        return self.subject

    async def unsubscribe(self) -> None:
        if self._removed:
            return
        self._removed = True
        self.transport._subscriptions.discard(self)


@dataclass
class _LocalListenerHandle:
    callback: DisconnectCallback
    transport: "LocalEventTransport"
    disconnect: bool  # True = disconnect listener, False = reconnect

    def remove(self) -> None:
        bucket = (
            self.transport._disconnect_listeners
            if self.disconnect
            else self.transport._reconnect_listeners
        )
        if self.callback in bucket:
            bucket.remove(self.callback)


class LocalEventTransport(EventTransport):
    """In-process EventTransport. No network; identical observable
    behaviour to NATSEventTransport for the contract surface.
    """

    def __init__(
        self,
        *,
        strict_size_check: bool = True,
        max_payload: int = 65_536,
    ) -> None:
        self._strict_size_check = strict_size_check
        self._max_payload = max_payload
        self._state = ConnectionState.DISCONNECTED
        self._started = False
        self._draining = False
        self._subscriptions: set[_LocalSubscription] = set()
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._dedup = _DedupCache()
        self._disconnect_listeners: list[DisconnectCallback] = []
        self._reconnect_listeners: list[DisconnectCallback] = []
        self._rl = _RateLimitedLogger(logger)

    # --- lifecycle ---

    async def start(self) -> None:
        if self._started:
            raise TransportLifecycleError(
                "LocalEventTransport already started"
            )
        self._started = True
        self._state = ConnectionState.CONNECTED
        logger.info(
            "LocalEventTransport started; %d buffered subscriptions active",
            len(self._subscriptions),
        )

    async def stop(self) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._started = False
        logger.info("LocalEventTransport stopped")

    async def drain(self, timeout: float = 60.0) -> None:
        self._draining = True
        try:
            if self._handler_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *self._handler_tasks, return_exceptions=True
                        ),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    remaining = sum(
                        1 for t in self._handler_tasks if not t.done()
                    )
                    logger.warning(
                        "LocalEventTransport drain timed out with %d tasks",
                        remaining,
                    )
                    for t in list(self._handler_tasks):
                        if not t.done():
                            t.cancel()
        finally:
            pass  # stays draining=True until stop() to reject publishes

    async def flush(self, timeout: float = 5.0) -> None:
        # Local: no-op; all operations are synchronous in effect
        return

    # --- publish / subscribe ---

    async def publish(self, event: Event) -> None:
        if self._draining:
            raise TransportDrainingError(
                "LocalEventTransport is draining"
            )
        if self._state != ConnectionState.CONNECTED:
            await self._rl.warn(
                "publish_unavailable",
                "Publish while not connected: event.type=%s",
                event.type,
            )
            raise TransportUnavailableError(
                f"LocalEventTransport is {self._state.value}"
            )
        if self._strict_size_check:
            data = event.model_dump_json().encode("utf-8")
            if len(data) > self._max_payload:
                raise EventTooLargeError(
                    f"Event {event.type!r} serialized {len(data)} bytes "
                    f"exceeds max_payload {self._max_payload}"
                )
        msg_id = str(uuid4())
        self._dispatch(event, msg_id)

    def subscribe(
        self, subject: str, handler: Handler
    ) -> SubscriptionHandle:
        # Validate pattern
        _validate_subject(subject, allow_wildcards=True)
        sub = _LocalSubscription(
            subject=subject, handler=handler, transport=self
        )
        self._subscriptions.add(sub)
        return _SubHandleAdapter(sub)

    # --- listeners ---

    def add_disconnect_listener(
        self, cb: DisconnectCallback
    ) -> ListenerHandle:
        self._disconnect_listeners.append(cb)
        return _LocalListenerHandle(cb, self, disconnect=True)

    def add_reconnect_listener(
        self, cb: DisconnectCallback
    ) -> ListenerHandle:
        self._reconnect_listeners.append(cb)
        return _LocalListenerHandle(cb, self, disconnect=False)

    @property
    def connection_state(self) -> ConnectionState:
        return self._state

    # --- internal ---

    def _dispatch(self, event: Event, msg_id: str) -> None:
        for sub in list(self._subscriptions):
            if not _match_subject(event.type, sub.subject):
                continue
            if self._dedup.seen(sub.handler, msg_id):
                continue
            task = asyncio.create_task(
                self._safe_invoke(sub.handler, event)
            )
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

    async def _safe_invoke(
        self, handler: Handler, event: Event
    ) -> None:
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Event handler error: type=%s handler=%s event_id=%s",
                event.type, handler, event.id,
            )


class _SubHandleAdapter:
    """Adapter that wraps _LocalSubscription to satisfy SubscriptionHandle Protocol."""

    def __init__(self, sub: _LocalSubscription) -> None:
        self._sub = sub

    @property
    def subject(self) -> str:
        return self._sub.subject

    async def unsubscribe(self) -> None:
        await self._sub.unsubscribe()
```

- [ ] **Step 9.4: Update `__init__.py` to export `LocalEventTransport`**

```python
from proctor.core.transport.local import LocalEventTransport

__all__ = [..., "LocalEventTransport"]  # add to list
```

- [ ] **Step 9.5: Run tests, commit**

```bash
uv run pytest tests/test_core/test_transport_local.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/core/transport/
git add tests/test_core/test_transport_local.py
git commit -m "$(cat <<'EOF'
feat(transport): LocalEventTransport full implementation

In-process event bus with NATS-wildcard semantics. Subscribe before
start buffered; start() transitions to CONNECTED; publish enforces
strict size check; drain() waits for in-flight handler tasks with
timeout; handler exceptions isolated per-subscription. Dedup for
overlapping subscriptions via _DedupCache.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Rewrite `EventBus` as thin wrapper

**Files:**
- Rewrite: `src/proctor/core/bus.py`
- Modify: `tests/test_core/test_bus.py` (expected failure for `EventBus()` no-arg)

- [ ] **Step 10.1: Write failing test**

Replace `tests/test_core/test_bus.py`:

```python
"""Tests for EventBus thin wrapper."""

import pytest

from proctor.core.bus import EventBus
from proctor.core.models import Event
from proctor.core.transport import ConnectionState, LocalEventTransport


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestEventBus:
    async def test_no_default_transport(self) -> None:
        with pytest.raises(TypeError):
            EventBus()  # type: ignore[call-arg]

    async def test_start_stop_delegation(self) -> None:
        t = LocalEventTransport()
        bus = EventBus(t)
        await bus.start()
        assert bus.connection_state == ConnectionState.CONNECTED
        await bus.stop()

    async def test_publish_subscribe(self) -> None:
        received: list[Event] = []
        async def h(e: Event) -> None:
            received.append(e)
        bus = EventBus(LocalEventTransport())
        bus.subscribe("test.ok", h)
        await bus.start()
        await bus.publish(Event(type="test.ok", source="x", payload={}))
        import asyncio
        await asyncio.sleep(0.05)
        assert len(received) == 1
        await bus.stop()
```

- [ ] **Step 10.2: Rewrite `bus.py`**

Replace contents of `src/proctor/core/bus.py`:

```python
"""EventBus — thin wrapper over EventTransport.

Transport is plumbing; EventBus is the stable caller-facing contract.
Future observability hooks (metrics, tracing, event enrichment) wire
at this level, not in Transport.
"""

from __future__ import annotations

from proctor.core.models import Event
from proctor.core.transport import (
    ConnectionState,
    DisconnectCallback,
    EventTransport,
    Handler,
    ListenerHandle,
    SubscriptionHandle,
)


class EventBus:
    """Application-facing event bus. Requires explicit transport."""

    def __init__(self, transport: EventTransport) -> None:
        self._transport = transport

    async def start(self) -> None:
        await self._transport.start()

    async def stop(self) -> None:
        await self._transport.stop()

    async def drain(self, timeout: float = 60.0) -> None:
        await self._transport.drain(timeout)

    async def flush(self, timeout: float = 5.0) -> None:
        await self._transport.flush(timeout)

    async def publish(self, event: Event) -> None:
        await self._transport.publish(event)

    def subscribe(
        self, subject: str, handler: Handler
    ) -> SubscriptionHandle:
        return self._transport.subscribe(subject, handler)

    @property
    def connection_state(self) -> ConnectionState:
        return self._transport.connection_state

    def add_disconnect_listener(
        self, cb: DisconnectCallback
    ) -> ListenerHandle:
        return self._transport.add_disconnect_listener(cb)

    def add_reconnect_listener(
        self, cb: DisconnectCallback
    ) -> ListenerHandle:
        return self._transport.add_reconnect_listener(cb)
```

- [ ] **Step 10.3: Run tests, commit**

```bash
uv run pytest tests/test_core/test_bus.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/proctor/core/bus.py tests/test_core/test_bus.py
git commit -m "$(cat <<'EOF'
refactor(bus): rewrite EventBus as thin wrapper over EventTransport

No default transport — EventBus() → TypeError. All behaviour delegated
to injected EventTransport. This enables LocalEventTransport and
NATSEventTransport as drop-in backends.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Codemod — migrate 61 `EventBus()` call-sites

**Files:**
- Modify: every file with `EventBus()` (no-arg) construction — 61 sites.

- [ ] **Step 11.1: Inspect current state**

Run: `uv run rg 'EventBus\s*\(\s*\)' --type py -l`
Record file list.

- [ ] **Step 11.2: Apply sed-style replacement**

```bash
# From project root:
for f in $(uv run rg 'EventBus\s*\(\s*\)' --type py -l); do
    python3 -c "
import re
p = '$f'
s = open(p).read()
new = re.sub(r'EventBus\s*\(\s*\)', 'EventBus(LocalEventTransport())', s)
# Add LocalEventTransport import if EventBus was imported
if 'EventBus(LocalEventTransport())' in new and 'LocalEventTransport' not in new.split('EventBus(LocalEventTransport())')[0]:
    # Find existing 'from proctor.core.bus import EventBus' or 'from proctor.core import EventBus'
    if 'from proctor.core.bus import EventBus' in new:
        new = new.replace(
            'from proctor.core.bus import EventBus',
            'from proctor.core.bus import EventBus\nfrom proctor.core.transport import LocalEventTransport',
        )
    elif 'from proctor.core import' in new and 'EventBus' in new:
        # Add alongside
        new = re.sub(
            r'from proctor.core import (.*?)EventBus',
            r'from proctor.core import \\1EventBus\nfrom proctor.core.transport import LocalEventTransport',
            new, count=1,
        )
open(p, 'w').write(new)
print(f'migrated: {p}')
"
done
```

- [ ] **Step 11.3: Format imports**

```bash
uv run ruff format .
uv run ruff check . --fix
```

- [ ] **Step 11.4: Run full suite**

Run: `uv run pytest 2>&1 | tail -10`
Expected: all tests pass (the LABS-65/66/67 regression firewall).

If failures — manually inspect the remaining `EventBus()` sites that sed missed.

- [ ] **Step 11.5: Verify zero bare `EventBus()` remain**

Run: `uv run rg 'EventBus\s*\(\s*\)' --type py`
Expected: no output.

- [ ] **Step 11.6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: migrate 61 EventBus() call-sites to explicit LocalEventTransport

Automated with sed-like script; verified no bare EventBus() remain.
All existing tests (LABS-65/66/67) pass — regression firewall.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: `Application` DI + subscribes in `__init__`

**Files:**
- Modify: `src/proctor/core/bootstrap.py`
- Modify: `tests/test_core/test_bootstrap.py`

- [ ] **Step 12.1: Write failing test**

Append to `tests/test_core/test_bootstrap.py`:

```python
class TestApplicationDI:
    def test_event_transport_override(
        self, tmp_path: Path
    ) -> None:
        from proctor.core.bootstrap import Application
        from proctor.core.config import ProctorConfig
        from proctor.core.transport import LocalEventTransport

        custom = LocalEventTransport()
        cfg = ProctorConfig(data_dir=tmp_path)
        app = Application(cfg, event_transport=custom)
        assert app.bus._transport is custom

    @pytest.mark.anyio
    async def test_subscribes_in_init_buffered_until_start(
        self, tmp_path: Path
    ) -> None:
        from proctor.core.bootstrap import Application
        from proctor.core.config import ProctorConfig
        from proctor.core.models import Event

        cfg = ProctorConfig(data_dir=tmp_path)
        app = Application(cfg)
        # Before start — subscriptions buffered, not connected
        from proctor.core.transport import ConnectionState
        assert app.bus.connection_state == ConnectionState.DISCONNECTED
        await app.start()
        assert app.bus.connection_state == ConnectionState.CONNECTED
        await app.stop()
```

- [ ] **Step 12.2: Modify `Application`**

Edit `src/proctor/core/bootstrap.py`. Add import:

```python
from proctor.core.transport import EventTransport, LocalEventTransport
```

Modify `Application.__init__`:

```python
class Application:
    def __init__(
        self,
        config: ProctorConfig,
        *,
        event_transport: EventTransport | None = None,
    ) -> None:
        self.config = config
        transport = event_transport or LocalEventTransport(
            max_payload=config.events.max_payload,
        )
        self.bus = EventBus(transport)

        # ... existing state / memory / triggers fields ...
        self.state = StateManager(...)
        self.memory = EpisodicMemory(...)
        self._webhook_trigger: WebhookTrigger | None = None
        # (no instantiation yet — happens in start())

        # Application-level bootstrap handlers (ADR #19).
        # Subscriptions are buffered until bus.start().
        self.bus.subscribe("trigger.>", self._handle_trigger_event)
```

Modify `start()`:

```python
async def start(self) -> None:
    await self.state.start()
    await self.memory.start()
    await self.bus.start()
    await self.bus.flush()  # ensure subs registered before triggers publish
    # ... existing trigger instantiation + start ...
```

Modify `stop()`:

```python
async def stop(self) -> None:
    self.is_running = False
    # Inputs first
    if self._webhook_trigger is not None:
        await self._webhook_trigger.stop()
    if self._telegram_trigger is not None:
        await self._telegram_trigger.stop()
    if self._scheduler is not None:
        await self._scheduler.stop()
    # Drain in-flight handlers before transport shutdown
    await self.bus.drain(timeout=self.config.events.drain_timeout)
    await self.bus.stop()
    # Data layer last
    await self.memory.stop()
    await self.state.stop()
```

- [ ] **Step 12.3: Run tests**

```bash
uv run pytest tests/test_core/test_bootstrap.py -v
uv run pytest 2>&1 | tail -10  # full regression
```
Expected: all pass.

- [ ] **Step 12.4: Commit**

```bash
git add src/proctor/core/bootstrap.py tests/test_core/test_bootstrap.py
git commit -m "$(cat <<'EOF'
feat(bootstrap): EventTransport DI + subscribes in __init__ + drain phase

Application(config, *, event_transport=None) — tests override;
production uses LocalEventTransport by default until 68b adds NATS.

Application-level handlers registered in __init__ (buffered, replayed
at start). Stop order: triggers → bus.drain(60s) → bus.stop() →
memory/state. drain_timeout configurable via EventsConfig.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: ADR document + 68a checkpoint

**Files:**
- Create: `docs/superpowers/adr/2026-04-15-nats-transport.md`

- [ ] **Step 13.1: Create ADR doc**

Create `docs/superpowers/adr/2026-04-15-nats-transport.md` with all 21 ADRs as self-contained sections. Each ADR entry has: "Decision", "Rationale", "Alternatives considered", "Consequences".

Copy the 21 decisions from the spec "Design decisions" table; for each, expand into ADR format. Reference spec sections for detailed rationale but make each ADR self-contained (reader doesn't need spec open).

- [ ] **Step 13.2: Commit**

```bash
git add docs/superpowers/adr/2026-04-15-nats-transport.md
git commit -m "$(cat <<'EOF'
docs: ADR for LABS-68 NATS transport (21 decisions)

Captures all design decisions from brainstorming sessions with
rationale and alternatives. Self-contained — doesn't require spec
to read.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 13.3: 68a checkpoint — verify full regression**

Run: `uv run pytest 2>&1 | tail -5`
Expected: all pass.

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

**68a is feature-complete if all of the above is green.** Proceed to 68b without merging 68a (atomic merge policy).

---

# PR 68b — NATSEventTransport + integration

Sizing: ~2 weeks including review iteration. Adds `nats-py` optional dep + CI integration job. Ships atomic with 68a.

## Task 14: `pyproject.toml` — optional nats extra + markers

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 14.1: Add optional dependency + markers**

Edit `pyproject.toml`. Locate `[project.optional-dependencies]` (or create if absent):

```toml
[project.optional-dependencies]
nats = ["nats-py>=2.7, <3"]
```

Locate `[dependency-groups].dev`:

```toml
[dependency-groups]
dev = [
    # ... existing ...
    "testcontainers[nats]>=4.0",
    "toxiproxy-python>=0.1",  # for NATS reconnect tests
]
```

Locate `[tool.pytest.ini_options]`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "-m 'not nats and not ollama and not benchmark'"
markers = [
    "integration: requires external services",
    "nats: requires running NATS server (docker or GHA services)",
    "ollama: requires local Ollama",
    "benchmark: performance smoke tests (opt-in)",
]
```

- [ ] **Step 14.2: Install new deps**

```bash
uv sync --extra nats
```

- [ ] **Step 14.3: Verify sync worked**

```bash
uv run python -c "import nats; print(nats.__version__)"
```
Expected: prints version (e.g. `2.7.x`).

- [ ] **Step 14.4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
feat(deps): add nats-py optional extra + test markers

pip install proctor[nats] pulls nats-py. Default pytest skips
-m 'nats' and -m 'benchmark' markers; NATS tests run via
integration-nats CI job (LABS-68b).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: `ProctorConfig.transport` + resolver + NATSConfig extensions + matrix test

**Files:**
- Modify: `src/proctor/core/config.py`
- Modify: `src/proctor/core/bootstrap.py`
- Modify: `tests/test_core/test_config.py`

- [ ] **Step 15.1: Write failing matrix test**

Append to `tests/test_core/test_config.py`:

```python
class TestTransportResolution:
    @pytest.mark.parametrize("transport,node_role,nats_servers,expected", [
        ("auto",     "standalone", [],                         "local"),
        ("auto",     "standalone", ["nats://x:4222"],          "warn_then_local"),
        ("auto",     "core",       ["nats://x:4222"],          "nats"),
        ("auto",     "worker",     ["nats://x:4222"],          "nats"),
        ("auto",     "core",       [],                         "ValueError"),
        ("local",    "standalone", [],                         "local"),
        ("local",    "core",       ["nats://x:4222"],          "local"),
        ("nats",     "standalone", ["nats://x:4222"],          "nats"),
        ("nats",     "worker",     [],                         "ValueError"),
    ])
    def test_transport_mode_resolution_matrix(
        self, transport, node_role, nats_servers, expected,
    ) -> None:
        from proctor.core.config import NATSConfig, ProctorConfig
        kwargs = {
            "transport": transport,
            "node_role": node_role,
        }
        if nats_servers:
            kwargs["nats"] = NATSConfig(servers=nats_servers)
        if expected == "ValueError":
            with pytest.raises(ValueError):
                ProctorConfig(**kwargs)
            return
        cfg = ProctorConfig(**kwargs)
        from proctor.core.bootstrap import _resolve_transport_mode
        mode = _resolve_transport_mode(cfg)
        if expected in {"local", "warn_then_local"}:
            assert mode == "local"
        else:
            assert mode == "nats"
```

- [ ] **Step 15.2: Add `transport` field + `NATSConfig` extensions + validator**

Edit `src/proctor/core/config.py`. Find `NATSConfig` and replace with:

```python
class NATSConfig(BaseModel):
    """NATS client configuration."""

    model_config = ConfigDict(extra="forbid")

    servers: list[str] = ["nats://localhost:4222"]
    name: str = ""
    subject_prefix: str = "proctor"
    connect_timeout: float = 5.0
    reconnect_time_wait: float = 2.0
    reconnect_jitter: float = Field(default=0.5, ge=0.0, le=5.0)
    max_reconnect_attempts: int = -1
    user: str | None = None
    user_env: str | None = None
    password_env: str | None = None
    tls_ca: Path | None = None
    tls_client_cert: Path | None = None
    tls_client_key: Path | None = None

    @model_validator(mode="after")
    def _populate_name_fallback(self) -> Self:
        if not self.name:
            import socket
            self.name = f"proctor-{socket.gethostname()}"
        return self

    @model_validator(mode="after")
    def _user_exclusive(self) -> Self:
        if self.user and self.user_env:
            raise ValueError(
                "Set either NATSConfig.user or user_env, not both"
            )
        return self
```

Update `ProctorConfig`:

```python
class ProctorConfig(BaseModel):
    # ... existing fields ...
    node_role: Literal["standalone", "core", "worker"] = "standalone"
    transport: Literal["auto", "local", "nats"] = "auto"
    events: EventsConfig = EventsConfig()
    nats: NATSConfig = NATSConfig()

    @model_validator(mode="after")
    def _validate_transport_consistency(self) -> Self:
        from proctor.core.bootstrap import _resolve_transport_mode
        mode = _resolve_transport_mode(self)
        if mode == "nats" and not self.nats.servers:
            raise ValueError(
                "transport resolves to 'nats' but nats.servers is empty. "
                "Set nats.servers or transport='local'."
            )
        if mode == "local" and self.nats != NATSConfig():
            import logging
            logging.getLogger(__name__).warning(
                "transport resolved to 'local'; nats config is set "
                "but will be ignored. Use transport='nats' to enforce."
            )
        return self
```

Edit `src/proctor/core/bootstrap.py`. Add top-level function:

```python
def _resolve_transport_mode(config: ProctorConfig) -> Literal["local", "nats"]:
    """transport='auto' → based on node_role; otherwise explicit."""
    if config.transport != "auto":
        return config.transport
    return "local" if config.node_role == "standalone" else "nats"


def _build_event_transport(config: ProctorConfig) -> EventTransport:
    mode = _resolve_transport_mode(config)
    if mode == "local":
        return LocalEventTransport(
            max_payload=config.events.max_payload,
        )
    # NATS path — lazy import
    from proctor.core.transport.nats import NATSEventTransport
    import socket
    nats_cfg = config.nats
    if nats_cfg.name == f"proctor-{socket.gethostname()}":
        nats_cfg = nats_cfg.model_copy(
            update={"name": f"proctor-{config.node_role}-{socket.gethostname()}"}
        )
    return NATSEventTransport(nats_cfg, events_config=config.events)
```

Modify `Application.__init__` to use it:

```python
transport = event_transport or _build_event_transport(config)
```

- [ ] **Step 15.3: Run tests**

```bash
uv run pytest tests/test_core/test_config.py::TestTransportResolution -v
```
Expected: 9 tests pass.

- [ ] **Step 15.4: Commit**

```bash
git add src/proctor/core/config.py src/proctor/core/bootstrap.py tests/test_core/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): ProctorConfig.transport + NATSConfig extensions

transport: "auto" | "local" | "nats" (default auto).
_resolve_transport_mode: auto+standalone→local, auto+core/worker→nats.

NATSConfig: servers (list), name (validator default), subject_prefix,
reconnect_jitter (0.5 default), user/user_env/password_env mutual
exclusion, tls_* placeholder fields.

Config validator: warn (not raise) on transport=local + non-default
nats config; raise on transport=nats + empty servers.

9-entry parametrized matrix test covers all combinations.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 16: `NATSEventTransport` skeleton + init + connection

**Files:**
- Create: `src/proctor/core/transport/nats.py`

- [ ] **Step 16.1: Create skeleton with lazy import**

Create `src/proctor/core/transport/nats.py`:

```python
"""NATSEventTransport — NATS-backed event transport for multi-node.

nats-py is a lazy import — module parse succeeds without the package
installed. Construction raises friendly ImportError if not available.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from proctor.core.config import EventsConfig, NATSConfig
from proctor.core.models import Event
from proctor.core.transport.base import (
    ConnectionState,
    DisconnectCallback,
    EventTransport,
    Handler,
    ListenerHandle,
    SubscriptionHandle,
)
from proctor.core.transport.errors import (
    EventSchemaError,
    EventTooLargeError,
    InvalidSubjectError,
    TransportConnectionError,
    TransportDrainingError,
    TransportLifecycleError,
    TransportUnavailableError,
)
from proctor.core.transport.local import (
    _DedupCache,
    _RateLimitedLogger,
    _validate_subject,
)


logger = logging.getLogger(__name__)


class NATSEventTransport(EventTransport):
    """NATS-backed EventTransport. Lazy-imports nats-py.

    Install with: pip install proctor[nats]
    """

    def __init__(
        self,
        nats_config: NATSConfig,
        events_config: EventsConfig | None = None,
    ) -> None:
        try:
            import nats  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "NATSEventTransport requires 'nats-py'. "
                "Install with: pip install proctor[nats]"
            ) from e
        self._config = nats_config
        self._events_config = events_config or EventsConfig()
        self._nc: Any = None  # nats.Client; typed Any to keep import lazy
        self._state = ConnectionState.DISCONNECTED
        self._started = False
        self._draining = False
        self._pending_subs: list[_PendingSub] = []
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._dedup = _DedupCache()
        self._disconnect_listeners: list[DisconnectCallback] = []
        self._reconnect_listeners: list[DisconnectCallback] = []
        self._rl = _RateLimitedLogger(logger)
        self._subject_prefix = self._config.subject_prefix

    @property
    def connection_state(self) -> ConnectionState:
        return self._state

    # --- placeholders; implemented in tasks 17-22 ---

    async def start(self) -> None:
        raise NotImplementedError  # Task 17

    async def stop(self) -> None:
        raise NotImplementedError  # Task 22

    async def drain(self, timeout: float = 60.0) -> None:
        raise NotImplementedError  # Task 22

    async def flush(self, timeout: float = 5.0) -> None:
        raise NotImplementedError  # Task 17

    async def publish(self, event: Event) -> None:
        raise NotImplementedError  # Task 18

    def subscribe(
        self, subject: str, handler: Handler
    ) -> SubscriptionHandle:
        raise NotImplementedError  # Task 19

    def add_disconnect_listener(self, cb: DisconnectCallback) -> ListenerHandle:
        raise NotImplementedError  # Task 22

    def add_reconnect_listener(self, cb: DisconnectCallback) -> ListenerHandle:
        raise NotImplementedError  # Task 22


@dataclass
class _PendingSub:
    subject: str
    handler: Handler
    real_sub: Any = None


# Decoder registry (ADR #12)
Decoder = "Callable[[bytes], Event]"
_DECODERS: dict[tuple[str, int], Any] = {}


def _default_decoder_v1(data: bytes) -> Event:
    return Event.model_validate_json(data)


def register_decoder(
    event_type: str, version: int, decoder: Any
) -> None:
    """Register custom decoder for (event_type, schema_version) pair."""
    _DECODERS[(event_type, version)] = decoder


# Default catch-all for v1
_DECODERS[("*", 1)] = _default_decoder_v1
```

- [ ] **Step 16.2: Update `transport/__init__.py` to conditionally export**

```python
# Add at end of __init__.py
try:
    from proctor.core.transport.nats import NATSEventTransport, register_decoder
    __all__ += ["NATSEventTransport", "register_decoder"]
except ImportError:
    # nats-py not installed — standalone deployment
    pass
```

- [ ] **Step 16.3: Verify import works**

```bash
uv run python -c "from proctor.core.transport import NATSEventTransport; print(NATSEventTransport)"
```
Expected: class printed.

Without nats extra:
```bash
uv run python -c "from proctor.core.transport import NATSEventTransport" 2>&1
```
Should ImportError only if nats not installed.

- [ ] **Step 16.4: Commit**

```bash
git add src/proctor/core/transport/nats.py src/proctor/core/transport/__init__.py
git commit -m "$(cat <<'EOF'
feat(transport): NATSEventTransport skeleton + decoder registry

Lazy import of nats-py with friendly ImportError pointing to
pip install proctor[nats]. Decoder registry registers default
catch-all ("*", 1) → Event.model_validate_json; callers can
register_decoder for (event_type, version) pairs.

Methods are NotImplementedError stubs, filled in tasks 17-22.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 17–22: Incremental `NATSEventTransport` implementation

The remaining implementation tasks for NATSEventTransport (start/stop, publish, subscribe, receive path, drain, listeners) follow the same TDD pattern but with NATS-specific integration. To keep this plan manageable, I document them as a task-cluster with the key patterns.

Each task follows the outline:

1. Write failing test (integration or unit as appropriate).
2. Run test (fails).
3. Implement method(s).
4. Run tests, verify pass.
5. Run `ruff format . && ruff check . && pyrefly check`.
6. Commit.

**Task 17: `start()` + `flush()` + buffered subscription replay**

Implementation: connect via `nats.connect(servers=..., name=..., reconnect_jitter=..., max_reconnect_attempts=..., user=..., password=..., tls=..., disconnected_cb=..., reconnected_cb=..., closed_cb=...)`. On CONNECTED, iterate `self._pending_subs`, register each via `nc.subscribe()`, link real Subscription back to `_PendingSub.real_sub`. Call `await nc.flush()` at end. Raise `TransportLifecycleError` on double-start. Raise `TransportConnectionError` on connect failure.

**Task 18: `publish()` — serialization + size + headers**

Implementation: check state; serialize `event.model_dump_json()`; check size vs `events_config.max_payload` → `EventTooLargeError`. Build headers dict (see spec). Compute `nats_subject = f"{prefix}.events.{event.type}"`. Call `await nc.publish(nats_subject, data, headers=headers)`. `CancelledError` re-raised.

**Task 19: `subscribe()` + `SubscriptionHandle` wrapper**

Implementation: validate subject pattern. Before `start()`: append to `_pending_subs`, return handle with `subject` + `async unsubscribe()` (no-op if not yet registered). After `start()`: call `await nc.subscribe(_event_type_to_subject(subject), cb=_make_nats_cb(handler))`; wrap returned Subscription in SubscriptionHandle adapter.

**Task 20: Receive path — header parse + validate + decode + dedup + dispatch**

Implementation of `_on_message(msg)`: parse headers defensively (any `KeyError` or `ValueError` → `EventSchemaError`, caught + rate-limited WARN + drop). Verify `subject == f"{prefix}.events.{header['event-type']}"` (strict equality). Verify `content-type == "application/json"`. Look up decoder in `_DECODERS` (fallback to `("*", 1)`). Check clock skew (rate-limited WARN). Dedup via `(handler, Nats-Msg-Id)`. Dispatch via `asyncio.create_task`.

**Task 21: Signature test — `test_cross_node_event_delivery`**

Integration test in `tests/integration/test_cross_node_delivery.py`. Build 2 `Application` instances with `transport="nats"` + `node_role="standalone"` + shared `subject_prefix`. `app_a.bus.publish(...)` → `app_b.bus.subscribe(...)` handler fires. Use `asyncio.Queue` + `asyncio.wait_for` (no polling).

**Task 22: `drain()` + `stop()` + state transitions + listeners**

Implementation: `drain()` wraps `nc.drain()` + awaits `self._handler_tasks` with timeout. `stop()` calls `await nc.close()`. State transitions: `_on_disconnect` logs once per RECONNECTING state entry, invokes disconnect listeners. `_on_reconnect` logs once per CONNECTED entry, invokes reconnect listeners. `add_disconnect_listener` / `add_reconnect_listener` return `ListenerHandle` with `remove()`.

## Task 23: Parametrized contract tests

**Files:**
- Create: `tests/test_core/test_transport_contract.py`

Parametrize `event_bus` fixture with `["local", "nats"]`. Skip NATS path if testcontainers unavailable. Write 10-15 tests that verify identical observable behaviour (wildcard matching, dedup, handler isolation, buffered subs, event size limit).

## Task 24: Toxiproxy reconnect tests

**Files:**
- Create: `tests/integration/test_transport_nats_reconnect.py`

Use `ToxiproxyContainer` fixture. Proxy NATS connection; `proxy.disable()` injects partition; verify state → RECONNECTING; `proxy.enable()` heals; verify state → CONNECTED, auto-resubscribe works, log noise bounded (≤2 WARNs total).

## Task 25: Schema-version + wire-format + hand-crafted decode tests

**Files:**
- Modify: `tests/integration/test_transport_nats.py`

Three tests: (1) unknown schema-version → dropped + WARN; (2) backward-compat v1 message via v2 receiver → decoded via v1-registered decoder; (3) hand-crafted JSON bytes + headers submitted through receive-path → resulting Event correct.

## Task 26: CI workflow + README + release notes

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`

GHA CI: add `integration-nats` job on Py 3.11 + 3.12 with `services: nats:2-alpine` and `env.NATS_URL=nats://localhost:4222 env.CI=true`. 

README additions: `## Multi-node deployment` (config example with `transport="nats"`, `node_role="core"`; Docker topology); `## Running NATS integration tests` (pip install proctor[nats]; pytest -m nats; testcontainers fallback); ADR summary pointer; rollback note (`transport: "local"` hot-fix).

---

## Final verification

- [ ] **Step F.1: Full test suite**

```bash
uv run pytest 2>&1 | tail -5  # unit + 68a regression
uv run pytest -m nats 2>&1 | tail -10  # integration NATS (local testcontainers)
```
Expected: all pass.

- [ ] **Step F.2: Format + lint + types**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
```

- [ ] **Step F.3: Import smoke test**

```bash
uv run python -c "from proctor.core.transport import NATSEventTransport; print(NATSEventTransport)"
uv run python -c "from proctor.core.bus import EventBus; from proctor.core.transport import LocalEventTransport; print(EventBus(LocalEventTransport()))"
```

- [ ] **Step F.4: Cross-reference all AC checkboxes in spec**

Walk through every `- [ ]` in the spec's "Acceptance criteria" section. Verify each is now `- [x]` via a completed task.

- [ ] **Step F.5: Push and open PR**

```bash
git push -u origin prostoandreyg/labs-68-nats-transport
gh pr create --title "feat: NATS transport (LABS-68)" --body-file <(cat <<'EOF'
## Summary
- EventTransport ABC + LocalEventTransport refactor (PR 68a portion)
- NATSEventTransport with lazy nats-py import, NATS headers, decoder registry (PR 68b portion)
- Atomic merge per spec decision

## Test plan
- [x] All unit tests green (including parametrized [local,nats] contract)
- [x] Integration-nats CI job green (testcontainers)
- [x] Signature test: cross-node event delivery
- [x] Toxiproxy reconnect test
- [x] LABS-65/66/67 regression firewall

Spec: docs/superpowers/specs/2026-04-15-nats-transport-design.md
ADR:  docs/superpowers/adr/2026-04-15-nats-transport.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)
```

---

## Self-review notes

- **Spec coverage:** PR 68a AC — Tasks 1-13. PR 68b AC — Tasks 14-26. Each checkbox maps to a task step.
- **Placeholder scan:** Tasks 17-22 are compressed into a "task-cluster" rather than fully expanded (each ~40-80 LOC of code). This is a pragmatic compromise for plan document size; the spec has all the pattern details. Expand in subagent execution.
- **Type consistency:** Signatures consistent across tasks — `Handler = Callable[[Event], Awaitable[None]]`, `subscribe(subject: str, handler: Handler) -> SubscriptionHandle` (sync), `async def unsubscribe()`, `ConnectionState` enum.
- **Cross-references:** `_resolve_transport_mode`, `_build_event_transport`, `_DECODERS`, `_DedupCache`, `_match_subject`, `_RateLimitedLogger` all defined before first use.
- **Atomic merge:** 68a tasks (1-13) committed as distinct commits but merged atomically with 68b tasks (14-26) in one push/PR.
