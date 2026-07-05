"""The four critical admission invariants (arch plan M4).

Each check is a pure function: ``None`` means pass, a string is the
human-readable block reason (prefixed with the invariant name).
"""

from proctor.core.globs import patterns_overlap
from proctor.router.models import AgentProfile, RunningTask


def check_concurrency_limit(
    running: list[RunningTask], max_concurrency: int
) -> str | None:
    """Block when the global running count is at the limit."""
    if len(running) >= max_concurrency:
        return f"concurrency_limit: {len(running)}/{max_concurrency} tasks running"
    return None


def check_agent_available(
    profile: AgentProfile, running: list[RunningTask]
) -> str | None:
    """Block when the candidate agent has no free slot.

    Bookkeeping over what TaskRouter admitted — not a live load query
    (AgentRuntime has no slot concept until Phase 3).
    """
    used = sum(1 for r in running if r.agent_id == profile.id)
    if used >= profile.max_slots:
        return (
            f"agent_available: agent {profile.id!r} has no free slots "
            f"({used}/{profile.max_slots})"
        )
    return None


def check_scope_isolation(scope: list[str], running: list[RunningTask]) -> str | None:
    """Block when any scope glob overlaps a running task's scope."""
    for r in running:
        for ours in scope:
            for theirs in r.scope:
                if patterns_overlap(ours, theirs):
                    return (
                        f"scope_isolation: {ours!r} overlaps {theirs!r} "
                        f"held by task {r.task_id}"
                    )
    return None


def check_branch_not_locked(
    branch: str | None, running: list[RunningTask]
) -> str | None:
    """Block when the exact branch is held by a running task."""
    if branch is None:
        return None
    for r in running:
        if r.branch == branch:
            return f"branch_not_locked: branch {branch!r} held by task {r.task_id}"
    return None
