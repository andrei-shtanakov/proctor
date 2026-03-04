# Proctor Architecture Design

**Date**: 2026-03-04
**Status**: Approved
**Author**: Operator + Claude

## 1. Overview

Proctor is a distributed autonomous agent system for a single operator (developer/devops). It executes tasks on schedule, reacts to external events, runs workloads across containers/SSH/VMs, builds pipelines (DAG/FSM/Decision Tree), and can modify its own skills and code.

### Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Architecture | Microkernel + distributed workers | Distribution is a core requirement; workers isolated from core |
| Language | Python 3.12+ (single language) | LLM ecosystem is Python-first; self-modification easier in one language |
| Messaging | NATS (+ JetStream) | Single binary, zero-config, covers pub/sub + queue + request/reply |
| Storage | SQLite (3 databases) + files | Simple, reliable, no external DB server needed |
| LLM | Multi-provider via LiteLLM | Claude for complex, local models for simple, routing between them |
| Tools | MCP (dynamic) | Standard protocol, no hardcoded tools, runtime provisioning |
| Framework | None (pydantic + asyncio + own engine) | Minimal dependencies, full control over orchestration |
| Users | Single operator | No multi-tenant complexity |

### Rejected Alternatives

| Rejected | Why |
|---|---|
| Elixir/OTP | Architecturally ideal (BEAM, supervision trees, distribution), but LLM ecosystem mismatch and dual-runtime overhead not justified for single operator |
| Monolith-first | Distribution is day-1 requirement |
| LangChain/LangGraph | Too many abstractions, vendor lock-in |
| AutoGen | Conversational-only, doesn't fit DAG/FSM |
| Temporal | Right idea, but Java/Go server too heavy |
| Celery/Redis | NATS JetStream covers task queues without extra infra |

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          PROCTOR CLUSTER                                │
│                                                                         │
│  ┌──────────────────────────────────────────────┐                       │
│  │              CORE (Node Leader)               │                      │
│  │  ┌──────────┐ ┌───────────┐ ┌─────────────┐  │    ┌──────────────┐  │
│  │  │ Event Bus│ │ Scheduler │ │ Task Router  │  │    │   NATS       │  │
│  │  │ (internal│ │ (cron/at/ │ │ (invariants  │  │◄──►│   Server     │  │
│  │  │  pubsub) │ │  interval)│ │  + scoring)  │  │    │  (messaging) │  │
│  │  └────┬─────┘ └─────┬─────┘ └──────┬───────┘  │    └──────────────┘  │
│  │       │             │              │           │           ▲          │
│  │  ┌────▼─────────────▼──────────────▼────────┐  │           │          │
│  │  │           State Manager                   │  │           │          │
│  │  │  (SQLite: tasks, memory, processes, logs) │  │           │          │
│  │  └───────────────────────────────────────────┘  │           │          │
│  └──────────────────────────────────────────────┘  │           │          │
│                                                     │           │          │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────▼────────┐  │          │
│  │  Worker Node  │  │  Worker Node  │  │   Worker Node      │  │          │
│  │  (local)      │  │  (SSH host)   │  │   (container)      │  │          │
│  │  ┌─────────┐  │  │  ┌─────────┐  │  │   ┌─────────┐     │  │          │
│  │  │ Agent   │  │  │  │ Agent   │  │  │   │ Agent   │     │  │          │
│  │  │ Runtime │  │  │  │ Runtime │  │  │   │ Runtime │     │  │          │
│  │  └─────────┘  │  │  └─────────┘  │  │   └─────────┘     │  │          │
│  └───────────────┘  └───────────────┘  └────────────────────┘  │          │
│                                                                 │          │
│  ┌──────────────────────────────────────────────────────────┐  │          │
│  │                    TRIGGER LAYER                          │  │          │
│  │  Telegram │ Webhooks │ Terminal │ FileWatcher │ Gmail     │  │          │
│  └──────────────────────────────────────────────────────────┘  │          │
└─────────────────────────────────────────────────────────────────┘          │
                                                                            │
  ┌───────────────┐   ┌───────────────┐                                     │
  │ Remote Host A │   │ Remote Host B │◄────────────────────────────────────┘
  │ (Worker Node) │   │ (Worker Node) │   (NATS connection)
  └───────────────┘   └───────────────┘
