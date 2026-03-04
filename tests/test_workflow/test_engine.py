"""Tests for WorkflowEngine dispatcher."""

import pytest

from proctor.workflow.dag import StepResult
from proctor.workflow.engine import WorkflowEngine, WorkflowResult
from proctor.workflow.spec import Step, WorkflowMode, WorkflowSpec

# ── Helpers ─────────────────────────────────────────────────────


async def _echo_llm(prompt: str) -> str:
    """LLM stub that echoes prompt back."""
    return f"response:{prompt}"


async def _failing_llm(prompt: str) -> str:
    """LLM stub that always raises."""
    msg = "LLM unavailable"
    raise RuntimeError(msg)


def _simple_spec(
    prompt: str = "Hello",
    workflow_id: str = "test-simple",
) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=workflow_id,
        mode=WorkflowMode.SIMPLE,
        prompt=prompt,
    )


def _dag_spec(
    steps: list[Step] | None = None,
    workflow_id: str = "test-dag",
) -> WorkflowSpec:
    if steps is None:
        steps = [
            Step(id="a", description="step A"),
            Step(id="b", description="step B", depends_on=["a"]),
        ]
    return WorkflowSpec(
        workflow_id=workflow_id,
        mode=WorkflowMode.DAG,
        steps=steps,
    )


# ── WorkflowResult model tests ─────────────────────────────────


class TestWorkflowResultModel:
    def test_success_result(self) -> None:
        r = WorkflowResult(workflow_id="w1", output="done")
        assert r.workflow_id == "w1"
        assert r.output == "done"
        assert r.error is None
        assert r.step_results is None

    def test_error_result(self) -> None:
        r = WorkflowResult(workflow_id="w2", error="failed")
        assert r.workflow_id == "w2"
        assert r.output is None
        assert r.error == "failed"

    def test_with_step_results(self) -> None:
        sr = {"s1": StepResult(step_id="s1", output="ok")}
        r = WorkflowResult(workflow_id="w3", output="final", step_results=sr)
        assert r.step_results is not None
        assert r.step_results["s1"].output == "ok"

    def test_serialization(self) -> None:
        r = WorkflowResult(workflow_id="w4", output="data")
        data = r.model_dump()
        restored = WorkflowResult.model_validate(data)
        assert restored.workflow_id == "w4"
        assert restored.output == "data"


# ── Simple workflow tests ───────────────────────────────────────


class TestSimpleWorkflow:
    @pytest.mark.anyio()
    async def test_simple_returns_llm_output(self) -> None:
        engine = WorkflowEngine(llm_call=_echo_llm)
        result = await engine.execute(_simple_spec("Hello"))

        assert result.workflow_id == "test-simple"
        assert result.output == "response:Hello"
        assert result.error is None

    @pytest.mark.anyio()
    async def test_simple_no_prompt_returns_error(self) -> None:
        spec = WorkflowSpec(
            workflow_id="no-prompt",
            mode=WorkflowMode.SIMPLE,
            prompt=None,
        )
        engine = WorkflowEngine(llm_call=_echo_llm)
        result = await engine.execute(spec)

        assert result.error is not None
        assert "prompt" in result.error.lower()

    @pytest.mark.anyio()
    async def test_simple_llm_failure(self) -> None:
        engine = WorkflowEngine(llm_call=_failing_llm)
        result = await engine.execute(_simple_spec())

        assert result.error is not None
        assert "unavailable" in result.error.lower()
        assert result.output is None

    @pytest.mark.anyio()
    async def test_simple_empty_prompt_returns_error(self) -> None:
        spec = WorkflowSpec(
            workflow_id="empty-prompt",
            mode=WorkflowMode.SIMPLE,
            prompt="",
        )
        engine = WorkflowEngine(llm_call=_echo_llm)
        result = await engine.execute(spec)

        assert result.error is not None
        assert "prompt" in result.error.lower()


# ── DAG workflow tests ──────────────────────────────────────────


