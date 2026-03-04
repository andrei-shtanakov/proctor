"""WorkflowEngine — dispatcher that routes WorkflowSpecs to executors.

Supports simple (single LLM call) and DAG (step graph) modes in Phase 1.
FSM and orchestrator modes return errors until Phase 4.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from proctor.workflow.dag import DAGExecutor, StepResult, topo_sort
from proctor.workflow.spec import Step, WorkflowMode, WorkflowSpec

logger = logging.getLogger(__name__)

LLMCall = Callable[[str], Awaitable[str]]


class WorkflowResult(BaseModel):
    """Result of executing a workflow."""

    workflow_id: str
    output: Any | None = None
    step_results: dict[str, StepResult] | None = None
    error: str | None = None


class WorkflowEngine:
    """Dispatcher that routes WorkflowSpecs to the correct executor.

    In Phase 1, supports SIMPLE (single LLM call) and DAG
    (topological step execution) modes.
    """

    def __init__(self, llm_call: LLMCall) -> None:
        self._llm_call = llm_call

    async def execute(self, spec: WorkflowSpec) -> WorkflowResult:
        """Execute a workflow based on its mode."""
        match spec.mode:
            case WorkflowMode.SIMPLE:
                return await self._execute_simple(spec)
            case WorkflowMode.DAG:
                return await self._execute_dag(spec)
            case _:
                return WorkflowResult(
                    workflow_id=spec.workflow_id,
                    error=f"Mode '{spec.mode}' not supported yet",
                )

    async def _execute_simple(self, spec: WorkflowSpec) -> WorkflowResult:
        """Execute a simple workflow: single LLM call with prompt."""
        if not spec.prompt:
            return WorkflowResult(
                workflow_id=spec.workflow_id,
                error="Simple workflow requires a prompt",
            )

        try:
            output = await self._llm_call(spec.prompt)
            return WorkflowResult(
                workflow_id=spec.workflow_id,
                output=output,
            )
        except Exception as exc:
            logger.error(
                "Simple workflow %s failed: %s",
                spec.workflow_id,
                exc,
                exc_info=True,
            )
            return WorkflowResult(
                workflow_id=spec.workflow_id,
                error=str(exc),
            )

    async def _execute_dag(self, spec: WorkflowSpec) -> WorkflowResult:
        """Execute a DAG workflow via DAGExecutor."""
        if not spec.steps:
            return WorkflowResult(
                workflow_id=spec.workflow_id,
                error="DAG workflow requires steps",
            )

        async def step_runner(step: Step, results: dict[str, StepResult]) -> StepResult:
            """Run a single DAG step by calling LLM with step context."""
            # Build prompt from step inputs and dependency outputs
            dep_context = ""
            for dep_id in step.depends_on:
                dep_result = results.get(dep_id)
                if dep_result and dep_result.output is not None:
                    dep_context += f"\n[{dep_id}]: {dep_result.output}"

            prompt = step.description or step.id
            if dep_context:
                prompt = f"{prompt}\n\nContext:{dep_context}"

            output = await self._llm_call(prompt)
            return StepResult(step_id=step.id, output=output)

        try:
            executor = DAGExecutor(steps=spec.steps, step_runner=step_runner)
            results = await executor.execute()

            # Find the last step's output (topological order)
            ordered = topo_sort(spec.steps)
            last_step_id = ordered[-1].id
            last_result = results.get(last_step_id)
            final_output = last_result.output if last_result else None

            return WorkflowResult(
                workflow_id=spec.workflow_id,
                output=final_output,
                step_results=results,
            )
        except Exception as exc:
            logger.error(
                "DAG workflow %s failed: %s",
                spec.workflow_id,
                exc,
                exc_info=True,
            )
            return WorkflowResult(
                workflow_id=spec.workflow_id,
                error=str(exc),
            )
