# Tasks

> Tasks with priorities, dependencies, and traceability to requirements

## Legend

**Priority:**
- P0 — Critical, blocks the release
- P1 — High, needed for full usability
- P2 — Medium, experience improvement
- P3 — Low, nice to have

**Status:**
- TODO
- IN PROGRESS
- DONE
- BLOCKED

**Estimate:**
- Use days (d) or hours (h)
- A range is preferred: 3-5d

---

## Definition of Done (for EVERY task)

> A task is NOT considered complete without fulfilling these items:

- [ ] **Unit tests** — coverage >= 80% of new code
- [ ] **Tests pass** — `uv run pytest` all green
- [ ] **Lint** — `uv run ruff check src/ tests/` passes
- [ ] **Format** — `uv run ruff format --check src/ tests/` passes
- [ ] **Types** — `pyrefly check` passes
- [ ] **Integration test** — if public interfaces are changed

---

## Milestone 1: Foundation (Phase 0)

### TASK-001: Project Scaffold and Dependencies
🔴 P0 | ✅ DONE | Est: 2h

**Description:**
Set up `pyproject.toml` with src layout, hatchling build backend, core runtime and dev dependencies. Create package structure with `__init__.py` and `__main__.py`. Configure ruff and pytest.

**Checklist:**
- [x] Update `pyproject.toml` with all dependencies (pydantic, aiosqlite, nats-py, litellm, tiktoken, mcp, aiohttp, pyyaml)
- [x] Add dev dependencies (pytest, anyio[trio], pytest-asyncio, ruff)
- [x] Configure hatchling build, pytest, and ruff sections
- [x] Create `src/proctor/__init__.py` with `__version__`
- [x] Create `src/proctor/__main__.py` with async placeholder
- [x] Create `tests/__init__.py` and `tests/conftest.py` with anyio_backend fixture
- [x] Update `.gitignore` (add data/, *.db, *.age)
- [x] Run `uv sync` and verify
- [x] Run `uv run ruff check` — no errors

**Traces to:** [NFR-003]
**Depends on:** —
**Blocks:** [TASK-002], [TASK-003], [TASK-004], [TASK-005], [TASK-006], [TASK-100]

---

### TASK-100: Test Infrastructure Setup
🔴 P0 | ✅ DONE | Est: 1h

**Description:**
Verify test infrastructure works: pytest discovers tests, anyio backend is configured, async tests run.

**Checklist:**
- [x] pytest discovers and runs tests from `tests/`
- [x] `@pytest.mark.asyncio` tests work with anyio backend
- [x] conftest.py provides `anyio_backend` fixture
- [x] ruff lint and format configured and passing

**Traces to:** [NFR-000]
**Depends on:** [TASK-001]
**Blocks:** [TASK-002], [TASK-003], [TASK-004], [TASK-005], [TASK-006]

---

### TASK-002: Core Models (Event, Task, Envelope)
🔴 P0 | ✅ DONE | Est: 2h

**Description:**
Implement pydantic models for Event, Task, TaskStatus, and Envelope in `src/proctor/core/models.py`. All models auto-generate UUID ids and UTC timestamps.

**Checklist:**
- [x] Create `src/proctor/core/__init__.py`
- [x] Implement `TaskStatus` StrEnum with PENDING, ASSIGNED, RUNNING, COMPLETED, FAILED
- [x] Implement `Event` model with auto id, type, source, payload, timestamp
- [x] Implement `Task` model with status machine, spec dict, optional fields
- [x] Implement `Envelope` model with reply_to, correlation_id, ttl_seconds
- [x] Write tests: creation, serialization, unique IDs, status values

**Tests (Definition of Done):**
- [x] Unit tests: Event creation and uniqueness
- [x] Unit tests: Task defaults and status transitions
- [x] Unit tests: Envelope with all optional fields
- [x] Coverage >= 80%

