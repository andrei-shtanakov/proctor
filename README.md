# Proctor

Distributed autonomous agent system with microkernel architecture. Version 0.1.0.

Proctor orchestrates LLM-powered agents that execute workflows (simple prompts, DAG pipelines), call tools, and communicate through an internal event bus. Designed for proactive operation — future phases add schedules, external triggers, and chained events.

## Status

Phase 0 (Foundation) and Phase 1 (MVP) complete. Phase 2 partially complete: SchedulerTrigger (cron/interval), TelegramTrigger (Bot API polling), and EpisodicMemory are implemented. The system accepts terminal input, Telegram messages, and scheduled events; executes simple and DAG workflows via LLM; and persists task state and episodic history in SQLite. NATS messaging, router, and webhook trigger are planned for remaining Phase 2 work.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

## Installation

```bash
git clone <repo-url> && cd proctor
uv sync
```

## Quick Start

```bash
# Run with pydantic defaults (no config file needed)
uv run python -m proctor

# Run with explicit config
uv run python -m proctor --config config/proctor.yaml
```

The system starts, initializes SQLite state, wires the event bus, starts the terminal trigger (stdin reader), and blocks on SIGINT/SIGTERM for graceful shutdown. Type prompts at stdin; use `/quit`, `/exit`, or `/q` to exit.

## Configuration

All fields have sensible pydantic defaults — a config file is entirely optional. Pass one via `--config` to override defaults. Example (`config/proctor.yaml`):

```yaml
node_role: standalone     # standalone | core | worker
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

telegram:
  bot_token: "your-bot-token"
  allowed_chat_ids: []          # empty = accept all chats
  poll_timeout: 30

schedules:
  - name: heartbeat
    interval_seconds: 3600
    payload: { type: ping }
  - name: daily-report
    cron: "0 9 * * *"
    payload: { action: report }
```

### Node Roles

Currently only `standalone` is functional. Other roles are defined for future distributed operation (Phase 3).

| Role | Description |
|------|-------------|
| `standalone` | All-in-one: core + worker in single process (current) |
| `core` | Coordinator only: task queue, scheduler, routing (planned) |
| `worker` | Executor only: picks tasks, runs agents (planned) |

### LLM Setup

