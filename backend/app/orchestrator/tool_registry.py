"""Registry for serializable tool definitions and private callable bindings."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from backend.app.safety.permissions import action_requires_approval
from backend.app.schemas.contracts import ApprovalRequest, ApprovalStatus, ToolCall, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


AGREED_TOOL_NAMES = frozenset({
    "browser.navigate", "browser.click", "browser.fill", "browser.record", "browser.replay", "browser.download",
    "document.upload", "document.search", "document.read_local_file", "context.retrieve", "context.relate", "report.generate",
})


class ToolUnavailableError(LookupError):
    pass


class ToolInputError(ValueError):
    pass


class ApprovalRequiredError(PermissionError):
    pass


class ToolExecutionCancelled(RuntimeError):
    pass


class ToolExecutionOutcomeUnknown(RuntimeError):
    """The callable ran, but its external outcome cannot be established safely."""


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
        registration = self._make_registration(definition, input_model, execute)
        if definition.tool_name in self._tools:
            raise ValueError(f"Tool '{definition.tool_name}' is already registered")
        self._tools[definition.tool_name] = registration

    def register_many(self, items) -> None:
        """Atomically register a group after validating every definition and binding."""
        staged: dict[str, RuntimeToolRegistration] = {}
        for definition, input_model, execute in items:
            registration = self._make_registration(definition, input_model, execute)
            name = definition.tool_name
            if name in self._tools or name in staged:
                raise ValueError(f"Tool '{name}' is already registered")
            staged[name] = registration
        self._tools.update(staged)

    def unregister_many(self, names) -> None:
        """Remove a previously staged registration set during startup rollback."""
        for name in names:
            self._tools.pop(name, None)

    @staticmethod
    def _make_registration(
        definition: ToolDefinition,
        input_model: type[BaseModel],
        execute: ToolCallable,
    ) -> RuntimeToolRegistration:
        if definition.tool_name not in AGREED_TOOL_NAMES:
            raise ValueError(f"Tool name '{definition.tool_name}' is not in the agreed tool namespace")
        if not callable(execute):
            raise TypeError("Registered tool execute value must be callable")
        if not isinstance(input_model, type) or not issubclass(input_model, BaseModel):
            raise TypeError("input_model must be a Pydantic BaseModel class")
        if definition.input_schema != input_model.model_json_schema():
            raise ValueError(f"Tool '{definition.tool_name}' input_schema does not match its Pydantic input validator")
        # Keep a private snapshot so a caller cannot mutate approval metadata after
        # registration. The Pydantic input model remains the execution validator.
        snapshot = definition.model_copy(deep=True)
        return RuntimeToolRegistration(snapshot, input_model, execute)

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
            fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors(include_input=False)})
            raise ToolInputError(f"Invalid input for '{name}' (fields: {', '.join(fields) or 'unknown'})") from exc

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
            logger.warning("Registered tool failed: tool_name=%s exception_class=%s", call.tool_name, type(exc).__name__)
            raise ToolExecutionOutcomeUnknown(
                f"Tool invocation raised {type(exc).__name__}; its external outcome is unknown"
            ) from exc
        try:
            raw_value = raw_result.model_dump() if isinstance(raw_result, ToolResult) else raw_result
            validated = ToolResult.model_validate(raw_value)
            if not validated.success:
                # A structured unsuccessful result is an explicit adapter report
                # that the operation failed. Keep the state known-failed and
                # discard adapter error text, which may contain private data.
                error = "Registered tool reported unsuccessful execution"
                if call.tool_name.startswith("browser.") and isinstance(validated.data, dict):
                    browser_failures = {
                        "local_demo_mode_not_enabled": (
                            "Local Demo Mode is not enabled. Restart the backend with "
                            "UNBOUND_BROWSER_LOCAL_DEMO=1 to use the fixed synthetic page."
                        ),
                        "browser_execution_disabled": "Browser execution is disabled by the runtime security gate.",
                    }
                    failure_code = validated.data.get("failure_code")
                    if failure_code in browser_failures:
                        logger.warning(
                            "Registered browser tool was blocked by runtime policy: tool_name=%s failure_code=%s",
                            call.tool_name,
                            failure_code,
                        )
                        error = browser_failures[failure_code]
                if call.tool_name == "document.read_local_file" and isinstance(validated.data, dict):
                    # Surface only a bounded internal category. Adapter error
                    # strings can contain paths or other private values.
                    safe_failure_codes = {
                        "file_changed_after_approval",
                        "file_changed_during_read",
                        "local_file_not_found",
                        "local_file_permission_denied",
                        "local_file_read_failed",
                        "local_file_validation_failed",
                        "file_size_limit_exceeded",
                        "local_provider_unavailable",
                        "non_local_provider_rejected",
                        "document_extraction_failed",
                        "document_persistence_failed",
                        "analysis_schema_validation_failed",
                        "analysis_evidence_validation_failed",
                        "analysis_generation_failed",
                        "analysis_validation_failed",
                    }
                    failure_code = validated.data.get("failure_code")
                    if failure_code in safe_failure_codes:
                        error = f"Local file operation failed ({failure_code})"
                return ToolResult(
                    success=False,
                    data=validated.data,
                    error=error,
                    evidence=validated.evidence,
                )
            json_value = validated.model_dump(mode="json")
            encoded = json.dumps(json_value, allow_nan=False, separators=(",", ":"))
            return ToolResult.model_validate_json(encoded)
        except ValidationError as exc:
            logger.warning("Registered tool returned invalid result: tool_name=%s", call.tool_name)
            return ToolResult(success=False, error="Tool returned an invalid result")
        except ToolExecutionOutcomeUnknown:
            raise
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
