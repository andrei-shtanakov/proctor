# Requirements Specification

> Proctor — Distributed autonomous agent system for a single operator

## 1. Context and Goals

### 1.1 Problem

Developers and devops engineers need an autonomous agent system that can execute tasks on schedule, react to external events, run workloads across containers/SSH/VMs, build pipelines (DAG/FSM/Decision Tree), and modify its own skills and code. Existing solutions (LangChain, AutoGen, Temporal) either impose too many abstractions, don't support distributed execution, or require heavy infrastructure.

### 1.2 Project Goals

| ID | Goal | Success Metric |
|----|------|----------------|
| G-1 | Accept tasks from terminal and execute via LLM agents | Terminal command -> DAG decomposition -> LLM execution -> result displayed |
| G-2 | Support DAG and simple workflow execution modes | Both `simple` (single prompt) and `dag` (multi-step with dependencies) work end-to-end |
| G-3 | Provide reliable state persistence and event-driven architecture | Tasks survive process restart; all components communicate via EventBus |

### 1.3 Stakeholders

| Role | Interests | Influence |
|------|----------|---------|
| Operator (developer/devops) | Execute complex tasks autonomously, monitor progress, approve risky actions | High |
| LLM Providers (Claude, Ollama) | API compatibility, proper token management | Medium |
| MCP Tool Servers | Standard protocol compliance, dynamic tool discovery | Medium |

### 1.4 Out of Scope

> Explicitly what is NOT included in Phase 1 (Foundation + MVP)

- Multi-operator / multi-tenant support
- NATS real messaging (NATS connection is stubbed; real implementation in Phase 2)
- Telegram, webhook, filesystem, email triggers (Phase 2)
- Scheduler (cron/at/interval) (Phase 2)
- Task router with scoring and invariants (Phase 2)
- FSM and orchestrator workflow modes (Phase 4)
- Docker/SSH/Vagrant workers (Phase 3)
- MCP proxy and vault (Phase 3)
- Self-modification pipeline (Phase 5)
- A2A external agent interop (Phase 6)
- Memory layers: episodic and semantic (Phases 2, 4)
- Budget tracking, health monitoring, TUI dashboard (Phase 5)
- Real LiteLLM integration (wired in Phase 1 but tested with mocks)

---

## 2. Functional Requirements

### 2.1 Core Infrastructure

#### REQ-001: Core Data Models
**As a** system developer
**I want** typed pydantic models for Event, Task, and Envelope
**So that** all inter-component communication is validated and serializable

**Acceptance Criteria:**
```gherkin
GIVEN the core module is imported
WHEN I create an Event with type, source, and payload
THEN it has an auto-generated UUID id and UTC timestamp
AND it can be serialized to JSON and deserialized back

GIVEN the core module is imported
WHEN I create a Task with a spec dict
THEN its status defaults to PENDING and retries defaults to 0

GIVEN the core module is imported
WHEN I create an Envelope with type, source, and payload
THEN it supports optional reply_to, correlation_id, target, and ttl_seconds
```

**Priority:** P0
**Traces to:** [TASK-002], [DESIGN-001]

---

#### REQ-002: Configuration Loading
**As an** operator
**I want** YAML-based configuration with sensible defaults
**So that** I can customize Proctor without editing code

**Acceptance Criteria:**
```gherkin
GIVEN a YAML config file exists at the specified path
WHEN I call load_config with that path
THEN a ProctorConfig pydantic model is returned with all values from the file

GIVEN no config file exists at the specified path
WHEN I call load_config
THEN a ProctorConfig with default values is returned

GIVEN a ProctorConfig
WHEN I access nested configs (llm, nats, scheduler)
THEN each has sensible defaults (e.g., default_model="claude-sonnet-4-20250514")
```

**Priority:** P0
**Traces to:** [TASK-003], [DESIGN-002]

---