Proctor uses [LiteLLM](https://docs.litellm.ai/) for provider abstraction (dependency installed, full wiring planned for Phase 2). Internally, LLM calls are abstracted behind a `Callable[[str], Awaitable[str]]` interface, allowing easy mocking in tests. Set the appropriate API key:

```bash
export ANTHROPIC_API_KEY=sk-...    # for Claude models
export OPENAI_API_KEY=sk-...       # for OpenAI models
# Ollama models work without API keys (local)
```

## Development

```bash
# Install all dependencies
uv sync

# Run tests
uv run pytest

# Run tests verbose / single file / single test
uv run pytest -v
uv run pytest tests/test_foo.py
uv run pytest tests/test_foo.py::test_bar

# Lint and format
uv run ruff check .
uv run ruff check . --fix
uv run ruff format .

# Type check
pyrefly check
```

## Project Structure

```
proctor/
├── config/
│   └── proctor.yaml              # Example configuration
├── docs/plans/                   # Architecture design and implementation plans
├── spec/
│   ├── requirements.md           # Functional and non-functional requirements
│   ├── tasks.md                  # Task breakdown with acceptance criteria
│   └── design.md                 # Design decisions and rationale
├── src/proctor/
│   ├── __init__.py               # Package init, __version__ = "0.1.0"
│   ├── __main__.py               # CLI entrypoint (argparse + signal handling)
│   ├── core/
│   │   ├── bootstrap.py          # Application lifecycle + event wiring
│   │   ├── bus.py                # Async EventBus with fnmatch wildcard subscriptions
│   │   ├── config.py             # YAML config loading with pydantic models
│   │   ├── memory.py             # EpisodicMemory: SQLite store for interaction history
│   │   ├── models.py             # Core models: Event, Task, Episode, Envelope, TaskStatus
│   │   └── state.py              # SQLite state manager (tasks, schedules, config_overrides)
│   ├── triggers/
│   │   ├── base.py               # Trigger ABC
│   │   ├── scheduler.py          # SchedulerTrigger: cron/interval event firing
│   │   ├── telegram.py           # TelegramTrigger: Bot API long-polling
│   │   └── terminal.py           # TerminalTrigger: stdin reader with /quit command
│   ├── workers/
│   │   └── runtime.py            # AgentRuntime: LLM loop with tool calling
│   └── workflow/
│       ├── dag.py                # DAG executor with topo-sort + parallel execution
│       ├── engine.py             # WorkflowEngine dispatcher (simple/DAG)
│       └── spec.py               # WorkflowSpec model (simple/dag/fsm/orchestrator)
└── tests/
    ├── conftest.py               # anyio backend fixture
    ├── test_core/                # Unit tests: models, config, bus, state, bootstrap
    ├── test_triggers/            # TerminalTrigger, TelegramTrigger, SchedulerTrigger tests
    ├── test_workers/             # AgentRuntime tests
    ├── test_workflow/            # WorkflowSpec, DAG, engine tests
    └── test_integration.py       # End-to-end: terminal -> workflow -> DB persistence
```

## Architecture

### Event-Driven Microkernel

All components communicate through an internal `EventBus` with async pub/sub and `fnmatch` wildcard pattern matching. The `Application` class wires everything together:

```
TerminalTrigger (stdin)  ─┐
TelegramTrigger (Bot API) ├─▶ Event(type="trigger.*")
SchedulerTrigger (cron)  ─┘
    │
    ▼
  EventBus
    │
    ▼ Application._handle_terminal()
Task(PENDING) → StateManager.save_task() → SQLite (state.db)
    │
    ▼ Task(RUNNING)
WorkflowEngine.execute(spec)
    ├── SIMPLE mode: single llm_call(prompt) → result
    └── DAG mode: DAGExecutor (topo-sort + parallel steps via llm_call)
    │
    ▼
Task(COMPLETED) → StateManager.save_task() → SQLite (state.db)
Episode → EpisodicMemory.save_episode() → SQLite (episodes.db)
    │
    ▼ Event(type="task.completed")
  EventBus → print result
```

### Core Models

| Model | Purpose |
|-------|---------|
| `Event` | Typed message with auto UUID, source, payload dict, UTC timestamp |
| `Task` | Status machine (PENDING→ASSIGNED→RUNNING→COMPLETED/FAILED), spec, result, retries, deadline |
| `Episode` | Agent interaction record: trigger type, user input, agent response, workflow result |
| `Envelope` | NATS message wrapper with correlation_id, reply_to, TTL (for Phase 2+) |
| `TaskStatus` | StrEnum: PENDING, ASSIGNED, RUNNING, COMPLETED, FAILED |

### Workflow Modes

| Mode | Status | Description |
|------|--------|-------------|
| `simple` | Implemented | Single prompt → LLM → result |
| `dag` | Implemented | Multi-step pipeline with dependencies and parallel execution |
| `fsm` | Defined, not implemented | Finite state machine for complex conversational flows |
| `orchestrator` | Defined, not implemented | Multi-agent coordination with planner/executor/reviewer |

### WorkflowSpec

Universal specification format for all workflow types:

```python
WorkflowSpec(
    workflow_id="research-task",
    mode=WorkflowMode.DAG,
    steps=[
        Step(id="gather", type=StepType.LLM, description="Gather info"),
        Step(id="analyze", type=StepType.LLM, description="Analyze",
             depends_on=["gather"]),
        Step(id="report", type=StepType.LLM, description="Write report",
             depends_on=["analyze"]),
    ],
    policies=WorkflowPolicies(max_runtime_seconds=300),
)
```

Step types defined: `LLM`, `SHELL`, `HTTP`, `SYSTEM`, `WAIT_EVENT` (only LLM is used currently).

### AgentRuntime

LLM agent loop with tool calling support. Used for multi-turn tool-calling scenarios:

```python
tool = ToolDef(
    name="search",
    description="Search the web",
    handler=search_handler,
)
runtime = AgentRuntime(llm_fn=my_llm, tools=[tool], max_turns=10)
result = await runtime.run("Find info about RISC-V")
# result.output, result.turns, result.tool_calls
```

Note: in the current simple/DAG workflow modes, the engine calls `llm_call` directly. AgentRuntime is available for future multi-turn agent workflows.

### SQLite State

Two databases with WAL mode for concurrent access:

**`data/state.db`** — operational state:

| Table | Purpose |
|-------|---------|
| `tasks` | Task state persistence with status, spec, result, timestamps |
| `schedules` | Cron/interval schedule definitions (schema ready) |
| `config_overrides` | Runtime configuration overrides (key-value) |

**`data/episodes.db`** — interaction history:

| Table | Purpose |
|-------|---------|
| `episodes` | Agent interaction records (trigger type, input, response, workflow result) |

Tasks are saved at every state transition. Episodes are saved after each workflow execution (success or failure).

## Tech Stack

| Category | Tools |
|----------|-------|
| Runtime | Python 3.12, pydantic 2.x, asyncio, anyio |
| Storage | aiosqlite |
| LLM | litellm, tiktoken |
| Messaging | nats-py (installed, Phase 2 integration) |
| Protocols | mcp SDK (installed, Phase 3+ integration) |
| HTTP | aiohttp |
| Scheduling | croniter |
| Config | pyyaml |
| Dev | pytest + anyio (NOT asyncio), ruff, pyrefly |

## Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| 0 | Foundation (models, config, bus, state, bootstrap) | Done |
| 1 | MVP (workflow engine, DAG, agent runtime, terminal trigger) | Done |
| 2 | Proactivity (scheduler, Telegram trigger, router, episodic memory) | Partial (scheduler, Telegram, episodic memory done; router, webhook pending) |
| 3 | Distribution (NATS transport, worker pool, task queue, MCP tools) | Planned |
| 4 | Advanced orchestration (FSM, multi-agent, self-modification) | Planned |
| 5 | Observability & control (OpenTelemetry, dashboards, audit log, TUI) | Planned |
| 6 | Security & hardening (RBAC, encryption, guardrails, A2A gateway) | Planned |