class TestDAGWorkflow:
    @pytest.mark.anyio()
    async def test_dag_linear_chain(self) -> None:
        engine = WorkflowEngine(llm_call=_echo_llm)
        result = await engine.execute(_dag_spec())

        assert result.error is None
        assert result.output is not None
        assert result.step_results is not None
        assert len(result.step_results) == 2
        assert "a" in result.step_results
        assert "b" in result.step_results

    @pytest.mark.anyio()
    async def test_dag_last_step_output_is_final(self) -> None:
        """Final output comes from the last step in topo order."""
        engine = WorkflowEngine(llm_call=_echo_llm)
        steps = [
            Step(id="first", description="do first"),
            Step(
                id="second",
                description="do second",
                depends_on=["first"],
            ),
        ]
        result = await engine.execute(_dag_spec(steps=steps))

        # The output should be the last step's LLM response
        assert result.output is not None
        assert "second" in result.output.lower()

    @pytest.mark.anyio()
    async def test_dag_passes_dep_context(self) -> None:
        """Steps receive dependency outputs as context."""
        prompts_received: list[str] = []

        async def tracking_llm(prompt: str) -> str:
            prompts_received.append(prompt)
            return f"result-of-{prompt.split()[0]}"

        engine = WorkflowEngine(llm_call=tracking_llm)
        steps = [
            Step(id="a", description="step-a"),
            Step(id="b", description="step-b", depends_on=["a"]),
        ]
        result = await engine.execute(_dag_spec(steps=steps))

        assert result.error is None
        # Second prompt should contain context from step a
        assert len(prompts_received) == 2
        assert "[a]" in prompts_received[1]

    @pytest.mark.anyio()
    async def test_dag_no_steps_returns_error(self) -> None:
        spec = WorkflowSpec(
            workflow_id="empty-dag",
            mode=WorkflowMode.DAG,
            steps=[],
        )
        engine = WorkflowEngine(llm_call=_echo_llm)
        result = await engine.execute(spec)

        assert result.error is not None
        assert "steps" in result.error.lower()

    @pytest.mark.anyio()
    async def test_dag_llm_failure_in_step(self) -> None:
        engine = WorkflowEngine(llm_call=_failing_llm)
        result = await engine.execute(_dag_spec())

        # DAG executor catches step errors
        assert result.step_results is not None
        # At least one step should have an error
        errors = [r for r in result.step_results.values() if r.error is not None]
        assert len(errors) > 0

    @pytest.mark.anyio()
    async def test_dag_cycle_returns_error(self) -> None:
        steps = [
            Step(id="a", depends_on=["b"]),
            Step(id="b", depends_on=["a"]),
        ]
        engine = WorkflowEngine(llm_call=_echo_llm)
        result = await engine.execute(_dag_spec(steps=steps))

        assert result.error is not None
        assert "cycle" in result.error.lower()


# ── Unsupported mode tests ──────────────────────────────────────


class TestUnsupportedModes:
    @pytest.mark.anyio()
    async def test_fsm_not_supported(self) -> None:
        spec = WorkflowSpec(
            workflow_id="fsm-test",
            mode=WorkflowMode.FSM,
        )
        engine = WorkflowEngine(llm_call=_echo_llm)
        result = await engine.execute(spec)

        assert result.error is not None
        assert "not supported" in result.error.lower()
        assert result.output is None

    @pytest.mark.anyio()
    async def test_orchestrator_not_supported(self) -> None:
        spec = WorkflowSpec(
            workflow_id="orch-test",
            mode=WorkflowMode.ORCHESTRATOR,
        )
        engine = WorkflowEngine(llm_call=_echo_llm)
        result = await engine.execute(spec)

        assert result.error is not None
        assert "not supported" in result.error.lower()
        assert result.output is None


# ── Import tests ────────────────────────────────────────────────


class TestImports:
    def test_import_from_package(self) -> None:
        from proctor.workflow import WorkflowEngine, WorkflowResult

        assert WorkflowEngine is not None
        assert WorkflowResult is not None
