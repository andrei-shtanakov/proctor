# NATS Transport (LABS-68) — Design

**Status:** Draft
**Date:** 2026-04-15
**Linear:** [LABS-68](https://linear.app/atp-platform-project/issue/LABS-68)
**Phase:** 2/3 boundary

## Goal

Refactor the in-memory `EventBus` into a pluggable `EventTransport` abstraction and add a NATS-backed implementation so Proctor can run in multi-node topologies. The signature use case: event published on node A → handler subscribed on node B fires, via shared NATS cluster. In `standalone` mode the behaviour remains identical to the current in-memory bus.

## Scope

In scope (split into two PRs, see "Implementation split"):

- **68a**: `EventTransport` ABC + `LocalEventTransport` (in-memory, NATS-wildcard semantics) + `EventBus` refactor + `Application` DI wiring + `Event` validators + `drain()` phase. No NATS library, no Docker, no CI changes. Standalone users see no behavioural change.
- **68b**: `NATSEventTransport` (lazy-imported), `EventsConfig`/`NATSConfig` extensions, transport mode resolution, CI integration job, parametrized `[local, nats]` contract tests, cross-node signature test, toxiproxy-based reconnect tests, README `## Multi-node deployment`.

Out of scope (explicitly deferred):

- **Task dispatch / worker queue** → LABS-69. `TaskTransport` is a separate ABC with different semantics (queue-group, at-least-once, ack). Wire format convention inherited from LABS-68 (ADR #5).
- **Outbox pattern** for publish-during-disconnect. `publish` fails fast with `TransportUnavailableError`; buffered outbox is a separate issue.
- **JetStream** (persistent streams, server-side dedup). `Nats-Msg-Id` header convention (ADR #11) makes the future migration additive.
- **Stripe/Slack-style auth** (moved from LABS-66 OOS; same reasoning).
- **Rate limiting at app layer**. Reverse-proxy or NATS auth is the right layer.
- **Observability metrics** (Prometheus counters for `event_handler_errors_total` etc). Hooks are placeholder log statements; real metrics land with OpenTelemetry integration (Phase 5).
- **Full mTLS**. Placeholder config fields (`tls_client_cert`, `tls_client_key`) in `NATSConfig`; only CA-based TLS wired in LABS-68.
- **Multi-token per NATS connection** (single `user`/`password` credentials; rotation via env-var change).

## Implementation split

### PR 68a — Transport abstraction refactor (no NATS)

`EventBus` becomes a thin wrapper over `EventTransport`. `LocalEventTransport` implements NATS-wildcard semantics in-process. All existing callers migrate to explicit `EventBus(LocalEventTransport())`. No `nats-py` dependency, no Docker, no CI changes.

**Invariant:** standalone users see identical behaviour before and after 68a. LABS-65/66/67 integration tests remain green as regression firewall.

Sizing: ~700 LOC production + ~900 LOC tests, ~1.5 weeks.

### PR 68b — NATSEventTransport + integration

`NATSEventTransport` with lazy `nats-py` import (via `pip install proctor[nats]` optional extra), transport mode resolution in bootstrap, wire format (JSON body + NATS headers), dedup cache, reconnect handling, CI `integration-nats` job using GitHub Actions `services:` block, parametrized shared test suite, cross-node signature test.

Sizing: ~900 LOC production + ~1200 LOC tests, ~2 weeks.

**Total realistic calendar: 3–4 weeks** including review iteration.

### Merge policy

Atomic merge within one day. 68a can review first (pure refactor, easier eye); 68b validates the ABC before merge. 68a shipping alone would freeze a leaky abstraction into 61 call-sites before NATS realities surface. Both PRs off the same branch `prostoandreyg/labs-68-nats-transport`.

## Design decisions (ADR summary)

| # | Decision | Rationale snapshot |
|---|---|---|
| 1 | Transport split: `EventTransport` (LABS-68) vs future `TaskTransport` (LABS-69) | Different semantics (fan-out vs queue-group); unified ABC would hide contract differences |
| 2 | Bridge/composite transport rejected | Loopback-dedup complexity; YAGNI |
| 3 | `EventBus()` requires explicit transport | No hidden magic; all 61 call-sites migrate to DI |
| 4 | Namespace taxonomy documented in full, implemented only for `events.*` | Reserves `tasks.*`, `control.*`, `health.*` for LABS-69+ |
| 5 | Symmetric wire format for LABS-69 `TaskTransport` | Same NATS header metadata convention; no divergent serialization across subsystems |
| 6 | Handler exception = swallow + log + counter | Subscription survives; ops monitoring via logs/metrics |
| 7 | Delivery semantics: at-most-once, no retries, no redelivery for events | Aligned with NATS core semantics; retries are caller responsibility |
| 8 | Subscribe-before-start = buffered, registered at `start()` | Linear bootstrap code; testability |
| 9 | Subject prefix = global config (default `proctor`) | Multi-env isolation on shared NATS cluster |
| 10 | NATS connection ownership: LABS-68 owns its own `nats.Client`; LABS-69 refactors to shared `NATSConnectionManager` | Deferred refactor debt; flagged for LABS-69 |
| 11 | Wire format: JSON body + NATS headers incl. `Nats-Msg-Id` | Debuggable via `nats sub`, consistent with episodes.db; `Nats-Msg-Id` is the NATS/JetStream native dedup header |
| 12 | Schema evolution: per `(event-type, schema-version)` decoder registry; readers accept prior versions (backward-compat), drop unknown future versions (forward-compat log) | Global version bumps meaningless across event types |
| 13 | Event charset `[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*` | Consistent with Python identifier style; underscores only, no dashes |
| 14 | Clock skew: NTP required; rate-limited WARN on detect (`> 1h`) | Latency metrics rely on wall-clock sync |
| 15 | Subscribe API: sync (returns handle); unsubscribe: async (requires I/O) | Matches nats.py semantics; enables `__init__` subscribe |
| 16 | Explicit `drain()` phase before `bus.stop()` | Prevents silent event loss during shutdown |
| 17 | `max_payload` lives in `EventsConfig`, not `NATSConfig` | Shared across transports; NATS server-side limit is separate |
| 18 | Dedup symmetric across both transports via `Nats-Msg-Id` | Identical observable behaviour for overlapping subscriptions |
| 19 | Subscription ownership: Application-level handlers in `Application.__init__`; component-level in `component.start()` | Policy removes PR-author ambiguity |
| 20 | Parametrized `[local, nats]` tests only for contract behaviour, not implementation details | CI time budget discipline |
| 21 | Handler dispatch via `asyncio.create_task` (fire-and-forget per handler); `drain()` awaits `self._handler_tasks` with timeout | Slow handler doesn't block pipeline; symmetric behaviour in both transports |

Full ADR text: `docs/superpowers/adr/2026-04-15-nats-transport.md` (generated alongside this spec).

## Architecture

### Module layout

```
src/proctor/core/transport/
├── __init__.py          # re-exports (EventTransport, LocalEventTransport,
│                        #  NATSEventTransport, errors, enums, protocols)
├── base.py              # ABC + Handler + SubscriptionHandle Protocol +
│                        # ConnectionState enum
├── errors.py            # TransportError hierarchy
├── local.py             # LocalEventTransport, _DedupCache,
│                        # _RateLimitedLogger, wildcard matcher
└── nats.py              # NATSEventTransport (lazy-imported nats-py)
```

`core/transport/` is future-proof for `core/transport/task_nats.py` etc. in LABS-69.

### Public surface

```python
# src/proctor/core/transport/base.py

Handler = Callable[[Event], Awaitable[None]]


class ConnectionState(Enum):
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"


class SubscriptionHandle(Protocol):
    @property
    def subject(self) -> str: ...
    async def unsubscribe(self) -> None: ...


class ListenerHandle(Protocol):
    def remove(self) -> None: ...


DisconnectCallback = Callable[[], Awaitable[None]] | Callable[[], None]


class EventTransport(ABC):
    """Transport for broadcast event delivery (fan-out, at-most-once).

    Subscribe accepts NATS-subject syntax (tokens separated by '.',
    '*' = single-token wildcard, '>' = multi-token trailing wildcard
    — only allowed as the last token). Both transports enforce
    identical wildcard and delivery semantics so tests are portable
    across backends.
    """

    @abstractmethod
    async def start(self) -> None:
        """Connect to backend and register buffered subscriptions.

        Idempotency: not idempotent. Double-call raises
        TransportLifecycleError.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Unsubscribe all; disconnect from backend.

        Must be preceded by drain() for graceful behaviour. Called
        alone after subscriptions have in-flight handlers, those
        handlers may fail mid-flight.
        """

    @abstractmethod
    async def drain(self, timeout: float = 60.0) -> None:
        """Reject new publishes (TransportDrainingError) and wait for
        in-flight handler tasks to complete within timeout. Logs WARN
        on tasks exceeding timeout; cancels them. After drain()
        completes, stop() is safe.
        """

    @abstractmethod
    async def flush(self, timeout: float = 5.0) -> None:
        """Wait until buffered subscribe/publish commands are ACKed
        by the broker. No-op for LocalEventTransport; forwards to
        nc.flush(timeout) for NATSEventTransport.
        """

    @abstractmethod
    async def publish(self, event: Event) -> None:
        """Serialize event, enforce max_payload, dispatch.

        Raises:
            EventTooLargeError: if serialized size > events.max_payload.
            TransportUnavailableError: if not CONNECTED.
            TransportDrainingError: during drain phase.

        Handler exceptions are swallowed + logged; never raised here.
        """

    @abstractmethod
    def subscribe(
        self, subject: str, handler: Handler
    ) -> SubscriptionHandle:
        """Register handler for subject pattern.

        Sync call — returns SubscriptionHandle immediately.

        Before transport.start(): subscription is buffered. Registered
        at start() together with flush to broker.

        After transport.start(): subscription registration proceeds
        in background. Caller should await bus.flush() before
        publishing events matching the subject if delivery of the
        first matching event is semantically required.

        Raises:
            InvalidSubjectError: if subject doesn't match charset /
                wildcard rules.
        """

    @property
    @abstractmethod
    def connection_state(self) -> ConnectionState: ...

    @abstractmethod
    def add_disconnect_listener(
        self, cb: DisconnectCallback
    ) -> ListenerHandle:
        """Register callback for state transitions into DISCONNECTED
        or RECONNECTING. cb may be sync or async; transport awaits
        async callbacks appropriately. Returns handle; call
        handle.remove() to deregister.
        """

    @abstractmethod
    def add_reconnect_listener(
        self, cb: DisconnectCallback
    ) -> ListenerHandle: ...
```

### `EventBus` — thin wrapper

```python
# src/proctor/core/bus.py

class EventBus:
    """Public API over EventTransport.

    Transport is plumbing; EventBus is the stable caller-facing
    contract. Future observability hooks (metrics, tracing, event
    enrichment interceptors) wire at this level, not in Transport.
    """

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

No default transport: `EventBus()` → `TypeError`. 61 call-sites migrate to explicit `EventBus(LocalEventTransport())` (codemod warranted, see 68a Task 12).

### `Application` integration

```python
# src/proctor/core/bootstrap.py

class Application:
    def __init__(
        self,
        config: ProctorConfig,
        *,
        event_transport: EventTransport | None = None,
    ) -> None:
        self.config = config
        transport = event_transport or _build_event_transport(config)
        self.bus = EventBus(transport)

        # ... other field init (state, memory, triggers holders) ...

        # Application-level bootstrap handlers go HERE (ADR #19).
        # Subscriptions are buffered; registered at start().
        self.bus.subscribe("trigger.>", self._handle_trigger_event)
        # (future: self.bus.subscribe("routing.>", self._handle_routing_event))


def _build_event_transport(config: ProctorConfig) -> EventTransport:
    mode = _resolve_transport_mode(config)
    if mode == "local":
        return LocalEventTransport(
            max_payload=config.events.max_payload,
        )
    # Inject node_role into NATS client name for debuggability —
    # `nats server ls` shows which role is which on shared clusters.
    nats_cfg = config.nats
    if nats_cfg.name == f"proctor-{socket.gethostname()}":  # fallback default
        nats_cfg = nats_cfg.model_copy(
            update={"name": f"proctor-{config.node_role}-{socket.gethostname()}"}
        )
    return NATSEventTransport(
        nats_cfg, events_config=config.events,
    )


def _resolve_transport_mode(
    config: ProctorConfig,
) -> Literal["local", "nats"]:
    if config.transport != "auto":
        return config.transport
    return "local" if config.node_role == "standalone" else "nats"
```

**Lifecycle order:**

```python
async def start(self) -> None:
    await self.state.start()
    await self.memory.start()
    await self.bus.start()   # transport.start() — connects + replays buffered subs
    await self.bus.flush()   # ensure subs registered before triggers publish
    # Component-level subscribes go inside component.start() (ADR #19):
    if self._webhook_trigger is not None:
        await self._webhook_trigger.start()
    # ... other triggers ...


async def stop(self) -> None:
    self.is_running = False
    # 1. Inputs first — no new events
    if self._webhook_trigger is not None:
        await self._webhook_trigger.stop()
    if self._telegram_trigger is not None:
        await self._telegram_trigger.stop()
    if self._scheduler is not None:
        await self._scheduler.stop()
    # 2. Drain in-flight handlers (allows final trigger.* events to complete)
    await self.bus.drain(timeout=self.config.events.drain_timeout)
    # 3. Stop transport — no more publishes accepted
    await self.bus.stop()
    # 4. Data layer last
    await self.memory.stop()
    await self.state.stop()
```

## Namespace taxonomy

```
<subject_prefix>.events.<type>           fan-out, at-most-once                [LABS-68]
<subject_prefix>.tasks.<sub>             queue-group, at-least-once + ack     [LABS-69]
<subject_prefix>.control.<sub>           pause/resume/drain commands          [future]
<subject_prefix>.health.<sub>            heartbeat, liveness signals          [future]
```

- `subject_prefix` from `NATSConfig.subject_prefix` (default `"proctor"`). Multi-env: `proctor-prod.events.*` vs `proctor-staging.events.*`.
- LABS-68 implements only `events.*`.
- Taxonomy documented in full so LABS-69+ cannot diverge silently.

**Subject charset (literal segments):** `[a-z][a-z0-9_]*` per token, dot-separated. Wildcards: `*` (single token, any position), `>` (multi-token, last position only).

**Event-type derives subject:** `nats_subject = f"{prefix}.events.{event.type}"`. `EventBus` API is `event.type`-centric; transport adds the prefix on publish/subscribe.

## Data flow

### Publish path

```
Caller → bus.publish(event)
  ↓
EventTransport.publish(event):
  1. If state != CONNECTED → raise TransportUnavailableError
     (rate-limited WARN log)
  2. If draining → raise TransportDrainingError
  3. Serialize event (both transports):
       data = event.model_dump_json().encode("utf-8")
       if len(data) > events.max_payload → EventTooLargeError
  4a. LocalEventTransport:
        for sub in _match(event.type):
            if dedup.seen(sub.handler, msg_id): continue
            await _safe_invoke(sub.handler, event)
  4b. NATSEventTransport:
        headers = {
          "content-type":    "application/json",
          "schema-version":  "1",
          "event-type":      event.type,
          "Nats-Msg-Id":     str(uuid4()),
          "published-at":    datetime.now(UTC).isoformat(timespec="microseconds"),
        }
        subject = f"{prefix}.events.{event.type}"
        await nc.publish(subject, data, headers=headers)
```

### Receive path (NATS only)

```
nats.py inbound message (subject, data, headers)
  ↓
NATSEventTransport._on_message(msg):
  1. Parse headers defensively:
       try:
           event_type = headers["event-type"]
           version = int(headers["schema-version"])
           content_type = headers["content-type"]
           msg_id = headers["Nats-Msg-Id"]
           published_at = parse_iso8601(headers["published-at"])
       except (KeyError, ValueError) as e:
           raise EventSchemaError(f"malformed headers: {e}")
     EventSchemaError is caught one frame up → rate-limited WARN + drop.
     NEVER propagates beyond transport (ADR #6).
  2. Verify subject == f"{prefix}.events.{event_type}".
     Mismatch → raise EventSchemaError("subject/header mismatch").
  3. Verify content_type == "application/json".
     Mismatch → raise EventSchemaError("unsupported content-type").
  4. Lookup decoder in registry (ADR #12):
       decoder = _DECODERS.get((event_type, version)) or _DECODERS.get(("*", version))
       if decoder is None:
           raise EventSchemaError(
               f"no decoder for ({event_type}, v{version}); "
               f"receiver is forward-compat-safe (drop)"
           )
  5. Check clock skew:
       skew = now_utc - published_at
       if abs(skew) > 1h → _rate_log.warn("clock_skew:{host}", ...)
  6. event = decoder(data)  # typically Event.model_validate_json
  7. Dispatch concurrently as background tasks (ADR #21):
       for sub in matches(subject):
           if dedup.seen(sub.handler, msg_id): continue
           task = asyncio.create_task(_safe_invoke(sub.handler, event))
           self._handler_tasks.add(task)
           task.add_done_callback(self._handler_tasks.discard)
     Slow handler does NOT block pipeline; drain() awaits all
     outstanding tasks before stop.
```

Subscribe buffering + flush on start:

```
bus.subscribe("trigger.>", h) before start()
  ↓
LocalEventTransport: append to _pending_subs list (sync, no I/O).
NATSEventTransport:  append to _pending_subs list.
  ↓
await bus.start()
  ↓
NATSEventTransport.start():
  1. nats.connect(servers=..., disconnected_cb=..., reconnected_cb=..., reconnect_jitter=0.5, ...)
  2. state = CONNECTED
  3. For pending in _pending_subs:
       real_sub = await nc.subscribe(subject=_event_type_to_subject(pending.subject),
                                      cb=_make_nats_cb(pending.handler))
       pending._link_to_real(real_sub)
  4. await nc.flush()
  5. logger.info("Registered %d buffered subscriptions", len(_pending_subs))
  6. _pending_subs.clear()
```

## Wire format

### Body

JSON via `event.model_dump_json()` (UTF-8 bytes). Receive: `Event.model_validate_json(data)`. Uses pydantic encoder — `datetime`/`UUID`/`Path`/`Decimal`/`set` are serialized predictably.

### NATS headers (invisible to subscribers)

| Header | Value | Purpose |
|---|---|---|
| `content-type` | `application/json` (only supported in LABS-68) | Future msgpack migration dispatches on this |
| `schema-version` | `1` | Forward/backward compat via `(event-type, version)` decoder registry |
| `event-type` | `event.type` literal | Dup of subject suffix; receive-side validates `subject == f"{prefix}.events.{header}"` |
| `Nats-Msg-Id` | `uuid4()` | NATS/JetStream standard dedup header; used by LABS-68 local dedup cache |
| `published-at` | ISO-8601 UTC with microsecond precision, e.g. `2026-04-15T12:34:56.789012+00:00` | Latency metric (`now - published_at`); requires NTP |

### Size limits

- `events.max_payload` (shared config) — default 65 536 bytes.
- `LocalEventTransport` checks at publish via `strict_size_check=True` (configurable). Prevents "works in dev, breaks on NATS" asymmetry.
- `NATSEventTransport` checks after serialize; raises `EventTooLargeError`.
- Note: blob-store reference pattern for larger payloads — OOS, separate issue.

## Event model validators

```python
# src/proctor/core/models.py

_EVENT_TYPE_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")


class Event(BaseModel):
    # ... existing fields ...

    @field_validator("type")
    @classmethod
    def _type_charset(cls, v: str) -> str:
        if not _EVENT_TYPE_RE.fullmatch(v):
            raise ValueError(
                f"Event.type {v!r} must match [a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*"
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
    def _payload_pydantic_serializable(
        cls, v: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            TypeAdapter(dict[str, Any]).dump_json(v)
        except Exception as e:
            raise ValueError(
                f"Event.payload not serializable by pydantic: {e}"
            ) from e
        return v
```

**Audit task:** before merging 68a, walk every `Event(type=...)` construction site (constant, f-string, variable, external). Representative fuzz-test for each dynamic source. See 68a Task "audit_event_construction".

LABS-66 `WebhookPathConfig.source_name` validator also tightens to `[a-z][a-z0-9_]*` (was `[a-z][a-z0-9_-]*`). Existing source_names (`github`, `ci`, `heartbeat`) pass; change is safe.

## Configuration

```python
# src/proctor/core/config.py

class EventsConfig(BaseModel):
    max_payload: int = 65_536       # bytes; shared across transports
    drain_timeout: float = 60.0      # seconds; LLM-heavy handlers need headroom


class NATSConfig(BaseModel):
    servers: list[str] = ["nats://localhost:4222"]
    name: str = ""                    # auto-populated by validator if empty
    subject_prefix: str = "proctor"
    connect_timeout: float = 5.0
    reconnect_time_wait: float = 2.0
    reconnect_jitter: float = Field(default=0.5, ge=0.0, le=5.0)
    max_reconnect_attempts: int = -1  # infinite
    user: str | None = None
    user_env: str | None = None
    password_env: str | None = None
    tls_ca: Path | None = None
    tls_client_cert: Path | None = None   # placeholder (full mTLS OOS)
    tls_client_key: Path | None = None    # placeholder

    # Note: name default with node_role suffix is populated in
    # _build_event_transport (node_role lives on ProctorConfig, not here).
    # Falls back to hostname-only if role is unknown.
    @model_validator(mode="after")
    def _populate_name_fallback(self) -> Self:
        if not self.name:
            self.name = f"proctor-{socket.gethostname()}"
        return self

    @model_validator(mode="after")
    def _user_exclusive(self) -> Self:
        if self.user and self.user_env:
            raise ValueError(
                "Set either NATSConfig.user or user_env, not both"
            )
        return self


class ProctorConfig(BaseModel):
    # ... existing fields ...
    node_role: Literal["standalone", "core", "worker"] = "standalone"
    transport: Literal["auto", "local", "nats"] = "auto"
    events: EventsConfig = EventsConfig()
    nats: NATSConfig = NATSConfig()

    @model_validator(mode="after")
    def _validate_transport_consistency(self) -> Self:
        mode = _resolve_transport_mode(self)
        if mode == "nats" and not self.nats.servers:
            raise ValueError(
                "transport resolves to 'nats' but nats.servers is empty. "
                "Set nats.servers or transport='local'."
            )
        if mode == "local" and self.nats != NATSConfig():
            logger.warning(
                "transport resolved to 'local'; nats config is set but "
                "will be ignored. Use transport='nats' to enforce."
            )
        return self
```

**Config matrix test (AC):**

```python
@pytest.mark.parametrize("transport,node_role,nats_servers,expected", [
    ("auto",     "standalone", [],                "resolves_local"),
    ("auto",     "standalone", ["nats://x:4222"], "warn_nats_ignored"),
    ("auto",     "core",       ["nats://x:4222"], "resolves_nats"),
    ("auto",     "worker",     ["nats://x:4222"], "resolves_nats"),
    ("auto",     "core",       [],                "ValueError"),
    ("local",    "standalone", [],                "resolves_local"),
    ("local",    "core",       ["nats://x:4222"], "resolves_local"),
    ("nats",     "standalone", ["nats://x:4222"], "resolves_nats"),
    ("nats",     "worker",     [],                "ValueError"),
])
def test_transport_mode_resolution_matrix(
    transport, node_role, nats_servers, expected
): ...
```

## Error handling

### Exception hierarchy

```python
# src/proctor/core/transport/errors.py

class TransportError(Exception):
    """Base for all event transport errors."""

class TransportConnectionError(TransportError):
    """Connect/start failures."""

class TransportLifecycleError(TransportError):
    """Double-start, stop-before-start, etc."""

class TransportUnavailableError(TransportError):
    """publish() attempted while not CONNECTED."""

class TransportDrainingError(TransportUnavailableError):
    """publish() attempted during drain phase."""

class InvalidSubjectError(TransportError, ValueError):
    """Subject violates charset / wildcard rules."""

class EventTooLargeError(TransportError):
    """Serialized event exceeds events.max_payload."""

class EventSchemaError(TransportError):
    """Internal control-flow exception raised inside transport when a
    received message can't be decoded. NEVER propagates beyond
    transport — caught internally for log + drop behaviour. Kept as
    a class (not bare logger call) so internal handlers can use
    `except EventSchemaError` pattern matching cleanly.
    """

class HandlerTimeoutError(TransportError):
    """Internal: handler exceeded drain soft timeout. Logged, task
    cancelled; never propagates beyond transport.
    """
```

### Logging contract

| Event | Level | Rate-limited? |
|---|---|---|
| Transport started | INFO | no |
| Transport stopped | INFO | no |
| State transition → RECONNECTING | WARN | no (rare) |
| State transition → CONNECTED (reconnect) | INFO | no (rare) |
| State transition → DISCONNECTED (permanent close) | ERROR | no |
| Publish rejected (unavailable / draining) | WARN | **yes**, 60s, aggregated count |
| Handler exception | ERROR with `exc_info` | **yes**, per `(handler, event_type)`, 60s |
| Event dropped (schema / size / mismatch on receive) | WARN | **yes**, per reason, 60s |
| Clock skew detected | WARN | **yes**, per publisher, 60s |
| Drain timed out | WARN | no |
| Buffered subscriptions registered at start | INFO | no |

`_RateLimitedLogger`:

```python
class _RateLimitedLogger:
    """Logs first occurrence immediately, then every `interval` sec
    with aggregated count. Uses asyncio.Lock for concurrent safety.
    """

    def __init__(self, logger: logging.Logger, interval: float = 60.0):
        self._logger = logger
        self._interval = interval
        self._last_logged: dict[str, float] = {}
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def warn(self, key: str, fmt: str, *args) -> None:
        async with self._lock:
            self._counts[key] += 1
            now = time.monotonic()
            last = self._last_logged.get(key, 0.0)
            if now - last >= self._interval:
                self._logger.warning(
                    f"{fmt} (occurrences since last log: %d)",
                    *args, self._counts[key],
                )
                self._last_logged[key] = now
                self._counts[key] = 0
```

### Failure points map

| Failure | Where raised | Who handles | Caller sees |
|---|---|---|---|
| NATS unreachable on start | `NATSEventTransport.start()` | `Application.start()` | `TransportConnectionError` → app doesn't start |
| Double-start | `EventTransport.start()` | caller | `TransportLifecycleError` |
| Mid-session disconnect | nats.py callback | Transport (reconnect auto) | `connection_state = RECONNECTING`; publish raises |
| Invalid subject on subscribe | `subscribe()` | caller (at init) | `InvalidSubjectError` |
| Publish while RECONNECTING | `publish()` | caller | `TransportUnavailableError` (rate-limited WARN) |
| Publish while draining | `publish()` | caller | `TransportDrainingError` |
| Event > max_payload | `_serialize()` | caller | `EventTooLargeError` |
| Handler exception | Transport `_safe_invoke` | Transport | rate-limited ERROR, subscription survives |
| Handler timeout in drain | `drain()` task group | Transport | WARN, task cancelled |
| Malformed / schema-unknown NATS message | receive-side decode | Transport | rate-limited WARN, drop |
| Subject / header mismatch | receive-side verify | Transport | rate-limited WARN, drop |
| Permanent close (max_reconnect reached) | nats.py `closed_cb` | Transport | state = DISCONNECTED; publishes fail |

## Lifecycle

### Startup order

```
Application.start():
  1. state.start()
  2. memory.start()
  3. bus.start()        # transport.start() — connect, replay buffered subs
  4. bus.flush()        # ensure subs registered on broker before triggers publish
  5. triggers.start()   # webhook, telegram, scheduler
```

### Shutdown order

```
Application.stop():
  1. triggers.stop()    # no new events into bus
  2. bus.drain(60s)     # in-flight handlers complete; publishes rejected
  3. bus.stop()         # transport.stop() — unsubscribe, disconnect
  4. memory.stop()
  5. state.stop()
```

### Subscribe timing

- **Application-level handlers** — registered in `Application.__init__` (ADR #19). Sync `subscribe()` returns immediately with a `SubscriptionHandle`; registration happens at `bus.start()`.
- **Component-level handlers** — registered in `component.start()`. By that time `bus.start()` has completed, so registration happens in background; `bus.flush()` in the component's start code ensures broker-side ack if first event delivery is critical.
- **Dynamic runtime subscribe** — works; caller aware that broker-side registration is async and may want `bus.flush()`.

## Testing strategy

### Test organization

**Contract tests** (parametrized `[local, nats]`, ADR #20):

- Wildcard matching semantics (`*`, `>`, literals, rejected patterns).
- Dedup on overlapping subscriptions (same handler called once per event).
- Subscribe-before-start buffering + flush on start.
- Handler exception isolation.
- Event validation reject (bad type / timezone / payload).
- Buffered subs logged at start.

**Implementation tests** (single-backend):

- `LocalEventTransport`: `_DedupCache` internals, weak-ref behaviour, TTL/LRU eviction, wildcard matcher edge cases, strict_size_check toggle.
- `NATSEventTransport`: header contents, `Nats-Msg-Id` uniqueness, subject prefix isolation, state transition logging bounded under prolonged disconnect, `reconnect_jitter` applied.

**Signature test** (NATS only, priority #1):

```python
@pytest.mark.nats
async def test_cross_node_event_delivery(nats_url, subject_prefix):
    """Signature test: event published on node A, handler on node B
    fires. This is the raison d'être for LABS-68 — demonstrates
    distributed event delivery via NATS. If this test passes, NATS
    transport works; if it fails, nothing else in LABS-68 matters.
    """
    cfg_a = _build_config(
        node_role="standalone", transport="nats",
        subject_prefix=subject_prefix,
    )
    cfg_b = _build_config(
        node_role="standalone", transport="nats",
        subject_prefix=subject_prefix,
    )
    app_a = Application(cfg_a)
    app_b = Application(cfg_b)

    queue: asyncio.Queue[Event] = asyncio.Queue()
    async def handler(event: Event) -> None:
        await queue.put(event)
    app_b.bus.subscribe("trigger.>", handler)

    await app_a.start()
    await app_b.start()
    try:
        await app_b.bus.flush()
        await app_a.bus.publish(
            Event(type="trigger.test", source="test", payload={"x": 1})
        )
        received = await asyncio.wait_for(queue.get(), timeout=5.0)
        assert received.type == "trigger.test"
    finally:
        await app_b.stop()
        await app_a.stop()
```

### Integration test fixtures

```python
@pytest.fixture(scope="session")
def nats_url() -> Iterator[str]:
    if os.getenv("CI") and os.getenv("NATS_URL"):
        yield os.environ["NATS_URL"]
        return
    try:
        from testcontainers.nats import NatsContainer
        with NatsContainer("nats:2-alpine") as nats:
            yield nats.get_connection_url()
    except Exception:
        pytest.skip("Docker not available — NATS integration tests skipped")


@pytest.fixture
def subject_prefix(request) -> str:
    """Unique namespace per test on shared NATS container."""
    node = request.node.name.replace("[", "-").replace("]", "")
    return f"test-{node}-{uuid4().hex[:8]}"


@pytest.fixture
def event_collector() -> tuple[asyncio.Queue, Handler]:
    queue: asyncio.Queue[Event] = asyncio.Queue()
    async def handler(event: Event) -> None:
        await queue.put(event)
    return queue, handler


async def wait_for_events(
    queue: asyncio.Queue[Event],
    count: int,
    timeout: float = 5.0,
) -> list[Event]:
    """Event-driven wait; no polling."""
    events = []
    for _ in range(count):
        events.append(
            await asyncio.wait_for(queue.get(), timeout=timeout)
        )
    return events


# --- Toxiproxy fixture for reconnect tests ---

@pytest.fixture
async def nats_via_toxiproxy():
    """NATS behind Toxiproxy for programmable network injection.

    toxi.disable() → client sees disconnect; toxi.enable() → client
    auto-reconnects. Used for honest reconnect tests without killing
    the NATS container (which would change port mapping).
    """
    from testcontainers.nats import NatsContainer
    # testcontainers[toxiproxy] provides ToxiproxyContainer
    from testcontainers.toxiproxy import ToxiproxyContainer

    with NatsContainer("nats:2-alpine") as nats, \
         ToxiproxyContainer() as toxy:
        # Bridge: clients connect to toxiproxy; toxiproxy forwards to NATS.
        proxy = toxy.create_proxy(
            "nats-proxy",
            upstream=f"{nats.get_container_host_ip()}:{nats.get_exposed_port(4222)}",
            listen="0.0.0.0:4223",
        )
        yield proxy, f"nats://{toxy.get_container_host_ip()}:{toxy.get_exposed_port(4223)}"
```

### Must-have tests (beyond cross-node signature)

- **Hand-crafted wire decode** — raw JSON bytes + headers assembled manually, pushed through `NATSEventTransport._on_message()`, verify resulting `Event` object correct. Guarantees external publisher (Go/JS future) wire-format stability.
- **Schema-version mismatch** — hand-craft a message with `schema-version: 2`, receiver knows only 1 → dropped with WARN.
- **Backward-compat decoder** — v2 receiver with v1 message → decodes correctly via backward-compat registry path.
- **Toxiproxy reconnect** — `testcontainers[toxiproxy]` as NATS proxy; `proxy.disable()` inject partition, `proxy.enable()` heal. Verify state transitions + auto-resubscribe + bounded log noise.
- **Config matrix** — parametrized 9-entry table (see "Configuration" section).
- **Clock skew unit** — inject `published-at` 2 hours in future via helper; verify rate-limited WARN fires.
- **Stop-order invariants** — via call-order instrumentation: triggers → drain → bus.stop → memory → state.
- **Smoke benchmark** (optional `@pytest.mark.benchmark`): 10k events via `LocalEventTransport` < 10 seconds; catches accidental O(n²) in dedup or matcher.

### CI

```yaml
# .github/workflows/ci.yml

jobs:
  unit:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run pytest      # implicit -m "not nats and not ollama and not benchmark"

  integration-nats:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ["3.11", "3.12"]
    services:
      nats:
        image: nats:2-alpine
        ports: ["4222:4222"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --extra nats
      - env:
          CI: "true"
          NATS_URL: "nats://localhost:4222"
        run: uv run pytest -m nats
```

### `pyproject.toml` changes

```toml
[project.optional-dependencies]
nats = ["nats-py>=2.7, <3"]

[dependency-groups]
dev = [
    # ... existing dev deps ...
    "testcontainers[nats,toxiproxy]>=4.0",
]

[tool.pytest.ini_options]
addopts = "-m 'not nats and not ollama and not benchmark'"
markers = [
    "integration: requires external services",
    "nats: requires running NATS server (docker or GHA services)",
    "ollama: requires local Ollama",
    "benchmark: performance smoke tests (opt-in, weekly or manual)",
]
```

## Risks

1. **Abstraction leaks at NATS edge.** ABC invariants may not match `nats.py` realities (subscribe ordering, drain semantics, backpressure). Mitigation: 68b lands before 68a merges; parametrized `[local, nats]` tests catch divergence.

2. **`nats-py` API changes.** Pin `>=2.7, <3`. Compatibility re-verified at implementation time.

3. **Testcontainers on self-hosted CI.** Ryuk reaper can fail. Escape hatch: `TESTCONTAINERS_RYUK_DISABLED=true`. GHA `services:` block avoids it in CI.

4. **Toxiproxy dependency weight.** `testcontainers[toxiproxy]` adds ~50MB container. Dev-only; accepted for honest reconnect tests.

5. **Log storm from rate-limited logger.** Concurrent counter bugs → `asyncio.Lock` in `_RateLimitedLogger`. Covered by concurrency smoke test.

6. **Breaking change for 61 call-sites.** Migration from `EventBus()` to `EventBus(LocalEventTransport())`. 61 sites exceeds manual threshold; write LibCST-aided codemod (~50 LOC). 68a Task 12.

7. **Clock skew false positives.** Ephemeral CI containers may have slight skew. WARN threshold of 1h should be safe; tune if needed.

8. **NATS `subject_prefix` collision.** Two deployments on same cluster without unique prefix → cross-talk. README `## Multi-node deployment` documents per-env prefix.

9. **JetStream future alignment.** `Nats-Msg-Id` header already matches JetStream native dedup. LABS-68 cache + future server-side dedup overlap is benign (same key).

10. **`drain_timeout` tuning.** Default 60s. LLM-heavy handlers may need 120s+. Configurable via `EventsConfig.drain_timeout`; tune per deployment.

11. **Rollback path.** For deployed multi-node setups, `transport: "local"` config override reverts to in-process bus (single-node only). Full revert = revert merge commit. Documented in release notes.

12. **Parametrized test maintenance burden.** Doubles CI time for shared suite. Policy (ADR #20): parametrize contract-level tests only. Review discipline required.

13. **Event charset tightening breaks dynamic constructions.** Audit task (68a) catches before merge; representative fuzz-test for each dynamic construction site (webhook `source_name`, scheduler job names, future workflow-emitted events).

14. **NATS reconnect thundering herd.** Single-operator today is safe; future scale-up to many workers → `reconnect_jitter: 0.5` default (see `NATSConfig`) prevents synchronized reconnect slam.

## Acceptance criteria

### PR 68a (transport abstraction)

- [ ] `EventTransport` ABC in `src/proctor/core/transport/base.py` with: `start`, `stop`, `drain`, `flush`, `publish`, `subscribe` (sync, buffered), `connection_state` property, `add_disconnect_listener`, `add_reconnect_listener`.
- [ ] `Handler = Callable[[Event], Awaitable[None]]` (async only).
- [ ] `SubscriptionHandle` Protocol with async `unsubscribe()` + `subject` property.
- [ ] `ListenerHandle` Protocol with `remove()`.
- [ ] `ConnectionState` enum: CONNECTED / RECONNECTING / DISCONNECTED.
- [ ] Unified exception hierarchy (`TransportError` + 8 subclasses).
- [ ] `_RateLimitedLogger` helper with `asyncio.Lock` concurrency.
- [ ] `LocalEventTransport` implements NATS-wildcard semantics (`*`, `>`, literal; no `?`, no `[...]`).
- [ ] `LocalEventTransport.publish` enforces `max_payload` via `strict_size_check=True` default.
- [ ] `_DedupCache` delivers same-handler-overlapping-subscriptions exactly once via `(id(handler), Nats-Msg-Id)` with weak-refs. Tested handler types: `async def` function, async lambda, bound method (via `WeakMethod`), `functools.partial(async_fn, arg)`, class with `async def __call__`. Each case: same handler subscribed to two overlapping patterns → called exactly once per event.
- [ ] `EventBus` thin wrapper; no default transport (`EventBus()` → TypeError).
- [ ] `Application(config, *, event_transport=None)` DI.
- [ ] Application-level subscribes in `Application.__init__` (buffered, registered at start + flush).
- [ ] `Application.stop()` order: triggers → `bus.drain(drain_timeout)` → `bus.stop()` → memory/state.
- [ ] `start()` is non-idempotent — `TransportLifecycleError` on double-start.
- [ ] `Event` pydantic validators: timezone-aware UTC, payload pydantic-serializable, charset `[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*`.
- [ ] `WebhookPathConfig.source_name` validator tightened to same charset (all existing source_names pass).
- [ ] `EventsConfig` with `max_payload`, `drain_timeout` fields.
- [ ] Audit of all `Event(type=...)` construction sites (constant, f-string, dynamic, external); representative fuzz-test per dynamic source.
- [ ] Codemod (`EventBus()` → `EventBus(LocalEventTransport())`) applied across 61 call-sites (verified via `rg 'EventBus\s*\(\s*\)' --type py | wc -l`).
- [ ] Release note documents `WebhookPathConfig.source_name` charset change with migration example (`my-service` → `my_service`). Config load with dash produces clear `ValidationError` pointing at field + recommended rename.
- [ ] Handler dispatch via `asyncio.create_task` with `self._handler_tasks` tracking set; `drain()` awaits all outstanding tasks within timeout then cancels remainder (ADR #21).
- [ ] LABS-65/66/67 integration tests all still green — regression firewall.
- [ ] ADR doc `docs/superpowers/adr/2026-04-15-nats-transport.md` committed.

### PR 68b (NATS transport)

- [ ] `NATSEventTransport` in `src/proctor/core/transport/nats.py` with lazy `import nats` + friendly `ImportError`.
- [ ] `nats-py` as `[nats]` optional extra (`pip install proctor[nats]`).
- [ ] `ProctorConfig.transport: Literal["auto", "local", "nats"]`; `_resolve_transport_mode` per `node_role`.
- [ ] `NATSConfig` extensions: `servers`, `name` (validator default), `subject_prefix`, `connect_timeout`, `reconnect_time_wait`, `reconnect_jitter`, `max_reconnect_attempts`, `user`/`user_env`/`password_env`, `tls_ca`/`tls_client_cert`/`tls_client_key`.
- [ ] Config validator warns (not raises) on `transport=local` + non-default `nats` config.
- [ ] Config matrix test (9-entry parametrized).
- [ ] Wire format: JSON body + NATS headers (`Nats-Msg-Id`, `content-type`, `schema-version`, `event-type`, `published-at`).
- [ ] Subject mapping: `<prefix>.events.<event.type>` on publish; subscribe pattern translated transparently.
- [ ] Receive-path validates: strict `subject ==` expected; known `schema-version`; drop + rate-limited WARN on mismatch.
- [ ] Dedup via `Nats-Msg-Id` header (same cache behaviour as local).
- [ ] Clock skew check (`|now - published-at| > 1h` → rate-limited WARN per publisher).
- [ ] Reconnect: `nats.py` auto-reconnect + state-transition logging only (not per-attempt); `reconnect_jitter=0.5` applied.
- [ ] `publish` during DISCONNECTED/RECONNECTING → `TransportUnavailableError`; no buffering.
- [ ] `drain()` wraps `nc.drain()` + handler-task await.
- [ ] Listener handle-based add/remove; sync or async callbacks.
- [ ] `_DECODERS: dict[tuple[str, int], Callable[[bytes], Event]]` decoder registry with default `("*", 1) → Event.model_validate_json` registered at module import. Lookup on receive: `_DECODERS.get((event_type, v)) or _DECODERS.get(("*", v))`.
- [ ] Test: publish v1 + receive via default decoder — pass.
- [ ] Test: register custom decoder for `("trigger.webhook.github", 2)`, publish v2 message — custom decoder used instead of default.
- [ ] Test: malformed `schema-version` header (`"abc"`, missing) → `EventSchemaError` caught internally, rate-limited WARN, message dropped, transport survives.
- [ ] NATS client `name` includes `node_role` when resolved by `_build_event_transport` (e.g. `proctor-core-hostname`); fallback to hostname-only when `NATSConfig.name` explicitly set by user.
- [ ] **Signature test** `test_cross_node_event_delivery` using Queue + `wait_for_events` (no polling).
- [ ] Schema-version mismatch dropped + logged (forward-compat).
- [ ] Backward-compat decoder registry test (v1 bytes in v2 receiver).
- [ ] Hand-crafted wire decode test (external publisher compat).
- [ ] Toxiproxy reconnect test: partition → raises; heal → auto-resubscribe + bounded log lines.
- [ ] Parametrized `[local, nats]` shared contract test suite — identical observable behaviour.
- [ ] Smoke benchmark (`@pytest.mark.benchmark`, optional).
- [ ] CI `integration-nats` job on Py 3.11 + 3.12 with GHA `services: nats`.
- [ ] README `## Multi-node deployment` + `## Running NATS integration tests` + ADR summary + rollback note.

## Open questions (acknowledged, deferred)

Items that surfaced during design but were deliberately deferred or
parked. Listed so reviewers know they are not oversights.

1. **Handler dispatch concurrency tuning.** ADR #21 commits to
   `asyncio.create_task` per matching handler (fire-and-forget with
   drain-time tracking). If in practice some Proctor deployments want
   strict sequential ordering between N handlers of the same event,
   add `EventsConfig.handler_dispatch: Literal["concurrent", "sequential"]`
   — out of scope for LABS-68, flag if the need surfaces.
2. **`source_name` migration policy.** LABS-68 tightens charset to
   underscores only. Current dev code uses only underscores already,
   so no migration needed in-repo. **External operators with dash in
   their webhook configs will hit a clear `ValidationError` on
   startup** — release notes + migration example in README. No
   auto-migration, no deprecation period.
3. **Toxiproxy fixture network wiring.** Spec shows sketch; real
   implementation may need to bind Toxiproxy to host network instead
   of container network, depending on testcontainers version. Allow
   ~0.5 day buffer in 68b plan for fixture discovery.
4. **`drain_timeout` default tuning.** 60s is conservative; LLM-heavy
   workflows may need 120s+. Configurable per deployment; revisit if
   production observation shows systematic drain timeouts.
5. **Breaking change coupling with LABS-69.** `NATSConnectionManager`
   refactor (ADR #10) happens in LABS-69 and will require
   `NATSEventTransport.__init__` signature change. Flagged but
   deferred.

## Toolchain requirements

- **`uv >= 0.5`** for PEP 735 `[dependency-groups]` syntax (used for
  dev-deps in `pyproject.toml`). Current project uv pin verified
  against `.python-version` / CI toolchain notes before 68b merge.
- **Python 3.11+** (already project-wide requirement).
- **Docker** for local integration test runs (testcontainers).
  Tests skip cleanly with clear message when Docker unavailable.

## Related issues

- **LABS-65** (Router) — consumes events via subscribe; `bus.subscribe("trigger.>", ...)` migration touch-point.
- **LABS-66** (Webhook) — publishes events through `bus`; `source_name` charset tightening.
- **LABS-67** (LiteLLM) — publishes `llm.*` events (future) through `bus`.
- **LABS-69** (Worker Pool) — introduces `TaskTransport` ABC; inherits wire-format convention (ADR #5); triggers `NATSConnectionManager` refactor (ADR #10).
