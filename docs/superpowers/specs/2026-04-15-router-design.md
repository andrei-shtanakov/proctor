# Router (LABS-65) — Design

**Status:** Draft
**Date:** 2026-04-15
**Linear:** [LABS-65](https://linear.app/atp-platform-project/issue/LABS-65)
**Phase:** 2 (Proactivity)

## Goal

Replace the hard-coded `Application._handle_terminal` path with a declarative
Router that maps `trigger.*` events to catalog workflows. Telegram and
Scheduler triggers — currently published but unhandled — become first-class
routable inputs. The component is also the foundation for future triggers
(webhook — LABS-66) and for cross-cutting routing concerns (rate-limit,
dead-letter) in later phases.

## Scope

In scope:

- New `Router` class in `src/proctor/core/router.py`.
- `RouteRule` pydantic model in `src/proctor/core/config.py`.
- `ProctorConfig.workflows: dict[str, WorkflowSpec]` and
  `ProctorConfig.routes: list[RouteRule]`.
- Three pydantic validators on `ProctorConfig`: orphan route references,
  key/field consistency in the workflow catalog, and shadowed-rules
  detection.
- Generic `_handle_trigger_event` replacing `_handle_terminal`; bootstrap
  subscribes on `trigger.*` rather than `trigger.terminal`.
- `routing.*` observability events published on the bus:
  `routing.unmatched`, `routing.binding_failed`.
- Unit tests for routing logic, config validators, and bootstrap end-to-end.
- README update documenting the routing surface and the closure of the
  "hidden regression" noted in the LABS-67 spec (Telegram/Scheduler were
  publishing to nobody).

Out of scope (tracked as follow-ups or later issues):

- CLI dry-run tool (`proctor routes --dry-run trigger.X`) — useful debug
  aid but not required for this issue.
- Rate-limiting / deduplication of routing-failure WARNINGs — matters when
  schedulers fire repeatedly without matching rules; handle in a
  follow-up when the need is real.
- Richer binding languages (Jinja, JMESPath) or non-string prompts —
  today we support dotted path → string terminal only.
- `source`-pattern matching on schedule items — use `payload.prompt` per
  schedule instead; revisit if a future use case justifies a separate
  trigger type per schedule.
- Multi-match / fan-out routing — routing stays single-decision; the bus
  already supports fan-out via additional subscribers.

## Design decisions

These were settled in brainstorming; they are the binding contract for
implementation.

| # | Decision |
|---|----------|
| Q1 | Rule shape = **catalog + workflow_id** (not inline, not factory). |
| Q1a | Catalog is a `dict[str, WorkflowSpec]`, not a list — the key *is* the id. |
| Q2 | Prompt binding lives on **`RouteRule`**, not on `WorkflowSpec`. Exactly one of `prompt` or `prompt_from_payload` per rule. |
| Q2a | `prompt_from_payload` is a dotted path (`"message.text"`), resolved via `str.split(".")`. Must resolve to a `str`. |
| Q3 | **First-match wins**, by YAML order. No explicit priority field. |
| Q3a | `ProctorConfig` validator rejects configs where a broader rule precedes a narrower one — heuristic: A strictly subsumes B iff `fnmatch(B, A) and not fnmatch(A, B)`. |
| Q4 | On unmatched event: publish `routing.unmatched` on the bus + log WARNING with tried patterns. No silent drop, no default-route field. |
| Q4a | Router is subscribed only to `trigger.*` (anti-loop). `routing.*` events are never re-routed. |
| Q5 | On binding failure (path missing or non-string terminal): publish `routing.binding_failed` + log WARNING. Task is **not** created — this is a config bug, not a workflow-execution failure. |

## Architecture

### New module: `src/proctor/core/router.py`

```python
class Router:
    """Map trigger events to workflow specs via declarative rules.

    Given an event, iterate routes in order, return a cloned WorkflowSpec
    with the resolved prompt on the first matching rule. On unmatched or
    binding-failure, publish a routing.* observability event on the bus
    and return None. Subscribers on routing.* should treat these as
    read-only signals — Router does not listen to its own namespace.
    """

    def __init__(
        self,
        bus: EventBus,
        routes: list[RouteRule],
        workflows: dict[str, WorkflowSpec],
    ) -> None: ...

    async def route(self, event: Event) -> WorkflowSpec | None: ...
```

### Public surface added to `src/proctor/core/config.py`

```python
class RouteRule(BaseModel):
    event_pattern: str                         # fnmatch pattern
    workflow_id: str
    prompt: str | None = None
    prompt_from_payload: str | None = None     # dotted path

    @model_validator(mode="after")
    def _exactly_one_prompt_source(self) -> Self: ...


class ProctorConfig(BaseModel):
    # ... existing fields ...
    workflows: dict[str, WorkflowSpec] = Field(default_factory=dict)
    routes: list[RouteRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_catalog_keys(self) -> Self: ...

    @model_validator(mode="after")
    def _validate_route_refs(self) -> Self: ...

    @model_validator(mode="after")
    def _no_shadowed_routes(self) -> Self: ...


def _is_strictly_broader(a: str, b: str) -> bool:
    """True if fnmatch pattern `a` strictly subsumes pattern `b`.

    Heuristic: treat `b` as a literal string; if `fnmatch(b, a)` and not
    `fnmatch(a, b)`, `a` is broader.
    """
    return fnmatchcase(b, a) and not fnmatchcase(a, b)
```

### Changes to existing components

| File | Change |
|------|--------|
| `src/proctor/core/bootstrap.py` | Remove `_handle_terminal`. Add `_handle_trigger_event(event)` with the same task+episode+ctxvar lifecycle, but it pulls the `WorkflowSpec` from `self._router.route(event)` instead of constructing one inline. Subscribe on `trigger.*`. Router is instantiated in `Application.__init__` (alongside `self.bus`, `self.state`, `self.memory`) and does not need to run lifecycle methods itself. |
| `src/proctor/core/__init__.py` | Re-export `Router`, `RouteRule`. |
| `src/proctor/workflow/spec.py` | No changes. Catalog entries keep `prompt=None`; Router uses `spec.model_copy(update={"prompt": resolved_prompt})`. |
| `config/proctor.yaml` | Add the `workflows:` and `routes:` sections (example below). |
| `README.md` | New **Routing** subsection; update Phase 2 status to remove the "Telegram/Scheduler events go nowhere" regression. |

### Example config

```yaml
workflows:
  chat:
    workflow_id: chat
    mode: simple
  heartbeat:
    workflow_id: heartbeat
    mode: simple

routes:
  - event_pattern: "trigger.terminal"
    workflow_id: chat
    prompt_from_payload: text
  - event_pattern: "trigger.telegram"
    workflow_id: chat
    prompt_from_payload: text
  - event_pattern: "trigger.scheduler"
    workflow_id: heartbeat
    prompt_from_payload: prompt   # each schedule item puts its prompt into payload

schedules:
  - name: heartbeat
    interval_seconds: 3600
    payload: { prompt: "Check system status and report" }
  - name: daily-report
    cron: "0 9 * * *"
    payload: { prompt: "Produce the daily operations report" }
```

Note: the scheduler route resolves `prompt` from each item's `payload`.
Different schedules → different prompts, same workflow catalog entry.
Adding a new schedule requires no Router/config changes beyond the
`schedules:` block itself.

With no `routes:` / `workflows:` configured (fresh checkout), Application
starts cleanly — every trigger event produces a `routing.unmatched` event
and a WARNING log. Nothing meaningful happens, but nothing crashes.

## Data flow

```
TerminalTrigger / TelegramTrigger / SchedulerTrigger
  ↓ publish Event(type="trigger.*", source=..., payload=...)
EventBus
  ↓ subscribe("trigger.*")
Application._handle_trigger_event(event):
  spec = await self._router.route(event)
  if spec is None:
      return                               # router already emitted routing.*
  task = Task(trigger_event=event.id, spec=spec.model_dump())
  state.save_task(task); state.save_task(task → RUNNING)
  episode = Episode(trigger_type=event.source, user_input=spec.prompt, agent_response="")
  memory.save_episode(episode)
  task_id_ctx.set(task.id); episode_id_ctx.set(episode.id)
  try:
      result = await engine.execute(spec)
  except Exception: …                      # → FAILED + task.failed (existing)
  finally: reset ctxvars
  # success/failure path: update episode, publish task.completed|failed
```

### Router.route internals

```python
async def route(self, event: Event) -> WorkflowSpec | None:
    for rule in self._routes:
        if not fnmatchcase(event.type, rule.event_pattern):
            continue
        # matched — now resolve the binding
        if rule.prompt is not None:
            prompt = rule.prompt
        else:
            prompt, reason = _resolve_path(
                event.payload, rule.prompt_from_payload
            )
            if prompt is None:
                logger.warning(
                    "Binding failed: pattern=%s workflow_id=%s "
                    "path=%s reason=%s",
                    rule.event_pattern, rule.workflow_id,
                    rule.prompt_from_payload, reason,
                )
                await self._bus.publish(
                    Event(
                        type="routing.binding_failed",
                        source="router",
                        payload={
                            "original_event_id": event.id,
                            "original_type": event.type,
                            "original_source": event.source,
                            "original_payload": event.payload,
                            "original_timestamp": event.timestamp.isoformat(),
                            "workflow_id": rule.workflow_id,
                            "binding_path": rule.prompt_from_payload,
                            "reason": reason,
                        },
                    )
                )
                return None
        base = self._workflows[rule.workflow_id]
        logger.debug(
            "Routed event type=%s to workflow_id=%s",
            event.type, rule.workflow_id,
        )
        return base.model_copy(update={"prompt": prompt})

    # no rule matched
    logger.warning(
        "No route matched event: type=%s source=%s id=%s "
        "(tried %d patterns: %s)",
        event.type, event.source, event.id, len(self._routes),
        ", ".join(r.event_pattern for r in self._routes),
    )
    await self._bus.publish(
        Event(
            type="routing.unmatched",
            source="router",
            payload={
                "original_event_id": event.id,
                "original_type": event.type,
                "original_source": event.source,
                "original_payload": event.payload,
                "original_timestamp": event.timestamp.isoformat(),
            },
        )
    )
    return None
```

### Dotted-path resolver

Returns `(value, reason)` tuple so the caller can record a specific
diagnostic on the `routing.binding_failed` event. On success, reason is
`None`; on failure, value is `None` and reason identifies the failure
class.

```python
def _resolve_path(
    payload: dict[str, Any], path: str
) -> tuple[str | None, str | None]:
    """Walk dotted path through nested dicts.

    On success: (value, None).
    On failure: (None, <reason>), where reason is one of:
      - "top-level key '<k>' missing"
      - "intermediate value at '<prefix>' is not a dict"
      - "terminal value at '<path>' is <type>, expected str"
    """
    current: Any = payload
    traversed: list[str] = []
    for key in path.split("."):
        if not isinstance(current, dict):
            prefix = ".".join(traversed) or "<root>"
            return None, f"intermediate value at '{prefix}' is not a dict"
        if key not in current:
            if not traversed:
                return None, f"top-level key '{key}' missing"
            prefix = ".".join(traversed)
            return None, f"key '{key}' missing under '{prefix}'"
        current = current[key]
        traversed.append(key)
    if not isinstance(current, str):
        return (
            None,
            f"terminal value at '{path}' is {type(current).__name__}, "
            "expected str",
        )
    return current, None
```

## Config validators

### `_exactly_one_prompt_source` (on `RouteRule`)

`prompt` and `prompt_from_payload` are mutually exclusive; at least one
required. Violation → `ValidationError`.

### `_validate_catalog_keys` (on `ProctorConfig`)

For each `(k, spec) in workflows.items()`, enforce
`spec.workflow_id == k`. This keeps the YAML key and the serialized field
in sync and prevents accidental drift when the file is edited by hand.

### `_validate_route_refs` (on `ProctorConfig`)

For each route, `rule.workflow_id` must be a key of `workflows`. Message
lists known workflow ids so the user can spot typos immediately.

### `_no_shadowed_routes` (on `ProctorConfig`)

For every pair of rules `(earlier, later)`, call `_is_strictly_broader`.
On a hit, raise with both patterns and the indices. Example:

```
route #0 pattern='trigger.*' shadows route #1 pattern='trigger.telegram'.
Put specific rules before catch-all rules.
```

The heuristic misses cases where patterns intersect without one being a
subset (e.g. `trigger.a.*` vs `trigger.*.b`). This is acceptable for
current proctor-a traffic — most patterns are two- or three-segment and
match by prefix. Future tightening tracked as "Risks" below.

## Error handling and observability

### Three Router outcomes

| Outcome | Return | Bus | Log |
|---------|--------|-----|-----|
| matched + binding ok | cloned `WorkflowSpec` | — | `DEBUG` (event.type, workflow_id) |
| unmatched | `None` | `routing.unmatched` | `WARNING` with tried patterns |
| binding failed | `None` | `routing.binding_failed` | `WARNING` with path, reason |

Internal exceptions (shouldn't happen in normal operation) log at `ERROR`
with `exc_info=True` and propagate to the bootstrap-level `try/except`.

### `routing.*` payload shapes (contract)

`routing.unmatched`:

```python
{
    "original_event_id": str,
    "original_type": str,
    "original_source": str,
    "original_payload": dict[str, Any],
    "original_timestamp": str,    # ISO-8601, from event.timestamp
}
```

`routing.binding_failed`:

```python
{
    "original_event_id": str,
    "original_type": str,
    "original_source": str,
    "original_payload": dict[str, Any],
    "original_timestamp": str,    # ISO-8601
    "workflow_id": str,
    "binding_path": str,
    "reason": str,                # one of:
                                  #   "top-level key 'X' missing"
                                  #   "key 'X' missing under 'A.B'"
                                  #   "intermediate value at 'A' is not a dict"
                                  #   "terminal value at 'A.B' is int, expected str"
}
```

The `original_*` fields are symmetric with `routing.unmatched` — metrics
subscribers can group failures by `original_source` or replay the payload
without having to correlate events across two separate bus messages. The
`reason` is specific enough for an operator to know immediately whether
they have a wrong key, wrong shape, or wrong type, and the string is
stable enough to match against in alerts if someone wants to.

**Namespace convention** (documented in the Router module docstring):
`routing.*` events are observability signals emitted by the Router.
Subscribers may use them for dashboards, alerts, dead-letter queues, or
rate-limit-watching. The Router itself does **not** subscribe to this
namespace — it only listens to `trigger.*` via the bootstrap subscription.

### Anti-loop

Bootstrap subscription is `trigger.*`, not `*`. The Router never sees
`routing.unmatched` or `routing.binding_failed` events, so it cannot emit
new ones in response. A dedicated unit test exercises this invariant.

### What does *not* go through Router

| Failure | Owner | Effect |
|---------|-------|--------|
| Config validation errors (orphan id, shadowed rule, key mismatch) | `load_config` → `pydantic.ValidationError` | Application refuses to start |
| `engine.execute` raising | existing `_handle_trigger_event` `try/except` | task → FAILED, `task.failed` event, episode with error |
| `memory.save_*` raising | existing bootstrap logic | propagates to outer `except` |

## Testing

### Router unit tests — `tests/test_core/test_router.py`

Fixtures: fresh `EventBus` per test (captures published events via a
simple list-accumulator subscriber).

Cases:

1. Happy path — `trigger.terminal` + rule with `prompt_from_payload=text`
   resolves to a spec whose `prompt` is the event's text.
2. Literal `prompt` bypasses the payload entirely.
3. First-match wins: two matching rules, only the first is applied.
4. Unmatched event: returns `None`, publishes `routing.unmatched` with
   every `original_*` field, WARNING logged with tried-patterns line.
5. Binding failure — top-level key missing: `prompt_from_payload=text`
   with `payload={}` → `None`, `routing.binding_failed` with
   `reason="top-level key 'text' missing"`.
6. Binding failure — non-string terminal: `prompt_from_payload=chat_id`
   with `payload={"chat_id": 123}` → `reason` starts with `"terminal
   value at 'chat_id' is int, expected str"`.
7. Nested dotted path happy: `message.text` with
   `payload={"message": {"text": "hi"}}` → resolves.
8. Nested dotted path — intermediate missing: `message.text` with
   `payload={"other": {}}` → `reason="key 'message' missing under '<root>'"`
   (or equivalent per the resolver contract).
8a. Nested dotted path — intermediate not a dict: `message.text` with
    `payload={"message": "hi"}` → `reason` contains "is not a dict".
9. Router ignores its own namespace (no subscription on `routing.*`,
   verified via bus state inspection).
10. `model_copy(update)` preserves other spec fields (mode, steps, etc.).

### Config validator tests — `tests/test_core/test_config.py`

1. `test_orphan_workflow_id` — route references missing id → raises.
2. `test_workflow_dict_key_vs_field_mismatch` — key/field drift → raises.
3. `test_shadowed_rule_raises` — broad-before-narrow → raises.
4. `test_shadowed_order_reversed_passes` — narrow-before-broad → valid.
5. `test_intersecting_not_subsuming_passes` — patterns that overlap but
   don't subset pass the heuristic.
6. `test_exactly_one_prompt_source` — both or neither → raises.
7. `test_empty_workflows_and_routes` — both empty → valid (dev default).

### Bootstrap integration tests — additions to `tests/test_core/test_bootstrap.py`

1. **Terminal end-to-end** — publish `trigger.terminal` via bus, verify
   task created, episode persisted, `agent_response` from mock LLM.
2. **Telegram end-to-end** — publish `trigger.telegram` with
   `{"text": "hi", "chat_id": 1}`, same checks.
3. **Scheduler end-to-end** — publish `trigger.scheduler` with
   `{"prompt": "heartbeat"}`, same checks.
4. **Unmatched event creates no task** — publish `trigger.webhook`
   (no matching rule), verify `state.db` has no new row, verify
   `routing.unmatched` was captured on the bus.
5. **Subscription invariant** — `application.bus` has a `trigger.*`
   subscription, not `trigger.terminal`.

### README

New `## Routing` section with the example YAML and a three-sentence
summary: rules match `trigger.*` in order; workflows live in a named
catalog; unmatched events emit `routing.unmatched`.

Update the Phase 2 status block to remove the "Telegram/Scheduler
events go nowhere" regression — this issue closes it.

## Acceptance criteria (mirrors LABS-65)

- [ ] `Router` class exported from `src/proctor/core/router.py`.
- [ ] `RouteRule` + `ProctorConfig.workflows` / `routes` in
      `src/proctor/core/config.py`.
- [ ] Three `ProctorConfig` validators: orphan refs, catalog key/field
      consistency, shadowed rules.
- [ ] `_handle_terminal` replaced by `_handle_trigger_event` subscribed
      on `trigger.*`.
- [ ] `routing.unmatched` and `routing.binding_failed` events published
      with the payload shapes documented above (including symmetric
      `original_*` fields on both).
- [ ] `_resolve_path` returns `(value, reason)` with specific reason
      strings for missing-key / not-dict / non-string-terminal.
- [ ] Unit tests for all cases listed above.
- [ ] README `## Routing` section with example YAML.
- [ ] README **Upgrading to LABS-65** subsection with the minimal
      `workflows:` + `routes:` block that restores pre-LABS-65 terminal
      behavior. (Breaking change: an existing user will get silent
      `routing.unmatched` on every stdin input until they add config.)
- [ ] `config/proctor.yaml` example updated with `workflows:` and
      `routes:` (and `schedules:` items carrying `payload.prompt`).

## Risks and future work

- **Event-storm on misconfigured scheduler.** A 30-second cron with no
  matching rule produces a `routing.unmatched` every tick. Tolerable
  with today's 3 triggers and one operator. If it becomes noise, add
  per-pattern rate limiting (e.g. log once per N minutes) either in the
  Router or in an observability subscriber listening on `routing.*`.
- **Shadow detection is heuristic.** `_is_strictly_broader` misses
  non-subset intersections like `trigger.a.*` vs `trigger.*.b`. Current
  fnmatch usage in proctor-a doesn't hit this case. Tighten when needed.
- **Binding language is intentionally narrow.** Dotted path → string
  terminal covers the three current triggers. Webhook (LABS-66) may
  bring JSON bodies needing Jinja/JMESPath — track as a separate binding
  issue when the use case arrives.
- **CLI dry-run tool.** A `proctor routes --dry-run trigger.X` command
  would short-circuit many routing debug sessions. Out of scope here;
  file as a follow-up when the first real debug need hits.

## Related issues

- **LABS-67** (LiteLLM integration) — noted this "hidden regression" in
  its spec. Closed by this issue. Also provides the verified
  `save_episode` idempotency (`memory.py:33-44`, `ON CONFLICT(id) DO
  UPDATE`) that this design's "pre-execution Episode save, update after"
  pattern depends on.
- **LABS-66** (WebhookTrigger) — blocked by this issue. Webhook simply
  publishes another `trigger.*`; the Router handles it without code
  changes once a route rule is added to the config.
- **LABS-68** (NATS transport) — routing becomes cross-node when
  distributed. `Router` staying declarative/pure is a design enabler:
  the same config works whether the bus is local or NATS-backed.
- **LABS-74** (tool calling / AgentRuntime wiring) — future extensions
  will add fields to `RouteRule` (e.g. `system`) or migrate to an
  explicit `Binding` layer. The RouteRule → engine-kwargs contract is
  the natural extension point.