```

### Principles

1. **Core** is the single leader process. Manages state, scheduling, task routing. Does not execute heavy work.
2. **Worker Nodes** connect to NATS, receive tasks, execute via Agent Runtime, return results. Can be local, Docker, SSH, or Vagrant VM.
3. **NATS** is the only external component. Single binary (~15MB), zero-config. Provides request/reply, pub/sub, queue groups.
4. **Trigger Layer** adapts external events into standard `Event` objects published to the Event Bus.
5. **State Manager** is the single source of truth (SQLite on Core). Workers don't access DB directly.

---

## 3. Modules

10 modules, each independently developable and testable:

```
proctor/
├── core/           # M1: Kernel — EventBus, State, Config, Bootstrap
├── triggers/       # M2: Input adapters (Telegram, webhook, FS, email, ...)
├── scheduler/      # M3: Schedules (cron, at, interval)
├── router/         # M4: Task routing + invariant checks
├── workflow/       # M5: Pipeline engine (DAG, FSM, Decision Tree, Orchestrator)
├── workers/        # M6: Workers and Agent Runtime
├── memory/         # M7: Memory (working, episodic, semantic, self-model)
├── mcp/            # M8: MCP client + server + manager + NATS proxy
├── infra/          # M9: Docker, SSH, Vagrant, Ansible, tmux
├── a2a/            # M10: A2A Gateway for external agent interop
└── control/        # M11: Safety, budget, health, dashboard
```

### M1: Core

```
core/
├── bus.py          # Internal asyncio pub/sub (NOT NATS — for intra-process only)
├── state.py        # SQLite wrapper (tasks, processes, config)
├── config.py       # YAML/TOML → pydantic models
├── bootstrap.py    # Start all modules, graceful shutdown
└── models.py       # Event, Task, Process, Result, Envelope
```

Key models:

```python
class Event(BaseModel):
    id: str
    type: str                    # "trigger.telegram", "schedule.due", "task.completed"
    source: str
    payload: dict[str, Any]
    timestamp: datetime

class Task(BaseModel):
    id: str
    status: TaskStatus           # pending → assigned → running → completed/failed
    spec: WorkflowSpec           # what to execute (DAG/FSM/simple)
    trigger_event: str | None
    worker_id: str | None
    result: Any | None
    retries: int = 0
    created_at: datetime
    deadline: datetime | None

class Envelope(BaseModel):
    id: str
    type: str
    source: str
    target: str | None
    payload: dict[str, Any]
    reply_to: str | None
    correlation_id: str | None
    timestamp: datetime
    ttl_seconds: int | None
```

### M2: Triggers

Each trigger is an independent adapter with a unified interface:

```python
class Trigger(ABC):
    async def start(self, bus: EventBus) -> None: ...
    async def stop(self) -> None: ...
