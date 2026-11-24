"""Explicit intent-to-task bridge; never invokes tools during classification/planning."""
from __future__ import annotations

import logging
import json
import os
from pathlib import PureWindowsPath
from typing import Any
from uuid import uuid4
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints, UUID4
from typing_extensions import Annotated

from backend.app.assistant.context import bounded_session_history, generate_assistant_reply
from backend.app.assistant.intent import (
    AssistantIntent,
    AssistantIntentCategory,
    classify_assistant_intent,
)
from backend.app.assistant.reminder_intent import parse_reminder_command
from backend.app.assistant.task_context import (
    DOCUMENT_CONTEXT_LOCAL_PROVIDER_REQUIRED,
    is_local_ollama_provider,
    resolve_document_followup,
)
from backend.app.documents.local_file import (
    LOCAL_FILE_DEFINITION,
    LocalFileError,
    LocalFileInput,
    local_path_candidate,
    log_local_path_diagnostic,
    parse_explicit_local_file_request,
    prepare_local_file_input,
)
from backend.app.conversation.store import ConversationStoreError
from backend.app.llm.provider import LLMConfigurationError, LLMProviderError
from backend.app.memory.store import MemoryStoreError
from backend.app.orchestrator.planner import PlanError, generate_plan
from backend.app.safety.secret_detection import looks_like_secret
from backend.app.schemas.contracts import TaskCreateRequest, TaskResponse, TaskStep


router = APIRouter(prefix="/assistant")
logger = logging.getLogger(__name__)
RequestText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
ReplyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class AssistantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: RequestText
    session_id: UUID4 | None = None


class AssistantRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: AssistantIntent
    reply: ReplyText
    task: TaskResponse | None = None


def _assistant_response(
    runtime: Any,
    body: AssistantRequest,
    intent: AssistantIntent,
    reply: str,
    task: TaskResponse | None = None,
) -> AssistantRequestResponse:
    """Persist a completed session exchange before returning it to the client."""
    if body.session_id is not None:
        try:
            runtime.conversation_store.append_exchange(
                body.session_id, uuid4(), body.message, reply,
                task_id=task.task_id if task is not None else None,
            )
        except ConversationStoreError as exc:
            logger.error(
                "Assistant conversation persistence failed: exception_class=%s",
                type(exc.__cause__ or exc).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail={"code": "conversation_storage_error", "message": "Conversation storage is unavailable"},
            ) from exc
    return AssistantRequestResponse(intent=intent, reply=reply, task=task)


def _tool_inventory(runtime: Any) -> list[dict[str, Any]]:
    return [
        {
            "tool_name": tool.tool_name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "requires_approval": tool.requires_approval,
        }
        for tool in runtime.registry.list_tools()
    ]