**Traces to:** [REQ-001]
**Depends on:** [TASK-001], [TASK-100]
**Blocks:** [TASK-004], [TASK-005]

---

### TASK-003: Config Loading
🔴 P0 | ✅ DONE | Est: 2h

**Description:**
Implement YAML config loading with nested pydantic models (LLMConfig, NATSConfig, SchedulerConfig, ProctorConfig). Provide sensible defaults and graceful handling of missing config files.

**Checklist:**
- [x] Implement `LLMConfig`, `NATSConfig`, `SchedulerConfig` models
- [x] Implement `ProctorConfig` root model with nested configs
- [x] Implement `load_config(path)` that returns defaults if file missing
- [x] Create `config/proctor.yaml` example
- [x] Write tests: default config, YAML loading, missing file, nested config defaults

**Tests (Definition of Done):**
- [x] Unit tests: default config values
- [x] Unit tests: YAML file loading
- [x] Unit tests: missing file returns defaults
- [x] Unit tests: nested LLM config defaults
- [x] Coverage >= 80%

**Traces to:** [REQ-002]
**Depends on:** [TASK-001], [TASK-100]
**Blocks:** [TASK-006]

---

### TASK-004: EventBus (Internal Async Pub/Sub)
🔴 P0 | ✅ DONE | Est: 3h

**Description:**
Implement async EventBus with fnmatch wildcard pattern matching, multiple subscriber support, error isolation, and unsubscribe capability.

**Checklist:**
- [x] Implement `_Subscription` with pattern, handler, UUID id
- [x] Implement `EventBus.subscribe(pattern, handler)` returning sub_id
- [x] Implement `EventBus.unsubscribe(sub_id)`
- [x] Implement `EventBus.publish(event)` with `asyncio.create_task` per handler
- [x] Implement `_safe_call` for exception isolation
- [x] Write tests: exact match, wildcard, multiple subscribers, unsubscribe, error isolation

**Tests (Definition of Done):**
- [x] Unit tests: subscribe and publish exact topic
- [x] Unit tests: wildcard pattern matching (trigger.*)
- [x] Unit tests: multiple subscribers on same pattern
- [x] Unit tests: unsubscribe stops delivery
- [x] Unit tests: handler error doesn't crash bus
- [x] Coverage >= 80%

**Traces to:** [REQ-003]
**Depends on:** [TASK-002]
**Blocks:** [TASK-006], [TASK-011]

---

### TASK-005: StateManager (SQLite Wrapper)
🔴 P0 | 🔄 IN_PROGRESS | Est: 4h

**Description:**
Implement async SQLite wrapper with aiosqlite for task persistence, config overrides, and schedule storage. Schema: tasks (with status index), schedules, config_overrides tables.

**Checklist:**
- [ ] Implement SQLite schema with 3 tables and index
- [ ] Implement `initialize()` — open DB, create tables, enable WAL
- [ ] Implement `close()` — close connection
- [ ] Implement `save_task()` — upsert by id
- [ ] Implement `get_task()` — by id, returns None if not found
- [ ] Implement `list_tasks(status?)` — filtered query
- [ ] Implement `_row_to_task()` — SQLite row to pydantic Task
- [ ] Implement `set_config()` / `get_config()` — key-value config overrides
- [ ] Implement `list_tables()` — for testing
- [ ] Write tests: table creation, CRUD, status filter, config overrides

**Tests (Definition of Done):**
- [ ] Unit tests: tables created on initialize
- [ ] Unit tests: save and get task roundtrip
- [ ] Unit tests: update task status
- [ ] Unit tests: list tasks by status
- [ ] Unit tests: get nonexistent returns None
- [ ] Unit tests: config override set/get/update
- [ ] Unit tests: config get with default
- [ ] Coverage >= 80%

**Traces to:** [REQ-004]
**Depends on:** [TASK-002]
**Blocks:** [TASK-006]

---

