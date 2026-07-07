# ADR: LABS-68 NATS Transport (21 decisions)

**Related spec:** `docs/superpowers/specs/2026-04-15-nats-transport-design.md`
**Related plan:** `docs/superpowers/plans/2026-04-15-nats-transport.md`
**Context:** Foundational event-transport layer for proctor distributed nodes. PR 68a established the `EventTransport` abstraction + `LocalEventTransport` with NATS-wildcard semantics. PR 68b adds `NATSEventTransport`. These 21 decisions were agreed during brainstorming (2026-04-14 — 2026-04-15) and locked before implementation.

---

## ADR 1: Transport split: EventTransport vs future TaskTransport

**Status:** Accepted — 2026-04-15

**Decision:** `EventTransport` (broadcast, fan-out, at-most-once) is a separate abstraction from the upcoming `TaskTransport` (queue-group, at-least-once) in LABS-69.

**Rationale:** Their NATS semantics are incompatible — fan-out pub/sub vs queue-group load-balancing. Unifying them into a single ABC would hide critical contract differences and cause incorrect defaults to spread across the system. Separating them from day one makes the guarantees explicit.

**Alternatives considered:**
- Single unified `Transport` ABC (rejected — ambiguous delivery semantics; NATS topic vs queue-group confusion)
- Transport tagged with role enum `(event, task)` (rejected — harder to test and reason about; reduces type safety)

**Consequences:** LABS-69 introduces its own `TaskTransport` ABC. No shared connection manager yet (see ADR 10). Code using one transport cannot accidentally use the other.

---

## ADR 2: Bridge/composite transport rejected

**Status:** Accepted — 2026-04-15

**Decision:** No `CompositeTransport` bridging local + NATS simultaneously.

**Rationale:** Loopback-deduplication would require message-id bookkeeping that overlaps with `_DedupCache` logic. The added complexity does not justify the use case (most deployments will be purely local or purely NATS). YAGNI.

**Alternatives considered:**
- Composite at configuration layer (rejected — still faces loopback problem)
- Dedup at message ID level (rejected — incompatible with transport pluggability)

**Consequences:** Tests must explicitly pick one transport at a time. Mixing transports requires explicit routing logic in application code, not the framework.

---

## ADR 3: EventBus requires explicit transport

**Status:** Accepted — 2026-04-15

**Decision:** `EventBus(transport)` is required; `EventBus()` with no arguments raises `TypeError`.

**Rationale:** Eliminates hidden magic and forces dependency injection. Migrating all 61 call-sites surfaced one place where an unintended in-memory bus was being used instead of the configured transport — explicit DI would have caught this during code review.

**Alternatives considered:**
- Default to `LocalEventTransport` (rejected — hides config errors; silent fallback coupling)
- Optional transport with None sentinel (rejected — same problem)

**Consequences:** All code constructing `EventBus` is updated. Tests become more explicit and testable. Dependency graph is visible at construction time.

---

## ADR 4: Namespace taxonomy documented, implemented only for events in 68

**Status:** Accepted — 2026-04-15

**Decision:** Subject roots are reserved up-front: `events.*` (LABS-68), `tasks.*` (LABS-69), `control.*` (LABS-70+), `health.*` (LABS-70+).

**Rationale:** Locks naming up-front so later subsystems do not conflict or surprise each other. Prevents ad-hoc allocation that causes namespace drift and debugging ambiguity.

