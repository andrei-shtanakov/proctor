"""Tests for DAG executor: topo_sort and DAGExecutor."""

import time

import anyio
import pytest

from proctor.workflow.dag import DAGExecutor, StepResult, topo_sort
from proctor.workflow.spec import Step

# ── topo_sort tests ──────────────────────────────────────────────


class TestTopoSortLinearChain:
    def test_linear_abc(self) -> None:
        """A -> B -> C produces [A, B, C]."""
        steps = [
            Step(id="a"),
            Step(id="b", depends_on=["a"]),
            Step(id="c", depends_on=["b"]),
        ]
        result = topo_sort(steps)
        ids = [s.id for s in result]
        assert ids == ["a", "b", "c"]

    def test_linear_reverse_input(self) -> None:
        """Input order doesn't matter — dependencies determine output."""
        steps = [
            Step(id="c", depends_on=["b"]),
            Step(id="b", depends_on=["a"]),
            Step(id="a"),
        ]
        result = topo_sort(steps)
        ids = [s.id for s in result]
        assert ids == ["a", "b", "c"]


class TestTopoSortParallel:
    def test_parallel_branches(self) -> None:
        """Root -> (left, right) -> join."""
        steps = [
            Step(id="root"),
            Step(id="left", depends_on=["root"]),
            Step(id="right", depends_on=["root"]),
            Step(id="join", depends_on=["left", "right"]),
        ]
        result = topo_sort(steps)
        ids = [s.id for s in result]
        # root must be first, join must be last
        assert ids[0] == "root"
        assert ids[-1] == "join"
        # left and right must come after root and before join
        assert set(ids[1:3]) == {"left", "right"}

    def test_independent_steps(self) -> None:
        """Steps with no dependencies can appear in any order."""
        steps = [
            Step(id="x"),
            Step(id="y"),
            Step(id="z"),
        ]
        result = topo_sort(steps)
        assert len(result) == 3
        assert {s.id for s in result} == {"x", "y", "z"}


class TestTopoSortCycleDetection:
    def test_simple_cycle(self) -> None:
        """A depends on B, B depends on A."""
        steps = [
            Step(id="a", depends_on=["b"]),
            Step(id="b", depends_on=["a"]),
        ]
        with pytest.raises(ValueError, match="[Cc]ycle"):
            topo_sort(steps)

    def test_self_cycle(self) -> None:
        """Step depends on itself."""
        steps = [Step(id="a", depends_on=["a"])]
        with pytest.raises(ValueError, match="[Cc]ycle"):
            topo_sort(steps)

    def test_three_node_cycle(self) -> None:
        """A -> B -> C -> A."""
        steps = [
            Step(id="a", depends_on=["c"]),
            Step(id="b", depends_on=["a"]),
            Step(id="c", depends_on=["b"]),
        ]
        with pytest.raises(ValueError, match="[Cc]ycle"):
            topo_sort(steps)


# ── DAGExecutor tests ────────────────────────────────────────────


async def _ok_runner(step: Step, results: dict[str, StepResult]) -> StepResult:
    """Simple runner that returns step id as output."""
    return StepResult(step_id=step.id, output=f"done-{step.id}")


async def _slow_runner(step: Step, results: dict[str, StepResult]) -> StepResult:
    """Runner that records execution order with tiny delays."""
    await anyio.sleep(0.01)
    return StepResult(step_id=step.id, output=f"done-{step.id}")


async def _failing_runner(step: Step, results: dict[str, StepResult]) -> StepResult:
    """Runner that fails on step 'bad'."""
    if step.id == "bad":
        return StepResult(step_id=step.id, error="step failed")
    return StepResult(step_id=step.id, output=f"done-{step.id}")


async def _exception_runner(step: Step, results: dict[str, StepResult]) -> StepResult:
    """Runner that raises exception on step 'boom'."""
    if step.id == "boom":
        msg = "unexpected crash"
        raise RuntimeError(msg)
    return StepResult(step_id=step.id, output=f"done-{step.id}")


class TestDAGExecutorSingleStep:
    @pytest.mark.anyio()
    async def test_single_step(self) -> None:
        steps = [Step(id="only")]
        executor = DAGExecutor(steps=steps, step_runner=_ok_runner)
        results = await executor.execute()

        assert len(results) == 1
        assert results["only"].output == "done-only"
        assert results["only"].error is None


class TestDAGExecutorLinear:
    @pytest.mark.anyio()
    async def test_linear_dag(self) -> None:
        """A -> B -> C executes in order with results passed."""
        order: list[str] = []

        async def tracking_runner(
            step: Step, results: dict[str, StepResult]
        ) -> StepResult:
            order.append(step.id)
            return StepResult(step_id=step.id, output=f"done-{step.id}")

        steps = [
            Step(id="a"),
            Step(id="b", depends_on=["a"]),
            Step(id="c", depends_on=["b"]),
        ]
        executor = DAGExecutor(steps=steps, step_runner=tracking_runner)
        results = await executor.execute()

        assert order == ["a", "b", "c"]
        assert results["a"].output == "done-a"
        assert results["b"].output == "done-b"
        assert results["c"].output == "done-c"


