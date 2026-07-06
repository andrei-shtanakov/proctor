"""Tests for capability scoring."""

from proctor.router.models import AgentProfile
from proctor.router.scoring import score_candidates
from proctor.workflow.spec import WorkflowMode, WorkflowSpec


def _spec(requires: list[str] | None = None) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="w", mode=WorkflowMode.SIMPLE, requires=requires or []
    )


def test_capability_filter() -> None:
    agents = [
        AgentProfile(id="py", capabilities=["python"], max_slots=2),
        AgentProfile(id="sh", capabilities=["shell"], max_slots=2),
    ]
    got = score_candidates(_spec(["python"]), agents)
    assert [c.profile.id for c in got] == ["py"]


def test_empty_requires_matches_all() -> None:
    agents = [AgentProfile(id="a"), AgentProfile(id="b")]
    assert len(score_candidates(_spec(), agents)) == 2


def test_free_slot_ranking() -> None:
    agents = [
        AgentProfile(id="busy", max_slots=4),
        AgentProfile(id="idle", max_slots=4),
    ]
    got = score_candidates(_spec(), agents, used_slots={"busy": 3})
    assert [c.profile.id for c in got] == ["idle", "busy"]
    assert got[0].score == 4.0
    assert got[1].score == 1.0


def test_zero_free_slots_kept() -> None:
    # agent_available (one place) decides, not the scorer
    agents = [AgentProfile(id="full", max_slots=2)]
    got = score_candidates(_spec(), agents, used_slots={"full": 2})
    assert [c.profile.id for c in got] == ["full"]


def test_stable_order_on_ties() -> None:
    agents = [AgentProfile(id="a"), AgentProfile(id="b")]
    got = score_candidates(_spec(), agents)
    assert [c.profile.id for c in got] == ["a", "b"]


def test_no_agents() -> None:
    assert score_candidates(_spec(), []) == []
