"""Tests for WorkflowSpec, Step, and related models."""

import json

from proctor.workflow.spec import (
    Step,
    StepRetry,
    StepType,
    WorkflowMode,
    WorkflowPolicies,
    WorkflowSpec,
)


class TestWorkflowMode:
    def test_values(self) -> None:
        assert WorkflowMode.SIMPLE == "simple"
        assert WorkflowMode.DAG == "dag"
        assert WorkflowMode.FSM == "fsm"
        assert WorkflowMode.ORCHESTRATOR == "orchestrator"

    def test_is_str(self) -> None:
        assert isinstance(WorkflowMode.SIMPLE, str)


class TestStepType:
    def test_values(self) -> None:
        assert StepType.LLM == "llm"
        assert StepType.SHELL == "shell"
        assert StepType.HTTP == "http"
        assert StepType.SYSTEM == "system"
        assert StepType.WAIT_EVENT == "wait_event"

    def test_is_str(self) -> None:
        assert isinstance(StepType.LLM, str)


class TestStepRetry:
    def test_defaults(self) -> None:
        retry = StepRetry()
        assert retry.max_attempts == 1
        assert retry.delay_seconds == 5

    def test_custom_values(self) -> None:
        retry = StepRetry(max_attempts=3, delay_seconds=30)
        assert retry.max_attempts == 3
        assert retry.delay_seconds == 30


class TestStep:
    def test_minimal_step(self) -> None:
        step = Step(id="step-1")
        assert step.id == "step-1"
        assert step.type == StepType.LLM
        assert step.description == ""
        assert step.inputs == {}
        assert step.outputs == {}
        assert step.depends_on == []
        assert step.tool is None
        assert step.retry.max_attempts == 1
        assert step.conditions == {}

    def test_full_step(self) -> None:
        step = Step(
            id="analyze",
            type=StepType.SHELL,
            description="Run analysis",
            inputs={"data": "raw.csv"},
            outputs={"report": "analysis.json"},
            depends_on=["fetch-data"],
            tool="shell_exec",
            retry=StepRetry(max_attempts=3, delay_seconds=10),
            conditions={"on_failure": "skip"},
        )
        assert step.id == "analyze"
        assert step.type == StepType.SHELL
        assert step.inputs == {"data": "raw.csv"}
        assert step.outputs == {"report": "analysis.json"}
        assert step.depends_on == ["fetch-data"]
        assert step.tool == "shell_exec"
        assert step.retry.max_attempts == 3
        assert step.conditions == {"on_failure": "skip"}

    def test_step_inputs_outputs(self) -> None:
        step = Step(
            id="transform",
            inputs={"source": "data.csv", "format": "json"},
            outputs={"result": "output.json", "log": "transform.log"},
        )
        assert "source" in step.inputs
        assert "format" in step.inputs
        assert step.inputs["source"] == "data.csv"
        assert "result" in step.outputs
        assert "log" in step.outputs
        assert step.outputs["result"] == "output.json"

    def test_step_multiple_dependencies(self) -> None:
        step = Step(
            id="merge",
            depends_on=["step-a", "step-b", "step-c"],
        )
        assert len(step.depends_on) == 3
        assert "step-a" in step.depends_on
        assert "step-b" in step.depends_on
        assert "step-c" in step.depends_on


class TestWorkflowPolicies:
    def test_defaults(self) -> None:
        policies = WorkflowPolicies()
        assert policies.max_runtime_seconds == 900
        assert policies.max_retries == 2
        assert policies.retry_delay_seconds == 60
        assert policies.require_security_review is False

    def test_custom_policies(self) -> None:
        policies = WorkflowPolicies(
            max_runtime_seconds=1800,
            max_retries=5,
            retry_delay_seconds=120,
            require_security_review=True,
        )
        assert policies.max_runtime_seconds == 1800
        assert policies.max_retries == 5
        assert policies.retry_delay_seconds == 120
        assert policies.require_security_review is True