```

```
triggers/
├── base.py         # ABC Trigger
├── telegram.py     # aiogram: long-polling / webhook
├── webhook.py      # aiohttp: HTTP POST → Event
├── terminal.py     # stdin/readline
├── filesystem.py   # watchfiles
├── email.py        # Gmail API
└── heartbeat.py    # Periodic self-wake (a la openclaw HEARTBEAT.md)
```

### M3: Scheduler

Three schedule types, stored in SQLite:

| Type | Example | Semantics |
|------|---------|-----------|
| `cron` | `0 9 * * 1` | Cron expression with TZ |
| `at` | `2026-03-05T14:00` | One-shot at timestamp |
| `interval` | `30m`, `6h` | Repeat every N |

Engine polls SQLite every 30s, emits `Event(type="schedule.due")` when due.

### M4: Router

Two-layer routing (inspired by arbiter from research):

1. **Score**: match task type → agent capabilities
2. **Invariants**: safety checks before assignment

Critical invariants (block assignment):
- `agent_available`: agent active + has free slots
- `scope_isolation`: files don't overlap with running tasks
- `branch_not_locked`: git branch not used by another task
- `concurrency_limit`: total running < max

Warning invariants (logged, don't block):
- `budget_remaining`, `retry_limit`, `rate_limit`, `agent_health`, `task_compatible`, `sla_feasible`

Fallback: up to 3 candidates tried. If all fail → reject + alert operator.

### M5: Workflow

Executes three pipeline types + orchestrator mode:

```
workflow/
├── engine.py       # Dispatcher: mode → executor
├── dag.py          # DAG: topo-sort, parallel steps, depends_on
├── fsm.py          # FSM: state + event → transition, persisted in SQLite
├── decision.py     # Decision Tree: rule-based routing (auto/LLM/human)
├── orchestrator.py # Multi-agent: manager + workers with shared state
├── spec.py         # WorkflowSpec pydantic model (JSON format from research.md)
├── compiler.py     # Natural language → WorkflowSpec (via LLM / Claude Code Skill)
└── steps.py        # Step executors: llm, shell, http, skill, wait_event
```

WorkflowSpec follows the JSON format from `research.md`: field `mode: "dag" | "fsm" | "orchestrator"`, arrays `steps` or `States`, plus `policies`, `security`, `channels`.

### M6: Workers

```
workers/
├── runtime.py      # Agent Runtime: LLM loop (prompt → tool calls → result)
├── registry.py     # Worker registry (NATS-based discovery)
├── local.py        # Local worker (asyncio subprocess)
├── docker.py       # Docker-based worker
├── remote.py       # SSH-based worker
└── nats_worker.py  # NATS client: subscribe tasks, send results
```

Agent Runtime gets tools from MCP servers (not hardcoded). LLM loop:
1. Receive task (instruction + context)
2. Call LLM with available tools (from MCP)
3. Execute tool calls
4. Return tool results to LLM
5. Repeat until done or limit reached

### M7: Memory

Three layers:

| Layer | Scope | Storage | Lifetime |
|-------|-------|---------|----------|
| Working | Per-task | RAM | Task duration |
| Episodic | Per-process | SQLite (episodes.db) | Permanent (with compaction) |
| Semantic | Global knowledge | SQLite FTS5 (knowledge.db) | Permanent (with decay/pruning) |

Plus **Self-Model**: identity, topology, operator profile, performance stats, budget, active processes.

Memory Optimizer runs on schedule (e.g. daily 3am):
1. Working memory cleanup (orphaned sessions)
2. Episodic compaction (summarize old episodes, drop raw buffers)
3. Semantic pruning (decay unused facts, merge duplicates)
4. Pattern extraction (recurring patterns → new facts)

### M8: MCP

```
mcp/
├── client.py       # MCP client: connect, list_tools, call_tool
├── server.py       # Proctor as MCP server (expose tasks, memory, scheduler)
├── controller.py   # Central controller: provision, lifecycle, permissions
├── registry.py     # SQLite: server definitions, status, node affinity
├── proxy.py        # NATS MCP Proxy: stdio ↔ NATS bridge for remote access
└── vault.py        # Encrypted secret storage + operator credential requests
```

Three MCP server affinities:
- **GLOBAL**: can run on any node (GitHub, Slack, web search)
- **NODE_LOCAL**: bound to specific node (filesystem, local DB)
- **ON_DEMAND**: started when agent requests it

Agent requests MCP access → Controller checks registry → if no credentials, asks operator → starts server → agent connects (directly if same node, via NATS proxy if remote).

Secret Vault: age-encrypted file on Core. Secrets injected as env vars at MCP server start. Never passed to agents directly. All access logged.

### M9: Infra

```
infra/
├── docker.py       # Docker SDK wrapper
├── ssh.py          # asyncssh wrapper
├── vagrant.py      # Vagrant CLI wrapper
├── ansible.py      # Ansible runner
├── tmux.py         # tmux session manager
└── provision.py    # VM create → provision → start worker
```

Thin wrappers over CLI/SDK with pydantic models for configuration.

### M10: A2A (Phase 6)

```
a2a/
├── gateway.py      # HTTP server: /.well-known/agent.json, /tasks endpoints
├── agent_card.py   # Agent Card generation from Proctor config
├── translator.py   # A2A Task ↔ internal NATS Envelope
└── client.py       # A2A client for calling external agents
```

### M11: Control

```
control/
├── safety.py       # Kill switch, FORBIDDEN list, operator approval queue
├── budget.py       # Cost tracking, limits, alerts
├── health.py       # Heartbeat monitor, node/worker/MCP diagnostics
└── dashboard.py    # Terminal TUI (optional, rich/textual)
```

---

## 4. NATS Topic Structure

```
proctor.
├── events.>                        # Pub/Sub: all system events
│   ├── events.trigger.*            # trigger.telegram, trigger.webhook, ...
│   ├── events.schedule.due         # schedule fired
│   ├── events.task.*               # task.created, task.assigned, task.completed, task.failed
│   ├── events.worker.*             # worker.registered, worker.heartbeat, worker.offline
│   └── events.mcp.*               # mcp.started, mcp.stopped, mcp.tool_called
│
├── tasks.
│   ├── tasks.submit                # Request/Reply: submit task to Router
│   ├── tasks.assign.{worker_id}    # Queue: task to specific worker
│   └── tasks.result.{task_id}      # Reply: execution result
│
├── agents.
│   ├── agents.{id}.inbox           # Direct message: agent → agent
│   ├── agents.{id}.reply           # Reply to direct message
│   └── agents.broadcast            # Pub/Sub: broadcast to all agents
│
├── mcp.
│   ├── mcp.request                 # Request: "I need server X"
│   ├── mcp.proxy.{server_id}.call  # Request/Reply: tool call via proxy
│   └── mcp.proxy.{server_id}.notify
│
├── nodes.
│   ├── nodes.{id}.heartbeat        # Pub: node heartbeat (every 30s)
│   ├── nodes.{id}.mcp.start        # Request: start MCP server on node
│   └── nodes.{id}.mcp.stop
│
└── operator.
    ├── operator.notify              # Pub: notifications to operator
    ├── operator.ask                 # Request/Reply: credential/approval requests
    └── operator.command             # Sub: commands from operator
