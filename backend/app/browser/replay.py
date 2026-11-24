"""Validated, sequential replay of stored browser workflows."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Collection, Literal, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from backend.app.browser.controller import (
    BrowserController,
    BrowserControllerError,
    _parse_target,
    _validate_http_url,
)
from backend.app.browser.replay_state import (
    PreparedReplayAction,
    ReplayActionJournalState,
    ReplayContinuation,
    ReplayExecutionResult,
    ReplayPendingAction,
    ReplayPolicyContext,
    ReplayStepAuthorization,
)
from backend.app.browser.replay_state_store import (
    ReplayAuthorizationError,
    ReplayStateBusyError,
    ReplayStateConflictError,
    ReplayStateIntegrityError,
    ReplayStateNotFoundError,
    ReplayStateStore,
    ReplayStateStoreError,
    ReplayWorkflowMismatchError,
)
from backend.app.browser.session_manager import BrowserSessionBinding, BrowserSessionManager
from backend.app.browser.workflow_store import WorkflowStore, WorkflowStoreError
from backend.app.safety.permissions import action_requires_approval
from backend.app.schemas.contracts import ToolCall, WorkflowAction, WorkflowDefinition, WorkflowParameter


_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_SENSITIVE = re.compile(
    r"(?:password|passwd|passcode|one.?time|\botp\b|token|secret|auth|credential|"
    r"api.?key|private.?key|access.?key|client.?key|refresh.?key|bearer|jwt|signature|"
    r"cookie|session|cvv|cvc|card.?number|security.?code|social.?security|\bssn\b|"
    r"national.?id|tax.?id|bank.?account|routing.?number)",
    re.IGNORECASE,
)
_URL_IN_MESSAGE = re.compile(r"https?://[^\s\]\[()<>{}\"']+", re.IGNORECASE)
_ACTION_ALIASES = {
    "navigate": "navigate",
    "browser.navigate": "navigate",
    "click": "click",
    "browser.click": "click",
    "fill": "fill",
    "browser.fill": "fill",
    "select": "select_option",
    "select_option": "select_option",
    "browser.select_option": "select_option",
}


class CancellationChecker(Protocol):
    def is_cancelled(self, task_id: UUID) -> bool: ...


class ReplayPolicyHandoff(Protocol):
    """Trusted backend handoff; implementations must not expose policy context publicly."""

    def publish(
        self,
        pending_action: ReplayPendingAction,
        policy_context: ReplayPolicyContext,
        *,
        workflow_digest: str,
        explanation: str,
    ) -> None: ...


def _session_for_tool_call(
    manager: BrowserSessionManager,
    tool_call: ToolCall,
) -> BrowserSessionBinding | None:
    """Prefer ToolCall-bound ownership; retain the Phase 9 structural fallback."""
    resolver = getattr(manager, "get_session_for_tool_call", None)
    if callable(resolver):
        return resolver(tool_call)
    return manager.get_session(tool_call.task_id)


@dataclass(frozen=True)
class ReplayStepResult:
    """Safe per-step outcome; it intentionally omits arguments and values."""

    step_id: str
    action: str
    success: bool
    message: str
    error: str | None = None
    fallback_used: bool = False
    awaiting_approval: bool = False


@dataclass
class WorkflowReplayResult:
    workflow_id: str
    success: bool
    status: Literal["completed", "failed", "awaiting_approval"]
    steps: list[ReplayStepResult] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class _PreparedAction:
    step: WorkflowAction
    action: str
    target: dict[str, Any] | None
    inputs: dict[str, Any]


class WorkflowReplay:
    """Replay workflows with complete parameter preflight and fail-stop behavior.

    ``approved_step_ids`` must contain only step IDs that received explicit user
    approval outside this module. Approval is checked again at each action.
    """

    def __init__(
        self,
        store: WorkflowStore,
        controller: BrowserController | None = None,
        *,
        replay_state_store: ReplayStateStore | None = None,
        session_manager: BrowserSessionManager | None = None,
        cancellation_checker: CancellationChecker | None = None,
        policy_handoff: ReplayPolicyHandoff | None = None,
    ) -> None:
        self.store = store
        self.controller = controller
        self.replay_state_store = replay_state_store
        self.session_manager = session_manager
        self.cancellation_checker = cancellation_checker
        self.policy_handoff = policy_handoff
        self._policy_handoffs: dict[UUID, ReplayPolicyHandoff] = {}

    def start_replay(
        self,
        workflow_id: str | UUID,
        parameters: Mapping[str, Any] | None,
        *,
        tool_call: ToolCall,
        policy_handoff: ReplayPolicyHandoff,
    ) -> ReplayExecutionResult:
        """Persist a replay and publish its first action without executing it."""
        state_store, session_manager, cancellation_checker = self._continuation_dependencies()
        if tool_call.tool_name != "browser.replay":
            raise ReplayStateStoreError("Replay requires a browser.replay ToolCall")
        if self._is_cancelled(cancellation_checker, tool_call.task_id):
            return self._safe_failure(tool_call, workflow_id, "Replay was cancelled before it started")
        try:
            workflow = self.store.load(workflow_id)
        except (WorkflowStoreError, ValueError):
            return self._safe_failure(tool_call, workflow_id, "Workflow could not be loaded safely")
        try:
            session = _session_for_tool_call(session_manager, tool_call)
        except Exception:
            session = None
        if session is None or not session.controller.is_started:
            return self._safe_failure(
                tool_call, workflow.workflow_id, "An active browser session is required to start replay"
            )

        provided = self._parameter_mapping(parameters)
        if isinstance(provided, str):
            return self._safe_failure(tool_call, workflow.workflow_id, provided, session.session_id)
        resolved, parameter_error = self._resolve_parameters(workflow, provided)
        if parameter_error:
            return self._safe_failure(tool_call, workflow.workflow_id, parameter_error, session.session_id)
        prepared, preparation_error = self._prepare_actions(workflow, resolved)
        if preparation_error:
            sensitive = self._sensitive_values(workflow, resolved)
            safe = self._safe_error(preparation_error, sensitive)
            return self._safe_failure(tool_call, workflow.workflow_id, safe, session.session_id)

        prepared_for_store = [
            PreparedReplayAction(
                step_id=item.step.step_id,
                action=item.action,
                arguments={"target": item.target, **item.inputs} if item.target is not None else dict(item.inputs),
            )
            for item in prepared
        ]
        try:
            initial = state_store.create(
                workflow,
                tool_call=tool_call,
                session_id=session.session_id,
                resolved_parameters=resolved,
                prepared_actions=prepared_for_store,
            )
            context = state_store.get_pending_policy_context(
                initial.continuation,
                tool_call=tool_call,
                current_workflow=workflow,
            )
            self._publish_pending(initial, context, policy_handoff)
            self._policy_handoffs[initial.replay_run_id] = policy_handoff
            return initial
        except (ReplayStateStoreError, ReplayStateConflictError, ReplayWorkflowMismatchError):
            return self._safe_failure(
                tool_call, workflow.workflow_id, "Replay could not be prepared safely", session.session_id
            )
        except Exception:
            return self._safe_failure(
                tool_call, workflow.workflow_id, "Replay policy handoff could not be completed", session.session_id
            )

    def resume_replay(
        self,
        continuation: ReplayContinuation,
        authorization: ReplayStepAuthorization,
        *,
        tool_call: ToolCall,
    ) -> ReplayExecutionResult:
        """Consume one authorization, dispatch one action, and checkpoint it."""
        state_store, session_manager, cancellation_checker = self._continuation_dependencies()
        if tool_call.tool_name != "browser.replay":
            return self._safe_failure(tool_call, continuation.workflow_id, "Replay ToolCall identity is invalid", continuation.session_id)
        try:
            workflow = self.store.load(continuation.workflow_id)
            current = state_store.load_result(
                continuation.replay_run_id,
                current_workflow=workflow,
            )
            if current.continuation != continuation:
                if current.status == "needs_attention":
                    if any(
                        checkpoint.state == ReplayActionJournalState.IN_PROGRESS
                        for checkpoint in current.checkpoints
                    ):
                        try:
                            return state_store.recover_after_restart(continuation.replay_run_id)
                        except ReplayStateBusyError:
                            # Another process still owns the dispatch lease.
                            return current
                    return current
                return self._safe_from_state(current, "Replay continuation is stale")
            if self._is_cancelled(cancellation_checker, tool_call.task_id):
                return state_store.mark_cancelled(continuation)
            context = state_store.get_pending_policy_context(
                continuation,
                tool_call=tool_call,
                current_workflow=workflow,
            )
            try:
                session = _session_for_tool_call(session_manager, tool_call)
            except Exception:
                session = None
            if (
                session is None
                or not session.controller.is_started
                or session.session_id != continuation.session_id
            ):
                return state_store.mark_session_lost(continuation)
            lease = state_store.claim_action(
                continuation,
                authorization,
                tool_call=tool_call,
                current_workflow=workflow,
                active_session_id=session.session_id,
            )
        except ReplayWorkflowMismatchError:
            try:
                return state_store.load_result(continuation.replay_run_id, current_workflow=workflow)
            except Exception:
                return self._safe_failure(tool_call, continuation.workflow_id, "Workflow version mismatch requires attention", continuation.session_id)
        except ReplayAuthorizationError:
            try:
                return self._safe_from_state(
                    state_store.load_result(continuation.replay_run_id, current_workflow=workflow),
                    "Replay authorization was rejected",
                )
            except Exception:
                return self._safe_failure(tool_call, continuation.workflow_id, "Replay authorization was rejected", continuation.session_id)
        except ReplayStateBusyError:
            return self._safe_failure(tool_call, continuation.workflow_id, "Replay action is already being processed", continuation.session_id)
        except (ReplayStateConflictError, ReplayStateIntegrityError, ReplayStateNotFoundError, WorkflowStoreError):
            try:
                return state_store.load_result(continuation.replay_run_id, current_workflow=workflow)
            except Exception:
                return self._safe_failure(tool_call, continuation.workflow_id, "Replay state could not be validated", continuation.session_id)

        if self._is_cancelled(cancellation_checker, tool_call.task_id):
            return state_store.cancel_claimed_before_dispatch(lease)
        try:
            action_result = self._execute_persisted_action(session.controller, context)
        except Exception:
            try:
                return state_store.mark_action_outcome_unknown(lease)
            except Exception:
                return self._safe_failure(tool_call, continuation.workflow_id, "Action outcome is uncertain; operator review is required", continuation.session_id)

        try:
            result = state_store.complete_action(lease, result=action_result)
        except Exception:
            try:
                return state_store.recover_after_restart(continuation.replay_run_id)
            except Exception:
                return self._safe_failure(tool_call, continuation.workflow_id, "Checkpoint persistence failed; action outcome is uncertain", continuation.session_id)

        if self._is_cancelled(cancellation_checker, tool_call.task_id):
            return state_store.mark_cancelled(result.continuation)
        if result.status != "pending_action" or result.continuation is None:
            return result
        try:
            next_context = state_store.get_pending_policy_context(
                result.continuation,
                tool_call=tool_call,
                current_workflow=workflow,
            )
            handoff = self._policy_handoffs.get(result.replay_run_id) or self.policy_handoff
            if handoff is None:
                return self._safe_from_state(result, "Replay policy handoff is unavailable")
            # Explanation is deliberately generic; sensitive resolved values are
            # neither needed nor fetched for this handoff.
            self._publish_pending(result, next_context, handoff)
            return result
        except Exception:
            return self._safe_from_state(result, "Next replay action could not be handed to policy safely")

    def _continuation_dependencies(self):
        if (
            self.replay_state_store is None
            or self.session_manager is None
            or self.cancellation_checker is None
        ):
            raise ReplayStateStoreError("Replay continuation dependencies are not configured")
        return self.replay_state_store, self.session_manager, self.cancellation_checker

    @staticmethod
    def _is_cancelled(checker: CancellationChecker, task_id: UUID) -> bool:
        try:
            return checker.is_cancelled(task_id) is True
        except Exception:
            # A failed cancellation check must fail closed before dispatch.
            return True

    @staticmethod
    def _execute_persisted_action(controller: BrowserController, context: ReplayPolicyContext) -> dict[str, Any]:
        arguments = context.exact_arguments
        if context.action == "navigate":
            url = arguments.get("url")
            _validate_http_url(url)
            controller.navigate(url)
            return {"executed": True}
        target = arguments.get("target")
        value = arguments.get("value")
        step = WorkflowAction(
            step_id=context.step_id,
            action=context.action,
            target=target,
            input={"value": value} if context.action in {"fill", "select_option"} else {},
        )
        item = _PreparedAction(step, context.action, target, {"value": value} if context.action in {"fill", "select_option"} else {})
        # Reuse the established locator fallback and operation validation.
        replay = WorkflowReplay.__new__(WorkflowReplay)
        replay.controller = controller
        replay._execute(item)
        return {"executed": True}

    @staticmethod
    def _publish_pending(
        result: ReplayExecutionResult,
        context: ReplayPolicyContext,
        handoff: ReplayPolicyHandoff,
    ) -> None:
        pending = result.pending_action
        if pending is None:
            return
        explanation = "Navigate to the workflow start page." if pending.step_id == "workflow.start" else f"Perform the {pending.action} action for workflow step {pending.step_id}."
        if result.continuation is None:
            raise ReplayStateConflictError("Replay continuation is unavailable for policy handoff")
        handoff.publish(
            pending,
            context,
            workflow_digest=result.continuation.workflow_digest,
            explanation=explanation,
        )

    @staticmethod
    def _safe_failure(
        tool_call: ToolCall,
        workflow_id: str | UUID,
        message: str,
        session_id: UUID | None = None,
    ) -> ReplayExecutionResult:
        try:
            workflow_uuid = UUID(str(workflow_id))
            if workflow_uuid.version != 4:
                workflow_uuid = uuid4()
        except (TypeError, ValueError, AttributeError):
            workflow_uuid = uuid4()
        return ReplayExecutionResult(
            task_id=tool_call.task_id,
            replay_run_id=uuid4(),
            workflow_id=workflow_uuid,
            session_id=session_id or uuid4(),
            status="failed",
            error=message,
        )

    @staticmethod
    def _safe_from_state(result: ReplayExecutionResult, message: str) -> ReplayExecutionResult:
        return result.model_copy(update={"status": "failed", "pending_action": None, "error": message})


    def replay(
        self,
        workflow_id: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        approved_step_ids: Collection[str] = (),
    ) -> WorkflowReplayResult:
        try:
            workflow = self.store.load(workflow_id)
        except (WorkflowStoreError, ValueError) as exc:
            return WorkflowReplayResult(
                workflow_id=str(workflow_id), success=False, status="failed",
                error=self._safe_error(str(exc), {}),
            )

        provided = self._parameter_mapping(parameters)
        if isinstance(provided, str):
            return self._failure(workflow, provided)
        resolved_parameters, parameter_error = self._resolve_parameters(workflow, provided)
        if parameter_error:
            return self._failure(workflow, parameter_error)

        prepared, preparation_error = self._prepare_actions(workflow, resolved_parameters)
        if preparation_error:
            sensitive_values = self._sensitive_values(workflow, resolved_parameters)
            return self._failure(workflow, self._safe_error(preparation_error, sensitive_values))

        approvals, approval_error = self._approval_ids(approved_step_ids, workflow)
        if approval_error:
            return self._failure(workflow, approval_error)
        pending_approval = [
            item for item in prepared
            if self._requires_approval(item) and item.step.step_id not in approvals
        ]
        if pending_approval:
            message = "Explicit approval is required before these workflow actions can run"
            return WorkflowReplayResult(
                workflow_id=str(workflow.workflow_id),
                success=False,
                status="awaiting_approval",
                steps=[ReplayStepResult(
                    step_id=item.step.step_id,
                    action=item.action,
                    success=False,
                    message="Explicit approval is required before this action can run",
                    awaiting_approval=True,
                ) for item in pending_approval],
                error=message,
            )
        if not self.controller.is_started:
            return self._failure(workflow, "Browser session is not started")

        sensitive_values = self._sensitive_values(workflow, resolved_parameters)
        result = WorkflowReplayResult(
            workflow_id=str(workflow.workflow_id), success=False, status="failed"
        )
        try:
            self.controller.navigate(str(workflow.start_url))
            result.steps.append(ReplayStepResult(
                step_id="workflow.start", action="navigate", success=True,
                message="Workflow start page loaded",
            ))
        except Exception as exc:
            safe_error = self._safe_error(str(exc), sensitive_values)
            result.steps.append(ReplayStepResult(
                step_id="workflow.start", action="navigate", success=False,
                message="Workflow start page could not be loaded", error=safe_error,
            ))
            result.error = safe_error
            return result

        for item in prepared:
            try:
                fallback_used = self._execute(item)
            except Exception as exc:
                safe_error = self._safe_error(str(exc), sensitive_values)
                result.steps.append(ReplayStepResult(
                    step_id=item.step.step_id, action=item.action, success=False,
                    message="Workflow action failed", error=safe_error,
                ))
                result.status = "failed"
                result.error = safe_error
                return result
            result.steps.append(ReplayStepResult(
                step_id=item.step.step_id, action=item.action, success=True,
                message="Workflow action completed", fallback_used=fallback_used,
            ))

        result.success = True
        result.status = "completed"
        return result

    @staticmethod
    def _parameter_mapping(parameters: Mapping[str, Any] | None) -> dict[str, Any] | str:
        if parameters is None:
            return {}
        if not isinstance(parameters, Mapping):
            return "Workflow parameters must be supplied as a mapping"
        if not all(isinstance(key, str) for key in parameters):
            return "Workflow parameter names must be strings"
        return dict(parameters)

    def _resolve_parameters(
        self,
        workflow: WorkflowDefinition,
        provided: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        definitions: dict[str, WorkflowParameter] = {}
        for parameter in workflow.parameters:
            if parameter.name in definitions:
                return {}, f"Workflow has duplicate parameter '{parameter.name}'"
            if not parameter.name.strip() or "{" in parameter.name or "}" in parameter.name:
                return {}, f"Workflow parameter name '{parameter.name}' is not supported"
            definitions[parameter.name] = parameter

        unknown = sorted(set(provided) - set(definitions))
        if unknown:
            return {}, f"Unknown workflow parameter(s): {', '.join(unknown)}"

        resolved: dict[str, Any] = {}
        missing: list[str] = []
        for name, definition in definitions.items():
            value = provided[name] if name in provided else definition.default
            if isinstance(value, str) and _PLACEHOLDER.fullmatch(value.strip()):
                value = None
            if value is None:
                if definition.required:
                    missing.append(name)
                continue
            validation_error = self._validate_parameter_type(definition, value)
            if validation_error:
                return {}, validation_error
            resolved[name] = value
        if missing:
            return {}, f"Missing required workflow parameter(s): {', '.join(missing)}"

        referenced = self._referenced_parameters(workflow)
        undefined = sorted(referenced - set(definitions))
        if undefined:
            return {}, f"Workflow references undefined parameter(s): {', '.join(undefined)}"
        unresolved = sorted(referenced - set(resolved))
        if unresolved:
            return {}, f"Missing values for workflow parameter(s): {', '.join(unresolved)}"
        return resolved, None

    @staticmethod
    def _validate_parameter_type(parameter: WorkflowParameter, value: Any) -> str | None:
        expected = parameter.type.strip().lower()
        checks = {
            "string": lambda v: isinstance(v, str),
            "str": lambda v: isinstance(v, str),
            "text": lambda v: isinstance(v, str),
            "secret": lambda v: isinstance(v, str),
            "password": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "bool": lambda v: isinstance(v, bool),
            "url": lambda v: isinstance(v, str) and WorkflowReplay._is_http_url(v),
        }
        check = checks.get(expected)
        if check is None:
            return f"Unsupported type '{parameter.type}' for workflow parameter '{parameter.name}'"
        if not check(value):
            return f"Invalid value type for workflow parameter '{parameter.name}'; expected {parameter.type}"
        return None

    @staticmethod
    def _is_http_url(value: str) -> bool:
        try:
            _validate_http_url(value)
            return True
        except BrowserControllerError:
            return False

    def _prepare_actions(
        self,
        workflow: WorkflowDefinition,
        parameters: Mapping[str, Any],
    ) -> tuple[list[_PreparedAction], str | None]:
        prepared: list[_PreparedAction] = []
        seen_step_ids: set[str] = set()
        for step in workflow.steps:
            if step.step_id in seen_step_ids:
                return [], f"Workflow has duplicate step ID '{step.step_id}'"
            seen_step_ids.add(step.step_id)
            action = _ACTION_ALIASES.get(step.action.strip().lower())
            if action is None:
                return [], f"Workflow step '{step.step_id}' uses unsupported action '{step.action}'"
            try:
                target_value = self._substitute(step.target, parameters)
                inputs = self._substitute(step.input, parameters)
            except ValueError as exc:
                return [], f"Workflow step '{step.step_id}' has invalid parameter placeholders: {exc}"
            if not isinstance(inputs, dict):
                return [], f"Workflow step '{step.step_id}' input must be an object"

            if action == "navigate":
                url = inputs.get("url")
                if not isinstance(url, str):
                    return [], f"Workflow step '{step.step_id}' needs a URL string"
                try:
                    _validate_http_url(url)
                except BrowserControllerError:
                    return [], f"Workflow step '{step.step_id}' has an invalid HTTP or HTTPS URL"
            else:
                if not isinstance(target_value, dict):
                    return [], f"Workflow step '{step.step_id}' needs a structured target"
                try:
                    _parse_target(target_value)
                except (TypeError, ValueError) as exc:
                    return [], f"Workflow step '{step.step_id}' has an invalid target: {exc}"
                if action in {"fill", "select_option"}:
                    if "value" not in inputs or not isinstance(inputs["value"], (str, int, float, bool)):
                        return [], f"Workflow step '{step.step_id}' needs a scalar value"
            prepared.append(_PreparedAction(step, action, target_value, inputs))
        return prepared, None

    @staticmethod
    def _substitute(value: Any, parameters: Mapping[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: WorkflowReplay._substitute(child, parameters) for key, child in value.items()}
        if isinstance(value, list):
            return [WorkflowReplay._substitute(child, parameters) for child in value]
        if not isinstance(value, str):
            return value
        remaining = _PLACEHOLDER.sub("", value)
        if "{{" in remaining or "}}" in remaining:
            raise ValueError("placeholder syntax is incomplete")
        matches = list(_PLACEHOLDER.finditer(value))
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            name = matches[0].group(1).strip()
            if name not in parameters:
                raise ValueError(f"'{name}' has no supplied value")
            return parameters[name]

        def replace(match: re.Match[str]) -> str:
            name = match.group(1).strip()
            if name not in parameters:
                raise ValueError(f"'{name}' has no supplied value")
            return str(parameters[name])

        return _PLACEHOLDER.sub(replace, value)

    @staticmethod
    def _referenced_parameters(workflow: WorkflowDefinition) -> set[str]:
        references: set[str] = set()

        def scan(value: Any) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    scan(child)
            elif isinstance(value, list):
                for child in value:
                    scan(child)
            elif isinstance(value, str):
                references.update(match.group(1).strip() for match in _PLACEHOLDER.finditer(value))

        for step in workflow.steps:
            scan(step.target)
            scan(step.input)
        return references

    @staticmethod
    def _approval_ids(
        approved_step_ids: Collection[str], workflow: WorkflowDefinition
    ) -> tuple[set[str], str | None]:
        if isinstance(approved_step_ids, (str, bytes)):
            return set(), "Approvals must be supplied as a collection of step IDs"
        try:
            approvals = set(approved_step_ids)
        except TypeError:
            return set(), "Approvals must be supplied as a collection of step IDs"
        if not all(isinstance(item, str) for item in approvals):
            return set(), "Approved step IDs must be strings"
        known_ids = {step.step_id for step in workflow.steps}
        unknown = sorted(approvals - known_ids)
        if unknown:
            return set(), f"Approval references unknown step ID(s): {', '.join(unknown)}"
        return approvals, None

    def _requires_approval(self, item: _PreparedAction) -> bool:
        tool_name = {
            "navigate": "browser.navigate",
            "click": "browser.click",
            "fill": "browser.fill",
            "select_option": "browser.fill",
        }[item.action]
        arguments = {"target": item.target, **item.inputs}
        description = " ".join(filter(None, (item.step.action, item.step.expected_outcome or "")))
        return action_requires_approval(
            tool_name=tool_name,
            description=description,
            arguments=arguments,
        )

    def _execute(self, item: _PreparedAction) -> bool:
        if item.action == "navigate":
            self.controller.navigate(item.inputs["url"])
            return False

        locator, chosen_target, fallback_used = self._resolve_locator(item)
        if item.action == "click":
            self.controller.click(chosen_target)
        elif item.action == "fill":
            self.controller.fill(chosen_target, self._as_string(item.inputs["value"]))
        elif item.action == "select_option":
            try:
                locator.select_option(
                    value=self._as_string(item.inputs["value"]),
                    timeout=self.controller.timeout_ms,
                )
            except Exception as exc:
                page = self.controller._require_page("select option")
                raise BrowserControllerError("select option", str(exc), url=page.url) from exc
        return fallback_used

    def _resolve_locator(self, item: _PreparedAction):
        page = self.controller._require_page(item.action)
        candidates = self._locator_candidates(item)
        for index, candidate in enumerate(candidates):
            locator = self.controller._locator(page, candidate)
            try:
                count = locator.count()
            except AttributeError:
                # Minimal test doubles and compatible adapters may not expose count.
                if index == 0:
                    return locator, candidate, False
                continue
            except Exception:
                continue
            if count > 0:
                return locator, candidate, index > 0
        raise BrowserControllerError(
            "locate", "No matching element was found using the primary or fallback locators",
            url=page.url,
        )

    @staticmethod
    def _locator_candidates(item: _PreparedAction) -> list[dict[str, Any]]:
        target = item.target or {}
        candidates = [dict(target)]
        kind = target.get("type", target.get("kind"))
        name = target.get("name")
        semantic_label = target.get("aria_label") or target.get("placeholder")
        visible_text = target.get("text") or name

        if semantic_label:
            candidates.append({"type": "label", "value": semantic_label, "exact": True})
        if target.get("placeholder"):
            candidates.append({"type": "placeholder", "value": target["placeholder"], "exact": True})
        if item.action in {"fill", "select_option"} and name and kind == "role":
            candidates.append({"type": "label", "value": name, "exact": True})
        if visible_text and item.action == "click":
            candidates.append({"type": "text", "value": visible_text, "exact": True})
        if target.get("id"):
            css_value = str(target["id"]).replace("\\", "\\\\").replace('"', '\\"')
            candidates.append({"type": "css", "selector": f'[id="{css_value}"]'})

        unique: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for candidate in candidates:
            try:
                parsed = _parse_target(candidate)
            except (TypeError, ValueError):
                continue
            identity = (parsed.kind, parsed.value, parsed.name, parsed.exact)
            if identity not in seen:
                seen.add(identity)
                unique.append(candidate)
        return unique

    @staticmethod
    def _as_string(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (str, int, float)):
            return str(value)
        raise ValueError("Action value must be a string, number, or boolean")

    @staticmethod
    def _sensitive_values(workflow: WorkflowDefinition, parameters: Mapping[str, Any]) -> dict[str, Any]:
        sensitive_names = {
            parameter.name for parameter in workflow.parameters
            if (
                _SENSITIVE.search(parameter.name)
                or _SENSITIVE.search(parameter.type)
                or _SENSITIVE.search(parameter.description)
            )
        }
        for step in workflow.steps:
            target_values = step.target or {}
            if any(_SENSITIVE.search(str(value)) for value in target_values.values()):
                sensitive_names.update(
                    match.group(1).strip()
                    for value in (step.input, step.target)
                    for match in _PLACEHOLDER.finditer(str(value))
                )
        return {name: parameters[name] for name in sensitive_names if name in parameters}

    @staticmethod
    def _safe_error(message: str, sensitive_values: Mapping[str, Any]) -> str:
        sanitized = message
        for value in sensitive_values.values():
            if value is not None:
                encoded = str(value)
                if encoded:
                    sanitized = sanitized.replace(encoded, "[REDACTED]")

        def redact_url(match: re.Match[str]) -> str:
            raw = match.group(0).rstrip(".,;:")
            try:
                parts = urlsplit(raw)
                hostname = parts.hostname or "[host]"
                if ":" in hostname and not hostname.startswith("["):
                    hostname = f"[{hostname}]"
                port = f":{parts.port}" if parts.port is not None else ""
                return f"{parts.scheme}://{hostname}{port}"
            except ValueError:
                return "[URL]"

        return _URL_IN_MESSAGE.sub(redact_url, sanitized)

    @staticmethod
    def _failure(workflow: WorkflowDefinition, error: str) -> WorkflowReplayResult:
        return WorkflowReplayResult(
            workflow_id=str(workflow.workflow_id), success=False, status="failed", error=error
        )
