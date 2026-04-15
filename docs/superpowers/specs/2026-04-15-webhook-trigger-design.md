# WebhookTrigger (LABS-66) — Design

**Status:** Draft
**Date:** 2026-04-15
**Linear:** [LABS-66](https://linear.app/atp-platform-project/issue/LABS-66)
**Phase:** 2 (Proactivity)

## Goal

Add an HTTP webhook trigger that receives POSTs from external systems
(GitHub, Stripe, CI pipelines, internal services), authenticates them,
and publishes `trigger.webhook.<source_name>` events on the bus. This
closes the last Phase 2 trigger gap and — combined with the LABS-65
Router — lets operators route any HTTP webhook to any catalog workflow
through YAML alone, no code changes.

## Scope

In scope:

- New `WebhookTrigger(Trigger)` in `src/proctor/triggers/webhook.py`.
- `WebhookConfig`, `WebhookPathConfig`, discriminated-union
  `AuthConfig` (HMAC / Bearer / none) in `src/proctor/core/config.py`.
- aiohttp-based HTTP server, bound to `WebhookConfig.host:port`,
  serving POST-only on per-path registration.
- Per-path authentication: HMAC-SHA256 (GitHub/Slack-compatible with
  configurable header + prefix), Bearer, or explicit `type: "none"`.
- Fire-and-forget semantics: 202 Accepted after bus publish, no wait
  on downstream workflow.
- In-flight admission cap via `InflightLimiter` (memory guardrail, not
  rate-limiting).
- Graceful drain on `stop()` with timeout.
- Integration with LABS-65 Router via `trigger.webhook.<source_name>`
  event type.
- Unit + integration tests (~40 tests).
- README updates: Routing section extended, `## Deployment topologies`
  added, `## Webhook trigger` subsection.

Out of scope (tracked as follow-ups):

- **Stripe-style auth** (`Stripe-Signature: t=...,v1=...` with
  timestamp-replay window): needs dedicated `auth.type: "stripe"`.
- **Slack-style signing** (`HMAC(secret, f"v0:{timestamp}:{body}")`):
  different base string construction, needs `auth.type: "slack"`.
- **Replay protection**: no timestamp verification. TLS + future dedup
  layer are the mitigations.
- **Multi-token per path** (`secret_envs: list[str]`): single-secret
  MVP. Rotation via env-mutation takes effect on next request.
- **Application-layer rate limiting**: reverse-proxy responsibility.
- **Idempotency / deduplication**: at-least-once delivery is a
  documented contract; dedup layer is a separate issue.
- **List-indexing in `prompt_from_payload`** (e.g.
  `body.commits.0.message`): LABS-65 `_resolve_path` only walks dicts.
  Workaround: use `body.head_commit.message` for GitHub; separate
  issue to extend the resolver.
- **Observability events** (`webhook.overloaded`, `webhook.accepted`):
  bus events already serve this purpose via `trigger.webhook.*`.
  Future OpenTelemetry integration is a separate concern.

## Design decisions (from brainstorming Q1–Q5)

| # | Decision |
|---|----------|
| Q1  | **Fire-and-forget** response: 202 Accepted + `correlation_id = event.id` after bus publish. Pre-flight sync errors (401/400/413/429/503) before publish; 202 regardless of downstream routing outcome. |
| Q1a | Idempotency not guaranteed — at-least-once delivery; duplicates possible on client retry. |
| Q2  | **Per-source path** config: `paths: dict[str, WebhookPathConfig]`. Event type = `trigger.webhook.<source_name>`; event source = `"webhook"`; payload carries `path`, filtered `headers`, parsed `body`. |
| Q2a | `source_name` explicit in `WebhookPathConfig` (default: URL basename). Validators enforce format `^[a-z][a-z0-9_-]*$`, uniqueness across paths, reserved-word exclusion. |
| Q3  | **HMAC + Bearer + none** as discriminated union on `auth.type`. Configurable HMAC header + prefix (covers GitHub + Slack-style without new code). Bearer is simple `Authorization: Bearer <token>` (RFC 6750 case-insensitive). `none` is explicit opt-in with loud WARNING on startup. |
| Q3a | `secret_env` — secret in env vars, never in YAML. Two-stage validation: pydantic checks format, `WebhookTrigger.start()` checks presence. |
| Q3b | All auth failures → identical `401 {"error": "unauthorized"}` response. Distinction lives in logs only (prevents oracle-style attacks). |
| Q4  | **In-flight cap only**, no app-layer rate limit. Explicit counter + `asyncio.Lock` + `try_acquire`. 503 + `Retry-After: 1` when saturated. Default `max_in_flight: 20`. `max_body_bytes: 1_048_576` in same issue (aiohttp `client_max_size`). Default `host: "127.0.0.1"` — production requires reverse-proxy. |
| Q4a | In-flight cap is **NOT** a DDoS defense. Reverse-proxy is mandatory for public exposure. |
| Q5  | **Drain with timeout** on stop(): stop accepting → `wait_idle(shutdown_timeout=30.0)` → force-close. Event-driven via `asyncio.Event` + `clear()`. Idempotent `stop()` (second call no-op). |
| Q5a | `WebhookTrigger.stop()` called **first** in `Application.stop()` — close inputs before internal state. |
| Q5b | Scope: HTTP handler drain only, **not** workflow drain. Workflow lifecycle is `Application.stop()`'s concern (future issue). |

## Architecture

### New module: `src/proctor/triggers/webhook.py`

Public surface:

```python
class WebhookTrigger(Trigger):
    """aiohttp-based HTTP server publishing trigger.webhook.<source_name>
    events on the bus. Per-path auth (HMAC/Bearer/none), fire-and-forget
    semantics (202 Accepted), graceful drain on stop().
    """

    def __init__(self, config: WebhookConfig) -> None: ...
    async def start(self, bus: EventBus) -> None: ...
    async def stop(self) -> None: ...

    @property
    def bound_port(self) -> int | None:
        """Actual bound port; useful when config.port=0 (test ephemeral)."""


class InflightLimiter:
    """Counter-based in-flight cap with event-driven idle signalling.
    try_acquire(): False when at limit (non-blocking).
    wait_idle(timeout): True on idle, False on timeout.
    """
    def __init__(self, limit: int) -> None: ...
    async def try_acquire(self) -> bool: ...
    async def release(self) -> None: ...
    async def wait_idle(self, timeout: float) -> bool: ...
    @property
    def in_flight(self) -> int: ...
    @property
    def limit(self) -> int: ...
```

`InflightLimiter` is a public class (no underscore prefix). Rationale:
it has its own unit tests exercised directly, Python doesn't enforce
privacy, and single-consumer-but-testable primitives don't warrant a
separate module.

### Changes to existing components

| File | Change |
|------|--------|
| `src/proctor/core/config.py` | Add `HMACAuthConfig`, `BearerAuthConfig`, `NoneAuthConfig`, discriminated-union alias `AuthConfig`, `WebhookPathConfig`, `WebhookConfig`. All auth variants use `model_config = ConfigDict(extra="forbid")` to reject nonsense cross-type fields. `ProctorConfig.webhook: WebhookConfig \| None = None`. One validator on `WebhookConfig`: `_validate_paths` (format + uniqueness + `source_name` derivation). |
| `src/proctor/core/bootstrap.py` | `self._webhook_trigger: WebhookTrigger \| None = None` in `__init__`. `start()` creates + starts it if `self.config.webhook is not None`. `stop()` stops it **first** (before other triggers, before internal state). |
| `src/proctor/triggers/__init__.py` | Re-export `WebhookTrigger`. |
| `config/proctor.yaml` | Example `webhook:` section with one GitHub (HMAC) path + one CI (Bearer) path. |
| `README.md` | `## Webhook trigger` subsection. `## Deployment topologies` with nginx / Traefik / k8s examples. Update Phase 2 roadmap row. |

### Anti-loop gate

`trigger.webhook.<source>` events go through LABS-65 Router via the
existing `trigger.*` subscription. WebhookTrigger has no new subscription
code; it only publishes. No loop possible.

## Config schema

```python
from typing import Annotated, Literal, Self
from pydantic import BaseModel, ConfigDict, Field, model_validator


class HMACAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["hmac"] = "hmac"
    secret_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    header: str = "X-Hub-Signature-256"      # examples: "X-Slack-Signature"
    prefix: str = "sha256="                   # examples: "v0=" (Slack-ish)


class BearerAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["bearer"] = "bearer"
    secret_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    header: str = "Authorization"


class NoneAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["none"] = "none"
    # no fields — explicit opt-in to unauthenticated endpoint


AuthConfig = Annotated[
    HMACAuthConfig | BearerAuthConfig | NoneAuthConfig,
    Field(discriminator="type"),
]


class WebhookPathConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_name: str | None = None
    auth: AuthConfig


class WebhookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = 8080
    paths: dict[str, WebhookPathConfig] = Field(min_length=1)
    max_in_flight: int = 20
    max_body_bytes: int = 1_048_576
    shutdown_timeout: float = 30.0
    keepalive_timeout: float = 75.0

    @model_validator(mode="after")
    def _validate_paths(self) -> Self:
        """Validate path format + derive + check source_name uniqueness.

        Per path:
        - Starts with "/", is not "/", does not end with "/".
        - Contains no fnmatch metacharacters (*, ?, [).

        Per effective source_name (explicit or basename):
        - Matches ^[a-z][a-z0-9_-]*$.
        - Not in {"", "*", "?"}.
        - Unique across all paths.

        Derivation of source_name (when omitted) is persisted on
        WebhookPathConfig.source_name inside this validator, so the
        webhook handler can rely on a non-None value at runtime.
        """
        ...
```

### Example YAML

```yaml
webhook:
  host: 127.0.0.1
  port: 8080
  max_in_flight: 20
  max_body_bytes: 1048576
  shutdown_timeout: 30.0
  paths:
    /webhook/github:
      source_name: github                     # → trigger.webhook.github
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

Matching routes in `ProctorConfig.routes`:

```yaml
routes:
  - event_pattern: "trigger.webhook.github"
    workflow_id: github-handler
    prompt_from_payload: body.head_commit.message    # GitHub push payload
  - event_pattern: "trigger.webhook.ci"
    workflow_id: ci-handler
    prompt_from_payload: body.message
```

### Default `host: "127.0.0.1"` rationale

Safe default. Public exposure is an explicit, considered action
(set `host: 0.0.0.0`). The documented pattern is `reverse-proxy →
127.0.0.1:8080` (sidecar topology) or `reverse-proxy → 0.0.0.0:8080
inside pod with NetworkPolicy` (Kubernetes ingress topology). README
documents both.

## Data flow

```
External client → POST /webhook/github
  ↓
Reverse proxy (TLS term, per-IP rate limiting, forwards raw body + headers)
  ↓
aiohttp server (host, port, client_max_size=max_body_bytes)
  ↓
_handle_webhook(request):
  1. cfg = self._paths[request.path]                       # per-path registration → KeyError impossible
  2. if not await self._limiter.try_acquire():             # admission check
         return 503 + Retry-After: 1
  3. try:
         raw_body = await request.read()                   # aiohttp enforces size; 413 auto
         if not _verify_auth(cfg.auth, request, raw_body): # HMAC/Bearer/none
             return 401
         body = json.loads(raw_body) if raw_body else {}   # 400 on JSONDecodeError
         headers = _safe_headers(request.headers)          # whitelist filter
         event = Event(
             type=f"trigger.webhook.{cfg.source_name}",
             source="webhook",
             payload={"path": request.path, "headers": headers, "body": body},
         )
         try:
             await self._bus.publish(event)
         except asyncio.CancelledError:
             raise                                          # let shutdown cancel
         except Exception:
             logger.exception(...)
             return 503 + Retry-After: 5
         return 202 + {"accepted": True, "correlation_id": event.id}
     finally:
         await self._limiter.release()
```

### Status code contract

| Situation | Status | Retry? |
|-----------|--------|--------|
| In-flight cap exceeded | 503 + `Retry-After: 1` | yes, short |
| Body > `max_body_bytes` | 413 (aiohttp auto) | no |
| Missing / invalid / malformed auth | 401 | no |
| Malformed JSON | 400 | no |
| `bus.publish` raised | 503 + `Retry-After: 5` | yes, longer |
| Successful publish | 202 | no (accepted) |
| GET/PUT/DELETE to registered path | 405 (aiohttp auto) | no |
| POST to unregistered path | 404 (aiohttp auto) | no |

### `_verify_auth` dispatch

```python
def _verify_auth(
    auth: AuthConfig, request: web.Request, raw_body: bytes
) -> bool:
    """Per-request verification. Secrets are re-read from os.environ
    on every call; rotation via os.environ[...] = new_value takes
    effect immediately without restart.
    """
    if auth.type == "none":
        return True
    if auth.type == "hmac":
        header = request.headers.get(auth.header)
        if header is None or not header.startswith(auth.prefix):
            return False
        sig_hex = header[len(auth.prefix):]
        secret = os.environ[auth.secret_env].encode()
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig_hex, expected)
    if auth.type == "bearer":
        header = request.headers.get(auth.header, "")
        parts = header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        token = parts[1]
        secret = os.environ[auth.secret_env]
        return hmac.compare_digest(token, secret)
    return False  # unreachable with discriminated union