```

Delivery guarantees:

| Pattern | Guarantee | Used for |
|---------|-----------|----------|
| Request/Reply | At-most-once + timeout | tasks.submit, mcp.call, operator.ask |
| Pub/Sub | At-most-once | events.*, heartbeat |
| Queue Group (JetStream) | At-least-once (with ack) | tasks.assign |

---

## 5. Data Flows

### Flow 1: Simple task (Telegram → execute → reply)

Operator sends message → Telegram trigger → Event → Bus → Router scores agents → assigns to best worker via NATS → Worker runs LLM loop with MCP tools → result back via NATS → Telegram reply.

### Flow 2: DAG pipeline on schedule

Scheduler fires → load WorkflowSpec (mode: "dag") → topo-sort steps → execute steps sequentially/in parallel via workers → security review step → deliver via channel (Telegram/email) → store episode in memory.

### Flow 3: FSM long-running process

Event triggers FSM → transition to new state → execute Task state via worker → Choice state branches → WaitForEvent persists state to SQLite → ... hours/days pass ... → external event arrives → restore FSM state → continue transitions.

Key: FSM persists state to SQLite at every transition. Survives Core restart.

### Flow 4: Agent requests MCP server at runtime

Agent needs Jira → `mcp.request` via NATS → Controller checks registry → no credentials → `operator.ask` "Jira API token?" → operator provides → Vault stores → Controller starts jira-mcp on optimal node → agent connects (direct or via NATS proxy).

### Flow 5: Multi-agent collaboration (orchestrator mode)

Workflow engine starts manager agent → manager plans + delegates via `agents.{id}.inbox` → researcher + analyst work in parallel → return results → manager reviews → delegates to writer → writer compiles report → security guard reviews → done.

---

## 6. Self-Modification

Three levels with increasing risk:

### Level 1: Configuration (safe, automatic)

Adjust retry counts, timeouts, routing weights, schedule intervals based on episode stats and operator feedback. Stored as config overrides in SQLite.

### Level 2: Workflow Templates (medium risk, auto + review)

Generate new WorkflowSpec templates from successful episodes. Modify existing templates. New templates flagged for operator review before production use.

### Level 3: Code (high risk, requires operator approval)

Process:
1. Agent identifies improvement ("tool X would be useful")
2. Generates code in sandbox (temp directory)
3. Runs tests in isolated container
4. Creates diff → sends to operator for review
5. Operator approves → git commit → hot-reload module
6. Operator rejects → log reason → learn from rejection

### FORBIDDEN (hardcoded, no override)

- Modify `core/bootstrap.py`
- Modify `mcp/vault.py`
- Modify self-modification guardrails themselves
- Disable operator approval for Level 3
- Modify critical invariants (scope isolation, concurrency)

---

## 7. Crash Recovery

### Core restart

1. Read SQLite: restore FSM states, pending schedules, MCP registry, active tasks
2. NATS reconnect: workers re-register via heartbeat (30s), JetStream replays unacked tasks
3. Reconciliation: tasks "running" with no worker heartbeat → re-queue; dead MCP servers → restart
4. System resumes within ~60 seconds, no data loss

### Worker crash

1. Heartbeat stops → Core detects within 30s
2. Worker marked "offline"
3. Unacked JetStream tasks → redelivered to another worker
4. MCP servers on that node → marked "stopped"

---

## 8. Control Plane

### Safety Controls
- Invariants (scope isolation, concurrency, budget)
- MCP permissions (per-server tool whitelist/blacklist)
- Secret vault with access logging
- Self-modification FORBIDDEN list
- Operator approval queue
- Kill switch: `operator.command "HALT_ALL"`

### Efficiency Controls
- Budget tracker (cost per task, daily/monthly limits)
- Token counter (per-agent, per-workflow)
- Duration monitor (flag tasks exceeding expected time)
- Retry limiter
- Decision Tree router (auto/LLM/human based on complexity x success rate)

### Health Monitoring
- Node heartbeats (30s via NATS)
- Worker heartbeats (30s)
- MCP server health checks (60s)
- SQLite WAL size monitoring
- Periodic self-diagnostic (daily)

---

## 9. Tech Stack

### Runtime Dependencies (~12 packages)

| Category | Package | Purpose |
|----------|---------|---------|
| Core | pydantic 2.x | Models, config, validation |
| Core | nats-py | NATS client + JetStream |
| Core | aiosqlite | Async SQLite |
| Core | pyyaml / tomli | Config files |
| LLM | litellm | Multi-provider LLM calls |
| LLM | tiktoken | Token counting |
| MCP | mcp (official SDK) | MCP client + server |
| Triggers | aiogram 3.x | Telegram bot |
| Triggers | aiohttp | Webhook server + HTTP client |
| Triggers | watchfiles | Filesystem monitoring |
| Triggers | google-auth | Gmail API |
| Infra | asyncssh | SSH connections |
| Infra | docker (SDK) | Container management |

### Dev Dependencies

| Package | Purpose |
|---------|---------|
| pytest + anyio | Testing |
| ruff | Format + lint |
| pyrefly | Type checking |

---

## 10. SQLite Schema

### state.db (operational state)

```sql
tasks          (id, status, spec_json, worker_id, trigger_event, result_json,
                retries, created_at, updated_at, deadline)
