from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from backend.app.llm.provider import LLMConfigurationError, LLMProviderError
from backend.app.assistant.intent import AssistantIntentCategory, classify_assistant_intent
from backend.app.orchestrator.planner import PlanError, generate_plan
from backend.app.orchestrator.task_manager import InvalidApprovalError
from backend.app.schemas.contracts import ApprovalStatus, EventStatus, ExecutionEvent, TaskCreateRequest, TaskResponse

router = APIRouter(prefix="/tasks")


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVED", "REJECTED"]


def _task_or_404(runtime, task_id: UUID) -> TaskResponse:
    try:
        return runtime.manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "Task not found"}) from exc


@router.post("", response_model=TaskResponse)
def create_task(body: TaskCreateRequest, request: Request) -> TaskResponse:
    runtime = request.app.state.runtime
    try:
        provider = runtime.get_provider()
        classification = classify_assistant_intent(
            body.user_request,
            provider,
            registered_tools=[
                {
                    "tool_name": tool.tool_name,
                    "description": tool.description,
                    "requires_approval": tool.requires_approval,
                }
                for tool in runtime.registry.list_tools()
            ],
        )
        if classification.category in {
            AssistantIntentCategory.CONVERSATION,
            AssistantIntentCategory.INFORMATION,
            AssistantIntentCategory.DRAFTING,
        }:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "not_a_task_request",
                    "message": "This is a conversation or information request. Send it to POST /api/assistant/requests or the session conversation endpoint; no task was created.",
                },
            )
        if classification.category == AssistantIntentCategory.CLARIFICATION_REQUIRED:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "task_clarification_required",
                    "message": classification.clarification_question or "Please clarify the action before requesting a task plan.",
                },
            )

        # Classification only gates conversation from the task API. Actionable
        # requests still pass through the existing structured planner, registry
        # validation, approvals, and independent verification.
        plan = generate_plan(body, runtime.registry, provider)
        task = runtime.manager.create(body, plan.steps, plan.missing_information)
        if plan.intent_missing_information:
            runtime.manager.event(
                task,
                "planning.intent_needs_information",
                EventStatus.NEEDS_ATTENTION,
                f"Intent extraction requested {len(plan.intent_missing_information)} clarification item(s).",
            )
        if plan.plan_missing_information:
            runtime.manager.event(
                task,
                "planning.plan_needs_information",
                EventStatus.NEEDS_ATTENTION,
                f"Plan generation requested {len(plan.plan_missing_information)} clarification item(s).",
            )
        if body.auto_execute:
            executed = runtime.executor.run(task.task_id)
            if executed.status.value == "completed":
                return runtime.summarize_verified_browser_task(task.task_id)
            return executed
        return task
    except HTTPException:
        raise
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=503, detail={"code": "llm_unavailable", "message": "LLM provider is not configured"}) from exc
    except LLMProviderError as exc:
        raise HTTPException(status_code=503, detail={"code": "llm_provider_error", "message": exc.public_message}) from exc
    except PlanError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_plan", "message": "The request could not be planned safely"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_task", "message": str(exc)}) from exc


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: UUID, request: Request) -> TaskResponse:
    return _task_or_404(request.app.state.runtime, task_id)


@router.post("/{task_id}/execute", response_model=TaskResponse)
def execute_task(task_id: UUID, request: Request) -> TaskResponse:
    runtime = request.app.state.runtime
    _task_or_404(runtime, task_id)
    try:
        executed = runtime.executor.run(task_id)
        if executed.status.value == "completed":
            return runtime.summarize_verified_browser_task(task_id)
        return executed
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_operation", "message": str(exc)}) from exc


@router.post("/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(task_id: UUID, request: Request) -> TaskResponse:
    runtime = request.app.state.runtime
    _task_or_404(runtime, task_id)
    return runtime.manager.cancel(task_id)


@router.get("/{task_id}/events", response_model=list[ExecutionEvent])
def task_events(task_id: UUID, request: Request) -> list[ExecutionEvent]:
    runtime = request.app.state.runtime
    _task_or_404(runtime, task_id)
    return runtime.manager.events(task_id)


@router.post("/{task_id}/approvals/{approval_id}", response_model=TaskResponse)
def resolve_approval(task_id: UUID, approval_id: UUID, body: ApprovalDecisionRequest, request: Request) -> TaskResponse:
    runtime = request.app.state.runtime
    _task_or_404(runtime, task_id)
    decision = ApprovalStatus.APPROVED if body.decision == "APPROVED" else ApprovalStatus.REJECTED
    try:
        return runtime.manager.resolve_approval(task_id, approval_id, decision)
    except InvalidApprovalError as exc:
        raise HTTPException(status_code=404, detail={"code": "approval_not_found", "message": "Pending approval not found"}) from exc
