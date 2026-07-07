# Proctor

Distributed autonomous agent system with microkernel architecture. Version 0.1.0.

Proctor orchestrates LLM-powered agents that execute workflows (simple prompts, DAG pipelines), call tools, and communicate through an internal event bus. Designed for proactive operation — future phases add schedules, external triggers, and chained events.

## Status

Phases 0–2 complete; Phase 3 (Distribution) is in progress — parts 1–3 done.

- **Phase 2 (Proactivity):** SchedulerTrigger (cron/interval), TelegramTrigger,
  WebhookTrigger (HTTP + HMAC/Bearer auth), EpisodicMemory, LiteLLM, the
  declarative Router, the NATS transport layer, and the TaskRouter admission
  layer (4 safety invariants + TTL pending queue).
- **Phase 3 part 1:** worker registry + remote dispatch — WorkerRegistry
  (heartbeat liveness, first-alive-owns fencing), capability scoring, WorkerNode
  (worker-role runtime), remote dispatch with rollback / loss-policy / deadline
  reaper.
- **Phase 3 part 2:** docker workers — `ContainerRuntime` (docker/podman CLI)
  and `DockerWorkerManager` (container fleet lifecycle: poll-loop restart,
  backoff/jitter/stability-reset/ceiling, fresh-id fencing).
- **Phase 3 part 3:** remote docker workers via `DOCKER_HOST=ssh://` (see
  `docs/remote-workers.md`).

The system accepts terminal input, Telegram messages, webhooks, and scheduled
events; routes them to catalog workflows; admits them through the safety
invariants; and executes locally or dispatches to worker nodes over local or
NATS transport, including container fleets the core launches itself. **Next:**
the `mcp/` dynamic-tool layer; a bare-host SSH worker is deferred.

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
transport: auto           # auto | local | nats
data_dir: data
log_level: INFO

llm:
  default_model: claude-sonnet-4-20250514
  fallback_model: ollama/llama3.2
  max_tokens: 4096
  temperature: 0.7

nats:
  servers:
    - nats://localhost:4222
  subject_prefix: proctor
  connect_timeout: 5.0
  reconnect_time_wait: 2.0
  reconnect_jitter: 0.5
  max_reconnect_attempts: -1

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

| Role | Description |
|------|-------------|
| `standalone` | All-in-one: core + worker in single process (default) |
| `core` | Coordinator: task queue, scheduler, routing — delivered over NATS |
| `worker` | Executor: picks tasks, runs agents — delivered over NATS |

### Transport mode

`transport` selects the EventTransport backend:

| Value | Behaviour |
|-------|-----------|
| `auto` (default) | `standalone` → `local`; `core`/`worker` → `nats` |
| `local` | In-process EventBus — zero external dependencies |
| `nats` | NATS-backed, multi-node safe — requires `pip install proctor[nats]` |

`local` and `nats` expose identical observable behaviour (wildcard matching,
dedup, drain semantics). See
`docs/superpowers/adr/2026-04-15-nats-transport.md` for the 21 ADRs behind
the design. Hot-fix rollback: set `transport: local` and restart.

Validator rules:
- `transport: nats` with empty `nats.servers` raises at startup.
- `transport: local` with a non-default `nats:` block warns (kept for
  forward-compat, not used).

### LLM Setup