schedules      (id, type, expression, tz, workflow_id, enabled, next_run, last_run)
workers        (id, node_id, status, capabilities_json, last_heartbeat, load)
nodes          (id, address, role, status, last_heartbeat)
mcp_servers    (id, def_json, node_id, status, pid, started_at)
fsm_states     (workflow_run_id, current_state, context_json, updated_at)
config_overrides (key, value_json, updated_at, source)
```

### episodes.db (history)

```sql
episodes       (id, task_id, workflow_id, task_type, trigger, plan_json,
                steps_log_json, result_json, metrics_json, reflections_json,
                extracted_facts_json, created_at)
```

### knowledge.db (semantic memory)

```sql
facts          (id, category, content, source_episode_id, confidence,
                verified, created_at, last_accessed, access_count)
-- FTS5 virtual table on facts.content
self_model     (key, value_json, updated_at)
operator       (key, value_json, updated_at)
```

---

## 11. Implementation Phases

### Phase 0: Foundation (1-2 weeks)
- pyproject.toml + dependencies
- core/models.py (Event, Task, Envelope)
- core/config.py + proctor.yaml
- core/state.py (SQLite init + basic CRUD)
- core/bus.py (internal EventBus)
- core/bootstrap.py (startup + shutdown)
- NATS connection skeleton

### Phase 1: MVP — Planner + Executor (2-3 weeks)
- workflow/spec.py (WorkflowSpec model)
- workflow/engine.py (dispatch by mode)
- workflow/dag.py (sequential first, parallel later)
- workflow/steps.py (llm + shell steps)
- workers/runtime.py (LLM loop: litellm + MCP tools)
- workers/local.py (local subprocess)
- triggers/terminal.py (operator types command → task)
- mcp/client.py (connect to 1-2 MCP servers)
- Tests

### Phase 2: Proactivity (2-3 weeks)
- scheduler/ (cron + at + interval)
- triggers/telegram.py, webhook.py, filesystem.py, heartbeat.py
- router/ (scoring + 4 critical invariants)
- memory/working.py + memory/episodic.py

### Phase 3: Distribution (2-3 weeks)
- workers/docker.py, workers/remote.py
- workers/registry.py (NATS-based discovery)
- mcp/proxy.py (NATS MCP proxy)
- mcp/controller.py (lifecycle, vault)
- infra/docker.py + infra/ssh.py
- Multi-node testing

### Phase 4: Intelligence (2-3 weeks)
- workflow/fsm.py, workflow/orchestrator.py
- workflow/compiler.py (NL → WorkflowSpec via LLM)
- workflow/decision.py (routing tree)
- memory/semantic.py (FTS5), memory/self_model.py, memory/optimizer.py

### Phase 5: Self-Modification + Infra (2-3 weeks)
- Self-modification pipeline (sandbox → test → review → deploy)
- infra/vagrant.py + ansible.py + provision.py + tmux.py
- control/ (safety, budget, health, dashboard)
- triggers/email.py (Gmail)

### Phase 6: External Interop (2 weeks)
- mcp/server.py (Proctor as MCP server)
- a2a/ (gateway, agent card, client)
- Full integration testing

---

## 12. Future Roadmap (Post-Phase 6)

### Near-term (Phases 7-8)

**Phase 7: Learning and Optimization**
- Decision Tree router trained on episode data (sklearn, periodic retrain)
- Automatic workflow optimization: analyze DAG timing → increase parallelism
- LLM model router: cost/quality trade-off per task type (cheap model for simple, expensive for complex)
- A/B testing for workflow templates: run two variants, compare outcomes
- Operator feedback loop: thumbs up/down on results → adjust routing weights

**Phase 8: Advanced Memory**
- Vector embeddings for semantic search (local model, e.g. sentence-transformers via Ollama)
- RAG over episodic memory: agent can query "how did I solve X last time?"
- Cross-episode reasoning: detect patterns across workflows ("deploy after 5pm always fails")
- Memory sharing between Proctor instances (NATS-based sync of knowledge.db)

### Mid-term (Phases 9-11)

**Phase 9: Multi-Operator**
- Role-based access: admin vs viewer vs limited operator
- Per-operator secret vaults and memory isolation
- Shared knowledge base with access control
- Operator handoff: "I'm offline, forward alerts to operator B"

**Phase 10: Advanced Orchestration**
- Behavior Trees (reactive alternative to FSM for complex agents)
- Hierarchical task networks (HTN) for automatic planning decomposition
- Monte Carlo Tree Search for plan exploration (try multiple strategies, pick best)
- Workflow versioning with rollback: if new template performs worse → auto-revert
- Long-running sagas with compensation (undo actions on failure)

**Phase 11: Ecosystem**
- Plugin system: third-party modules packaged as Python packages
- Marketplace for workflow templates (share/import community templates)
- Proctor-to-Proctor federation: multiple Proctor instances coordinate via A2A
- Web dashboard (replacing terminal TUI): real-time via WebSocket

### Long-term Vision

**Phase 12: Autonomy**
- Goal-directed behavior: operator sets high-level goals, Proctor autonomously plans milestones, schedules, and executes
- Proactive suggestions: "I noticed X pattern, should I set up a workflow for it?"
- Self-healing infrastructure: detect degraded node → provision replacement VM → migrate workers
- Budget-aware planning: "this goal costs ~$X and takes ~Y hours, proceed?"

**Phase 13: Observability Platform**
- OpenTelemetry integration: traces spanning trigger → router → worker → MCP → LLM
- Grafana/Prometheus export for metrics
- Replay system: re-run any episode with different parameters for debugging
- Cost attribution: break down spending by workflow, agent, MCP server, LLM provider

**Phase 14: Security Hardening**
- Sandboxed code execution (gVisor/Firecracker for untrusted generated code)
- Network policies per worker (restrict outbound access)
- Audit log with tamper-proof storage
- Secrets rotation automation
- Formal verification of critical invariants

### Research Directions

- **LLM-as-Judge calibration**: train calibrated critic models on Proctor's own task outcomes
- **Reinforcement learning for routing**: replace Decision Tree with RL agent that optimizes cost/quality Pareto frontier
- **Natural language invariants**: operator writes safety rules in plain text, Proctor compiles to code
- **Collaborative planning**: Proctor proposes plan, operator edits in structured editor, Proctor executes
- **Emergent specialization**: workers that self-specialize over time (one becomes "the deployment expert", another "the research expert") based on success patterns

---

## 13. Startup

```bash
# Dev: single node, everything local
uv run python -m proctor --config config/proctor.yaml

# Production: Core node
uv run python -m proctor --role core --config config/proctor.yaml

# Production: Worker node
uv run python -m proctor --role worker --core-nats nats://core-host:4222
```
