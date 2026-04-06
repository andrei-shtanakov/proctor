```markdown
# Implement TelegramTrigger — Tasks Specification

## Milestone 1: Config & Model

### TASK-001: Add TelegramConfig to ProctorConfig
🔴 P0 | 🔄 IN_PROGRESS | Est: 30m

Add a `TelegramConfig` pydantic model to `src/proctor/core/config.py` and wire it into `ProctorConfig` as an optional `telegram` field.

**Checklist:**
- [ ] Create `TelegramConfig(BaseModel)` with `bot_token: str`, `allowed_chat_ids: list[int]`, `poll_timeout: int = 30`
- [ ] Add `telegram: TelegramConfig | None = None` to `ProctorConfig`
- [ ] Verify existing config tests still pass (`uv run pytest`)
- [ ] Run `pyrefly check` and fix any type errors

**Depends on:**

## Milestone 2: TelegramTrigger Implementation

### TASK-002: Implement TelegramTrigger core
🔴 P0 | 🔄 IN_PROGRESS | Est: 1-2h

Create `src/proctor/triggers/telegram.py` following the TerminalTrigger pattern. Uses aiohttp to long-poll Telegram Bot API `getUpdates`, filters by `allowed_chat_ids`, and publishes `trigger.telegram` events on EventBus.

**Checklist:**
- [ ] Create `TelegramTrigger(Trigger)` with `__init__(self, config: TelegramConfig)`
- [ ] Implement `start(self, bus: EventBus)` — create `aiohttp.ClientSession`, launch polling task
- [ ] Implement `stop(self)` — cancel task, close aiohttp session
- [ ] Implement `_poll_loop(self, bus: EventBus)` — call `getUpdates` with `offset` and `timeout`, handle HTTP errors with retry/backoff
- [ ] Filter messages: skip if `chat_id` not in `allowed_chat_ids` (when list is non-empty)
- [ ] Extract message text and publish `Event(type="trigger.telegram", source="telegram", payload={"text": ..., "chat_id": ..., "message_id": ...})`
- [ ] Track `_offset` to avoid reprocessing messages
- [ ] Add logging at debug/info/error levels
- [ ] Run `uv run ruff format .` and `uv run ruff check .`
- [ ] Run `pyrefly check` and fix any type errors

**Depends on:** TASK-001

### TASK-003: Register TelegramTrigger in triggers module
🔴 P0 | 🔄 IN_PROGRESS | Est: 15m

Export `TelegramTrigger` from `src/proctor/triggers/__init__.py` and wire it into bootstrap if `config.telegram` is set.

**Checklist:**
- [ ] Add `TelegramTrigger` import and export in `src/proctor/triggers/__init__.py`
- [ ] Update bootstrap in `src/proctor/core/bootstrap.py` to instantiate and start `TelegramTrigger` when `config.telegram` is not None
- [ ] Verify existing tests still pass

**Depends on:** TASK-002

## Milestone 3: Tests

### TASK-004: Unit tests for TelegramTrigger
🔴 P0 | 🔄 IN_PROGRESS | Est: 1-2h

Create `tests/test_triggers/test_telegram.py` with comprehensive tests using mocked aiohttp responses.

**Checklist:**
- [ ] Test `start` creates session and launches polling task
- [ ] Test `stop` cancels task and closes session
- [ ] Test successful message polling publishes `trigger.telegram` event with correct payload
- [ ] Test `allowed_chat_ids` filtering — messages from disallowed chats are skipped
- [ ] Test empty `allowed_chat_ids` allows all chats
- [ ] Test offset tracking — subsequent polls use updated offset
- [ ] Test HTTP error handling (non-200 response) — trigger retries without crashing
- [ ] Test malformed API response (missing `result` key) is handled gracefully
- [ ] Test messages without `text` field are skipped
- [ ] Use `anyio` for async tests (not asyncio), following project conventions
- [ ] Run full test suite: `uv run pytest`

**Depends on:** TASK-002

### TASK-005: Config tests for TelegramConfig
🟠 P1 | 🔄 IN_PROGRESS | Est: 30m

Add tests verifying TelegramConfig loads correctly from YAML and defaults work.

**Checklist:**
- [ ] Test `ProctorConfig` with no `telegram` section yields `None`
- [ ] Test `ProctorConfig` with valid `telegram` section parses `bot_token` and `allowed_chat_ids`
- [ ] Test `TelegramConfig` defaults (`poll_timeout=30`)
- [ ] Run `uv run pytest`

**Depends on:** TASK-001

## Milestone 4: Quality

### TASK-006: Lint, format, and type-check pass
🟠 P1 | ⬜ TODO | Est: 15m

Final quality gate — ensure all checks pass on the complete changeset.

**Checklist:**
- [ ] Run `uv run ruff format .`
- [ ] Run `uv run ruff check .` — zero errors
- [ ] Run `pyrefly check` — zero errors
- [ ] Run `uv run pytest` — all tests pass

**Depends on:** TASK-003, TASK-004, TASK-005
```