### TASK-006: Bootstrap (Application Startup/Shutdown)
🔴 P0 | ⬜ TODO | Est: 3h

**Description:**
Implement Application class that owns all core components (EventBus, StateManager), manages lifecycle, and wires event handlers. Update `__main__.py` with signal handling.

**Checklist:**
- [ ] Implement `Application.__init__` — create bus, state
- [ ] Implement `Application.start()` — init state, subscribe handlers, set running
- [ ] Implement `Application.stop()` — close state, unset running
- [ ] Implement `Application.set_llm_call()` — inject LLM function
- [ ] Subscribe `_handle_terminal` to `trigger.terminal` on start
- [ ] Update `__main__.py` with signal handling (SIGINT, SIGTERM)
- [ ] Write tests: start/stop lifecycle, db created, event bus works

**Tests (Definition of Done):**
- [ ] Unit tests: app starts and stops cleanly
- [ ] Unit tests: state.db file created
- [ ] Unit tests: event bus functional after start
- [ ] Coverage >= 80%

**Traces to:** [REQ-005]
**Depends on:** [TASK-003], [TASK-004], [TASK-005]
**Blocks:** [TASK-012]

---

## Milestone 2: MVP (Phase 1)

### TASK-007: WorkflowSpec Model
🔴 P0 | ✅ DONE | Est: 2h

**Description:**
Implement the universal WorkflowSpec pydantic model with support for simple, DAG, FSM (placeholder), and orchestrator (placeholder) modes. Includes Step, StepType, StepRetry, WorkflowPolicies models.

**Checklist:**
- [x] Create `src/proctor/workflow/__init__.py`
- [x] Implement `WorkflowMode` and `StepType` StrEnums
- [x] Implement `Step` model with id, type, description, inputs, outputs, depends_on, retry
- [x] Implement `StepRetry` and `WorkflowPolicies` models
- [x] Implement `WorkflowSpec` with mode-specific fields and shared policies
- [x] Write tests: simple spec, DAG spec with steps, serialization roundtrip

**Tests (Definition of Done):**
- [x] Unit tests: simple spec creation
- [x] Unit tests: DAG spec with step dependencies
- [x] Unit tests: policies defaults
- [x] Unit tests: serialization roundtrip
- [x] Unit tests: step inputs/outputs
- [x] Coverage >= 80%

**Traces to:** [REQ-006]
**Depends on:** [TASK-001], [TASK-100]
**Blocks:** [TASK-008], [TASK-009]

---

### TASK-008: DAG Executor
🔴 P0 | 🔄 IN_PROGRESS | Est: 4h

**Description:**
Implement topological sort with cycle detection and parallel DAG execution using asyncio.TaskGroup. Steps wait for their dependencies via asyncio.Event signaling.

**Checklist:**
- [ ] Implement `topo_sort()` with DFS-based cycle detection
- [ ] Implement `StepResult` model
- [ ] Implement `DAGExecutor.__init__` with steps and step_runner
- [ ] Implement `DAGExecutor.execute()` with TaskGroup-based parallel execution
- [ ] Handle dependency failure propagation (skip dependents)
- [ ] Handle step execution errors
- [ ] Write tests: linear chain, parallel branches, cycle detection, failure propagation

**Tests (Definition of Done):**
- [ ] Unit tests: topo_sort linear chain
- [ ] Unit tests: topo_sort parallel steps
- [ ] Unit tests: cycle detection raises ValueError
- [ ] Unit tests: single step execution
- [ ] Unit tests: execute linear DAG with mock runner
- [ ] Unit tests: execute parallel DAG with mock runner
- [ ] Unit tests: step failure stops dependents
- [ ] Coverage >= 80%

**Traces to:** [REQ-007]
**Depends on:** [TASK-007]
**Blocks:** [TASK-009]

---

### TASK-009: Workflow Engine (Dispatcher)
🔴 P0 | ⬜ TODO | Est: 3h

