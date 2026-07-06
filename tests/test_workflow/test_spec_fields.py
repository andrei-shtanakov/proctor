"""WorkflowSpec/WorkflowPolicies fields added for distribution."""

from proctor.workflow.spec import WorkflowMode, WorkflowPolicies, WorkflowSpec


def test_requires_defaults_empty() -> None:
    spec = WorkflowSpec(workflow_id="w", mode=WorkflowMode.SIMPLE)
    assert spec.requires == []


def test_requires_roundtrip() -> None:
    spec = WorkflowSpec(workflow_id="w", mode=WorkflowMode.SIMPLE, requires=["python"])
    assert spec.requires == ["python"]


def test_retry_on_worker_loss_defaults_false() -> None:
    assert WorkflowPolicies().retry_on_worker_loss is False
