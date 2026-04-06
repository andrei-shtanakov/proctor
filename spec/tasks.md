# Implement SchedulerTrigger (cron/interval) — Tasks Specification

## Milestone 1: Config & Models

### TASK-001: Add Schedule Item Config Model
🔴 P0 | 🔄 IN_PROGRESS | Est: 1h

Add a `ScheduleItemConfig` pydantic model to `src/proctor/core/config.py` representing a single scheduled job entry. Update `ProctorConfig` to include a `schedules: list[ScheduleItemConfig]` field. Each item specifies a name, a cron expression OR a fixed interval (seconds), the event payload to emit, and an enabled flag.

**Checklist:**
- [ ] Define `ScheduleItemConfig` model with fields: `name` (str), `cron` (str | None), `interval_seconds` (float | None), `payload` (dict), `enabled` (bool, default True)
- [ ] Add pydantic validator ensuring exactly one of `cron` or `interval_seconds` is set
- [ ] Add `schedules: list[ScheduleItemConfig] = []` to `ProctorConfig`
- [ ] Verify existing config loading still works with no schedules defined

**Depends on:**

### TASK-002: Add croniter Dependency
🔴 P0 | 🔄 IN_PROGRESS | Est: 30m

Add `croniter` as a runtime dependency for cron expression parsing. Verify it installs cleanly and is importable.

**Checklist:**
- [ ] Run `uv add croniter`
- [ ] Verify `uv sync` succeeds
- [ ] Verify `from croniter import croniter` works in a quick script

**Depends on:**

## Milestone 2: Core Implementation

### TASK-003: Implement SchedulerTrigger Class
🔴 P0 | 🔄 IN_PROGRESS | Est: 2-3h

Create `src/proctor/triggers/scheduler.py` implementing `SchedulerTrigger(Trigger)`. The trigger accepts a list of `ScheduleItemConfig` items, starts an asyncio task per schedule, and publishes `trigger.scheduler` events on the EventBus when each schedule fires.

**Checklist:**
- [ ] Create `src/proctor/triggers/scheduler.py`
- [ ] Implement `SchedulerTrigger.__init__(self, schedules: list[ScheduleItemConfig])` storing schedules and internal state
- [ ] Implement `start(self, bus: EventBus)` — launch one asyncio task per enabled schedule item
- [ ] Implement `stop(self)` — cancel all running tasks with proper cleanup (suppress CancelledError, like TerminalTrigger)
- [ ] Implement `_run_cron(self, item, bus)` — loop using croniter to compute next fire time, asyncio.sleep until then, publish event
- [ ] Implement `_run_interval(self, item, bus)` — loop with fixed asyncio.sleep, publish event
- [ ] Publish `Event(type="trigger.scheduler", source=f"scheduler:{item.name}", payload=item.payload)` on each fire
- [ ] Add logging at DEBUG (each fire) and INFO (start/stop) levels
- [ ] Handle edge case: if next cron time is in the past (e.g. after long sleep), skip to next future occurrence

**Depends on:** TASK-001, TASK-002

### TASK-004: Register SchedulerTrigger in Bootstrap
🟠 P1 | ⬜ TODO | Est: 1h

Wire `SchedulerTrigger` into the application bootstrap so it starts alongside `TerminalTrigger` when schedules are configured.

**Checklist:**
- [ ] Import `SchedulerTrigger` in bootstrap module
- [ ] Instantiate `SchedulerTrigger` from `config.schedules` if list is non-empty and `config.scheduler.enabled`
- [ ] Call `scheduler_trigger.start(bus)` during startup
- [ ] Call `scheduler_trigger.stop()` during shutdown
- [ ] Export `SchedulerTrigger` from `src/proctor/triggers/__init__.py`

**Depends on:** TASK-003

## Milestone 3: Testing

### TASK-005: Unit Tests for ScheduleItemConfig Validation
🔴 P0 | 🔄 IN_PROGRESS | Est: 1h

Test the config model validation: valid cron, valid interval, both set (error), neither set (error), disabled items.

**Checklist:**
- [ ] Create `tests/test_triggers/test_scheduler.py`
- [ ] Test valid cron-based config (`cron="*/5 * * * *"`, no interval)
- [ ] Test valid interval-based config (`interval_seconds=60`, no cron)
- [ ] Test validation error when both `cron` and `interval_seconds` are set
- [ ] Test validation error when neither `cron` nor `interval_seconds` is set
- [ ] Test `enabled=False` is accepted
- [ ] Test config loads from YAML with schedules section

**Depends on:** TASK-001

### TASK-006: Unit Tests for SchedulerTrigger Cron Mode
🔴 P0 | 🔄 IN_PROGRESS | Est: 1-2h

Test that cron-based schedules fire events at the correct times. Use time mocking or short cron expressions to keep tests fast.

**Checklist:**
- [ ] Test that a cron schedule publishes an event on the bus after firing
- [ ] Test that `stop()` cleanly cancels cron tasks without errors
- [ ] Test that disabled schedule items are not started
- [ ] Test that the event has correct `type`, `source`, and `payload` fields
- [ ] Use `anyio` for async tests (not asyncio), per project conventions

**Depends on:** TASK-003

### TASK-007: Unit Tests for SchedulerTrigger Interval Mode
🔴 P0 | 🔄 IN_PROGRESS | Est: 1h

Test that interval-based schedules fire events repeatedly at the configured interval.

**Checklist:**
- [ ] Test that an interval schedule publishes events on the bus
- [ ] Test that multiple intervals fire multiple events (with short interval like 0.1s)
- [ ] Test that `stop()` cleanly cancels interval tasks
- [ ] Test event payload matches config
- [ ] Use `anyio` for async tests

**Depends on:** TASK-003

### TASK-008: Integration Test — SchedulerTrigger with EventBus
🟠 P1 | ⬜ TODO | Est: 1h

End-to-end test: create a real EventBus, start SchedulerTrigger with a short interval, verify events arrive on the bus via a subscriber.

**Checklist:**
- [ ] Subscribe to `trigger.scheduler` on a real EventBus instance
- [ ] Start SchedulerTrigger with a 0.1s interval schedule
- [ ] Collect published events for ~0.5s, verify at least 2 events received
- [ ] Stop trigger, verify clean shutdown
- [ ] Use `anyio` for async tests

**Depends on:** TASK-006, TASK-007

## Milestone 4: Quality

### TASK-009: Lint, Format, and Type Check
🟠 P1 | ⬜ TODO | Est: 30m

Run all code quality tools on new and modified files, fix any issues.

**Checklist:**
- [ ] Run `uv run ruff format .` and fix formatting
- [ ] Run `uv run ruff check .` and fix lint issues
- [ ] Run `pyrefly check` and fix type errors
- [ ] Verify all existing tests still pass with `uv run pytest`

**Depends on:** TASK-004, TASK-008