#### REQ-003: Internal Event Bus
**As a** system developer
**I want** an async pub/sub bus with wildcard pattern matching
**So that** modules can communicate without tight coupling

**Acceptance Criteria:**
```gherkin
GIVEN an EventBus instance
WHEN I subscribe to "trigger.*" and publish an Event with type "trigger.telegram"
THEN the subscriber handler is called with the event

GIVEN multiple subscribers on the same pattern
WHEN an event is published
THEN all matching handlers are called

GIVEN a handler that raises an exception
WHEN an event is published to its pattern
THEN other handlers still execute and the error is logged

GIVEN a subscription ID
WHEN I call unsubscribe with that ID
THEN subsequent events no longer reach that handler
```

**Priority:** P0
**Traces to:** [TASK-004], [DESIGN-003]

---

#### REQ-004: SQLite State Persistence
**As a** system developer
**I want** async SQLite storage for tasks and configuration
**So that** task state survives process restarts

**Acceptance Criteria:**
```gherkin
GIVEN a StateManager initialized with a db path
WHEN initialize() is called
THEN tasks, schedules, and config_overrides tables are created

GIVEN a Task saved via save_task()
WHEN I call get_task(task_id)
THEN the same task is returned with all fields intact

GIVEN multiple tasks with different statuses
WHEN I call list_tasks(status=PENDING)
THEN only tasks with PENDING status are returned

GIVEN a config override set via set_config("key", value)
WHEN I call get_config("key")
THEN the stored value is returned
```

**Priority:** P0
**Traces to:** [TASK-005], [DESIGN-004]

---

#### REQ-005: Application Lifecycle
**As an** operator
**I want** a clean startup/shutdown lifecycle
**So that** all components initialize and tear down gracefully

**Acceptance Criteria:**
```gherkin
GIVEN a ProctorConfig
WHEN I create an Application and call start()
THEN the data directory is created, SQLite is initialized, EventBus is active, and is_running is True

GIVEN a running Application
WHEN I call stop()
THEN SQLite is closed, is_running is False, and no resources leak

GIVEN the __main__.py entrypoint
WHEN I run python -m proctor
THEN the Application starts and responds to SIGINT/SIGTERM for graceful shutdown
```

**Priority:** P0
**Traces to:** [TASK-006], [DESIGN-005]

---

### 2.2 Workflow Engine

#### REQ-006: WorkflowSpec Model
**As a** system developer
**I want** a universal workflow specification model
**So that** all pipeline types (simple, DAG, FSM, orchestrator) share a common format

**Acceptance Criteria:**
```gherkin
GIVEN a WorkflowSpec with mode=SIMPLE and a prompt
WHEN I serialize and deserialize it
THEN all fields are preserved

GIVEN a WorkflowSpec with mode=DAG and a list of Steps
WHEN Steps have depends_on references, inputs, and outputs
THEN the model validates and all references are accessible

GIVEN a WorkflowPolicies instance with defaults
WHEN I inspect max_retries and max_runtime_seconds
THEN they have reasonable default values (e.g., max_retries=2, max_runtime=900s)
```

**Priority:** P0
**Traces to:** [TASK-007], [DESIGN-006]

---

#### REQ-007: DAG Execution
**As a** system developer
**I want** topological sort and parallel execution of DAG steps
**So that** multi-step workflows execute in correct dependency order

**Acceptance Criteria:**
```gherkin
GIVEN a list of Steps with linear dependencies (A -> B -> C)
WHEN topo_sort is called
THEN steps are returned in order A, B, C

GIVEN a DAG with parallel branches (root -> left, root -> right -> join)
WHEN DAGExecutor executes with a step runner
THEN root runs first, left and right run in parallel, join runs last

GIVEN a DAG with a cycle (A depends on B, B depends on A)
WHEN topo_sort is called
THEN a ValueError is raised mentioning "cycle"

GIVEN a step that returns an error
WHEN its dependents are reached
THEN they are skipped with a "dependency failed" error
```

