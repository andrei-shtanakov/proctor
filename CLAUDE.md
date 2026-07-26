# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Active Work & Roadmap

- **Current task list:** `./TODO.md` — read it at the start of every session
- **Project-specific tasks:** `spec/tasks.md` (Phase 2)
- **Ecosystem roadmap (strategic):** `../prograph-vault/authored/notes/ecosystem-roadmap.md` — R-01…R-16 across Maestro / arbiter / ATP / spec-runner
- **Latest weekly status:** `../prograph-vault/authored/notes/status/2026-07-08-status.md`
- **Sibling projects** (reference only): `../maestro/`, `../arbiter/`, `../atp-platform/`, `../spec-runner/`

proctor's role in the ecosystem: **first real Mode-2 consumer of Maestro**. Not named in ecosystem roadmap. Responsibility is dogfooding — when Maestro breaks a proctor task, file an issue with reproducible yaml + logs.

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
- SQLite for state: `state.db` (operational), `episodes.db` (history) — implemented; `knowledge.db` (semantic memory + FTS5) is planned for Phase 4, not yet present
- LiteLLM for multi-provider LLM calls; MCP for dynamic tool provisioning
- Single operator (no multi-tenant complexity)

**Module layout** (`src/proctor/`):

| Module | Purpose |
|--------|---------|
| `core/` | Kernel — EventBus on top of the `transport/` abstraction (LocalEventTransport in-process, NATSEventTransport cross-node, `transport: auto\|local\|nats` in config), SQLite state manager, config (YAML→pydantic), bootstrap, EpisodicMemory, core models (Event, Task, Episode, Envelope) |
| `triggers/` | Input adapters — Trigger ABC, TerminalTrigger (stdin→events), TelegramTrigger (Bot API polling), SchedulerTrigger (cron/interval), WebhookTrigger (HTTP, HMAC/Bearer auth). Future: filesystem, email, heartbeat |
| `workflow/` | Pipeline engine — WorkflowSpec model, DAG executor (topo-sort + parallel), WorkflowEngine dispatcher. Supports simple and DAG modes |
| `workers/` | Agent Runtime + WorkerRegistry (discovery/liveness) + WorkerNode (worker-role runtime) + DockerWorkerManager (container fleet lifecycle). Future: remote.py |
| `router/` | Admission layer (M4) — TaskRouter: 4 safety invariants, TTL pending queue, capability-scoring seam for Phase 3 |
| `infra/` | Thin async CLI wrappers — `docker.py` (ContainerRuntime; remote fleets via `ssh_host` — see `docs/remote-workers.md`). Future: ssh.py, vagrant.py |

**Planned modules** (not yet implemented):

| Module | Purpose | Phase |
|--------|---------|-------|
| `mcp/` | MCP client/server/controller/registry/proxy | Phase 3 |
| `a2a/` | A2A Gateway for external agent interop | Phase 6 |
| `control/` | Safety (kill switch, FORBIDDEN list), budget tracking, health monitoring, TUI dashboard | Phase 5 |

**Data flow:** Trigger → Event → EventBus → Router (score + invariants) → NATS → Worker (Agent Runtime: LiteLLM + MCP tools) → Result → State.

**Core model:** `Event` (typed messages), `Task` (status machine: pending→assigned→running→completed/failed), `Episode` (agent interaction record for episodic memory), `Envelope` (NATS message wrapper with correlation_id, reply_to, TTL).

**Event subjects (implemented, dot-namespace — no `proctor.` prefix):** `trigger.{terminal,telegram,scheduler,webhook.<source>}` (inputs), `routing.{unmatched,binding_failed,queued,dequeued,expired,rejected}` (admission observability), `task.assign.{worker_id}` (dispatch to a worker), `task.result` (worker → core), `task.{completed,failed}` (outcome), `worker.{registered,heartbeat,offline}` (registry protocol, full-profile heartbeat), `docker_worker.{restarted,failed}` (fleet lifecycle). The same subjects work identically over Local and NATS transport. The architecture doc's `proctor.`-prefixed scheme (`proctor.mcp.proxy.*`, `proctor.operator.*`, `agents.{id}.inbox`) is design intent for later phases, not the current wire contract.

## Tech Stack

- Python 3.12, pydantic 2.x, asyncio, anyio, aiosqlite, nats-py, litellm, tiktoken, mcp SDK, aiohttp, croniter, pyyaml
- Dev: pytest + anyio (NOT asyncio for async tests), ruff, pyrefly
- Uses `src/` layout with hatchling build backend