class TestWorkflowSpec:
    def test_simple_spec(self) -> None:
        spec = WorkflowSpec(
            workflow_id="simple-1",
            mode=WorkflowMode.SIMPLE,
            description="A simple prompt workflow",
            prompt="Summarize this text",
        )
        assert spec.workflow_id == "simple-1"
        assert spec.version == "1.0.0"
        assert spec.mode == WorkflowMode.SIMPLE
        assert spec.prompt == "Summarize this text"
        assert spec.steps == []
        assert spec.policies.max_retries == 2

    def test_dag_spec_with_steps(self) -> None:
        steps = [
            Step(
                id="fetch",
                type=StepType.HTTP,
                description="Fetch data",
                inputs={"url": "https://api.example.com/data"},
                outputs={"response": "raw_data"},
            ),
            Step(
                id="analyze",
                type=StepType.LLM,
                description="Analyze data",
                inputs={"data": "$fetch.response"},
                outputs={"analysis": "result"},
                depends_on=["fetch"],
            ),
            Step(
                id="report",
                type=StepType.LLM,
                description="Generate report",
                inputs={"analysis": "$analyze.analysis"},
                depends_on=["analyze"],
            ),
        ]
        spec = WorkflowSpec(
            workflow_id="dag-1",
            mode=WorkflowMode.DAG,
            description="Data pipeline",
            steps=steps,
        )
        assert spec.mode == WorkflowMode.DAG
        assert len(spec.steps) == 3
        assert spec.steps[0].id == "fetch"
        assert spec.steps[1].depends_on == ["fetch"]
        assert spec.steps[2].depends_on == ["analyze"]
        assert spec.prompt is None

    def test_dag_step_references_accessible(self) -> None:
        steps = [
            Step(id="a", outputs={"val": "1"}),
            Step(id="b", depends_on=["a"], inputs={"from_a": "$a.val"}),
        ]
        spec = WorkflowSpec(
            workflow_id="ref-test",
            mode=WorkflowMode.DAG,
            steps=steps,
        )
        step_map = {s.id: s for s in spec.steps}
        assert "a" in step_map
        assert "b" in step_map
        assert step_map["b"].depends_on == ["a"]
        assert step_map["b"].inputs["from_a"] == "$a.val"

    def test_spec_defaults(self) -> None:
        spec = WorkflowSpec(
            workflow_id="defaults",
            mode=WorkflowMode.SIMPLE,
        )
        assert spec.version == "1.0.0"
        assert spec.description == ""
        assert spec.prompt is None
        assert spec.steps == []
        assert spec.states == {}
        assert spec.orchestrator == {}
        assert spec.security == {}
        assert spec.channels == {}

    def test_spec_with_custom_policies(self) -> None:
        spec = WorkflowSpec(
            workflow_id="custom-pol",
            mode=WorkflowMode.SIMPLE,
            prompt="test",
            policies=WorkflowPolicies(
                max_retries=5,
                max_runtime_seconds=3600,
            ),
        )
        assert spec.policies.max_retries == 5
        assert spec.policies.max_runtime_seconds == 3600

    def test_serialization_roundtrip_simple(self) -> None:
        spec = WorkflowSpec(
            workflow_id="round-1",
            mode=WorkflowMode.SIMPLE,
            description="Roundtrip test",
            prompt="Hello world",
            policies=WorkflowPolicies(max_retries=3),
        )
        json_str = spec.model_dump_json()
        restored = WorkflowSpec.model_validate_json(json_str)
        assert restored.workflow_id == spec.workflow_id
        assert restored.mode == spec.mode
        assert restored.prompt == spec.prompt
        assert restored.policies.max_retries == 3

    def test_serialization_roundtrip_dag(self) -> None:
        steps = [
            Step(
                id="step-a",
                type=StepType.SHELL,
                inputs={"cmd": "echo hi"},
                outputs={"stdout": "text"},
                retry=StepRetry(max_attempts=2, delay_seconds=10),
            ),
            Step(
                id="step-b",
                depends_on=["step-a"],
                inputs={"prev": "$step-a.stdout"},
            ),
        ]
        spec = WorkflowSpec(
            workflow_id="round-dag",
            mode=WorkflowMode.DAG,
            description="DAG roundtrip",
            steps=steps,
            security={"sandbox": True},
            channels={"notify": "terminal"},
        )
        data = spec.model_dump()
        restored = WorkflowSpec.model_validate(data)

        assert restored.mode == WorkflowMode.DAG
        assert len(restored.steps) == 2
        assert restored.steps[0].type == StepType.SHELL
        assert restored.steps[0].retry.max_attempts == 2
        assert restored.steps[1].depends_on == ["step-a"]
        assert restored.security == {"sandbox": True}
        assert restored.channels == {"notify": "terminal"}

    def test_serialization_via_json_module(self) -> None:
        """Verify model_dump produces JSON-serializable dicts."""
        spec = WorkflowSpec(
            workflow_id="json-test",
            mode=WorkflowMode.DAG,
            steps=[Step(id="x", inputs={"n": 42})],
        )
        dumped = spec.model_dump()
        json_str = json.dumps(dumped)
        loaded = json.loads(json_str)
        restored = WorkflowSpec.model_validate(loaded)
        assert restored.steps[0].inputs["n"] == 42

    def test_import_from_package(self) -> None:
        """All public types are importable from proctor.workflow."""
        from proctor.workflow import (
            Step,
            StepRetry,
            StepType,
            WorkflowMode,
            WorkflowPolicies,
            WorkflowSpec,
        )

        assert WorkflowMode.SIMPLE == "simple"
        assert StepType.LLM == "llm"
        assert Step(id="t").id == "t"
        assert StepRetry().max_attempts == 1
        assert WorkflowPolicies().max_retries == 2
        assert (
            WorkflowSpec(workflow_id="w", mode=WorkflowMode.SIMPLE).workflow_id == "w"
        )
