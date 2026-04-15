# Proctor

Distributed autonomous agent system with microkernel architecture. Version 0.1.0.

Proctor orchestrates LLM-powered agents that execute workflows (simple prompts, DAG pipelines), call tools, and communicate through an internal event bus. Designed for proactive operation — future phases add schedules, external triggers, and chained events.

## Status

Phase 0 (Foundation) and Phase 1 (MVP) complete. Phase 2 in progress:
SchedulerTrigger (cron/interval), TelegramTrigger (Bot API polling),
EpisodicMemory, LiteLLM integration, and the declarative Router are all
implemented. The system accepts terminal input, Telegram messages, and
scheduled events; routes them to catalog workflows via the Router; and
persists task state and episodic history in SQLite. NATS messaging and
the webhook trigger are the remaining Phase 2 items.

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

Proctor uses [LiteLLM](https://docs.litellm.ai/) for provider abstraction. The call path `llm_call(prompt) -> str` (see `src/proctor/workers/llm.py`) delegates to `litellm.acompletion`, retries the primary model on transient errors (`max_retries` attempts, flat 1 s backoff), then falls back to `fallback_model` if it is set. Every attempt is persisted to the `llm_calls` table in `episodes.db` with token counts, latency, model, and any error. Set the appropriate API key:

```bash
export ANTHROPIC_API_KEY=sk-...    # for Claude models
export OPENAI_API_KEY=sk-...       # for OpenAI models
# Ollama models work without API keys (local)
```

## Routing

Trigger events (`trigger.terminal`, `trigger.telegram`, `trigger.scheduler`,
future `trigger.webhook`) flow through a declarative Router that matches
each event against a list of rules and dispatches it to a named workflow.

Example (`config/proctor.yaml` excerpt):

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
    prompt_from_payload: prompt
```

Rule semantics:

- **First match wins** — rules are iterated in YAML order. Put specific
  patterns before broader ones; the loader rejects configurations where
  a broader rule precedes a narrower one.
- **Prompt binding** — each rule specifies exactly one of `prompt`
  (literal) or `prompt_from_payload` (dotted path into `event.payload`).
- **Unmatched events** publish a `routing.unmatched` event on the bus
  and log a WARNING; no task is created. Binding failures publish
  `routing.binding_failed`. Both are intended for dashboards and alerts.

### Upgrading to LABS-65

The Router replaces the previous hard-coded handler for terminal input.
**Breaking:** a running Proctor without `workflows:`/`routes:` will
accept stdin lines but route them to nobody (`routing.unmatched` WARNING
in the log). To restore the pre-LABS-65 terminal behavior, add:

```yaml
workflows:
  chat:
    workflow_id: chat
    mode: simple

routes:
  - event_pattern: "trigger.terminal"
    workflow_id: chat
    prompt_from_payload: text
```

With Telegram / Scheduler triggers already enabled in your config,
either add matching route rules now or accept the `routing.unmatched`
signal as a diagnostic (previously those triggers published to nobody
silently — this is an observability improvement).

## Webhook trigger

HTTP endpoint that receives POSTs from external systems (GitHub,
Stripe, CI pipelines, internal services), authenticates them, and
publishes `trigger.webhook.<source_name>` events on the bus. Router
then dispatches those events to catalog workflows via the existing YAML rules.

Example config:

```yaml
webhook:
  host: 127.0.0.1
  port: 8080
  paths:
    /webhook/github:
      source_name: github
      auth:
        type: hmac
        secret_env: GITHUB_WEBHOOK_SECRET
        header: X-Hub-Signature-256
        prefix: "sha256="
    /webhook/ci:
      auth:
        type: bearer
        secret_env: PROCTOR_CI_TOKEN
```

The path `/webhook/github` → event `trigger.webhook.github`. Routing:

```yaml
routes:
  - event_pattern: "trigger.webhook.github"
    workflow_id: ci-reviewer
    prompt_from_payload: body.head_commit.message
```

### Response semantics

Webhook handlers return **202 Accepted** immediately after publishing
the event. The handler does **not** wait for the downstream workflow —
HTTP response times are milliseconds, not seconds, matching how
GitHub, Stripe, Slack, and the rest of the industry expect webhook
receivers to behave. Response body:

```json
{ "accepted": true, "correlation_id": "<event uuid>" }
```

### At-least-once delivery

Webhook events are delivered **at least once**. Client retries, proxy
retries, or server crashes after publish can produce duplicates.
Workflow authors should treat duplicates as normal and use dedup keys:

- GitHub: `payload.headers["X-GitHub-Delivery"]` (always present).
- Internal clients: send `X-Request-Id`.
- `correlation_id` returned in 202 response.

### Authentication

Three `auth.type` variants:

- **`hmac`** — HMAC-SHA256. Header + prefix configurable. Covers
  GitHub-style signing. Slack and Stripe use different base-string
  constructions and are out of scope.
- **`bearer`** — `Authorization: Bearer <token>` (RFC 6750,
  case-insensitive scheme).
- **`none`** — no auth. Explicit opt-in. Triggers startup WARNING
  per such path. **Do not use in production.**

Secrets live in env vars via `secret_env`. `WebhookTrigger.start()`
fails fast with `RuntimeError` listing all missing vars.

All auth failures return an identical `401 {"error": "unauthorized"}`.

### Capacity and shutdown

- `max_in_flight` (default **20**) — concurrent handler cap. 21st
  request returns `503 + Retry-After: 1`.
- `max_body_bytes` (default **1048576** — 1 MB) — aiohttp enforces;
  excess returns `413`.
- `shutdown_timeout` (default **30s**) — max drain time on `stop()`.

## Deployment topologies

The default `host: 127.0.0.1` means Proctor's webhook endpoint is
reachable only from localhost. **A reverse-proxy in front of Proctor
is required for public exposure** — Proctor does not do TLS
termination, per-IP rate limiting, or IP-level abuse detection. The
in-flight cap is a memory-footprint guardrail, not a DDoS defense.

### Sidecar nginx

```nginx
server {
    listen 443 ssl;
    server_name proctor.example.com;
    ssl_certificate /etc/letsencrypt/live/proctor.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/proctor.example.com/privkey.pem;

    limit_req_zone $binary_remote_addr zone=webhook:10m rate=10r/s;

    location /webhook/ {
        limit_req zone=webhook burst=20 nodelay;
        proxy_pass http://127.0.0.1:8080;
        proxy_read_timeout 10s;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

### Kubernetes (Traefik ingress)

```yaml
# proctor.yaml excerpt
webhook:
  host: 0.0.0.0        # reachable from ingress
  port: 8080
```

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: proctor-webhook }
spec:
  podSelector: { matchLabels: { app: proctor } }
  ingress:
  - from:
    - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: ingress-nginx } }
    ports:
    - port: 8080