Proctor uses [LiteLLM](https://docs.litellm.ai/) for provider abstraction. The call path `llm_call(prompt) -> str` (see `src/proctor/workers/llm.py`) delegates to `litellm.acompletion`, retries the primary model on transient errors (`max_retries` attempts, flat 1 s backoff), then falls back to `fallback_model` if it is set. Every attempt is persisted to the `llm_calls` table in `episodes.db` with token counts, latency, model, and any error. Set the appropriate API key:

```bash
export ANTHROPIC_API_KEY=sk-...    # for Claude models
export OPENAI_API_KEY=sk-...       # for OpenAI models
# Ollama models work without API keys (local)
```

## Routing

Trigger events (`trigger.terminal`, `trigger.telegram`, `trigger.scheduler`,
`trigger.webhook.<source>`) flow through a declarative Router that matches
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

## Multi-node deployment

For distributed operation split the system into a `core` coordinator
plus one or more `worker` executors connected by a shared NATS cluster.

### Core node (`core.yaml`)

```yaml
node_role: core
node_id: core-1
transport: nats
nats:
  servers:
    - nats://nats-1.internal:4222
    - nats://nats-2.internal:4222
  subject_prefix: proctor-prod   # namespace env-wise (prod / staging)
  reconnect_jitter: 0.5
```

### Worker node (`worker.yaml`)

```yaml
node_role: worker
node_id: worker-7
transport: nats
nats:
  servers:
    - nats://nats-1.internal:4222
    - nats://nats-2.internal:4222
  subject_prefix: proctor-prod
```

### Docker Compose topology

```yaml
services:
  nats:
    image: nats:2-alpine
    ports: ["4222:4222"]
  core:
    image: proctor:latest
    command: python -m proctor --config /etc/proctor/core.yaml
    depends_on: [nats]
  worker:
    image: proctor:latest
    command: python -m proctor --config /etc/proctor/worker.yaml
    depends_on: [nats]
    deploy: { replicas: 3 }
```

### Rollback to single-node

If NATS is unavailable and you need a fast hot-fix, switch the stack to
the in-process transport — no code changes required:

```yaml
transport: local
```

A warning is logged if `nats:` is still populated; the `nats` block is
ignored. Revert to `transport: nats` when the cluster is healthy again.

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

The default `uv run pytest` deselects tests that need external services:
`nats`, `ollama`, `docker`, and `benchmark` markers are all excluded. Run
each opt-in group by its marker.

**Ollama** — start a local server and pull the default model, then run the
marked tests:

```bash
ollama serve
ollama pull llama3.2
uv run pytest -m ollama
```

If Ollama is not reachable at `localhost:11434`, the tests skip cleanly.

**Docker/remote** — container-worker tests need a container runtime
(docker/podman) present and, for the remote path, the `proctor:latest` image
built and a reachable remote socket. They skip cleanly when unavailable:

```bash
uv run pytest -m docker
```

### Running NATS integration tests

NATS-backed contract and reconnect tests are gated by the `nats` marker.
They require `pip install proctor[nats]` and either `NATS_URL` in the
environment or a working docker/podman daemon for `testcontainers`.

```bash
uv sync --extra nats

# With testcontainers (auto-starts NATS + Toxiproxy containers)
uv run pytest -m nats

# With a pre-running NATS server
export NATS_URL=nats://localhost:4222
uv run pytest -m nats tests/integration/test_transport_contract.py
```

On podman hosts the tests work with the compatibility socket:

```bash
export DOCKER_HOST=unix://$(podman info --format '{{.Host.RemoteSocket.Path}}')
export TESTCONTAINERS_RYUK_DISABLED=true
# If the system Docker config has `credsStore: desktop`, point to a clean one:
mkdir -p /tmp/empty-docker-config && echo '{}' > /tmp/empty-docker-config/config.json
export DOCKER_CONFIG=/tmp/empty-docker-config
uv run pytest -m nats
```

The CI `integration-nats` job wires NATS via GitHub Actions `services:`
and sets `NATS_URL` directly — no testcontainers on CI.

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
├── docs/remote-workers.md         # Remote container workers (DOCKER_HOST=ssh://)
├── Dockerfile                     # Worker-role image
├── src/proctor/
│   ├── __init__.py               # Package init, __version__ = "0.1.0"
│   ├── __main__.py               # CLI entrypoint (argparse + signal handling)
│   ├── core/
│   │   ├── bootstrap.py          # Application lifecycle + event wiring
│   │   ├── bus.py                # EventBus over the transport abstraction
│   │   ├── config.py             # YAML config loading with pydantic models
│   │   ├── globs.py              # Shared fnmatch glob heuristics (routing + scope)
│   │   ├── memory.py             # EpisodicMemory: SQLite store for interaction history
│   │   ├── models.py             # Core models: Event, Task, Episode, Envelope, TaskStatus
│   │   ├── router.py             # Declarative Router (trigger event → catalog workflow)
│   │   ├── state.py              # SQLite state manager (tasks, schedules, config_overrides)
│   │   └── transport/            # EventTransport ABC + LocalEventTransport + NATSEventTransport
│   ├── triggers/
│   │   ├── base.py               # Trigger ABC
│   │   ├── scheduler.py          # SchedulerTrigger: cron/interval event firing
│   │   ├── telegram.py           # TelegramTrigger: Bot API long-polling
│   │   ├── terminal.py           # TerminalTrigger: stdin reader with /quit command
│   │   └── webhook.py            # WebhookTrigger: HTTP POST handler with auth
│   ├── router/                   # TaskRouter admission: invariants, scoring, TTL queue
│   │   ├── invariants.py         # 4 critical safety invariants
│   │   ├── models.py             # AgentProfile, Candidate, AdmitDecision, QueueEntry
│   │   ├── queue.py              # PendingQueue (pure FIFO with TTL)
│   │   ├── router.py             # TaskRouter facade (admit/release/retry)
│   │   └── scoring.py            # Capability scoring (requires ⊆ capabilities)
│   ├── infra/
│   │   └── docker.py             # ContainerRuntime: async docker/podman CLI wrapper
│   ├── workers/
│   │   ├── docker.py             # DockerWorkerManager: container fleet lifecycle
│   │   ├── llm.py                # build_llm_call: LiteLLM closure + telemetry
│   │   ├── node.py               # WorkerNode: worker-role runtime
│   │   ├── registry.py           # WorkerRegistry: discovery + heartbeat liveness
│   │   └── runtime.py            # AgentRuntime: LLM loop with tool calling
│   └── workflow/
│       ├── dag.py                # DAG executor with topo-sort + parallel execution
│       ├── engine.py             # WorkflowEngine dispatcher (simple/DAG)
│       └── spec.py               # WorkflowSpec model (simple/dag/fsm/orchestrator)
└── tests/
    ├── conftest.py               # anyio backend fixture
    ├── test_core/                # models, config, bus, state, bootstrap, router, globs
    ├── test_triggers/            # terminal, telegram, scheduler, webhook
    ├── test_router/              # invariants, scoring, queue, TaskRouter, dispatch
    ├── test_infra/               # ContainerRuntime
    ├── test_workers/             # AgentRuntime, registry, node, docker manager
    ├── test_workflow/            # WorkflowSpec, DAG, engine
    └── integration/              # NATS + docker-marker end-to-end tests
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
| Messaging | nats-py (integrated — `transport: nats`, `[nats]` extra) |
| Containers | docker/podman CLI (subprocess wrapper, no Python SDK dep) |
| Protocols | mcp SDK (installed, Phase 3 `mcp/` integration pending) |
| HTTP | aiohttp |
| Scheduling | croniter |
| Config | pyyaml |
| Dev | pytest + anyio (NOT asyncio), ruff, pyrefly |

## Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| 0 | Foundation (models, config, bus, state, bootstrap) | Done |
| 1 | MVP (workflow engine, DAG, agent runtime, terminal trigger) | Done |
| 2 | Proactivity (scheduler, Telegram trigger, router, episodic memory, webhook, NATS transport, admission layer) | Done |
| 3 | Distribution (worker registry + dispatch, docker & remote-docker workers, MCP tools) | In progress — registry/dispatch, docker & remote-docker workers done; `mcp/` and a bare-host SSH worker remain |
| 4 | Advanced orchestration (FSM, multi-agent, self-modification) | Planned |
| 5 | Observability & control (OpenTelemetry, dashboards, audit log, TUI) | Planned |
| 6 | Security & hardening (RBAC, encryption, guardrails, A2A gateway) | Planned |
