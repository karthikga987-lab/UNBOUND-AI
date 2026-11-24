from datetime import datetime, timedelta, timezone

from backend.app.orchestrator.task_manager import SQLiteTaskRepository, TaskManager
from backend.app.main import create_app
from backend.app.schemas.contracts import (
    ApprovalRequest,
    ApprovalStatus,
    EvidenceItem,
    EvidenceSourceType,
    EventStatus,
    StepStatus,
    TaskCreateRequest,
    TaskStatus,
    TaskStep,
    ToolCall,
)


def make_task(manager: TaskManager, *, approval: bool = False):
    return manager.create(
        TaskCreateRequest(user_request="Read synthetic local test data"),
        [TaskStep(
            step_id="step_1",
            tool_name="document.read_local_file",
            description="Read the approved synthetic file",
            input={"file_path": "C:\\safe\\synthetic.txt"},
            expected_outcome="The file is extracted",
            requires_approval=approval,
        )],
    )


def test_completed_task_events_and_evidence_survive_repository_restart(tmp_path):
    db = tmp_path / "tasks.sqlite3"
    first = TaskManager(SQLiteTaskRepository(db))
    task = first.get_for_execution(make_task(first).task_id)
    task.status = TaskStatus.COMPLETED
    task.plan[0].status = StepStatus.COMPLETED
    task.completed_steps = ["step_1"]
    task.evidence.append(EvidenceItem(
        task_id=task.task_id,
        source_type=EvidenceSourceType.DOCUMENT,
        source_name="synthetic.txt",
        excerpt="Synthetic fact.",
        supports_claim="Synthetic fact.",
    ))
    first.event(task, "task.verified", EventStatus.COMPLETED, "Task completed and verified", "step_1")
    first.save(task)

    restored = TaskManager(SQLiteTaskRepository(db))
    recovered = restored.get(task.task_id)
    assert recovered.status == TaskStatus.COMPLETED
    assert recovered.completed_steps == ["step_1"]
    assert recovered.evidence[0].excerpt == "Synthetic fact."
    assert [item.event_type for item in restored.events(task.task_id)] == ["task.created", "task.verified"]


def test_default_fastapi_composition_reopens_persisted_tasks_after_runtime_recreation(tmp_path, monkeypatch):
    monkeypatch.setenv("UNBOUND_TASK_DB", str(tmp_path / "app-tasks.sqlite3"))
    first_app = create_app()
    first_runtime = first_app.state.runtime
    task = first_runtime.manager.create(
        TaskCreateRequest(user_request="Persist through the application runtime"),
        [],
        missing_information=["Synthetic clarification"],
    )

    second_app = create_app()
    second_runtime = second_app.state.runtime
    restored = second_runtime.manager.get(task.task_id)
    assert restored.user_request == "Persist through the application runtime"
    assert restored.status == TaskStatus.NEEDS_ATTENTION
    assert second_runtime.manager.events(task.task_id)[0].event_type == "task.needs_attention"


def test_running_task_is_recovered_to_attention_without_automatic_retry(tmp_path):
    db = tmp_path / "tasks.sqlite3"
    first = TaskManager(SQLiteTaskRepository(db))
    task = first.get_for_execution(make_task(first).task_id)
    task.status = TaskStatus.EXECUTING
    task.current_step = "step_1"
    task.plan[0].status = StepStatus.RUNNING
    first.save_for_execution(task)

    restored = TaskManager(SQLiteTaskRepository(db))
    recovered = restored.get(task.task_id)
    assert recovered.status == TaskStatus.NEEDS_ATTENTION
    assert recovered.plan[0].status == StepStatus.FAILED
    assert "outcome is unknown" in recovered.plan[0].error
    assert restored.events(task.task_id)[-1].event_type == "task.recovered_needs_attention"


def test_unexpired_pending_approval_survives_and_remains_exactly_bound(tmp_path):
    db = tmp_path / "tasks.sqlite3"
    first = TaskManager(SQLiteTaskRepository(db), approval_ttl_seconds=600)
    task = first.get_for_execution(make_task(first, approval=True).task_id)
    approval = ApprovalRequest(
        task_id=task.task_id,
        step_id="step_1",
        tool_name="document.read_local_file",
        description="Read the exact synthetic file",
        arguments={"file_path": "C:\\safe\\synthetic.txt"},
    )
    task.status = TaskStatus.AWAITING_APPROVAL
    task.plan[0].status = StepStatus.AWAITING_APPROVAL
    task.pending_approvals = [approval]
    first.save(task)

    restored = TaskManager(SQLiteTaskRepository(db), approval_ttl_seconds=600)
    pending = restored.get(task.task_id).pending_approvals[0]
    result = restored.resolve_approval(task.task_id, pending.approval_id, ApprovalStatus.APPROVED)
    assert result.status == TaskStatus.CREATED
    assert result.pending_approvals == []
    call = ToolCall(
        task_id=task.task_id,
        step_id="step_1",
        tool_name="document.read_local_file",
        arguments={"file_path": "C:\\safe\\synthetic.txt"},
    )
    approval_history = restored.approved_request(call)
    assert approval_history is not None
    assert restored.claim_tool_call(task.task_id, call, approval_history) is True
    assert restored.claim_tool_call(task.task_id, call, approval_history) is False


def test_expired_approval_is_cancelled_during_recovery(tmp_path):
    db = tmp_path / "tasks.sqlite3"
    first = TaskManager(SQLiteTaskRepository(db), approval_ttl_seconds=1)
    task = first.get_for_execution(make_task(first, approval=True).task_id)
    approval = ApprovalRequest(
        task_id=task.task_id,
        step_id="step_1",
        tool_name="document.read_local_file",
        description="Read the exact synthetic file",
        arguments={"file_path": "C:\\safe\\synthetic.txt"},
        created_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    task.status = TaskStatus.AWAITING_APPROVAL
    task.plan[0].status = StepStatus.AWAITING_APPROVAL
    task.pending_approvals = [approval]
    first.save(task)

    restored = TaskManager(SQLiteTaskRepository(db), approval_ttl_seconds=1)
    recovered = restored.get(task.task_id)
    assert recovered.status == TaskStatus.NEEDS_ATTENTION
    assert recovered.pending_approvals == []
    history = restored.approval_history(task.task_id)
    assert len(history) == 1
    assert history[0].status == ApprovalStatus.CANCELLED


def test_browser_replay_approval_fails_closed_after_restart(tmp_path):
    db = tmp_path / "tasks.sqlite3"
    first = TaskManager(SQLiteTaskRepository(db))
    task = first.get_for_execution(make_task(first).task_id)
    task.status = TaskStatus.AWAITING_APPROVAL
    task.pending_approvals = [ApprovalRequest(
        task_id=task.task_id,
        step_id="step_1",
        tool_name="browser.replay",
        description="A browser workflow action requires approval.",
        arguments={},
    )]
    first.save(task)

    restored = TaskManager(SQLiteTaskRepository(db))
    recovered = restored.get(task.task_id)
    assert recovered.status == TaskStatus.NEEDS_ATTENTION
    assert recovered.pending_approvals == []
    assert restored.approval_history(task.task_id)[0].status == ApprovalStatus.CANCELLED