**Priority:** P0
**Traces to:** [TASK-008], [DESIGN-007]

---

#### REQ-008: Workflow Engine Dispatcher
**As a** system developer
**I want** a dispatcher that routes WorkflowSpecs to the correct executor
**So that** the system handles multiple workflow modes transparently

**Acceptance Criteria:**
```gherkin
GIVEN a WorkflowSpec with mode=SIMPLE and a prompt
WHEN the WorkflowEngine executes it
THEN the LLM is called with the prompt and the result is returned

GIVEN a WorkflowSpec with mode=DAG and steps
WHEN the WorkflowEngine executes it
THEN all steps execute via DAGExecutor and the last step's output is the final result

GIVEN a WorkflowSpec with mode=FSM (not yet implemented)
WHEN the WorkflowEngine executes it
THEN a WorkflowResult with an error "not supported" is returned
```

**Priority:** P0
**Traces to:** [TASK-009], [DESIGN-008]

---

### 2.3 Agent Runtime

#### REQ-009: LLM Agent Loop with Tool Calling
**As a** system developer
**I want** an agent runtime that loops between LLM calls and tool executions
**So that** agents can use external tools to accomplish tasks

**Acceptance Criteria:**
```gherkin
GIVEN an AgentRuntime with no tools and a mock LLM that returns text
WHEN run() is called with an instruction
THEN it returns the text result in 1 turn

GIVEN an AgentRuntime with a tool and a mock LLM that first calls the tool then returns text
WHEN run() is called
THEN the tool is executed, its result is fed back to the LLM, and the final text is returned in 2 turns

GIVEN an AgentRuntime with max_turns=3 and a mock LLM that always requests tool calls
WHEN run() is called
THEN it stops after 3 turns and returns the last known state

GIVEN a tool call for an unknown tool
WHEN the agent processes it
THEN an error ToolResult is created and the LLM continues to the next turn
```

**Priority:** P0
**Traces to:** [TASK-010], [DESIGN-009]

---

### 2.4 Triggers

#### REQ-010: Terminal Input Trigger
**As an** operator
**I want** to type commands in the terminal to create tasks
**So that** I can interact with Proctor directly from the command line

**Acceptance Criteria:**
```gherkin
GIVEN a TerminalTrigger started with an EventBus
WHEN a non-empty line is received
THEN an Event with type="trigger.terminal" and the text in payload is published

GIVEN a TerminalTrigger
WHEN an empty or whitespace-only line is received
THEN no event is published

GIVEN a TerminalTrigger
WHEN "/quit" or "/exit" is received
THEN the trigger signals to stop
```

**Priority:** P0
**Traces to:** [TASK-011], [DESIGN-010]

---

### 2.5 Integration

#### REQ-011: End-to-End Terminal to Result
**As an** operator
**I want** to type a command and see the LLM result
**So that** Proctor is usable as an MVP

**Acceptance Criteria:**
```gherkin
GIVEN a started Application with a configured LLM function
WHEN a "trigger.terminal" event is published with text "Tell me about RISC-V"
THEN a simple workflow is created, the LLM is called, and a "task.completed" event with the result is published

GIVEN a started Application
WHEN terminal commands are processed
THEN tasks are persisted in SQLite and their status transitions are tracked
```

**Priority:** P0
**Traces to:** [TASK-012], [DESIGN-005], [DESIGN-008]

---

## 3. Non-Functional Requirements

### NFR-000: Testing Requirements
| Aspect | Requirement |
|--------|------------|
| Unit test coverage | >= 80% for all core, workflow, workers, triggers modules |
| Integration tests | Terminal -> workflow -> result end-to-end test |
| Test framework | pytest + anyio (NOT asyncio directly) |
| CI requirement | All tests pass before merge |
| Async testing | Use `@pytest.mark.asyncio` with anyio backend |

