"""Unit tests for the four critical admission invariants."""

from proctor.router.invariants import (
    check_agent_available,
    check_branch_not_locked,
    check_concurrency_limit,
    check_scope_isolation,
)
from proctor.router.models import AgentProfile, RunningTask


def _running(n: int, agent_id: str = "local") -> list[RunningTask]:
    return [RunningTask(task_id=f"t{i}", agent_id=agent_id) for i in range(n)]


class TestConcurrencyLimit:
    def test_below_limit_passes(self) -> None:
        assert check_concurrency_limit(_running(3), 4) is None

    def test_exactly_at_limit_blocks(self) -> None:
        reason = check_concurrency_limit(_running(4), 4)
        assert reason is not None
        assert "concurrency_limit" in reason

    def test_empty_running_passes(self) -> None:
        assert check_concurrency_limit([], 1) is None


class TestAgentAvailable:
    def test_free_slot_passes(self) -> None:
        profile = AgentProfile(id="local", max_slots=2)
        assert check_agent_available(profile, _running(1)) is None

    def test_full_slots_block(self) -> None:
        profile = AgentProfile(id="local", max_slots=2)
        reason = check_agent_available(profile, _running(2))
        assert reason is not None
        assert "agent_available" in reason

    def test_other_agents_tasks_do_not_count(self) -> None:
        profile = AgentProfile(id="local", max_slots=1)
        running = _running(3, agent_id="remote")
        assert check_agent_available(profile, running) is None


class TestScopeIsolation:
    def test_empty_scope_never_conflicts(self) -> None:
        running = [RunningTask(task_id="t", agent_id="a", scope=["src/**"])]
        assert check_scope_isolation([], running) is None

    def test_running_without_scope_never_conflicts(self) -> None:
        running = [RunningTask(task_id="t", agent_id="a")]
        assert check_scope_isolation(["src/**"], running) is None

    def test_overlapping_globs_block(self) -> None:
        running = [RunningTask(task_id="t", agent_id="a", scope=["src/**"])]
        reason = check_scope_isolation(["src/foo.py"], running)
        assert reason is not None
        assert "scope_isolation" in reason
        assert "t" in reason

    def test_disjoint_globs_pass(self) -> None:
        running = [RunningTask(task_id="t", agent_id="a", scope=["docs/**"])]
        assert check_scope_isolation(["src/**"], running) is None


class TestBranchNotLocked:
    def test_none_branch_passes(self) -> None:
        running = [RunningTask(task_id="t", agent_id="a", branch="release")]
        assert check_branch_not_locked(None, running) is None

    def test_same_branch_blocks(self) -> None:
        running = [RunningTask(task_id="t", agent_id="a", branch="release")]
        reason = check_branch_not_locked("release", running)
        assert reason is not None
        assert "branch_not_locked" in reason

    def test_different_branch_passes(self) -> None:
        running = [RunningTask(task_id="t", agent_id="a", branch="main")]
        assert check_branch_not_locked("release", running) is None