class TestDAGExecutorParallel:
    @pytest.mark.anyio()
    async def test_parallel_branches(self) -> None:
        """Root -> (left, right) -> join. Left and right run in parallel."""
        timestamps: dict[str, float] = {}

        async def timed_runner(
            step: Step, results: dict[str, StepResult]
        ) -> StepResult:
            timestamps[step.id] = time.monotonic()
            await anyio.sleep(0.05)
            return StepResult(step_id=step.id, output=f"done-{step.id}")

        steps = [
            Step(id="root"),
            Step(id="left", depends_on=["root"]),
            Step(id="right", depends_on=["root"]),
            Step(id="join", depends_on=["left", "right"]),
        ]
        executor = DAGExecutor(steps=steps, step_runner=timed_runner)
        results = await executor.execute()

        assert all(r.error is None for r in results.values())
        assert len(results) == 4

        # root ran before left and right
        assert timestamps["root"] < timestamps["left"]
        assert timestamps["root"] < timestamps["right"]
        # join ran after both left and right
        assert timestamps["join"] > timestamps["left"]
        assert timestamps["join"] > timestamps["right"]
        # left and right started at roughly the same time (parallel)
        assert abs(timestamps["left"] - timestamps["right"]) < 0.03


class TestDAGExecutorFailurePropagation:
    @pytest.mark.anyio()
    async def test_failure_skips_dependents(self) -> None:
        """When 'bad' fails, 'after-bad' is skipped."""
        steps = [
            Step(id="ok"),
            Step(id="bad", depends_on=["ok"]),
            Step(id="after-bad", depends_on=["bad"]),
        ]
        executor = DAGExecutor(steps=steps, step_runner=_failing_runner)
        results = await executor.execute()

        assert results["ok"].output == "done-ok"
        assert results["bad"].error == "step failed"
        assert results["after-bad"].error is not None
        assert "Dependency" in results["after-bad"].error

    @pytest.mark.anyio()
    async def test_exception_in_runner(self) -> None:
        """Runner exception is caught and stored as error."""
        steps = [
            Step(id="boom"),
            Step(id="after", depends_on=["boom"]),
        ]
        executor = DAGExecutor(steps=steps, step_runner=_exception_runner)
        results = await executor.execute()

        assert results["boom"].error == "unexpected crash"
        assert results["after"].error is not None
        assert "Dependency" in results["after"].error

    @pytest.mark.anyio()
    async def test_parallel_failure_only_affects_dependents(self) -> None:
        """Failure in one branch doesn't affect independent branch."""
        steps = [
            Step(id="root"),
            Step(id="bad", depends_on=["root"]),
            Step(id="good", depends_on=["root"]),
            Step(id="after-bad", depends_on=["bad"]),
            Step(id="after-good", depends_on=["good"]),
        ]
        executor = DAGExecutor(steps=steps, step_runner=_failing_runner)
        results = await executor.execute()

        assert results["root"].output == "done-root"
        assert results["bad"].error == "step failed"
        assert results["after-bad"].error is not None
        assert results["good"].output == "done-good"
        assert results["after-good"].output == "done-after-good"

    @pytest.mark.anyio()
    async def test_cycle_rejected(self) -> None:
        """DAGExecutor rejects cyclic graphs at execute time."""
        steps = [
            Step(id="a", depends_on=["b"]),
            Step(id="b", depends_on=["a"]),
        ]
        executor = DAGExecutor(steps=steps, step_runner=_ok_runner)
        with pytest.raises(ValueError, match="[Cc]ycle"):
            await executor.execute()


class TestDAGExecutorResults:
    @pytest.mark.anyio()
    async def test_results_contain_all_steps(self) -> None:
        steps = [
            Step(id="a"),
            Step(id="b", depends_on=["a"]),
            Step(id="c"),
        ]
        executor = DAGExecutor(steps=steps, step_runner=_ok_runner)
        results = await executor.execute()

        assert set(results.keys()) == {"a", "b", "c"}

    @pytest.mark.anyio()
    async def test_runner_receives_prior_results(self) -> None:
        """Step runner receives results from completed dependencies."""
        received: dict[str, dict[str, StepResult]] = {}

        async def capture_runner(
            step: Step, results: dict[str, StepResult]
        ) -> StepResult:
            received[step.id] = dict(results)
            return StepResult(step_id=step.id, output=f"done-{step.id}")

        steps = [
            Step(id="a"),
            Step(id="b", depends_on=["a"]),
        ]
        executor = DAGExecutor(steps=steps, step_runner=capture_runner)
        await executor.execute()

        assert "a" not in received.get("a", {})
        assert "a" in received["b"]
        assert received["b"]["a"].output == "done-a"


class TestStepResultModel:
    def test_success_result(self) -> None:
        r = StepResult(step_id="s1", output="hello")
        assert r.step_id == "s1"
        assert r.output == "hello"
        assert r.error is None

    def test_error_result(self) -> None:
        r = StepResult(step_id="s2", error="failed")
        assert r.step_id == "s2"
        assert r.output is None
        assert r.error == "failed"

    def test_serialization(self) -> None:
        r = StepResult(step_id="s3", output={"key": "val"})
        data = r.model_dump()
        restored = StepResult.model_validate(data)
        assert restored.step_id == "s3"
        assert restored.output == {"key": "val"}


class TestImports:
    def test_import_from_package(self) -> None:
        """DAG types are importable from proctor.workflow."""
        from proctor.workflow import (
            DAGExecutor,
            StepResult,
            topo_sort,
        )

        assert DAGExecutor is not None
        assert StepResult is not None
        assert topo_sort is not None