**Definition of Done for any task:**
- [ ] Unit tests written and passing
- [ ] Coverage has not decreased
- [ ] Integration test if interfaces are affected
- [ ] `ruff check` and `ruff format` pass
- [ ] `pyrefly check` passes

**Traces to:** [TASK-100]

---

### NFR-001: Performance
| Metric | Requirement |
|--------|------------|
| Startup time | < 2 seconds for Application.start() |
| EventBus latency | < 10ms for event delivery to subscribers |
| SQLite operations | < 50ms for task CRUD operations |
| DAG execution overhead | < 100ms beyond step execution time |

**Traces to:** [TASK-005], [TASK-008]

---

### NFR-002: Reliability
| Aspect | Requirement |
|--------|------------|
| Handler isolation | EventBus handler errors do not crash other handlers |
| State persistence | All task state survives process restart |
| Graceful shutdown | Application.stop() cleanly releases all resources |
| DAG error handling | Step failures propagate to dependents without crashing executor |

**Traces to:** [TASK-004], [TASK-006], [TASK-008]

---

### NFR-003: Code Quality
| Metric | Requirement |
|--------|------------|
| Type hints | All public APIs fully typed |
| Linting | ruff check passes with E, F, I, UP rules |
| Formatting | ruff format with line-length=88 |
| Type checking | pyrefly check passes |
| Models | All data models use pydantic BaseModel |

**Traces to:** [TASK-001]

---

## 4. Constraints and Tech Stack

### 4.1 Technology Constraints

| Aspect | Decision | Rationale |
|--------|---------|-----------|
| Language | Python 3.12+ | LLM ecosystem is Python-first; self-modification easier in single language |
| Architecture | Microkernel + distributed workers | Distribution is day-1 requirement; workers isolated from core |
| Messaging | NATS (+ JetStream) | Single binary, zero-config, covers pub/sub + queue + request/reply |
| Storage | SQLite (3 databases) | Simple, reliable, no external DB server needed |
| LLM | Multi-provider via LiteLLM | Claude for complex, local models for simple, routing between them |
| Tools | MCP (dynamic) | Standard protocol, no hardcoded tools, runtime provisioning |
| Framework | None (pydantic + asyncio) | Minimal dependencies, full control over orchestration |
| Build | hatchling with src layout | Standard Python packaging |
| Package manager | uv | Fast, reliable Python package management |

### 4.2 Integration Constraints

- NATS connection stubbed in Phase 1 (no real NATS dependency yet)
- LLM calls abstracted behind callable interface (real LiteLLM wiring deferred)
- MCP client interface defined but not connected to real MCP servers

### 4.3 Business Constraints

- Single operator: no multi-tenant complexity
- Budget: LLM API costs managed per-task (tracked from Phase 5)
- Timeline: Phase 0 (1-2 weeks) + Phase 1 (2-3 weeks)

---

## 5. Acceptance Criteria

### Milestone 1: Foundation (Phase 0)
- [ ] REQ-001 — Core models (Event, Task, Envelope) with full validation
- [ ] REQ-002 — Config loading from YAML with defaults
- [ ] REQ-003 — EventBus with wildcard subscriptions
- [ ] REQ-004 — SQLite StateManager with task CRUD and config overrides
- [ ] REQ-005 — Application bootstrap with start/stop lifecycle
- [ ] NFR-000 — Test infrastructure with pytest + anyio
- [ ] NFR-003 — Code quality gates (ruff, pyrefly)

### Milestone 2: MVP (Phase 1)
- [ ] REQ-006 — WorkflowSpec model with simple and DAG modes
- [ ] REQ-007 — DAG execution with topo-sort and parallel steps
- [ ] REQ-008 — Workflow engine dispatcher
- [ ] REQ-009 — Agent runtime with LLM loop and tool calling
- [ ] REQ-010 — Terminal trigger for operator input
- [ ] REQ-011 — End-to-end integration: terminal -> workflow -> result
- [ ] NFR-001 — Performance targets met
- [ ] NFR-002 — Reliability requirements met
