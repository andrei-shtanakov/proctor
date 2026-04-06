```markdown
# Implement EpisodicMemory Store — Tasks Specification

## Milestone 1: Episode Model & Schema

### TASK-001: Define Episode Pydantic Model
🔴 P0 | ⬜ TODO | Est: 30m

Add the `Episode` pydantic model to `src/proctor/core/models.py` (or directly in `memory.py`). Fields: `id` (str, UUID default), `timestamp` (datetime), `trigger_type` (str), `user_input` (str), `agent_response` (str), `workflow_result` (dict | None).

**Checklist:**
- [ ] Define `Episode(BaseModel)` with all required fields
- [ ] Add sensible defaults (uuid4 for id, utcnow for timestamp)
- [ ] Ensure JSON-serializable (workflow_result as dict | None)

**Depends on:**

### TASK-002: Define SQLite Schema and SQL Constants
🔴 P0 | ⬜ TODO | Est: 30m

Create `src/proctor/core/memory.py` with the SQLite DDL and DML constants following the pattern in `state.py`. Table: `episodes` with columns matching the Episode model. Include CREATE TABLE, INSERT, SELECT by id, SELECT with LIMIT, and search (LIKE on user_input + agent_response).

**Checklist:**
- [ ] `_CREATE_EPISODES` DDL with proper column types and PRIMARY KEY
- [ ] `_INSERT_EPISODE` parameterized INSERT statement
- [ ] `_SELECT_EPISODE` by id
- [ ] `_SELECT_EPISODES_LIST` with ORDER BY timestamp DESC and LIMIT
- [ ] `_SEARCH_EPISODES` with LIKE on user_input and agent_response columns
- [ ] Index on timestamp column

**Depends on:** TASK-001

## Milestone 2: EpisodicMemory Class

### TASK-003: Implement EpisodicMemory Init and Lifecycle
🔴 P0 | 🔄 IN_PROGRESS | Est: 1h

Implement `EpisodicMemory` class with `__init__(db_path)`, `initialize()`, and `close()` methods. Follow `StateManager` patterns: WAL mode, `aiosqlite.Row` row factory, parent dir creation, assert-based connection guards.

**Checklist:**
- [ ] `__init__` accepts `Path` for db_path, stores `_db: aiosqlite.Connection | None`
- [ ] `initialize()` creates parent dirs, opens connection, sets WAL + row_factory, runs DDL
- [ ] `close()` safely closes connection and sets `_db = None`
- [ ] Add module-level logger

**Depends on:** TASK-002

### TASK-004: Implement save_episode and get_episode
🔴 P0 | 🔄 IN_PROGRESS | Est: 1h

Implement `save_episode(episode: Episode) -> None` and `get_episode(episode_id: str) -> Episode | None`. Save serializes the model to SQL params and commits. Get fetches by id and returns None if not found.

**Checklist:**
- [ ] `save_episode` inserts episode row with all fields, commits
- [ ] `get_episode` queries by id, returns `Episode` or `None`
- [ ] Add `_row_to_episode` helper function to convert `aiosqlite.Row` to `Episode`
- [ ] Handle `workflow_result` JSON serialization/deserialization (json.dumps/loads)

**Depends on:** TASK-003

### TASK-005: Implement list_episodes and search_episodes
🟠 P1 | ⬜ TODO | Est: 1h

Implement `list_episodes(limit: int = 50) -> list[Episode]` (most recent first) and `search_episodes(query: str, limit: int = 20) -> list[Episode]` (LIKE search on user_input and agent_response).

**Checklist:**
- [ ] `list_episodes` returns episodes ordered by timestamp DESC with limit
- [ ] `search_episodes` uses SQL LIKE with `%query%` on user_input and agent_response
- [ ] Both methods return `list[Episode]`
- [ ] Default limits are sensible (50 for list, 20 for search)

**Depends on:** TASK-004

## Milestone 3: Testing

### TASK-006: Tests for Episode Model and Lifecycle
🔴 P0 | 🔄 IN_PROGRESS | Est: 1h

Create `tests/test_core/test_memory.py` with tests for Episode model creation, EpisodicMemory initialize/close, and table creation. Use tmp_path fixture for DB, anyio for async.

**Checklist:**
- [ ] Test Episode model instantiation with defaults
- [ ] Test Episode model with all fields explicit
- [ ] Test EpisodicMemory initialize creates DB file and episodes table
- [ ] Test EpisodicMemory close is idempotent
- [ ] Use `@pytest.mark.anyio` for async tests, `tmp_path` for DB path

**Depends on:** TASK-004

### TASK-007: Tests for CRUD Operations
🔴 P0 | 🔄 IN_PROGRESS | Est: 1-2h

Test save_episode, get_episode, list_episodes, and search_episodes. Cover happy paths, not-found cases, ordering, limits, and search matching.

**Checklist:**
- [ ] Test save and retrieve round-trip (all fields preserved)
- [ ] Test get_episode returns None for nonexistent id
- [ ] Test list_episodes returns newest first
- [ ] Test list_episodes respects limit parameter
- [ ] Test search_episodes matches on user_input
- [ ] Test search_episodes matches on agent_response
- [ ] Test search_episodes returns empty list for no matches
- [ ] Test workflow_result None and non-None serialization

**Depends on:** TASK-005, TASK-006

## Milestone 4: Integration

### TASK-008: Wire EpisodicMemory into Bootstrap
🟠 P1 | ⬜ TODO | Est: 30m

Add EpisodicMemory initialization and shutdown to the application bootstrap sequence. Decide whether to use a separate `memory.db` or share `state.db` — follow existing config patterns.

**Checklist:**
- [ ] Add memory DB path to config (or derive from existing data_dir)
- [ ] Initialize EpisodicMemory in bootstrap `startup()`
- [ ] Close EpisodicMemory in bootstrap `shutdown()`
- [ ] Verify existing tests still pass after wiring

**Depends on:** TASK-005
```
