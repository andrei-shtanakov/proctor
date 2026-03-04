# Design Specification

> Architecture, APIs, data schemas, and key decisions for Proctor (Phase 1)

## 1. Architecture Overview

### 1.1 Principles

| Principle | Description |
|-----------|-------------|
| Microkernel | Core manages state and coordination; workers execute heavy work in isolation |
| Async-first | All I/O is asyncio-based; no blocking calls in the main loop |
| Event-driven | Components communicate via typed events on an internal pub/sub bus |
| Pydantic everywhere | All models, configs, and messages are validated pydantic BaseModels |
| Testability | Every component accepts injected dependencies; no global state |
| Single operator | No multi-tenant complexity; one user, one system |

### 1.2 High-Level Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    PROCTOR (Phase 1 MVP)                     │
│                                                              │
│  ┌─────────────┐   ┌────────────┐   ┌───────────────────┐  │
│  │  Terminal    │──►│  EventBus  │──►│  Application      │  │
│  │  Trigger     │   │  (pubsub)  │   │  (bootstrap)      │  │
│  └─────────────┘   └────┬───────┘   └───────┬───────────┘  │
│                          │                   │               │
│                          ▼                   ▼               │
│              ┌───────────────────┐  ┌────────────────┐      │
│              │  Workflow Engine   │  │  StateManager  │      │
│              │  (simple + DAG)   │  │  (SQLite)      │      │
│              └────────┬──────────┘  └────────────────┘      │
│                       │                                      │
│                       ▼                                      │
│              ┌──────────────────┐                            │
│              │  DAG Executor    │                            │
│              │  (topo-sort +    │                            │
│              │   parallel exec) │                            │
│              └────────┬─────────┘                            │
│                       │                                      │
│                       ▼                                      │
│              ┌──────────────────┐                            │
│              │  Agent Runtime   │                            │
│              │  (LLM loop +    │                            │
│              │   tool calling)  │                            │
│              └──────────────────┘                            │
└─────────────────────────────────────────────────────────────┘
```

**Data Flow:** Terminal Input -> Event(trigger.terminal) -> EventBus -> Application._handle_terminal -> WorkflowSpec(SIMPLE) -> WorkflowEngine -> LLM call -> Event(task.completed)

**Traces to:** [REQ-005], [REQ-011]

---

## 2. Components

### DESIGN-001: Core Models

#### Description
Typed pydantic models for all inter-component communication. Three primary models: Event (system messages), Task (work units), Envelope (NATS message wrapper for future distributed messaging).

#### Interface

```python
class TaskStatus(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class Event(BaseModel):
    id: str  # auto-generated UUID
    type: str  # e.g., "trigger.telegram", "task.completed"
    source: str
    payload: dict[str, Any]
    timestamp: datetime  # auto-generated UTC

class Task(BaseModel):
    id: str  # auto-generated UUID
    status: TaskStatus  # defaults to PENDING
    spec: dict[str, Any]
    trigger_event: str | None
    worker_id: str | None
    result: Any | None
    retries: int  # defaults to 0
    created_at: datetime
    updated_at: datetime
    deadline: datetime | None

class Envelope(BaseModel):
    id: str  # auto-generated UUID
    type: str
    source: str
    target: str | None
    payload: dict[str, Any]
    reply_to: str | None
    correlation_id: str | None
    timestamp: datetime
    ttl_seconds: int | None
```

**Traces to:** [REQ-001]

---

### DESIGN-002: Configuration System

#### Description
YAML-based configuration with nested pydantic models. Falls back to sensible defaults when config file is missing.

#### Interface

```python
class LLMConfig(BaseModel):
    default_model: str = "claude-sonnet-4-20250514"
    fallback_model: str = "ollama/llama3.2"
    max_tokens: int = 4096
    temperature: float = 0.7

class NATSConfig(BaseModel):
    url: str = "nats://localhost:4222"
    connect_timeout: float = 5.0
    reconnect_time_wait: float = 2.0
    max_reconnect_attempts: int = 60

class SchedulerConfig(BaseModel):
    poll_interval_seconds: int = 30
    enabled: bool = True

class ProctorConfig(BaseModel):
    node_role: str = "standalone"
    node_id: str = "node-1"
    nats_url: str = "nats://localhost:4222"
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    llm: LLMConfig
    nats: NATSConfig
    scheduler: SchedulerConfig

def load_config(path: Path) -> ProctorConfig: ...
```

#### Configuration

```yaml
# config/proctor.yaml
node_role: standalone
node_id: node-local
nats_url: nats://localhost:4222
data_dir: data
log_level: INFO

llm:
  default_model: claude-sonnet-4-20250514
  fallback_model: ollama/llama3.2
  max_tokens: 4096

scheduler:
  poll_interval_seconds: 30
  enabled: true
```

**Traces to:** [REQ-002]

---

### DESIGN-003: EventBus

#### Description
Internal async pub/sub for intra-process module communication. Uses `fnmatch` for wildcard pattern matching. Not NATS -- this is for local communication only.

#### Interface

```python
Handler = Callable[[Event], Awaitable[None]]

class EventBus:
    def subscribe(self, pattern: str, handler: Handler) -> str:
        """Subscribe to events matching pattern. Returns subscription ID."""

    def unsubscribe(self, sub_id: str) -> None:
        """Remove a subscription by ID."""

    async def publish(self, event: Event) -> None:
        """Publish event to all matching subscribers."""
```

#### Key Behavior
- Pattern matching via `fnmatch.fnmatch`: `"trigger.*"` matches `"trigger.telegram"`
- Handlers execute as async tasks (`asyncio.create_task`)
- Handler exceptions are logged but don't crash other handlers or the bus
- Each subscription gets a UUID for unsubscribe

**Traces to:** [REQ-003]

---

### DESIGN-004: StateManager

#### Description
Async SQLite wrapper for persistent operational state. Uses `aiosqlite` for non-blocking database access. Manages tasks, schedules, and config overrides.

#### Interface

```python
class StateManager:
    def __init__(self, db_path: Path) -> None: ...

    async def initialize(self) -> None:
        """Open DB and create tables."""

    async def close(self) -> None:
        """Close DB connection."""

    async def save_task(self, task: Task) -> None:
        """Insert or update a task (upsert by id)."""

    async def get_task(self, task_id: str) -> Task | None:
        """Get task by ID. Returns None if not found."""

    async def list_tasks(
        self, status: TaskStatus | None = None
    ) -> list[Task]:
        """List tasks, optionally filtered by status."""

    async def set_config(self, key: str, value: Any) -> None:
        """Set a config override (upsert)."""

    async def get_config(self, key: str, default: Any = None) -> Any:
        """Get a config override, or default."""

    async def list_tables(self) -> list[str]:
        """List all tables (for testing)."""
```

**Traces to:** [REQ-004]

---

### DESIGN-005: Application Bootstrap

#### Description
Main application container. Owns all core components, manages their lifecycle, and wires event handlers. Entry point for the `python -m proctor` command.

#### Interface

```python
LLMCall = Callable[[str], Awaitable[str]]

class Application:
    config: ProctorConfig
    bus: EventBus
    state: StateManager
    is_running: bool

    def __init__(self, config: ProctorConfig) -> None: ...
    def set_llm_call(self, llm_call: LLMCall) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

#### Wiring
- On start: creates data_dir, initializes StateManager, subscribes `_handle_terminal` to `"trigger.terminal"`
- `_handle_terminal`: creates WorkflowSpec(SIMPLE) from terminal text, executes via WorkflowEngine, publishes `task.completed` or `task.failed` event
- On stop: closes StateManager, sets `is_running=False`

**Traces to:** [REQ-005], [REQ-011]

---

### DESIGN-006: WorkflowSpec Model

#### Description
Universal workflow specification. A single pydantic model that describes all pipeline types: simple (single prompt), DAG (directed acyclic graph of steps), FSM (finite state machine, Phase 4), and orchestrator (multi-agent, Phase 4).

#### Interface

```python
class WorkflowMode(StrEnum):
    SIMPLE = "simple"
    DAG = "dag"
    FSM = "fsm"
    ORCHESTRATOR = "orchestrator"

class StepType(StrEnum):
    LLM = "llm"
    SHELL = "shell"
    HTTP = "http"
    SYSTEM = "system"
    WAIT_EVENT = "wait_event"

class Step(BaseModel):
    id: str
    type: StepType = StepType.LLM
    description: str = ""
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    depends_on: list[str]
    tool: str | None = None
    retry: StepRetry
    conditions: dict[str, Any]

class WorkflowPolicies(BaseModel):
    max_runtime_seconds: int = 900
    max_retries: int = 2
    retry_delay_seconds: int = 60
    require_security_review: bool = False

class WorkflowSpec(BaseModel):
    workflow_id: str
    version: str = "1.0.0"
    mode: WorkflowMode
    description: str
    prompt: str | None  # Simple mode
    steps: list[Step]  # DAG mode
    states: dict  # FSM mode (Phase 4)
    orchestrator: dict  # Orchestrator mode (Phase 4)
    policies: WorkflowPolicies
    security: dict
    channels: dict
```

**Traces to:** [REQ-006]

---

### DESIGN-007: DAG Executor

#### Description
Executes a list of Steps respecting dependency order. Uses topological sort for ordering and asyncio.TaskGroup for parallel execution of independent steps.

#### Interface

```python
class StepResult(BaseModel):
    step_id: str
    output: Any | None = None
    error: str | None = None

StepRunner = Callable[
    [Step, dict[str, StepResult]], Awaitable[StepResult]
]

def topo_sort(steps: list[Step]) -> list[Step]:
    """Topological sort with cycle detection."""

class DAGExecutor:
    def __init__(
        self,
        steps: list[Step],
        step_runner: StepRunner,
    ) -> None: ...

    async def execute(self) -> dict[str, StepResult]:
        """Run all steps respecting dependencies."""
```

#### Algorithm
1. Validate DAG (cycle detection via DFS)
2. Create `asyncio.Event` per step for dependency signaling
3. Launch all steps concurrently in a `TaskGroup`
4. Each step waits for its dependency events before executing
5. If a dependency failed, the step is skipped with an error
6. Returns dict of step_id -> StepResult

**Traces to:** [REQ-007]

---

### DESIGN-008: Workflow Engine

#### Description
Dispatcher that routes WorkflowSpecs to the appropriate executor based on `mode`. In Phase 1, supports `simple` and `dag` modes.

#### Interface

```python
LLMCall = Callable[[str], Awaitable[str]]

class WorkflowResult(BaseModel):
    workflow_id: str
    output: Any | None = None
    step_results: dict[str, StepResult] | None = None
    error: str | None = None

class WorkflowEngine:
    def __init__(self, llm_call: LLMCall) -> None: ...
    async def execute(self, spec: WorkflowSpec) -> WorkflowResult: ...
```

#### Dispatch Logic
- `SIMPLE`: call LLM with prompt, return text result
- `DAG`: create step_runner that calls LLM per step (injecting dependency outputs), execute via DAGExecutor, return last step's output
- `FSM` / `ORCHESTRATOR`: return error "not supported yet"

**Traces to:** [REQ-008]

---

### DESIGN-009: Agent Runtime

#### Description
LLM agent loop: receives instruction, calls LLM, executes tool calls, feeds results back, repeats until text response or max turns.

#### Interface

```python
class ToolDef(BaseModel):
    name: str
    description: str
    handler: Any  # async callable

class ToolResult(BaseModel):
    tool_name: str
    output: str | None = None
    error: str | None = None

class AgentResult(BaseModel):
    output: str
    turns: int
    tool_calls: list[ToolResult]

LLMFn = Callable[
    [list[dict], list[dict] | None], Awaitable[dict[str, Any]]
]

class AgentRuntime:
    def __init__(
        self,
        llm_fn: LLMFn,
        tools: list[ToolDef],
        max_turns: int = 10,
    ) -> None: ...

    async def run(self, instruction: str) -> AgentResult: ...
```

#### LLM Response Protocol
```json
{"type": "text", "content": "Final answer"}
{"type": "tool_call", "tool_name": "get_weather", "tool_args": {"city": "Tbilisi"}}
```

#### Loop Logic
1. Build initial message list with user instruction
2. Call LLM with messages and tool definitions
3. If response is `text`: return AgentResult
4. If response is `tool_call`: execute tool, add call+result to messages, loop
5. If max_turns reached: return last known state

**Traces to:** [REQ-009]

---

### DESIGN-010: Terminal Trigger

#### Description
Reads lines from stdin and publishes them as events on the EventBus. Implements the Trigger ABC.

#### Interface

```python
class Trigger(ABC):
    @abstractmethod
    async def start(self, bus: EventBus) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...

class TerminalTrigger(Trigger):
    async def start(self, bus: EventBus) -> None: ...
    async def stop(self) -> None: ...
    async def _process_line(
        self, line: str, bus: EventBus
    ) -> str | None: ...
```

#### Behavior
- Non-empty lines -> `Event(type="trigger.terminal", source="terminal", payload={"text": line})`
- Empty/whitespace lines -> ignored
- `/quit`, `/exit`, `/q` -> returns "quit" signal
- Reads stdin via `asyncio.StreamReader` for non-blocking I/O

**Traces to:** [REQ-010]

---

## 3. Data Schemas

### 3.1 SQLite Schema (state.db)

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    spec_json TEXT NOT NULL,
    trigger_event TEXT,
    worker_id TEXT,
    result_json TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deadline TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    expression TEXT NOT NULL,
    tz TEXT DEFAULT 'UTC',
    workflow_id TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run TEXT,
    last_run TEXT
);

