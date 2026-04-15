# LABS-68 — NATS transport

**Released:** 2026-04-15

## Summary

Pluggable event-transport layer. `EventBus` is now a thin wrapper over
an `EventTransport` — either in-process (`LocalEventTransport`) or
across nodes via NATS (`NATSEventTransport`). Identical observable
behaviour across both backends (wildcard matching, dedup, drain).
Lays the foundation for multi-node `core`/`worker` deployments.

Ships atomically as PR 68a + PR 68b — the abstraction isn't useful
without the NATS implementation, and the NATS implementation isn't
useful without the abstraction.

## What's new

- **`EventTransport` ABC** with two implementations. Swap via
  `ProctorConfig.transport: "auto" | "local" | "nats"`.
- **`transport: "auto"`** resolves by `node_role`: `standalone` → local,
  `core`/`worker` → NATS.
- **Lazy `nats-py` dependency.** `pip install proctor[nats]` to opt in.
  Friendly `ImportError` otherwise.
- **Wire format:** JSON body + 5 NATS headers
  (`content-type`, `schema-version`, `event-type`, `Nats-Msg-Id`,
  `published-at`). Interoperable with future Go/JS publishers.
- **Decoder registry.** `register_decoder(event_type, version, fn)` for
  per-type schema evolution. Unknown versions dropped with rate-limited
  WARN (forward-compat).
- **Dedup cache** uniform across transports via `Nats-Msg-Id` +
  handler identity — overlapping subscriptions deliver each event
  exactly once.
- **Reconnect + jitter.** `reconnect_jitter: 0.5` default; state
  transitions logged at most once per entry.
- **Multi-node config section** in README with Docker Compose topology,
  `core.yaml` / `worker.yaml` examples, and rollback instructions.

## Observability

- `NATSEventTransport.connection_state` returns
  `CONNECTED | RECONNECTING | DISCONNECTED`.
- `add_disconnect_listener()` / `add_reconnect_listener()` return
  `ListenerHandle` with `.remove()`.
- Clock-skew WARN rate-limited when received events' `published-at`
  drifts >1 hour (NTP recommended).

## Breaking changes

- `NATSConfig.url: str` → `NATSConfig.servers: list[str]`.
  Migrate YAML:
  ```diff
  - nats:
  -   url: nats://localhost:4222
  + nats:
  +   servers:
  +     - nats://localhost:4222
  ```
- `nats-py` moved from runtime to optional extra. `pip install proctor`
  no longer installs it. Multi-node deployments must use
  `pip install proctor[nats]`.
- `EventBus()` now requires an `EventTransport` argument. Pre-LABS-68
  callers that did `EventBus()` should construct as
  `EventBus(LocalEventTransport())`. All in-tree call-sites migrated in
  PR 68a.
- `ProctorConfig.node_role` tightened to `Literal["standalone", "core",
  "worker"]` — invalid strings now raise at load.

## Rollback

```yaml
transport: local
```

Restart. NATS config is ignored (with WARN) but accepted.

## Tests

- 572 passing unit tests (+19 over 68a baseline).
- 11 parametrized `[local, nats]` contract tests — identical behaviour
  verified under both backends.
- 3 Toxiproxy-driven reconnect tests — state transitions and delivery
  resume after induced partitions.
- 1 cross-node signature test — two transports on the same NATS with
  shared subject prefix exchange events.
- CI `integration-nats` job runs contract + reconnect suites against a
  GHA `services: nats:2-alpine`.

## References

- Spec: `docs/superpowers/specs/2026-04-15-nats-transport-design.md`
- ADRs: `docs/superpowers/adr/2026-04-15-nats-transport.md` (21 entries)
- Plan: `docs/superpowers/plans/2026-04-15-nats-transport.md`
