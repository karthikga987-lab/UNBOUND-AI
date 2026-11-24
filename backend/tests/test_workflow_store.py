from __future__ import annotations

import json
from uuid import uuid4

import pytest

from backend.app.browser.workflow_store import (
    CorruptWorkflowError,
    DuplicateWorkflowError,
    SensitiveWorkflowDataError,
    WorkflowNotFoundError,
    WorkflowStore,
)
from backend.app.schemas.contracts import WorkflowAction, WorkflowDefinition, WorkflowParameter


def make_workflow(*, workflow_id=None, name="Quarterly report"):
    return WorkflowDefinition(
        workflow_id=workflow_id or uuid4(),
        name=name,
        description="Download a report using supplied parameters",
        start_url="https://example.test/reports",
        parameters=[
            WorkflowParameter(name="year", type="integer", required=True, description="Report year"),
            WorkflowParameter(name="region", type="string", required=False, default="north"),
        ],
        steps=[
            WorkflowAction(step_id="step_1", action="navigate", input={"url": "https://example.test/reports"}),
            WorkflowAction(
                step_id="step_2",
                action="fill",
                target={"type": "label", "value": "Year"},
                input={"value": "{{year}}"},
            ),
            WorkflowAction(
                step_id="step_3",
                action="click",
                target={"type": "role", "role": "button", "name": "Search"},
                expected_outcome="Results appear",
            ),
        ],
    )


def test_save_list_and_load_preserve_shared_contract_data(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    workflow = make_workflow()

    saved = store.save(workflow)

    assert saved == workflow
    listed = store.list_workflows()
    assert listed == [workflow]
    loaded = store.load(workflow.workflow_id)
    assert isinstance(loaded, WorkflowDefinition)
    assert loaded.parameters == workflow.parameters
    assert loaded.steps == workflow.steps
    assert [step.step_id for step in loaded.steps] == ["step_1", "step_2", "step_3"]


def test_storage_file_is_json_serializable_and_round_trips_with_shared_model(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    workflow = make_workflow()
    store.save(workflow)
    file_path = tmp_path / "workflows" / f"{workflow.workflow_id}.json"

    decoded = json.loads(file_path.read_text(encoding="utf-8"))
    restored = WorkflowDefinition.model_validate(decoded)
    assert restored == workflow
    assert restored.steps[1].input == {"value": "{{year}}"}
    assert restored.parameters[1].default == "north"


def test_missing_storage_is_empty_and_missing_workflow_is_safe(tmp_path):
    store = WorkflowStore(tmp_path / "not-created-yet")
    assert store.list_workflows() == []
    assert store.delete(uuid4()) is False
    with pytest.raises(WorkflowNotFoundError, match="not found"):
        store.load(uuid4())


def test_duplicate_workflow_id_does_not_overwrite_existing_workflow(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    workflow_id = uuid4()
    original = make_workflow(workflow_id=workflow_id, name="Original")
    store.save(original)

    with pytest.raises(DuplicateWorkflowError):
        store.save(make_workflow(workflow_id=workflow_id, name="Replacement"))
    assert store.load(workflow_id).name == "Original"


def test_delete_removes_workflow_and_repeated_delete_returns_false(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    workflow = make_workflow()
    store.save(workflow)

    assert store.delete(workflow.workflow_id) is True
    assert store.list_workflows() == []
    assert store.delete(workflow.workflow_id) is False


def test_corrupted_json_is_reported_without_echoing_file_contents(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    store.directory.mkdir(parents=True)
    workflow_id = uuid4()
    (store.directory / f"{workflow_id}.json").write_text('{ "password": "never-show-this-secret" ', encoding="utf-8")

    with pytest.raises(CorruptWorkflowError) as error:
        store.load(workflow_id)
    assert "never-show-this-secret" not in str(error.value)
    with pytest.raises(CorruptWorkflowError):
        store.list_workflows()


def test_schema_invalid_json_is_rejected_on_load(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    store.directory.mkdir(parents=True)
    workflow_id = uuid4()
    (store.directory / f"{workflow_id}.json").write_text(
        json.dumps({"workflow_id": str(workflow_id), "name": "Missing start URL"}), encoding="utf-8"
    )
    with pytest.raises(CorruptWorkflowError):
        store.load(workflow_id)


@pytest.mark.parametrize(
    "workflow",
    [
        make_workflow(name="Sensitive action").model_copy(update={
            "steps": [WorkflowAction(step_id="step_1", action="fill", input={"password": "plain secret"})]
        }),
        WorkflowDefinition(
            name="Sensitive query",
            start_url="https://example.test/?access_token=plain-secret",
        ),
        WorkflowDefinition(
            name="Sensitive parameter default",
            start_url="https://example.test",
            parameters=[WorkflowParameter(name="password", type="string", default="plain-secret")],
        ),
    ],
)
def test_obvious_sensitive_values_are_rejected_without_echoing_values(tmp_path, workflow):
    store = WorkflowStore(tmp_path / "workflows")
    with pytest.raises(SensitiveWorkflowDataError) as error:
        store.save(workflow)
    assert "plain-secret" not in str(error.value)
    assert "plain secret" not in str(error.value)
    assert store.list_workflows() == []


def test_invalid_workflow_id_cannot_be_used_as_a_storage_path(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    with pytest.raises(ValueError, match="UUID4"):
        store.load("../../outside")
    assert not store.directory.exists()
