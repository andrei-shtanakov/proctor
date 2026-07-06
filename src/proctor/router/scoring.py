"""Capability scoring: filter by requires, rank by free slots."""

from collections.abc import Mapping

from proctor.router.models import AgentProfile, Candidate
from proctor.workflow.spec import WorkflowSpec


def score_candidates(
    spec: WorkflowSpec,
    agents: list[AgentProfile],
    used_slots: Mapping[str, int] | None = None,
) -> list[Candidate]:
    """Candidates able to run ``spec``, best (most free slots) first.

    Zero-free-slot agents stay in the list: the agent_available
    invariant is the single place that rejects them. Sort is stable —
    ties keep registry order.
    """
    used = used_slots or {}
    required = set(spec.requires)
    eligible = [a for a in agents if required <= set(a.capabilities)]
    scored = [
        Candidate(profile=a, score=float(a.max_slots - used.get(a.id, 0)))
        for a in eligible
    ]
    return sorted(scored, key=lambda c: -c.score)
