"""WorkflowSpec — universal workflow specification model.

Describes all pipeline types: simple (single prompt), DAG (directed
acyclic graph of steps), FSM (Phase 4), and orchestrator (Phase 4).
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WorkflowMode(StrEnum):
    """Pipeline execution mode."""

    SIMPLE = "simple"
    DAG = "dag"
    FSM = "fsm"
    ORCHESTRATOR = "orchestrator"


class StepType(StrEnum):
    """Type of work a step performs."""

    LLM = "llm"
    SHELL = "shell"
    HTTP = "http"
    SYSTEM = "system"
    WAIT_EVENT = "wait_event"


class StepRetry(BaseModel):
    """Retry policy for an individual step."""

    max_attempts: int = 1
    delay_seconds: int = 5


class Step(BaseModel):
    """A single unit of work in a DAG workflow."""

    id: str
    type: StepType = StepType.LLM
    description: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    tool: str | None = None
    retry: StepRetry = Field(default_factory=StepRetry)
    conditions: dict[str, Any] = Field(default_factory=dict)


class WorkflowPolicies(BaseModel):
    """Shared execution policies for a workflow."""

    max_runtime_seconds: int = 900
    max_retries: int = 2
    retry_delay_seconds: int = 60
    require_security_review: bool = False


class WorkflowSpec(BaseModel):
    """Universal workflow specification.

    Supports simple (prompt-only), DAG (step graph), FSM (state machine),
    and orchestrator (multi-agent) modes via a single model.
    """

    workflow_id: str
    version: str = "1.0.0"
    mode: WorkflowMode
    description: str = ""
    prompt: str | None = None
    steps: list[Step] = Field(default_factory=list)
    states: dict[str, Any] = Field(default_factory=dict)
    orchestrator: dict[str, Any] = Field(default_factory=dict)
    policies: WorkflowPolicies = Field(default_factory=WorkflowPolicies)
    security: dict[str, Any] = Field(default_factory=dict)
    channels: dict[str, Any] = Field(default_factory=dict)
