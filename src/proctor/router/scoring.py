"""Capability scoring — the seam Phase 3 fills with real candidates."""

from proctor.router.models import AgentProfile, Candidate
from proctor.workflow.spec import WorkflowSpec


def score_candidates(spec: WorkflowSpec, agents: list[AgentProfile]) -> list[Candidate]:
    """Score agents for a spec. v1: every agent scores 1.0, order kept.

    Phase 3 replaces the body with capability matching against the
    worker registry; the signature is the contract.
    """
    return [Candidate(profile=agent, score=1.0) for agent in agents]
