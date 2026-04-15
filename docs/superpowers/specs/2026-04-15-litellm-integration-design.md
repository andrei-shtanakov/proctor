# LiteLLM Integration (LABS-67) — Design

**Status:** Draft
**Date:** 2026-04-15
**Linear:** [LABS-67](https://linear.app/atp-platform-project/issue/LABS-67)
**Phase:** 2 (Proactivity)

## Goal

Replace the mock LLM callable currently wired into `WorkflowEngine` with a real
LiteLLM-backed implementation. Unlocks provider switching (Claude / OpenAI /
Ollama) via config, transient-error fallback to a secondary model, and
per-call token accounting.

## Scope

In scope:

- New module `src/proctor/workers/llm.py` implementing `llm_call(prompt, model=None) -> str`.
- Factory `build_llm_call(config, memory)` producing a `Callable` compatible with
  the existing `LLMCall` alias in `bootstrap.py` (no change to
  `Application.set_llm_call()`).
- Fallback to `config.llm.fallback_model` on transient errors only.
- Per-call persistence in a new `llm_calls` table in `episodes.db`.
- Context propagation of `task_id` / `step_id` / `episode_id` via
  `contextvars.ContextVar`.
- Unit tests with mocked `litellm.acompletion`, plus a skip-if-unavailable
  integration test against local Ollama.

Out of scope (tracked as separate issues):

- Tool-calling / AgentRuntime integration with LiteLLM — will be a follow-up
  issue (`LLMFn(messages, tool_defs)` contract), proposed as **LABS-74**.
- Per-workflow system prompt and richer message construction — bundled with
  the tool-calling issue.
- Router wiring for Telegram / Scheduler triggers — belongs to
  [LABS-65](https://linear.app/atp-platform-project/issue/LABS-65).
  Related observation: Telegram/Scheduler trigger events are currently
  published but **not subscribed**, so `EpisodicMemory` only captures the
  Terminal path today. README overstates this. LABS-65 should fix both the
  subscriptions and the README wording.

## Non-goals

- No new LLM providers beyond what LiteLLM already supports out of the box.
- No distributed tracing / OpenTelemetry wiring (Phase 5).
- No cost aggregation / dashboard — schema supports it, but queries come later.

## Architecture

### New module: `src/proctor/workers/llm.py`

```python
from contextvars import ContextVar

task_id_ctx: ContextVar[str | None] = ContextVar("task_id", default=None)
step_id_ctx: ContextVar[str | None] = ContextVar("step_id", default=None)
episode_id_ctx: ContextVar[str | None] = ContextVar("episode_id", default=None)

def build_llm_call(config: LLMConfig, memory: EpisodicMemory) -> LLMCall:
    """Returns a closure capturing config + memory.

    The closure is `async def _call(prompt: str, model: str | None = None) -> str`,
    structurally compatible with the existing `LLMCall = Callable[[str], Awaitable[str]]`
    alias in `bootstrap.py` because `model` has a default value — callers
    (WorkflowEngine, DAG executor) keep using `await llm_fn(prompt)` unchanged.
    """
    ...
```

The closure reads context vars at invocation time, calls `litellm.acompletion`,
handles fallback, writes one `LLMCallRecord` per attempt, and returns text.

### Changes to existing components

| File | Change |
|------|--------|
| `src/proctor/core/models.py` | Add `LLMCallRecord` pydantic model. |
| `src/proctor/core/memory.py` | Add `llm_calls` DDL to `initialize()`; add `save_llm_call(record)`. |
| `src/proctor/core/config.py` | Add `LLMConfig.request_timeout: float = 60.0`. |
| `src/proctor/core/bootstrap.py` | Create `Episode` **before** workflow execution and set `episode_id_ctx` / `task_id_ctx`. Existing `save_episode` is idempotent (see `memory.py:33-44`, `ON CONFLICT(id) DO UPDATE`). |
| `src/proctor/workflow/engine.py`, `src/proctor/workflow/dag.py` | Before each LLM call, set `step_id_ctx`; reset in `finally`. Simple mode sets `step_id_ctx` to `None`; DAG sets it to the step id. |
| `src/proctor/__main__.py` | Replace mock with `build_llm_call(app.config.llm, app.memory)`. |

### No changes to `Episode` / `episodes` table

Token data lives in the normalized `llm_calls` table. Episode-level totals are
computed via SQL aggregation when needed. Rationale: a single Episode may span
multiple LLM calls (DAG, future multi-turn), and flattening loses fidelity.

## Data model

### `LLMCallRecord` (new pydantic model)

```python
class LLMCallRecord(BaseModel):
    id: str = Field(default_factory=_uuid)
    episode_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    model: str                                 # actually used model
    fallback_used: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None       # Anthropic prompt caching
    cache_write_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None                   # None on success
    created_at: datetime = Field(default_factory=_utcnow)
```

### `llm_calls` table (in `episodes.db`)

```sql
CREATE TABLE IF NOT EXISTS llm_calls (
  id                 TEXT PRIMARY KEY,
  episode_id         TEXT,
  task_id            TEXT,
  step_id            TEXT,
  model              TEXT NOT NULL,
  fallback_used      INTEGER NOT NULL DEFAULT 0,
  prompt_tokens      INTEGER,
  completion_tokens  INTEGER,
  cache_read_tokens  INTEGER,
  cache_write_tokens INTEGER,
  latency_ms         INTEGER,
  error              TEXT,
  created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_episode ON llm_calls(episode_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_task    ON llm_calls(task_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at);
```

Migration: `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` — safe
for both fresh and existing `episodes.db`. No `ALTER TABLE` required.

### Cross-DB FK note

`llm_calls.task_id` references `tasks.id` from `state.db` but **is not
enforced**. SQLite cannot express cross-database foreign keys — attempting it
via `ATTACH` would break WAL isolation. Treat `task_id` (and by extension
`episode_id`) as denormalized lookup keys. Do not add cross-DB constraints in
the future.

## Data flow

### Simple workflow

```
TerminalTrigger → Event(trigger.terminal)
  ↓
bootstrap._handle_terminal:
  task = Task(...);             state.save_task(task)
  episode = Episode(id=uuid, trigger_type=..., user_input=text,
                    agent_response="")   # placeholder, updated later
  memory.save_episode(episode)  # creates row (idempotent)
  task_id_ctx.set(task.id)
  episode_id_ctx.set(episode.id)
  try:
      result = await engine.execute(spec)
      episode.agent_response = result.output
      memory.save_episode(episode)       # update via ON CONFLICT
  finally:
      reset context vars
```

Inside `engine.execute(spec)`:

```
simple mode:
  step_id_ctx stays None
  output = await llm_fn(spec.prompt)

DAG mode (per step):
  token = step_id_ctx.set(step.id)
  try:
      output = await llm_fn(step.prompt)
  finally:
      step_id_ctx.reset(token)
```

Inside `llm_call` (produced by `build_llm_call`):

```
start = monotonic()
chosen = model or config.default_model
try:
    resp = await litellm.acompletion(
        model=chosen,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        timeout=config.request_timeout,
        num_retries=0,                   # our fallback logic owns retries
    )
    record success → memory.save_llm_call(...)
    return resp.choices[0].message.content

except TRANSIENT as exc:
    log WARNING
    record failure of `chosen` → memory.save_llm_call(... error=..., fallback_used=False)
    try second attempt on config.fallback_model
    record outcome (success or error, fallback_used=True)
    return text or raise

except Exception as exc:
    record failure → memory.save_llm_call(... error=..., fallback_used=False)
    raise
```

## Error handling

### Transient error classes (trigger fallback)

```python
_TRANSIENT = (
    litellm.RateLimitError,
    litellm.APIConnectionError,
    litellm.Timeout,
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
)
```

### Non-transient (propagate without fallback)

Authentication, bad request, context-length exceeded, content policy, and any
unknown exception. Rationale: these are bugs or config mistakes; silently
falling back to Ollama would hide them.

### Retry policy

- One attempt on primary, one attempt on fallback. No retry within either.
- LiteLLM's built-in retries are disabled via `num_retries=0` so our
  `llm_calls` rows reflect actual network events rather than hidden retries.

### Records on failure

Every outcome — success, primary-transient, primary-fatal, fallback-success,
fallback-fatal — writes exactly one `LLMCallRecord`. A full fallback cycle
therefore writes 2 rows (primary failure + fallback outcome); a fatal primary
writes 1.

### Usage extraction (safe against missing fields)

```python
usage = getattr(resp, "usage", None)
prompt_tokens      = getattr(usage, "prompt_tokens",                None)
completion_tokens  = getattr(usage, "completion_tokens",            None)
cache_write_tokens = getattr(usage, "cache_creation_input_tokens",  None)
cache_read_tokens  = getattr(usage, "cache_read_input_tokens",      None)
```

Ollama usage fields may be absent — all columns are nullable.

### Logging levels

| Event | Level |
|-------|-------|
| Successful call | `DEBUG` |
| Fallback engaged | `WARNING` with error class + message |
| Fatal (primary non-transient or fallback failure) | `ERROR` with `exc_info=True` |

## Configuration

```python
class LLMConfig(BaseModel):
    default_model: str = "claude-sonnet-4-20250514"
    fallback_model: str = "ollama/llama3.2"
    max_tokens: int = 4096
    temperature: float = 0.7
    request_timeout: float = 60.0        # NEW
```

Environment variables for provider credentials (e.g. `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`) are read by LiteLLM directly; no additional wiring in
Proctor code.

## Testing

### Unit tests — `tests/test_llm.py`

Fixtures:
- `monkeypatch` on `litellm.acompletion`.
- `memory` — `EpisodicMemory` on `tmp_path`, initialized.

Each test asserts both the return value and the resulting rows in `llm_calls`
(verified through a small raw-SQL helper inside the test, no new public
method on `EpisodicMemory`).

Cases:

1. **happy path** — text + usage → 1 row, `fallback_used=0`, `error IS NULL`, correct tokens.
2. **explicit model override** — `llm_call(prompt, model="gpt-4o")` uses the override; record.model matches.
3. **fallback transient** — first call raises `RateLimitError`, second succeeds → 2 rows (failure of primary, success of fallback with `fallback_used=1`).
4. **fallback itself fails** — both raise → 2 error rows, exception propagates.
5. **non-transient propagates** — `AuthenticationError` → 1 error row, fallback model not invoked (mock call count = 1), exception propagates.
6. **contextvars flow** — set `task_id_ctx`/`step_id_ctx`/`episode_id_ctx`, run call, verify columns.
7. **missing usage** — mock returns response without `usage`; row created with NULL tokens.
8. **cache tokens** — mock returns `cache_creation_input_tokens` and `cache_read_input_tokens`; verify mapping to `cache_write_tokens`/`cache_read_tokens`.
9. **latency measured** — mock awaits `anyio.sleep(0.01)`; `latency_ms >= 10`.
10. **num_retries=0 enforced** — intercept kwargs of mocked `acompletion`, assert `num_retries=0`.

### Memory tests — additions to `tests/test_memory.py`

- `save_llm_call` round-trip via raw SQL.
- Indexes exist (`PRAGMA index_list('llm_calls')`).
- `initialize()` is idempotent on an existing database.

### Integration test — `tests/integration/test_llm_ollama.py`

```python
@pytest.mark.integration
@pytest.mark.anyio
async def test_ollama_real_call(tmp_path):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("http://localhost:11434/api/tags", timeout=2):
                pass
    except Exception:
        pytest.skip("Ollama not running at localhost:11434")

    cfg = LLMConfig(default_model="ollama/llama3.2",
                    fallback_model="ollama/llama3.2")
    memory = EpisodicMemory(tmp_path / "episodes.db")
    await memory.initialize()
    call = build_llm_call(cfg, memory)

    result = await call("Say 'hello' and nothing else.")
    assert result
```

`pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = ["integration: requires external services (Ollama, etc.)"]
```

CI: `pytest -m "not integration"`. Local with Ollama: `pytest -m integration`.

README gets a short "Running integration tests" section with the Ollama pull
command and marker usage.

## Acceptance criteria (mirrors LABS-67)

- [ ] `src/proctor/workers/llm.py` exports `llm_call` (via `build_llm_call`
      factory) built on `litellm.acompletion`.
- [ ] `default_model` / `fallback_model` / `max_tokens` / `temperature` /
      `request_timeout` read from `LLMConfig`.
- [ ] Automatic fallback on transient errors only (per the classes above),
      with `WARNING` log on fallback.
- [ ] Token accounting in `llm_calls` table with `prompt_tokens`,
      `completion_tokens`, `cache_read_tokens`, `cache_write_tokens`.
- [ ] Unit tests covering the cases listed above.
- [ ] Integration test against local Ollama (skipped when unavailable).
- [ ] README updated with the integration-test run instructions.

## Risks and open questions

- **Ollama usage fidelity.** Ollama's LiteLLM adapter may not surface
  `prompt_tokens`/`completion_tokens` consistently across model families.
  Tests tolerate `None`; real coverage will be assessed post-merge.
- **LiteLLM exception hierarchy drift.** The transient class list is taken
  from current LiteLLM; if a future release renames classes we re-check on
  upgrade.
- **Cost not persisted.** `response.cost` (when available) is not stored in
  this iteration — easy addition later, but cross-provider semantics vary.