**Alternatives considered:**
- Allocate namespace as needed (rejected — historical drift, reviewers can't assume subject role)

**Consequences:** Reviewers can assume a subject's role from its first token. Future subsystems inherit the pattern. Schema migration tooling can use the root to route messages.

---

## ADR 5: Symmetric wire format across transports

**Status:** Accepted — 2026-04-15

**Decision:** Same JSON body + NATS headers convention applies to Events (LABS-68) and will apply to Tasks (LABS-69).

**Rationale:** Tooling like `nats sub`, trace correlation, and debugging tools work uniformly. Reduces cognitive load when switching between event and task messages. Single serialization format for episodes.db.

**Alternatives considered:**
- Per-subsystem serialization (rejected — divergent tooling, harder debugging)

**Consequences:** Schema changes in one subsystem must consider the pattern impact. NATS server upgrades apply uniformly to all message types.

---

## ADR 6: Handler exception = swallow + log + counter

**Status:** Accepted — 2026-04-15

**Decision:** Handler exceptions are logged with full traceback and a per-subscription error counter, never propagated to publisher or other subscribers.

**Rationale:** One bad handler must not crash the bus or prevent other subscribers from seeing the event. Preserves availability and decoupling. Ops monitor via logs and metrics.

**Alternatives considered:**
- Propagate to publisher (rejected — publisher coupling; violates pub/sub contract)
- Ring-buffer of last-N exceptions (deferred — observability is Phase 5 work)

**Consequences:** Silent errors are possible if logs aren't monitored. Error counter metric must be exposed. Handler developers must test failure modes.

---

## ADR 7: Delivery: at-most-once, no retries, no redelivery for events

**Status:** Accepted — 2026-04-15

**Decision:** If a subscriber's handler fails or the network drops a message, it is gone — publisher must retry if needed.

**Rationale:** Aligns with NATS core semantics (not JetStream). Retries are the publisher's concern or a higher-layer orchestrator's responsibility. At-most-once is simpler to reason about for event broadcasting.

**Alternatives considered:**
- At-least-once with JetStream (rejected — different subsystem contract; will exist in LABS-69 Tasks)

**Consequences:** Critical state transitions must go through Tasks (LABS-69), not Events. Event handlers should be idempotent or state-replayable. Docs must clarify this guarantee.

---

## ADR 8: Subscribe-before-start = buffered

**Status:** Accepted — 2026-04-15

**Decision:** `transport.subscribe()` is synchronous (no I/O), buffers the subscription; `start()` registers them with the backend.

**Rationale:** Keeps bootstrap code linear (subscriptions can live in `__init__`). Eases testing (no async required in constructors). Decouples subscription registration from transport availability.

**Alternatives considered:**
- Async subscribe only (rejected — forces `await` in `__init__`; breaks Python async constructor pattern)

**Consequences:** No connection-level validation until `start()`. Subscribing after `start()` requires special handling (deferred to Phase 3 if needed).

---

## ADR 9: Subject prefix = global config

**Status:** Accepted — 2026-04-15

**Decision:** `NATSConfig.subject_prefix` (default `proctor`) is prepended to every subject by both publishers and subscribers.

**Rationale:** Multi-environment isolation on shared NATS clusters (e.g., dev and staging can share a NATS server). Single source of truth for prefix prevents publisher/subscriber skew.

**Alternatives considered:**
- Per-subject prefix (rejected — inconsistent and error-prone)

**Consequences:** Misconfigured prefix creates silent delivery gaps (no error). Ops must validate prefix consistency across nodes. Tests must use distinct prefixes.

---

## ADR 10: NATS connection ownership deferred

**Status:** Accepted — 2026-04-15

**Decision:** LABS-68's `NATSEventTransport` owns its own `nats.Client`; LABS-69 introduces `NATSConnectionManager` to share one connection for both EventTransport and TaskTransport.

**Rationale:** Avoids blocking LABS-68 on an abstraction that only becomes valuable once LABS-69 Tasks exist. Two transports with two connections is acceptable for MVP.

**Alternatives considered:**
- Introduce `NATSConnectionManager` now (rejected — premature; complicates 68b review)

**Consequences:** LABS-69 must refactor 68's direct `nats.Client` usage. Code debt flagged explicitly for LABS-69 kickoff. Test isolation must manage multiple connections during that window.

---

## ADR 11: Wire format: JSON + NATS headers with Nats-Msg-Id

**Status:** Accepted — 2026-04-15

**Decision:** Body is the Event's JSON serialization; metadata (correlation_id, schema_version, dedup_id) lives in NATS headers; dedup uses the native `Nats-Msg-Id` header.

**Rationale:** JSON is human-debuggable in `nats sub`; episodes.db uses the same serialization. `Nats-Msg-Id` is the NATS/JetStream standard dedup header, making a future JetStream migration additive, not breaking.

**Alternatives considered:**
- Bespoke `message-id` header (rejected — non-standard; breaks JetStream migration path)
- Protobuf/msgpack (rejected — loses debuggability; harder to inspect in NATS tools)

**Consequences:** Callers not coupled to a custom envelope type. JetStream dedup and persistence require only adding `nats.json_deserializer` and activating streams. Wire format is stable across versions.

---

## ADR 12: Schema evolution: per-(event-type, schema-version) decoder registry

**Status:** Accepted — 2026-04-15

**Decision:** Each event type manages its own versioning; readers accept prior versions (backward-compat), log+drop unknown future versions (forward-compat).

**Rationale:** Global schema-version bumps are meaningless across independent event types. Per-type versioning decouples upgrade cycles. Backward-compat on read allows gradual rollout.

**Alternatives considered:**
- Global monotonic version (rejected — forced simultaneous upgrades across all event types)

**Consequences:** Decoder registry is per-event. Unknown future schema logs an observability ping but doesn't fail. Event schema migration docs must cover the decoder pattern.

---

## ADR 13: Event charset [a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*

**Status:** Accepted — 2026-04-15

**Decision:** Event type names restricted to lowercase ASCII + digits + underscores, dot-separated (e.g., `user.created`, `task_completed.v2`).

**Rationale:** Consistent with Python identifier style and NATS subject charset. No dashes avoid confusion with hyphens in package names. Enforces readability.

**Alternatives considered:**
- Allow dashes (rejected — NATS subject ambiguity; subtle dedup bugs with URL-encoded subjects)
- Allow mixed case (rejected — case-sensitivity issues in routing and schema registries)

**Consequences:** Migration-time validation catches existing bad names. Docs specify the charset requirement. Linters can enforce this in event definitions.

---

## ADR 14: Clock skew: NTP required; rate-limited WARN > 1h

**Status:** Accepted — 2026-04-15

**Decision:** `Event.timestamp` is assumed UTC; if publisher and subscriber clocks diverge more than 1 hour, transport logs a rate-limited WARN.

**Rationale:** Latency metrics (publish→handler) rely on wall-clock sync. NTP synchronization is standard ops practice. Clock skew detection provides early warning.

**Alternatives considered:**
- Synthetic monotonic timestamps (rejected — harder cross-node correlation)

**Consequences:** Ops must run NTP on all nodes. Diagnosis docs must mention clock skew as a troubleshooting step. Large datacenters with poor NTP may need custom handling.

---

## ADR 15: Subscribe sync / unsubscribe async

**Status:** Accepted — 2026-04-15

**Decision:** `transport.subscribe()` returns a `SubscriptionHandle` synchronously; `SubscriptionHandle.unsubscribe()` is async (may do I/O with backend).

**Rationale:** Matches `nats-py` semantics. Enables subscribe in `__init__`. Async unsubscribe allows clean disconnect from backend without blocking shutdown.

**Alternatives considered:**
- Async subscribe (rejected — forces `await` in constructors)
- Sync unsubscribe (rejected — lies if backend is unavailable)

**Consequences:** Unsubscribe must be awaited. Tests must carefully clean up in `teardown`. Early unsubscribes on fast shutdown might not reach backend (acceptable per ADR 7).

---

## ADR 16: Explicit drain phase

**Status:** Accepted — 2026-04-15

**Decision:** `bus.drain(timeout)` is called after triggers stop and before `bus.stop()`. Prevents in-flight handlers from being cancelled mid-execution.

**Rationale:** Prevents silent event loss when shutdown races with handlers. Gives slow handlers time to finish; cancels stragglers after timeout. Decouples handler completion from transport cleanup.

**Alternatives considered:**
- Drain inside `stop()` (rejected — coupling; caller may want different timeout)

**Consequences:** Bootstrap order is strict: trigger stop → drain → stop. Failure to drain is a config smell. Long-running handlers must finish within drain timeout.

---

## ADR 17: max_payload in EventsConfig, not NATSConfig

**Status:** Accepted — 2026-04-15

**Decision:** `EventsConfig.max_payload` (default 64 KiB) applies to both `LocalEventTransport` and `NATSEventTransport`.

**Rationale:** Enforces symmetric size limits across transports. NATS server-side limit is a separate concern. Prevents discovery that one transport rejects large events after switching modes.

**Alternatives considered:**
- Transport-specific config (rejected — test divergence; surprising behavioural differences)

**Consequences:** NATS server must be configured with `max_payload >= EventsConfig.max_payload`. Very large events (e.g., binary attachments) must use a different pattern (object store references).

---

## ADR 18: Symmetric dedup via Nats-Msg-Id

**Status:** Accepted — 2026-04-15

**Decision:** Both transports deduplicate using `(handler, msg_id)` where `msg_id` is the `Nats-Msg-Id` header value.

**Rationale:** Overlapping subscriptions to the same handler with multiple matching patterns must fire the handler once — identical observable behaviour across local and NATS. Prevents handler re-entrancy bugs.

**Alternatives considered:**
- No local dedup (rejected — asymmetric behaviour between transports)

**Consequences:** `_DedupCache` is shared between transports. Contract tests parametrized over `[local, nats]` verify dedup on both. Cache size must be monitored to prevent unbounded growth.

---

## ADR 19: Subscription ownership policy

**Status:** Accepted — 2026-04-15

**Decision:** Application-level handlers subscribe in `Application.__init__`; component-level handlers subscribe in `component.start()`.

**Rationale:** Removes ambiguity — reviewer can predict where a subscription lives by checking if it's app-wide or component-scoped. Eliminates the "where should I put this?" question that has caused past bugs.

**Alternatives considered:**
- Subscribe anywhere (rejected — caused merge conflicts and subtle startup-order bugs)

**Consequences:** PR-author checklist includes "subscription owned by Application or Component?". Start-up order is predictable and auditable.

---

## ADR 20: Parametrized [local, nats] tests only for contract

**Status:** Accepted — 2026-04-15

**Decision:** Tests are parametrized across transports only for behaviour surfaced by the ABC (lifecycle, publish, subscribe, dedup, size limits, wildcards); implementation-specific tests (e.g., NATS header conversion, reconnect logic) are NATS-only.

**Rationale:** CI time budget; NATS tests require testcontainers. Parametrizing everything would double the test matrix without commensurate value.

**Alternatives considered:**
- Parametrize everything (rejected — slow and wasteful; implementation details are not contract)

**Consequences:** NATS-specific tests marked with `@pytest.mark.nats`. CI runs local tests in quick path, NATS tests gated. Docs clarify which tests are shared contract vs implementation.

---

## ADR 21: Handler dispatch via asyncio.create_task

**Status:** Accepted — 2026-04-15

**Decision:** Each matched handler runs in its own `asyncio.Task` (fire-and-forget); `drain()` awaits the task set with timeout, cancelling stragglers on timeout.

**Rationale:** Slow handler doesn't block the event pipeline or other handlers. Symmetric behaviour across local and NATS — no special-case sequential dispatch. Bounded shutdown via drain timeout.

**Alternatives considered:**
- Sequential dispatch (rejected — head-of-line blocking; one slow handler stalls the bus)
- Trio/anyio nursery (rejected — proctor is asyncio-only)

**Consequences:** Handler execution order within one event is not guaranteed. Tests use `await asyncio.sleep(...)` or `bus.flush()` to serialize when needed. Fast-path performance is improved but tracing becomes harder.

---