```

Set `terminationGracePeriodSeconds` on the Proctor Deployment to at
least `shutdown_timeout + 15s` (default 45s) so the pod has time to
drain HTTP handlers before SIGKILL.

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

### Running integration tests

Integration tests against external services (currently: Ollama) are marked
with the `integration` pytest marker and skipped by default.

To run them, start a local Ollama server and pull the default model:

```bash
ollama serve
ollama pull llama3.2
```

Then:

```bash
uv run pytest -m integration
```

If Ollama is not reachable at `localhost:11434`, the tests skip cleanly — no
action required for the default `uv run pytest` run.

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
│   │   ├── terminal.py           # TerminalTrigger: stdin reader with /quit command
│   │   └── webhook.py            # WebhookTrigger: HTTP POST handler with auth
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
  EventBus (subscribes "trigger.*")
    │
    ▼ Application._handle_trigger_event(event)
    │   │
    │   ▼ Router.route(event) → WorkflowSpec | None
    │         │                     │
    │         │ (None — publishes routing.unmatched or
    │         │  routing.binding_failed and returns)
    │         ▼
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
| 2 | Proactivity (scheduler, Telegram trigger, router, episodic memory, webhook) | Done (scheduler, Telegram, episodic memory, router, LiteLLM, webhook). NATS transport deferred to Phase 3. |
| 3 | Distribution (NATS transport, worker pool, task queue, MCP tools) | Planned |
| 4 | Advanced orchestration (FSM, multi-agent, self-modification) | Planned |
| 5 | Observability & control (OpenTelemetry, dashboards, audit log, TUI) | Planned |
| 6 | Security & hardening (RBAC, encryption, guardrails, A2A gateway) | Planned |
