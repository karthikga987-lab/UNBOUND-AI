"""Registry for serializable tool definitions and private callable bindings."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from backend.app.safety.permissions import action_requires_approval
from backend.app.schemas.contracts import ApprovalRequest, ApprovalStatus, ToolCall, ToolDefinition, ToolResult


AGREED_TOOL_NAMES = frozenset({
    "browser.navigate", "browser.click", "browser.fill", "browser.record", "browser.replay", "browser.download",
    "document.upload", "document.search", "context.retrieve", "context.relate", "report.generate",
})


class ToolUnavailableError(LookupError):
    pass


class ToolInputError(ValueError):
    pass


class ApprovalRequiredError(PermissionError):
    pass


class ToolExecutionCancelled(RuntimeError):
    pass


ToolCallable = Callable[[BaseModel, ToolCall], ToolResult | dict[str, Any]]


@dataclass(frozen=True)
class RuntimeToolRegistration:
    """Private runtime binding; never serialized as a shared ToolDefinition."""

    definition: ToolDefinition
    input_model: type[BaseModel]
    execute: ToolCallable


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RuntimeToolRegistration] = {}

    def register(
        self,
        definition: ToolDefinition,
        input_model: type[BaseModel],
        execute: ToolCallable,
    ) -> None:
        if definition.tool_name not in AGREED_TOOL_NAMES:
            raise ValueError(f"Tool name '{definition.tool_name}' is not in the agreed tool namespace")
        if definition.tool_name in self._tools:
            raise ValueError(f"Tool '{definition.tool_name}' is already registered")
        if not callable(execute):
            raise TypeError("Registered tool execute value must be callable")
        if not isinstance(input_model, type) or not issubclass(input_model, BaseModel):
            raise TypeError("input_model must be a Pydantic BaseModel class")
        if definition.input_schema != input_model.model_json_schema():
            raise ValueError(f"Tool '{definition.tool_name}' input_schema does not match its Pydantic input validator")
        # Keep a private snapshot so a caller cannot mutate approval metadata after
        # registration. The Pydantic input model remains the execution validator.
        snapshot = definition.model_copy(deep=True)
        self._tools[definition.tool_name] = RuntimeToolRegistration(snapshot, input_model, execute)

    def list_tools(self) -> list[ToolDefinition]:
        return [registration.definition.model_copy(deep=True) for registration in self._tools.values()]

    def get_tool(self, name: str) -> ToolDefinition:
        registration = self._get_registration(name)
        return registration.definition.model_copy(deep=True)

    def _get_registration(self, name: str) -> RuntimeToolRegistration:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolUnavailableError(f"Tool '{name}' is unavailable") from exc

    def validate_input(self, name: str, value: dict[str, Any]) -> BaseModel:
        registration = self._get_registration(name)
        try:
            return registration.input_model.model_validate(value)
        except ValidationError as exc:
            raise ToolInputError(f"Invalid input for '{name}': {exc}") from exc

    def action_requires_approval(self, call: ToolCall, description: str = "") -> bool:
        definition = self.get_tool(call.tool_name)
        return action_requires_approval(
            tool_name=call.tool_name,
            description=description,
            arguments=call.arguments,
            metadata={"requires_approval": definition.requires_approval},
        )

    def execute(
        self,
        call: ToolCall,
        *,
        description: str = "",
        approval: ApprovalRequest | None = None,
        requires_approval: bool = False,
        before_execute: Callable[[], bool] | None = None,
    ) -> ToolResult:
        registration = self._get_registration(call.tool_name)
        arguments = self.validate_input(call.tool_name, call.arguments)
        needs_approval = action_requires_approval(
            tool_name=call.tool_name,
            description=description,
            arguments=call.arguments,
            metadata={"requires_approval": registration.definition.requires_approval or requires_approval},
        )
        if needs_approval and (not self._approval_matches(approval, call) or before_execute is None):
            raise ApprovalRequiredError(f"Tool action '{call.tool_name}' requires an approved manager decision")
        if before_execute is not None and not before_execute():
            raise ToolExecutionCancelled("Task was cancelled before the tool action started")
        try:
            raw_result = registration.execute(arguments, call)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
        try:
            raw_value = raw_result.model_dump() if isinstance(raw_result, ToolResult) else raw_result
            validated = ToolResult.model_validate(raw_value)
            json_value = validated.model_dump(mode="json")
            encoded = json.dumps(json_value, allow_nan=False, separators=(",", ":"))
            return ToolResult.model_validate_json(encoded)
        except ValidationError as exc:
            return ToolResult(success=False, error=f"Tool returned an invalid result: {exc}")
        except Exception:
            return ToolResult(success=False, error="Tool returned data that is not JSON serializable")

    @staticmethod
    def _approval_matches(approval: ApprovalRequest | None, call: ToolCall) -> bool:
        return bool(
            approval
            and approval.status == ApprovalStatus.APPROVED
            and approval.task_id == call.task_id
            and approval.step_id == call.step_id
            and approval.tool_name == call.tool_name
            and approval.arguments == call.arguments
        )