```

### Header whitelist

```python
_SAFE_HEADER_NAMES = frozenset({
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
    """Whitelist-filter request headers before publishing to bus.

    Auth headers (Authorization, X-Hub-Signature-256, Stripe-Signature,
    Cookie, etc.) MUST be excluded: event.payload is persisted to
    episodes.db; leaking credentials there is a security incident.

    Multi-value headers: last-wins via dict conversion. Duplicate
    headers are rare in webhook traffic; if a future use case needs
    them preserved, switch to list[tuple[str, str]].
    """
    result: dict[str, str] = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in _SAFE_HEADER_NAMES or kl.startswith(_SAFE_HEADER_PREFIXES):
            result[k] = v
    return result
```

### `InflightLimiter` implementation

```python
class InflightLimiter:
    """Counter-based in-flight cap with event-driven idle signalling.

    asyncio.Event (not anyio.Event) because Proctor de facto runs on
    asyncio (aiosqlite, litellm are asyncio-only); `clear()` gives a
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

## Lifecycle

### `WebhookTrigger.start(bus)`

```python
async def start(self, bus: EventBus) -> None:
    # 1. Fail-fast: verify all required env secrets are present.
    missing = sorted({
        cfg.auth.secret_env
        for cfg in self._paths.values()
        if cfg.auth.type != "none" and cfg.auth.secret_env not in os.environ
    })
    if missing:
        raise RuntimeError(
            f"Missing required webhook secrets in env: {missing}"
        )

    # 2. Loud WARNING for each unauthenticated path.
    for path, cfg in self._paths.items():
        if cfg.auth.type == "none":
            logger.warning(
                "Webhook path %r has NO AUTHENTICATION (auth.type: none). "
                "Do not use in production.",
                path,
            )

    self._bus = bus
    app = web.Application(client_max_size=self._config.max_body_bytes)
    for path in self._paths:
        app.router.add_post(path, self._handle_webhook)    # POST only
    self._runner = web.AppRunner(
        app, keepalive_timeout=self._config.keepalive_timeout
    )
    await self._runner.setup()
    self._site = web.TCPSite(
        self._runner, self._config.host, self._config.port
    )
    await self._site.start()
    logger.info(
        "WebhookTrigger started on %s:%d with %d path(s)",
        self._config.host, self._config.port, len(self._paths),
    )
```

### `WebhookTrigger.stop()`

```python
async def stop(self) -> None:
    # idempotent — second call is a no-op
    if self._stopped:
        return
    self._stopped = True

    # 1. Stop accepting new connections.
    if self._site is not None:
        await self._site.stop()

    # 2. Drain in-flight handlers with timeout.
    drained = await self._limiter.wait_idle(self._config.shutdown_timeout)
    if not drained:
        logger.warning(
            "Webhook shutdown timed out with %d in-flight requests",
            self._limiter.in_flight,
        )

    # 3. Force-close remaining connections.
    if self._runner is not None:
        await self._runner.cleanup()
    logger.info("WebhookTrigger stopped")
```

### `Application.stop()` order

```python
async def stop(self) -> None:
    self.is_running = False
    # Close inputs first — WebhookTrigger first so the HTTP endpoint
    # stops accepting new POSTs before internal state tears down.
    if self._webhook_trigger is not None:
        await self._webhook_trigger.stop()
    if self._telegram_trigger is not None:
        await self._telegram_trigger.stop()
    if self._scheduler is not None:
        await self._scheduler.stop()
    # (future) engine drain here
    await self.memory.close()
    await self.state.close()
```

## Error handling and observability

### Error categories

| Class | Example | HTTP | Log level | Client should retry? |
|-------|---------|------|-----------|----------------------|
| Client error | bad signature, malformed JSON | 401 / 400 | `INFO` | No |
| Resource cap | in-flight saturated | 503 + `Retry-After: 1` | `WARNING` | Yes, soon |
| Infra error | `bus.publish` raised | 503 + `Retry-After: 5` | `ERROR` with `exc_info` | Yes, longer |
| Accepted | published | 202 | `DEBUG` | N/A |

**Never:**

- Leak internal details in responses. Oracle-style auth distinctions
  live **only** in logs.
- Return 500. Unhandled exceptions surface as 500 via aiohttp, but
  that's a bug — tests must fail on any 500.
- Log raw bodies or full headers at `INFO+`. `DEBUG` only.

### Log contract

```python
logger.debug("webhook accepted: path=%s source=%s event_id=%s", ...)
logger.info("webhook auth failed: path=%s reason=%s", ...)  # routine for
                                                             # public endpoints
logger.warning("webhook overloaded: rejected %s (in_flight=%d/%d)", ...)
logger.exception("webhook failed to publish event for %s", ...)
logger.warning("Webhook path %r has NO AUTHENTICATION. ...", ...)
logger.warning("Webhook shutdown timed out with %d in-flight requests", ...)
```

Auth failures are `INFO`, not `WARNING`: unauthenticated scans are
routine on public endpoints, and WARNING would drown real signals.
Reverse-proxy + fail2ban sees IP-level patterns better.

### At-least-once delivery (explicit contract)

Webhook events are delivered **at least once**. Duplicates can arise
when publish succeeds but the 202 response fails (client disconnect,
server crash, proxy retry). Workflow authors must assume every
`trigger.webhook.*` event is potentially duplicated.

Recommended dedup keys:

- GitHub: `payload.headers["X-GitHub-Delivery"]` (always present).
- Internal clients: send `X-Request-Id`.
- `correlation_id` from 202 response (server-side UUID).

A dedicated dedup layer with 24h SQLite-backed window is a separate
issue.

### In-flight cap is NOT DDoS protection

> `max_in_flight` is a memory-footprint guardrail. An attacker with
> invalid credentials can briefly saturate slots (until auth rejects
> their requests). Proctor relies on a reverse-proxy (nginx
> `limit_req_zone`, Traefik `RateLimit`, Cloudflare) for per-IP rate
> limiting. Running Proctor's webhook endpoint directly on a public
> interface without such a proxy is unsupported.

README `## Deployment topologies` subsection documents concrete nginx
and Traefik configs.

### `CancelledError` passthrough pattern

Every `except Exception` block in webhook code:

```python
try:
    await self._bus.publish(event)
except asyncio.CancelledError:
    raise
except Exception:
    logger.exception(...)
    return web.json_response(..., status=503)
```

Never swallow `CancelledError`, or `Application.stop()` cannot cancel
in-flight handlers cleanly during drain.

### Observability boundary

Webhook does NOT emit custom events like `webhook.accepted` or
`webhook.overloaded`. The bus is the observability channel: every
webhook → exactly one `trigger.webhook.<source>` event, and downstream
subscribers (metrics, Telegram notifier, future OpenTelemetry) consume
from there. Operational metrics (auth failures, overload) are log +
external monitoring concerns, not bus events.

## Testing

### Unit tests — `tests/test_triggers/test_webhook.py`

Module-scoped `webhook` fixture to share one aiohttp server across
~40 tests (saves ~4-8s on CI). Tests vary payload / headers / auth,
not config.

```python
# Fixture sketch — subscriber uses async callable (EventBus contract)
@pytest.fixture(scope="module")
async def webhook(request):
    # ... monkeypatch env ...
    cfg = WebhookConfig(host="127.0.0.1", port=0, paths={...})
    trigger = WebhookTrigger(cfg)
    bus = EventBus()
    await trigger.start(bus)
    yield trigger, bus, f"http://127.0.0.1:{trigger.bound_port}"
    await trigger.stop()


@pytest.fixture
def captured_events(webhook):
    trigger, bus, _ = webhook
    events: list[Event] = []
    arrived = asyncio.Event()

    async def _collect(e: Event) -> None:
        events.append(e)
        arrived.set()

    bus.subscribe("trigger.webhook.*", _collect)
    # Tests await arrived.wait() before asserting event content.
    # Relies on EventBus dispatching subscribers asynchronously;
    # the signal pattern handles the publish-vs-delivery ordering.
    return events, arrived
```

Helper:

```python
def _sign_hmac(
    body: bytes,
    secret: str,
    *,
    prefix: str = "sha256=",
    algo: Callable = hashlib.sha256,
) -> str:
    """Build HMAC header value, e.g. 'sha256=<hex>' (GitHub-style)."""
    return prefix + hmac.new(secret.encode(), body, algo).hexdigest()
```

Test classes:

**`TestAuthHMAC`** (6 tests):
1. Valid signature → 202 + event with correct type/source/payload.
2. Missing signature header → 401, no event.
3. Bad signature → 401, no event.
4. Custom HMAC prefix mismatch (config `prefix="v0="`, client sends
   `sha256=...`) → 401.
5. Empty body + valid HMAC over empty body → 202 (edge case).
6. Custom HMAC header + prefix (non-GitHub configuration, e.g.
   `header="X-Slack-Signature"`, `prefix="v0="`). Note: this does not
   make Proctor Slack-compatible — Slack uses `HMAC(secret, f"v0:{ts}:{body}")`
   which is out of scope.

**`TestAuthBearer`** (5 tests):
1. `Authorization: Bearer <token>` match → 202.
2. `Authorization: bearer <token>` (lowercase) → 202 (RFC 6750).
3. Missing `Authorization` → 401.
4. Wrong token → 401.
5. Non-Bearer scheme (`Authorization: Basic ...`) → 401.

**`TestAuthNone`** (2 tests):
1. POST to `/webhook/open` without any auth header → 202.
2. `start()` emits `WARNING` containing `"NO AUTHENTICATION"`
   (via `caplog`).

**`TestHeaderWhitelist`** (2 tests):
1. Auth headers (`Authorization`, `X-Hub-Signature-256`, `Cookie`,
   `Stripe-Signature`) NOT in `event.payload["headers"]`.
2. Safe headers (`Content-Type`, `User-Agent`, `X-GitHub-Event`,
   `X-GitHub-Delivery`, `X-Forwarded-For`) present in payload.

**`TestBodyParsing`** (3 tests):
1. Valid JSON → payload dict matches.
2. Malformed JSON → 400, no event.
3. Empty body → `payload["body"] == {}`, event published.

**`TestStatusCodes`** (3 tests):
1. POST to unregistered path → 404 (aiohttp), no event.
2. GET to registered path → 405 (aiohttp), no event.
3. Body > `max_body_bytes` → 413 (aiohttp), no event.

**`TestInflightCap`** (2 tests — critical Q4 contract):
1. Saturate 20 slots via `monkeypatch`-ed blocking publish; 21st POST
   → 503 + `Retry-After: 1`, no event.
2. After release, next POST succeeds.

**`TestBusPublishFailure`** (1 test — Q4/Q5 infra contract):
1. Mock `bus.publish` to raise → 503 + `Retry-After: 5`, logged at
   `ERROR` with `exc_info`.

**`TestCancelledErrorPassthrough`** (1 test):
1. Mock `bus.publish` to raise `asyncio.CancelledError` → handler
   propagates (no 503), request task cancelled cleanly.

**`TestLifecycle`** (4 tests):
1. `start()` with missing env → `RuntimeError` listing **all** missing
   vars, not just the first.
2. `start()` with complete env — starts, port bound (`bound_port` not
   None), `/webhook/open` logs WARNING.
3. `stop()` drains in-flight: monkeypatch slow publish, fire 3
   concurrent POSTs, `stop()` returns within `shutdown_timeout`, all
   3 received 202.
4. `stop()` called twice → second call is no-op (no exception).

**`TestInflightLimiter`** (5 unit tests, no HTTP — directly on
`InflightLimiter`):
1. `try_acquire()` under limit → True, count increments.
2. `try_acquire()` at limit → False, count unchanged.
3. `release()` decrements, signals idle at zero.
4. `wait_idle(0.01)` returns False while busy; True after `release()`.
5. Concurrent acquire+release (10 tasks) — `wait_idle` correctly
   returns True after all released.

### Config validator tests — `tests/test_core/test_config.py`

**`TestWebhookConfigValidation`** (8 tests):
1. `paths={}` → ValidationError `"at least one"`.
2. Path not starting with `/` → ValidationError.
3. Path with fnmatch metacharacter `*` → ValidationError.
4. Path with trailing `/` (not `/`) → ValidationError.
5. Two paths with same effective `source_name` → ValidationError
   `"uniqueness"` or similar.
6. `source_name` with uppercase (`Github`) → ValidationError.
7. `HMACAuthConfig` with extra field (`bearer_header`) →
   ValidationError (requires `extra="forbid"`).
8. `secret_env` not matching `^[A-Z][A-Z0-9_]*$` → ValidationError.

### Integration tests — additions to `tests/test_integration.py`

**`TestWebhookIntegration`** (2 tests):
1. Full e2e: `Application` with webhook config + Router rule
   `trigger.webhook.github → chat`; POST with valid HMAC → episode
   created with mock LLM output.
2. Unmatched webhook event (path configured, no route rule): POST →
   202, Router publishes `routing.unmatched`, no task / no episode.

### README updates

- `## Webhook trigger` subsection: config example, auth schemes, 202
  semantics, at-least-once delivery disclaimer, dedup suggestions.
- `## Deployment topologies` with nginx (sidecar) and Traefik (k8s
  ingress) configs and explicit "reverse-proxy mandatory for public
  exposure" note.
- Phase 2 roadmap row updated (webhook done).

## Acceptance criteria

- [ ] `WebhookTrigger(Trigger)` in `src/proctor/triggers/webhook.py`
      with `start(bus)`, `stop()`, and `bound_port` property.
- [ ] `InflightLimiter` with `asyncio.Event`-based `wait_idle`,
      unit-tested in isolation.
- [ ] `HMACAuthConfig`, `BearerAuthConfig`, `NoneAuthConfig`,
      discriminated-union `AuthConfig`, `WebhookPathConfig`,
      `WebhookConfig` in `src/proctor/core/config.py` with
      `extra="forbid"` on all.
- [ ] `WebhookConfig._validate_paths` validator performing all checks
      listed in the "Config schema" section; derives and persists
      `source_name` when omitted.
- [ ] Secret env presence verified in `WebhookTrigger.start()`
      (not in pydantic) — raises `RuntimeError` with complete list of
      missing vars.
- [ ] Loud startup WARNING per unauthenticated path.
- [ ] Header whitelist applied before publishing; auth headers
      excluded from `event.payload`.
- [ ] In-flight cap: 503 + `Retry-After: 1`. Default `max_in_flight=20`.
- [ ] `asyncio.CancelledError` re-raised in all `except Exception`
      blocks inside the handler.
- [ ] Graceful drain: stop accepting → `wait_idle(shutdown_timeout)` →
      force-close; idempotent; WARNING on timeout.
- [ ] `Application.stop()` stops `WebhookTrigger` **before** other
      triggers.
- [ ] Unit + integration tests per sections above (~40 tests total).
- [ ] README: `## Webhook trigger`, `## Deployment topologies`, Phase 2
      roadmap update.
- [ ] `config/proctor.yaml` example with at least one HMAC path and one
      Bearer path.

## Risks

1. **At-least-once delivery** — duplicates possible; no built-in dedup.
   Workflow authors must use `X-GitHub-Delivery` / `X-Request-Id` /
   `correlation_id` as dedup keys.
2. **In-flight cap is not DDoS defense** — reverse-proxy rate-limiting
   mandatory for public exposure.
3. **Stripe-style auth** (timestamp + signature + replay window) not
   supported; separate `auth.type: "stripe"` issue.
4. **Slack-style auth** (different base string) not supported; separate
   `auth.type: "slack"` issue. The `header`/`prefix` configurability in
   `HMACAuthConfig` accommodates Slack's *header shape* but not the
   signing algorithm.
5. **Replay protection** absent. TLS + future dedup layer mitigate.
6. **Multi-token per path** — single secret MVP. `secret_envs: list[str]`
   is a follow-up issue (pre-revoke rotation).
7. **Header whitelist may become stale** — new auth schemes or
   identifying headers appear. Periodic audit required; consider
   whitelist-regression tests.
8. **List-indexing in `prompt_from_payload`** unsupported; workaround
   via dict-only paths. LABS-65 resolver extension is a separate issue.
9. **Observability events deferred** — `webhook.overloaded`,
   `auth_failure_total{path}` etc. live in logs now; future
   OpenTelemetry / Prometheus middleware integration is a separate
   issue.

## Related issues

- **LABS-65** (Router) — provides the `trigger.*` subscription and
  catalog routing this trigger plugs into. No changes to Router or its
  config needed.
- **LABS-67** (LiteLLM) — downstream of the workflow that webhook
  events trigger. No direct coupling.
- **LABS-68** (NATS transport) — future distribution layer. Webhook
  publishes to the same `EventBus` abstraction; NATS-backed bus will
  work transparently.
