"""Data models for the TaskRouter admission layer."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from proctor.core.models import Task
from proctor.workflow.spec import WorkflowSpec


class AgentProfile(BaseModel):
    """An execution candidate. v1: the single local AgentRuntime."""

    id: str
    capabilities: list[str] = Field(default_factory=list)
    max_slots: int = 4


class Candidate(BaseModel):
    """A scored agent candidate for a task."""

    profile: AgentProfile
    score: float


class RunningTask(BaseModel):
    """TaskRouter's bookkeeping view of an admitted task."""

    task_id: str
    agent_id: str
    scope: list[str] = Field(default_factory=list)
    branch: str | None = None


class AdmitDecision(BaseModel):
    """Outcome of TaskRouter.admit()."""

    verdict: Literal["admitted", "queued", "rejected"]
    reason: str | None = None
    agent_id: str | None = None


class QueueEntry(BaseModel):
    """A blocked task waiting in the pending queue.

    ``expires_at`` is the admit-TTL — deliberately NOT ``Task.deadline``
    (run-deadline, a different lifecycle stage). ``trigger_source`` is
    opaque passthrough so bootstrap can build the Episode later.
    """

    task: Task
    spec: WorkflowSpec
    trigger_source: str
    enqueued_at: datetime
    expires_at: datetime
    reason: str
    not_before: datetime | None = None
    agent_id: str | None = None  # set when a dequeue reserves a slot
