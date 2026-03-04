"""DAG Executor — topological sort and parallel step execution.

Executes a list of Steps respecting dependency order. Uses DFS-based
topological sort for ordering and anyio TaskGroup for parallel
execution of independent steps.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from pydantic import BaseModel

from proctor.workflow.spec import Step

logger = logging.getLogger(__name__)


class StepResult(BaseModel):
    """Result of executing a single DAG step."""

    step_id: str
    output: Any | None = None
    error: str | None = None


StepRunner = Callable[[Step, dict[str, StepResult]], Awaitable[StepResult]]


def topo_sort(steps: list[Step]) -> list[Step]:
    """Topological sort with DFS-based cycle detection.

    Returns steps in dependency order. Raises ValueError if a
    cycle is detected.
    """
    step_map = {s.id: s for s in steps}
    visited: set[str] = set()
    in_stack: set[str] = set()
    order: list[str] = []

    def _dfs(node_id: str) -> None:
        if node_id in in_stack:
            raise ValueError(f"Cycle detected involving step '{node_id}'")
        if node_id in visited:
            return

        in_stack.add(node_id)
        step = step_map.get(node_id)
        if step is not None:
            for dep_id in step.depends_on:
                _dfs(dep_id)

        in_stack.remove(node_id)
        visited.add(node_id)
        order.append(node_id)

    for step in steps:
        _dfs(step.id)

    return [step_map[sid] for sid in order]


class DAGExecutor:
    """Executes DAG steps with parallel execution of independent steps.

    Uses anyio.Event per step for dependency signaling and
    anyio task groups for concurrent execution.
    """

    def __init__(
        self,
        steps: list[Step],
        step_runner: StepRunner,
    ) -> None:
        self._steps = steps
        self._step_runner = step_runner

    async def execute(self) -> dict[str, StepResult]:
        """Run all steps respecting dependencies.

        1. Validate DAG (cycle detection via topo_sort)
        2. Create anyio.Event per step for signaling
        3. Launch all steps concurrently in a task group
        4. Each step waits for dependency events before executing
        5. If a dependency failed, the step is skipped
        """
        # Validate — raises ValueError on cycles
        topo_sort(self._steps)

        results: dict[str, StepResult] = {}
        events: dict[str, anyio.Event] = {s.id: anyio.Event() for s in self._steps}

        async def _run_step(step: Step) -> None:
            # Wait for all dependencies
            for dep_id in step.depends_on:
                if dep_id in events:
                    await events[dep_id].wait()

            # Check if any dependency failed
            for dep_id in step.depends_on:
                dep_result = results.get(dep_id)
                if dep_result is not None and dep_result.error is not None:
                    results[step.id] = StepResult(
                        step_id=step.id,
                        error=(f"Dependency '{dep_id}' failed: {dep_result.error}"),
                    )
                    events[step.id].set()
                    return

            # Execute the step
            try:
                result = await self._step_runner(step, results)
                results[step.id] = result
            except Exception as exc:
                logger.error("Step %s failed: %s", step.id, exc, exc_info=True)
                results[step.id] = StepResult(
                    step_id=step.id,
                    error=str(exc),
                )
            finally:
                events[step.id].set()

        async with anyio.create_task_group() as tg:
            for step in self._steps:
                tg.start_soon(_run_step, step)

        return results