@router.post("/classify", response_model=AssistantIntent)
def classify_request(body: AssistantRequest, request: Request) -> AssistantIntent:
    """Classify a turn without answering it or creating/executing a task.

    The React client uses this boundary to choose between the persistent
    conversation API and the assistant task-preparation API. The same validated
    classifier remains authoritative for the later assistant request.
    """
    runtime = request.app.state.runtime
    if looks_like_secret(body.message):
        raise HTTPException(
            status_code=422,
            detail={"code": "sensitive_content_rejected", "message": "Do not submit passwords, API keys, or authentication secrets"},
        )
    try:
        history: list[dict[str, str]] = []
        if body.session_id is not None:
            if runtime.conversation_store.get_session(body.session_id) is None:
                raise HTTPException(status_code=404, detail={"code": "conversation_not_found", "message": "Conversation session was not found"})
            history = bounded_session_history(runtime, body.session_id, body.message)

        task_context = resolve_document_followup(runtime, body.session_id, body.message)
        if task_context is not None:
            if task_context["status"] == "ambiguous":
                return AssistantIntent(
                    category=AssistantIntentCategory.CLARIFICATION_REQUIRED,
                    current_turn_authorizes_execution=False,
                    clarification_question=task_context["reply"],
                )
            return AssistantIntent(category=AssistantIntentCategory.INFORMATION)

        # Keep deterministic local-file and reminder routing on the backend.
        if parse_explicit_local_file_request(body.message) is not None:
            return AssistantIntent(category=AssistantIntentCategory.ACTION_EXECUTION, current_turn_authorizes_execution=True)
        if _looks_like_file_action(body.message):
            return AssistantIntent(
                category=AssistantIntentCategory.CLARIFICATION_REQUIRED,
                clarification_question="Please provide one exact absolute Windows path to a PDF, DOCX, or TXT file that you want UNBOUND to read.",
            )
        timezone_name = runtime.user_timezone
        if parse_reminder_command(body.message, timezone_name) is not None or (
            body.session_id is not None and runtime.get_reminder_draft(str(body.session_id)) is not None
        ):
            return AssistantIntent(category=AssistantIntentCategory.CONVERSATION)

        provider = runtime.get_provider()
        return classify_assistant_intent(
            body.message,
            provider,
            history=history,
            registered_tools=_tool_inventory(runtime),
        )
    except HTTPException:
        raise
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=503, detail={"code": "llm_unavailable", "message": "LLM provider is not configured"}) from exc
    except LLMProviderError as exc:
        raise HTTPException(status_code=503, detail={"code": "llm_provider_error", "message": exc.public_message}) from exc
    except (ConversationStoreError, MemoryStoreError) as exc:
        logger.error("Assistant classification context unavailable: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "assistant_context_unavailable", "message": "Assistant context is unavailable"}) from exc


def _clarification_response(intent: AssistantIntent, question: str) -> AssistantRequestResponse:
    clarification = AssistantIntent.model_validate({
        **intent.model_dump(),
        "category": AssistantIntentCategory.CLARIFICATION_REQUIRED,
        "current_turn_authorizes_execution": False,
        "clarification_question": question,
    })
    return AssistantRequestResponse(intent=clarification, reply=question)


@router.post("/requests", response_model=AssistantRequestResponse)
async def handle_assistant_request(body: AssistantRequest, request: Request) -> AssistantRequestResponse:
    """Classify a user-controlled request and, when suitable, prepare a task only."""
    runtime = request.app.state.runtime
    if looks_like_secret(body.message):
        raise HTTPException(
            status_code=422,
            detail={"code": "sensitive_content_rejected", "message": "Do not submit passwords, API keys, or authentication secrets"},
        )

    if body.session_id is not None and runtime.conversation_store.get_session(body.session_id) is None:
        raise HTTPException(status_code=404, detail={"code": "conversation_not_found", "message": "Conversation session was not found"})

    try:
        if body.session_id is not None:
            task_context = resolve_document_followup(runtime, body.session_id, body.message)
            if task_context is not None:
                if task_context["status"] in {"ambiguous", "unavailable"}:
                    intent = AssistantIntent(
                        category=(AssistantIntentCategory.CLARIFICATION_REQUIRED
                                  if task_context["status"] == "ambiguous"
                                  else AssistantIntentCategory.INFORMATION),
                        current_turn_authorizes_execution=False,
                        clarification_question=(task_context["reply"] if task_context["status"] == "ambiguous" else None),
                    )
                    return _assistant_response(runtime, body, intent, task_context["reply"])
                history = bounded_session_history(runtime, body.session_id, body.message)
                provider = runtime.get_conversation_provider()
                reply = (
                    generate_assistant_reply(runtime, history, body.message, task_context=task_context["task"])
                    if is_local_ollama_provider(provider)
                    else DOCUMENT_CONTEXT_LOCAL_PROVIDER_REQUIRED
                )
                return _assistant_response(
                    runtime, body,
                    AssistantIntent(category=AssistantIntentCategory.INFORMATION),
                    reply,
                )
    except (ConversationStoreError, MemoryStoreError) as exc:
        logger.error("Task context lookup failed: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "assistant_context_unavailable", "message": "Assistant context is unavailable"}) from exc

    if os.getenv("UNBOUND_LOCAL_PATH_DIAGNOSTICS") == "1":
        try:
            raw_payload = json.loads(await request.body())
            raw_message = raw_payload.get("message") if isinstance(raw_payload, dict) else None
            log_local_path_diagnostic(
                "api.raw_request_body.message",
                local_path_candidate(raw_message) if isinstance(raw_message, str) else None,
            )
        except (ValueError, TypeError):
            # The normal FastAPI validation path handles malformed request bodies.
            pass

    # This narrow, deterministic path makes explicit file access a first-class
    # task without asking an LLM to infer the path or authorize its access.
    # The regular conversation endpoint remains response-only.
    # `body.message` is FastAPI/Pydantic's decoded request text. The opt-in
    # diagnostic reports only its path candidate, never the surrounding message.
    log_local_path_diagnostic("api.request.message", local_path_candidate(body.message))
    parsed_file_request = parse_explicit_local_file_request(body.message)
    if parsed_file_request is not None:
        if body.session_id is not None and runtime.conversation_store.get_session(body.session_id) is None:
            raise HTTPException(status_code=404, detail={"code": "conversation_not_found", "message": "Conversation session was not found"})
        exact_path, analysis_request = parsed_file_request
        log_local_path_diagnostic("api.parser.output", exact_path)
        if not any(tool.tool_name == LOCAL_FILE_DEFINITION.tool_name for tool in runtime.registry.list_tools()):
            return _assistant_response(
                runtime, body,
                AssistantIntent(category=AssistantIntentCategory.ACTION_EXECUTION, current_turn_authorizes_execution=True),
                "Local file reading is unavailable in this runtime. No file was accessed.",
            )
        try:
            tool_input = prepare_local_file_input(exact_path, analysis_request)
            log_local_path_diagnostic("api.pre_validation_tool_input", str(tool_input.get("file_path", "")))
            validated_input = runtime.registry.validate_input(LOCAL_FILE_DEFINITION.tool_name, tool_input)
            if getattr(validated_input, "file_path", None) != exact_path:
                raise LocalFileError("The validated path did not preserve the exact path supplied by the user")
            log_local_path_diagnostic("api.post_validation_tool_input", str(validated_input.file_path))
            # Carry the validated snapshot forward verbatim. In particular, do
            # not normalize Windows path components such as 8.3 names (KARTHI~1).
            tool_input = validated_input.model_dump()
        except (LocalFileError, ValueError) as exc:
            clarification = AssistantIntent(
                category=AssistantIntentCategory.CLARIFICATION_REQUIRED,
                clarification_question="I couldn't validate that exact local file path. Please provide an existing absolute Windows path to a PDF, DOCX, or TXT file within the configured size limit.",
            )
            return _assistant_response(runtime, body, clarification, clarification.clarification_question)

        intent = AssistantIntent(
            category=AssistantIntentCategory.ACTION_EXECUTION,
            current_turn_authorizes_execution=True,
        )
        task_request = TaskCreateRequest(user_request=body.message, auto_execute=False)
        filename = PureWindowsPath(exact_path).name
        step = TaskStep(
            step_id="read_and_analyse_file",
            tool_name=LOCAL_FILE_DEFINITION.tool_name,
            description=f"UNBOUND requests permission to read and analyse {filename} at {exact_path}. The file will not be opened until you approve this exact action.",
            input=tool_input,
            requires_approval=True,
            expected_outcome="The selected file is extracted, stored in the local document index, and its analysis is grounded in retrievable source excerpts.",
        )
        try:
            task = runtime.manager.create(task_request, [step])
        except ValueError:
            raise HTTPException(status_code=422, detail={"code": "invalid_local_file_task", "message": "The local file request could not be prepared safely"})
        log_local_path_diagnostic("api.task_plan_input", str(task.plan[0].input.get("file_path", "")))
        reply = (
            f"I can read {filename} at {exact_path}. I have not opened it. "
            f"To review this exact read request, call POST /api/tasks/{task.task_id}/execute; "
            "that will create a pending approval. Approve that approval explicitly, then call execute again."
        )
        return _assistant_response(runtime, body, intent, reply, task)

    # A clear file action with a Windows drive prefix but no single supported
    # path is a clarification, never a fallthrough to generic planning.
    if _looks_like_file_action(body.message):
        clarification = AssistantIntent(
            category=AssistantIntentCategory.CLARIFICATION_REQUIRED,
            clarification_question="Please provide one exact absolute Windows path to a PDF, DOCX, or TXT file that you want UNBOUND to read.",
        )
        return _assistant_response(runtime, body, clarification, clarification.clarification_question)

    try:
        history: list[dict[str, str]] = []
        if body.session_id is not None:
            if runtime.conversation_store.get_session(body.session_id) is None:
                raise HTTPException(status_code=404, detail={"code": "conversation_not_found", "message": "Conversation session was not found"})
            history = bounded_session_history(runtime, body.session_id, body.message)

        provider = runtime.get_provider()
        intent = classify_assistant_intent(
            body.message,
            provider,
            history=history,
            registered_tools=_tool_inventory(runtime),
        )

        if intent.category in {
            AssistantIntentCategory.CONVERSATION,
            AssistantIntentCategory.INFORMATION,
            AssistantIntentCategory.DRAFTING,
        }:
            reply = generate_assistant_reply(runtime, history, body.message)
            return _assistant_response(runtime, body, intent, reply)

        if intent.category == AssistantIntentCategory.CLARIFICATION_REQUIRED:
            return _assistant_response(
                runtime, body, intent,
                intent.clarification_question or "Could you clarify what you want me to do?",
            )

        if not runtime.registry.list_tools():
            return _assistant_response(
                runtime, body, intent,
                "I cannot prepare this task because no tools are currently registered for it. No action was taken.",
            )

        task_request = TaskCreateRequest(user_request=body.message, auto_execute=False)
        plan = generate_plan(task_request, runtime.registry, provider)
        if plan.missing_information:
            question = intent.clarification_question or f"Please clarify: {plan.missing_information[0]}"
            clarification_response = _clarification_response(intent, question)
            return _assistant_response(runtime, body, clarification_response.intent, clarification_response.reply)
        task = runtime.manager.create(task_request, plan.steps)
        approval_steps = [step.step_id for step in task.plan if step.requires_approval]
        step_summary = "; ".join(f"{step.step_id}: {step.description}" for step in task.plan)
        reply = f"Task plan {task.task_id} is ready. Nothing has been executed. Steps: {step_summary}."
        if approval_steps:
            reply += f" Approval will be required for step(s): {', '.join(approval_steps)}."
        reply += f" To proceed, explicitly call POST /api/tasks/{task.task_id}/execute."
        return _assistant_response(runtime, body, intent, reply, task)
    except HTTPException:
        raise
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=503, detail={"code": "llm_unavailable", "message": "LLM provider is not configured"}) from exc
    except LLMProviderError as exc:
        raise HTTPException(status_code=503, detail={"code": "llm_provider_error", "message": exc.public_message}) from exc
    except PlanError as exc:
        logger.warning("Assistant task planning rejected: %s", type(exc).__name__)
        raise HTTPException(status_code=422, detail={"code": "invalid_plan", "message": "The request could not be planned safely"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_assistant_request", "message": "The request could not be converted into a valid task"}) from exc
    except ConversationStoreError as exc:
        logger.error("Conversation context unavailable: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "conversation_storage_error", "message": "Conversation context is unavailable"}) from exc
    except MemoryStoreError as exc:
        logger.error("Approved memory retrieval failed: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "memory_storage_error", "message": "Personal memory storage is unavailable"}) from exc


def _looks_like_file_action(message: str) -> bool:
    import re

    return bool(
        re.match(r"^\s*(?:(?:please|can you|could you|would you)\s+)?(?:(?:open|read|analy[sz]e|summari[sz]e|review|inspect|extract|explain)\b|give\s+me\s+(?:a\s+)?summary\s+of\b)", message, re.IGNORECASE)
        and re.search(r"\b(?:file|document|pdf|docx|txt)\b|[A-Za-z]:\\", message, re.IGNORECASE)
    )