## Implementation Status

Phase 0 (Foundation) and Phase 1 (MVP) are complete. Phase 2 is complete.

**Completed:** Core models, config loading, EventBus, StateManager, bootstrap, WorkflowSpec, DAG executor, WorkflowEngine, Agent Runtime, Terminal Trigger, end-to-end integration, SchedulerTrigger (cron/interval), TelegramTrigger (Bot API polling), WebhookTrigger (HTTP with HMAC/Bearer auth), EpisodicMemory (SQLite-backed interaction history), transport layer (EventTransport ABC, LocalEventTransport, NATSEventTransport + resolver, contract and Toxiproxy reconnect tests), CI (GitHub Actions: unit + integration-nats jobs), TaskRouter (admission invariants + TTL queue), worker registry + remote dispatch (WorkerRegistry liveness/fencing, capability scoring, WorkerNode worker-role runtime, remote dispatch with rollback/loss-policy/reaper, local-transport and NATS multi-node integration tests), docker worker (ContainerRuntime docker/podman CLI wrapper, DockerWorkerManager fleet lifecycle with fresh-id fencing and poll-loop restart — backoff/jitter/stability-reset/ceiling, bootstrap wiring + log sink, Dockerfile + base worker config, docker-marker integration test).

**Current phase:** Phase 2 complete. The system accepts terminal input, Telegram messages, webhooks, and scheduled events. Executes simple and DAG workflows via LLM. Persists task state and episodic history in SQLite. Events flow over local or NATS transport. Admission layer enforces 4 safety invariants with TTL-pending queue semantics. Tasks requiring specific capabilities are dispatched to remote workers over local or NATS transport, with liveness-based loss handling. The core can also launch and supervise container-based worker fleets directly.

**Next:** Phase 3 — mcp/ (SSH bare-host worker deferred).

## Key Conventions

- All models use pydantic `BaseModel`
- Async everywhere (anyio for triggers/tests, aiosqlite, nats-py async client)
- EventBus rides on the EventTransport abstraction: LocalEventTransport (in-process) or NATSEventTransport (cross-node), selected via `transport: auto|local|nats` in config
- LLM calls abstracted behind `Callable[[str], Awaitable[str]]` interface (mock in tests, real LiteLLM in future)
- Agent Runtime uses tool definitions (`ToolDef`) — tools are dynamic, not hardcoded
- Task state persists to SQLite at every transition (survives process restart)

## Reference Docs

- `docs/plans/2026-03-04-proctor-architecture-design.md` — full architecture with module specs, NATS topics, SQLite schemas, data flows
- `docs/plans/2026-03-04-proctor-phase0-phase1-plan.md` — task-by-task implementation plan for foundation + MVP

## Repo scope & boundaries

- **Этот репо:** `proctor` — git-корень `all_ai_orchestrators/proctor/`, remote `git@github.com:andrei-shtanakov/proctor.git`.
- **Соседи (READ-ONLY reference):** `../arbiter/`, `../atp-platform/`, `../deployer/`, `../dispatcher/`, `../maestro/`, `../libretto/`, `../prograph/`, `../prograph-vault/`, `../robin-runtime/`, `../robin-toolkit/`, `../spec-runner/`, `../spec-runner-vscode/`, `../steward/` — их код не редактировать.
- Нужна правка у соседа → **стоп**: запиши handoff в `../prograph-vault/authored/notes/`
  (кросс-проектное) или `../_cowork_output/` (черновик), не трогай его файлы.
- Кросс-репные контракты — **вендорить пиненой копией внутрь**, не ссылаться наружу.
- Полное правило (SSOT): `../prograph-vault/authored/rules/repo-boundaries.md`.

## Git workflow (у репо есть remote)

- Ветка `<type>/<slug>` → push → `gh pr create`. **Прямые коммиты в `master` запрещены.**
- После открытия PR — прочитать ревью **GitHub Copilot**: валидные замечания исправлять
  новыми коммитами в ту же ветку; невалидные — ответить с обоснованием, **не применять
  вслепую**; итерировать, пока не останется открытых замечаний.
- **Не мержить.** Мерж делает пользователь.
- После мержа пользователем: `git switch master && git pull --ff-only`, затем удалить
  влитую ветку (`git branch -d <branch>`) и `git fetch --prune`; убрать прочие влитые ветки.
- Никогда не делать force-push в общие ветки; не трогать другие репо (см. scope выше).
- Полное правило (SSOT): `../prograph-vault/authored/rules/git-workflow.md`.
