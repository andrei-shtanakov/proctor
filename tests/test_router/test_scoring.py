"""Tests for the v1 scoring seam."""

from proctor.router.models import AgentProfile
from proctor.router.scoring import score_candidates
from proctor.workflow.spec import WorkflowMode, WorkflowSpec


def _spec() -> WorkflowSpec:
    return WorkflowSpec(workflow_id="w", mode=WorkflowMode.SIMPLE)


def test_single_agent_scores_one() -> None:
    agents = [AgentProfile(id="local", max_slots=4)]
    candidates = score_candidates(_spec(), agents)
    assert len(candidates) == 1
    assert candidates[0].profile.id == "local"
    assert candidates[0].score == 1.0


def test_order_preserved() -> None:
    agents = [AgentProfile(id="a"), AgentProfile(id="b")]
    candidates = score_candidates(_spec(), agents)
    assert [c.profile.id for c in candidates] == ["a", "b"]


def test_empty_agents() -> None:
    assert score_candidates(_spec(), []) == []