**Description:**
Implement WorkflowEngine that dispatches WorkflowSpec to the correct executor based on mode. Supports simple (direct LLM call) and DAG (via DAGExecutor) modes.

**Checklist:**
- [ ] Implement `WorkflowResult` model
- [ ] Implement `WorkflowEngine.__init__` with LLM call function
- [ ] Implement `execute()` with match/case dispatch
- [ ] Implement `_execute_simple()` — LLM call with prompt
- [ ] Implement `_execute_dag()` — build step_runner, execute via DAGExecutor
- [ ] Return error for unsupported modes (FSM, orchestrator)
- [ ] Write tests: simple workflow, DAG workflow, unsupported mode

**Tests (Definition of Done):**
- [ ] Unit tests: simple workflow execution
- [ ] Unit tests: DAG workflow execution
- [ ] Unit tests: unsupported mode returns error
- [ ] Coverage >= 80%

**Traces to:** [REQ-008]
**Depends on:** [TASK-007], [TASK-008]
**Blocks:** [TASK-012]

---

### TASK-010: Agent Runtime (LLM Loop)
🔴 P0 | ✅ DONE | Est: 4h

**Description:**
Implement the Agent Runtime: an LLM agent loop that alternates between calling the LLM and executing tool calls until a text response is returned or max_turns is reached.

**Checklist:**
- [x] Create `src/proctor/workers/__init__.py`
- [x] Implement `ToolDef`, `ToolResult`, `AgentResult` models
- [x] Implement `AgentRuntime.__init__` with llm_fn, tools, max_turns
- [x] Implement `AgentRuntime.run()` — the main loop
- [x] Implement `_call_tool()` — execute tool with error handling
- [x] Handle unknown tool names gracefully
- [x] Handle max_turns limit
- [x] Write tests: no-tools completion, tool call + response, max turns, unknown tool

**Tests (Definition of Done):**
- [x] Unit tests: simple completion without tools
- [x] Unit tests: tool call and response
- [x] Unit tests: max turns limit
- [x] Unit tests: unknown tool returns error
- [x] Coverage >= 80%

**Traces to:** [REQ-009]
**Depends on:** [TASK-001], [TASK-100]
**Blocks:** [TASK-012]

---

### TASK-011: Terminal Trigger
🟠 P1 | 🔄 IN_PROGRESS | Est: 2h

**Description:**
Implement the terminal trigger that reads lines from stdin and publishes them as events. Includes the Trigger ABC and TerminalTrigger implementation.

**Checklist:**
- [ ] Create `src/proctor/triggers/__init__.py`
- [ ] Implement `Trigger` ABC with `start(bus)` and `stop()` methods
- [ ] Implement `TerminalTrigger` with stdin reading via asyncio.StreamReader
- [ ] Implement `_process_line()` — empty lines ignored, quit commands, event publishing
- [ ] Write tests: process line publishes event, empty lines ignored, quit command

**Tests (Definition of Done):**
- [ ] Unit tests: non-empty line publishes trigger.terminal event
- [ ] Unit tests: empty and whitespace lines ignored
- [ ] Unit tests: /quit returns "quit" signal
- [ ] Coverage >= 80%

**Traces to:** [REQ-010]
**Depends on:** [TASK-004]
**Blocks:** [TASK-012]

---

### TASK-012: Integration — Wire Everything Together
🔴 P0 | ⬜ TODO | Est: 3h

**Description:**
Wire terminal trigger events to workflow engine execution in the Application bootstrap. Add `_handle_terminal` that creates a simple WorkflowSpec from terminal text, executes it, and publishes the result event. Write end-to-end integration test.