CREATE TABLE IF NOT EXISTS config_overrides (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
```

### 3.2 Event Payload Schemas

```json
// trigger.terminal
{"text": "search for RISC-V info"}

// task.completed
{"workflow_id": "terminal-<uuid>", "output": "LLM result text", "error": null}

// task.failed
{"workflow_id": "terminal-<uuid>", "output": null, "error": "error message"}
```

---

## 4. Integrations

### 4.1 LLM Provider (Phase 1: Mock / Injected)

| Aspect | Value |
|--------|-------|
| Protocol | Async callable interface |
| Interface | `LLMCall = Callable[[str], Awaitable[str]]` |
| Phase 1 | Mock implementations for testing |
| Future | LiteLLM for multi-provider (Claude, Ollama, etc.) |

**Traces to:** [REQ-008], [REQ-009]

---

## 5. Key Decisions (ADR)

### ADR-001: Microkernel with No Framework
**Status:** Accepted
**Date:** 2026-03-04

**Context:**
We need an agent orchestration system. Options include LangChain, AutoGen, Temporal, or custom.

**Decision:**
Build on raw pydantic + asyncio with no agent framework.

**Rationale:**
- LangChain/LangGraph impose too many abstractions and vendor lock-in
- AutoGen is conversation-only, doesn't fit DAG/FSM patterns
- Temporal is right in concept but Java/Go server is too heavy
- pydantic + asyncio gives full control with minimal dependencies

**Consequences:**
- (+) Full control over orchestration patterns
- (+) Minimal dependencies (~12 runtime packages)
- (+) Easy to understand and modify
- (-) More initial boilerplate
- (-) No community plugins/extensions

**Traces to:** [REQ-005]

---

### ADR-002: NATS for Messaging (Stubbed in Phase 1)
**Status:** Accepted
**Date:** 2026-03-04

**Context:**
Need inter-node messaging for distributed workers. Options: Redis, RabbitMQ, Kafka, NATS.

**Decision:**
Use NATS (+ JetStream). Stub connection in Phase 1; real implementation in Phase 2+.

**Rationale:**
- Single binary (~15MB), zero-config
- Covers pub/sub + queue groups + request/reply in one system
- JetStream adds at-least-once delivery for task queues
- Lighter than Kafka/RabbitMQ for single-operator use

**Consequences:**
- (+) No extra infrastructure in Phase 1 (stubbed)
- (+) Clean upgrade path to distributed in Phase 2
- (-) NATS is less common than Redis/Kafka in tutorials
- (-) Stub means some distributed behavior untested until Phase 2

**Traces to:** [REQ-005]

---

### ADR-003: SQLite for State (Three Databases)
**Status:** Accepted
**Date:** 2026-03-04

**Context:**
Need persistent storage for tasks, episodes, and knowledge. Options: PostgreSQL, SQLite, Redis.

**Decision:**
Use SQLite with three separate databases: state.db (operational), episodes.db (history), knowledge.db (semantic + FTS5).

**Rationale:**
- No external server needed (single operator)
- aiosqlite provides async access
- Separate DBs prevent cross-concern queries and simplify backups
- FTS5 provides full-text search without additional infrastructure

**Consequences:**
- (+) Zero infrastructure for storage
- (+) Portable (just files)
- (+) FTS5 for semantic memory search
- (-) Single-writer limitation (fine for single operator)
- (-) No concurrent access from multiple nodes (state.db on Core only)

**Traces to:** [REQ-004]

---

## 6. Data Flow

### 6.1 Terminal Command Execution

```
Operator types command
    │
    ▼
┌──────────────┐     ┌────────────┐     ┌──────────────────┐
│ Terminal      │────►│  EventBus  │────►│  Application     │
│ Trigger      │     │            │     │  _handle_terminal │
└──────────────┘     └────────────┘     └────────┬─────────┘
                                                  │
                                                  ▼
                                        ┌──────────────────┐
                                        │  WorkflowEngine  │
                                        │  _execute_simple  │
                                        └────────┬─────────┘
                                                  │
                                                  ▼
                                        ┌──────────────────┐
                                        │  LLM Call        │
                                        │  (mock/litellm)  │
                                        └────────┬─────────┘
                                                  │
                                                  ▼
                                        Event(task.completed)
                                                  │
                                                  ▼
                                        Display to operator
```

### 6.2 DAG Workflow Execution

```
WorkflowSpec(mode=DAG, steps=[A, B, C])
    │
    ▼
┌──────────────────┐
│  WorkflowEngine  │
│  _execute_dag    │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  DAGExecutor     │
│  topo_sort()     │
└────────┬─────────┘
         │
         ├──► Step A (no deps) ──► LLM ──► StepResult
         │         │
         │         ▼
         ├──► Step B (dep: A) ──► LLM ──► StepResult
         ├──► Step C (dep: A) ──► LLM ──► StepResult  (parallel with B)
         │              │
         │              ▼
         └──► Step D (dep: B,C) ──► LLM ──► StepResult
                                              │
                                              ▼
                                    WorkflowResult(output=D.output)
```

---

## 7. Security Model

### 7.1 Authentication
Phase 1: Single operator, no authentication required. The operator runs Proctor locally.

### 7.2 Authorization
| Role | Permissions |
|------|-------------|
| Operator | Full access to all commands and configuration |

### 7.3 Data Protection
- SQLite databases stored in configurable `data_dir`
- No secrets stored in Phase 1 (vault in Phase 3)
- Config files should not contain credentials (use env vars for API keys)

---

## 8. API Reference

### 8.1 CLI Commands

```bash
# Start Proctor (standalone mode)
uv run python -m proctor --config config/proctor.yaml

# Start with default config
uv run python -m proctor
```

### 8.2 Configuration File

```yaml
# config/proctor.yaml
node_role: standalone    # standalone | core | worker
node_id: node-local
nats_url: nats://localhost:4222
data_dir: data
log_level: INFO

llm:
  default_model: claude-sonnet-4-20250514
  fallback_model: ollama/llama3.2
  max_tokens: 4096
  temperature: 0.7

nats:
  url: nats://localhost:4222
  connect_timeout: 5.0
  reconnect_time_wait: 2.0
  max_reconnect_attempts: 60

scheduler:
  poll_interval_seconds: 30
  enabled: true
```

---

## 9. Directory Structure

```
proctor/
├── src/
│   └── proctor/
│       ├── __init__.py          # Package init, __version__
│       ├── __main__.py          # CLI entrypoint
│       ├── core/
│       │   ├── __init__.py
│       │   ├── models.py        # Event, Task, Envelope, TaskStatus
│       │   ├── config.py        # ProctorConfig, load_config
│       │   ├── bus.py           # EventBus
│       │   ├── state.py         # StateManager (SQLite)
│       │   └── bootstrap.py     # Application lifecycle
│       ├── workflow/
│       │   ├── __init__.py
│       │   ├── spec.py          # WorkflowSpec, Step, WorkflowMode
│       │   ├── dag.py           # topo_sort, DAGExecutor
│       │   └── engine.py        # WorkflowEngine dispatcher
│       ├── workers/
│       │   ├── __init__.py
│       │   └── runtime.py       # AgentRuntime, ToolDef
│       └── triggers/
│           ├── __init__.py
│           ├── base.py          # Trigger ABC
│           └── terminal.py      # TerminalTrigger
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # anyio_backend fixture
│   ├── test_core/
│   │   ├── __init__.py
│   │   ├── test_models.py
│   │   ├── test_config.py
│   │   ├── test_bus.py
│   │   ├── test_state.py
│   │   └── test_bootstrap.py
│   ├── test_workflow/
│   │   ├── __init__.py
│   │   ├── test_spec.py
│   │   ├── test_dag.py
│   │   └── test_engine.py
│   ├── test_workers/
│   │   ├── __init__.py
│   │   └── test_runtime.py
│   ├── test_triggers/
│   │   ├── __init__.py
│   │   └── test_terminal.py
│   └── test_integration.py
├── config/
│   └── proctor.yaml
├── spec/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── docs/
│   └── plans/
│       ├── 2026-03-04-proctor-architecture-design.md
│       └── 2026-03-04-proctor-phase0-phase1-plan.md
├── pyproject.toml
├── CLAUDE.md
├── README.md
└── .gitignore
```
