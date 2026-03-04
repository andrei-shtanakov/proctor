# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Proctor is a distributed autonomous agent system for a single operator. It executes tasks on schedule, reacts to external events, runs workloads across containers/SSH/VMs, builds pipelines (DAG/FSM/Decision Tree), and can modify its own skills and code. Microkernel architecture with NATS messaging, SQLite state, and LLM-driven agent runtimes with MCP tool access.

## Commands

```bash
# Dependencies
uv sync                              # Install all deps
uv add <package>                     # Add runtime dep
uv add --dev <package>               # Add dev dep

# Run
uv run python -m proctor --config config/proctor.yaml

# Tests
uv run pytest                        # All tests
uv run pytest tests/test_foo.py      # Single file
uv run pytest tests/test_foo.py::test_bar  # Single test
uv run pytest -x                     # Stop on first failure

# Code quality
uv run ruff format .                 # Format
uv run ruff check .                  # Lint
uv run ruff check . --fix            # Lint + auto-fix
pyrefly init                         # Init type checker (once)
pyrefly check                        # Type check (run after every change)
```

## Architecture

**Key design decisions:**
- Microkernel + distributed workers (no framework — pydantic + asyncio + own engine)
- NATS (+ JetStream) for all inter-node messaging (single binary, covers pub/sub + queue + request/reply)
- SQLite x3 for state: `state.db` (operational), `episodes.db` (history), `knowledge.db` (semantic memory + FTS5)
- LiteLLM for multi-provider LLM calls; MCP for dynamic tool provisioning
- Single operator (no multi-tenant complexity)

**Module layout** (`src/proctor/`):

| Module | Purpose |
|--------|---------|
| `core/` | Kernel — internal asyncio EventBus, SQLite state manager, config (YAML→pydantic), bootstrap, core models (Event, Task, Envelope) |
| `triggers/` | Input adapters (Telegram, webhook, filesystem, email, terminal, heartbeat) — each implements `Trigger` ABC |
| `scheduler/` | Cron/at/interval schedules stored in SQLite, emits `schedule.due` events |
| `router/` | Two-layer task routing: capability scoring + safety invariants (scope isolation, branch locking, concurrency limits) |
| `workflow/` | Pipeline engine — DAG, FSM, Decision Tree, multi-agent orchestrator. WorkflowSpec is the central pydantic model |
| `workers/` | Agent Runtime (LLM loop: prompt→tool calls→result), worker registry (NATS discovery), local/Docker/SSH workers |
| `memory/` | Three-layer: working (RAM, per-task), episodic (SQLite, per-process), semantic (FTS5, global). Plus self-model |
| `mcp/` | MCP client/server/controller/registry/proxy. NATS MCP Proxy bridges stdio↔NATS for remote tool access |
| `infra/` | Thin wrappers: Docker SDK, asyncssh, Vagrant CLI, Ansible runner, tmux |
| `a2a/` | A2A Gateway for external agent interop (Phase 6) |
| `control/` | Safety (kill switch, FORBIDDEN list), budget tracking, health monitoring, TUI dashboard |

**Data flow:** Trigger → Event → EventBus → Router (score + invariants) → NATS → Worker (Agent Runtime: LiteLLM + MCP tools) → Result → State.

**Core model:** `Event` (typed messages), `Task` (status machine: pending→assigned→running→completed/failed), `Envelope` (NATS message wrapper with correlation_id, reply_to, TTL).

**NATS topics:** `proctor.events.>` (pub/sub), `proctor.tasks.{submit,assign,result}` (request/reply + queue), `proctor.agents.{id}.inbox` (direct), `proctor.mcp.proxy.{id}.call` (tool calls), `proctor.operator.{notify,ask,command}`.

## Tech Stack

- Python 3.12, pydantic 2.x, asyncio, aiosqlite, nats-py, litellm, tiktoken, mcp SDK, aiohttp, pyyaml
- Dev: pytest + anyio (NOT asyncio for async tests), ruff, pyrefly
- Uses `src/` layout with hatchling build backend

## Implementation Status

The project is in pre-implementation phase. Architecture design and Phase 0+1 plan exist in `docs/plans/`. No source code yet.

**Current phase:** Phase 0 (Foundation) — project scaffold, core models, config, SQLite state, EventBus, bootstrap, NATS skeleton.

**Next:** Phase 1 (MVP) — WorkflowSpec, DAG engine, LLM step execution, Agent Runtime, local worker, terminal trigger, MCP client.

## Key Conventions

- All models use pydantic `BaseModel`
- Async everywhere (asyncio, aiosqlite, nats-py async client)
- Workers never access SQLite directly — all state through Core via NATS
- MCP tools are dynamic (not hardcoded) — agents discover tools at runtime
- FSM states persist to SQLite at every transition (survives Core restart)
- Self-modification has 3 levels: config (auto), workflow templates (auto+review), code (requires operator approval)
- FORBIDDEN list is hardcoded: cannot modify `core/bootstrap.py`, `mcp/vault.py`, or self-modification guardrails

## Reference Docs

- `docs/plans/2026-03-04-proctor-architecture-design.md` — full architecture with module specs, NATS topics, SQLite schemas, data flows
- `docs/plans/2026-03-04-proctor-phase0-phase1-plan.md` — task-by-task implementation plan for foundation + MVP