**Checklist:**
- [ ] Update `Application.set_llm_call()` to create WorkflowEngine
- [ ] Implement `_handle_terminal()` — create WorkflowSpec, execute, publish result event
- [ ] Subscribe `_handle_terminal` to "trigger.terminal" in `start()`
- [ ] Write integration test: terminal event -> workflow -> task.completed event
- [ ] Run all tests: `uv run pytest tests/ -v`
- [ ] Run lint: `uv run ruff check src/ tests/`
- [ ] Run format: `uv run ruff format --check src/ tests/`

**Tests (Definition of Done):**
- [ ] Integration test: terminal command executes simple workflow and emits result
- [ ] All existing unit tests still pass
- [ ] Coverage >= 80%

**Traces to:** [REQ-011]
**Depends on:** [TASK-006], [TASK-009], [TASK-010], [TASK-011]
**Blocks:** —

---

## Dependency Graph

```
TASK-001 (Project Scaffold)
    │
    ├──► TASK-100 (Test Infrastructure)
    │        │
    │        ├──► TASK-002 (Core Models)
    │        │        │
    │        │        ├──► TASK-004 (EventBus)
    │        │        │        │
    │        │        │        └──► TASK-011 (Terminal Trigger) ──┐
    │        │        │                                          │
    │        │        └──► TASK-005 (StateManager) ──┐           │
    │        │                                       │           │
    │        ├──► TASK-003 (Config Loading) ──────────┤           │
    │        │                                       │           │
    │        │                                       ▼           │
    │        │                              TASK-006 (Bootstrap) ─┤
    │        │                                                    │
    │        ├──► TASK-007 (WorkflowSpec)                        │
    │        │        │                                          │
    │        │        ├──► TASK-008 (DAG Executor)               │
    │        │        │        │                                 │
    │        │        │        └──► TASK-009 (Workflow Engine) ───┤
    │        │                                                    │
    │        └──► TASK-010 (Agent Runtime) ───────────────────────┤
    │                                                             │
    │                                                             ▼
    └─────────────────────────────────────────────────► TASK-012 (Integration)
```

---

## Summary by Milestone

### Milestone 1: Foundation (Phase 0)

| Priority | Count | Est. Total |
|----------|-------|------------|
| P0 | 6 | ~14h |
| **Total** | **6** | **~14h** |

Tasks: TASK-001, TASK-100, TASK-002, TASK-003, TASK-004, TASK-005, TASK-006

### Milestone 2: MVP (Phase 1)

| Priority | Count | Est. Total |
|----------|-------|------------|
| P0 | 5 | ~16h |
| P1 | 1 | ~2h |
| **Total** | **6** | **~18h** |

Tasks: TASK-007, TASK-008, TASK-009, TASK-010, TASK-011, TASK-012

### Overall

| Priority | Count | Est. Total |
|----------|-------|------------|
| P0 | 11 | ~30h |
| P1 | 1 | ~2h |
| **Total** | **12** | **~32h** |

---

## Risk Register

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| aiosqlite WAL mode issues on macOS | Medium | Low | Test on macOS early; fallback to journal mode |
| asyncio.TaskGroup error handling complexity | Medium | Medium | Thorough tests for DAG failure propagation |
| LiteLLM API changes | Low | Low | Phase 1 uses mocks; real integration deferred |
| MCP SDK breaking changes | Medium | Low | Phase 1 only defines interface; real MCP in Phase 2 |
| pytest-asyncio + anyio configuration conflicts | Medium | Medium | Pin versions; test in CI early |

---

## Notes

- Phase 1 uses mock LLM implementations. Real LiteLLM wiring is deferred to Phase 2 integration.
- NATS connection is stubbed. The EventBus handles all intra-process communication.
- The project already has `pyproject.toml` and basic structure. TASK-001 updates (not creates from scratch).
- All async tests use `@pytest.mark.asyncio` with anyio backend, never raw asyncio.
- Recommended execution order follows the dependency graph: scaffold -> test infra -> models -> (config | bus | state) -> bootstrap -> (spec | runtime | terminal) -> (dag | engine) -> integration.
