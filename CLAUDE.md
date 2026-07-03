# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Active Work & Roadmap

- **Current task list:** `./TODO.md` — read it at the start of every session
- **Project-specific tasks:** `spec/tasks.md` (Phase 2)
- **Ecosystem roadmap (strategic):** `../_cowork_output/roadmap/ecosystem-roadmap.md` — R-01…R-16 across Maestro / arbiter / ATP / spec-runner
- **Latest weekly status:** `../_cowork_output/status/2026-04-10-status.md`
- **Sibling projects** (reference only): `../Maestro/`, `../arbiter/`, `../atp-platform/`, `../spec-runner/`

proctor-a's role in the ecosystem: **first real Mode-2 consumer of Maestro**. Not named in ecosystem roadmap. Responsibility is dogfooding — when Maestro breaks a proctor-a task, file an issue with reproducible yaml + logs.

## `../_cowork_output/` is dev-only — never a code/runtime resource

`../_cowork_output/` (the polyrepo **sibling** workspace) is the development-time coordination area (cross-team ADRs, status notes, contract drafts, PM/dev tooling). Users and teams installing or cloning this project do NOT have it. Rules:

- Shipped/runtime code must never read, import, or resolve paths under `../_cowork_output/`.
- Canonical shippable facts live inside the owning repo (e.g. the ecosystem agents-catalog SSOT is `atp-platform/method/agents-catalog.toml`). Cross-repo contracts this repo depends on must be **vendored in** as pinned copies — never referenced from `../_cowork_output/` at runtime.
- Only workspace-local dev tooling (e.g. `../_cowork_output/devtools/`) and documentation may reference it.

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
| `core/` | Kernel — internal asyncio EventBus, SQLite state manager, config (YAML→pydantic), bootstrap, EpisodicMemory, core models (Event, Task, Episode, Envelope) |
| `triggers/` | Input adapters — Trigger ABC, TerminalTrigger (stdin→events), TelegramTrigger (Bot API polling), SchedulerTrigger (cron/interval). Future: webhook, filesystem, email, heartbeat |
| `workflow/` | Pipeline engine — WorkflowSpec model, DAG executor (topo-sort + parallel), WorkflowEngine dispatcher. Supports simple and DAG modes |
| `workers/` | Agent Runtime (LLM loop: prompt→tool calls→result). Future: worker registry, local/Docker/SSH workers |

**Planned modules** (not yet implemented):

| Module | Purpose | Phase |
|--------|---------|-------|
| `router/` | Two-layer task routing: capability scoring + safety invariants | Phase 2 |
| `mcp/` | MCP client/server/controller/registry/proxy | Phase 3 |
| `infra/` | Thin wrappers: Docker SDK, asyncssh, Vagrant CLI, Ansible runner, tmux | Phase 3 |
| `a2a/` | A2A Gateway for external agent interop | Phase 6 |
| `control/` | Safety (kill switch, FORBIDDEN list), budget tracking, health monitoring, TUI dashboard | Phase 5 |

**Data flow:** Trigger → Event → EventBus → Router (score + invariants) → NATS → Worker (Agent Runtime: LiteLLM + MCP tools) → Result → State.

**Core model:** `Event` (typed messages), `Task` (status machine: pending→assigned→running→completed/failed), `Episode` (agent interaction record for episodic memory), `Envelope` (NATS message wrapper with correlation_id, reply_to, TTL).

**NATS topics:** `proctor.events.>` (pub/sub), `proctor.tasks.{submit,assign,result}` (request/reply + queue), `proctor.agents.{id}.inbox` (direct), `proctor.mcp.proxy.{id}.call` (tool calls), `proctor.operator.{notify,ask,command}`.

## Tech Stack

- Python 3.12, pydantic 2.x, asyncio, anyio, aiosqlite, nats-py, litellm, tiktoken, mcp SDK, aiohttp, croniter, pyyaml
- Dev: pytest + anyio (NOT asyncio for async tests), ruff, pyrefly
- Uses `src/` layout with hatchling build backend

## Implementation Status

Phase 0 (Foundation) and Phase 1 (MVP) are complete. Phase 2 is partially complete.

**Completed:** Core models, config loading, EventBus, StateManager, bootstrap, WorkflowSpec, DAG executor, WorkflowEngine, Agent Runtime, Terminal Trigger, end-to-end integration, SchedulerTrigger (cron/interval), TelegramTrigger (Bot API polling), EpisodicMemory (SQLite-backed interaction history).

**Current phase:** Phase 2 (partially complete). The system accepts terminal input, Telegram messages, and scheduled events. Executes simple and DAG workflows via LLM. Persists task state and episodic history in SQLite.

**Next:** Remaining Phase 2 work — NATS real messaging, router, additional triggers (webhook). Then Phase 3+.

## Key Conventions

- All models use pydantic `BaseModel`
- Async everywhere (anyio for triggers/tests, aiosqlite, nats-py async client)
- EventBus handles all intra-process communication (NATS stubbed for Phase 2)
- LLM calls abstracted behind `Callable[[str], Awaitable[str]]` interface (mock in tests, real LiteLLM in future)
- Agent Runtime uses tool definitions (`ToolDef`) — tools are dynamic, not hardcoded
- Task state persists to SQLite at every transition (survives process restart)

## Reference Docs

- `docs/plans/2026-03-04-proctor-architecture-design.md` — full architecture with module specs, NATS topics, SQLite schemas, data flows
- `docs/plans/2026-03-04-proctor-phase0-phase1-plan.md` — task-by-task implementation plan for foundation + MVP
