"""Constrained plan generation over actually registered tools."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from backend.app.llm.provider import LLMProvider, get_provider_from_env
from backend.app.orchestrator.intent import TaskIntent, understand_intent
from backend.app.orchestrator.tool_registry import ToolRegistry
from backend.app.schemas.contracts import TaskCreateRequest, TaskStep


class PlanDraft(BaseModel):
    steps: list[TaskStep] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class TaskPlan(BaseModel):
    intent: TaskIntent
    steps: list[TaskStep]
    missing_information: list[str] = Field(default_factory=list)


class PlanError(ValueError):
    pass


def generate_plan(request: str | TaskCreateRequest, registry: ToolRegistry, provider: LLMProvider | None = None) -> TaskPlan:
    llm = provider or get_provider_from_env()
    intent = understand_intent(request, llm)
    available = [{"name": t.tool_name, "description": t.description, "input_schema": t.input_schema, "requires_approval": t.requires_approval} for t in registry.list_tools()]
    if not available:
        return TaskPlan(intent=intent, steps=[], missing_information=intent.missing_information + ["No tools are currently registered to perform this task."])
    prompt = json.dumps({"intent": intent.model_dump(mode="json"), "registered_tools": available}, ensure_ascii=False)
    try:
        draft = llm.generate_json(
            system="Create a minimal ordered plan using only the listed registered tools. Never invent capabilities, tool names, required input values, or executable code. If essential details are missing, put them in missing_information. Mark sensitive or irreversible steps requires_approval=true. Each step has step_id, tool_name, description, input, expected_outcome, requires_approval, status.",
            prompt=prompt,
            response_model=PlanDraft,
        )
    except (ValidationError, ValueError) as exc:
        raise PlanError(f"Could not create a valid task plan: {exc}") from exc
    step_ids = [step.step_id for step in draft.steps]
    duplicates = sorted({step_id for step_id in step_ids if step_ids.count(step_id) > 1})
    if duplicates:
        raise PlanError(f"Planner returned duplicate step_id values: {', '.join(duplicates)}")
    combined_missing = list(dict.fromkeys(intent.missing_information + draft.missing_information))
    checked: list[TaskStep] = []
    for i, step in enumerate(draft.steps, 1):
        try:
            tool = registry.get_tool(step.tool_name)
        except LookupError as exc:
            raise PlanError(f"Planner selected unregistered tool '{step.tool_name}'") from exc
        try:
            registry.validate_input(step.tool_name, step.input)
        except ValueError as exc:
            combined_missing.append(f"Please clarify the input for '{step.tool_name}' ({step.step_id}): {exc}")
            continue
        step.step_id = step.step_id or f"step_{i}"
        step.requires_approval = step.requires_approval or tool.requires_approval
        checked.append(step)
    if not combined_missing and not checked:
        raise PlanError("Planner returned no steps and no clarification request")
    return TaskPlan(intent=intent, steps=checked, missing_information=combined_missing)
