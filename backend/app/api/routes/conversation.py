from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, UUID4, field_validator
from typing_extensions import Annotated

from backend.app.assistant.context import bounded_history, bounded_session_history, generate_assistant_reply
from backend.app.assistant.instructions import ASSISTANT_SYSTEM_INSTRUCTIONS
from backend.app.assistant.reminder_intent import handle_reminder_conversation
from backend.app.assistant.task_context import (
    DOCUMENT_CONTEXT_LOCAL_PROVIDER_REQUIRED,
    is_local_ollama_provider,
    recover_legacy_task_links,
    resolve_document_followup,
)
from backend.app.conversation.store import ConversationStoreError
from backend.app.llm.provider import LLMConfigurationError, LLMProviderError
from backend.app.memory.store import MemoryStoreError
from backend.app.reminders.store import ReminderStoreError
from backend.app.safety.secret_detection import looks_like_secret

router = APIRouter(prefix="/conversation")
sessions_router = APIRouter(prefix="/conversations")
logger = logging.getLogger(__name__)
ConversationText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
ReplyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: ConversationText


class ConversationRequest(BaseModel):
    """Legacy stateless request retained for existing API clients."""

    model_config = ConfigDict(extra="forbid")
    message: ConversationText
    history: list[ConversationTurn] = Field(default_factory=list, max_length=12)
    timezone: str | None = Field(default=None, max_length=100)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("timezone must be a valid IANA time zone") from exc
        return value


class ConversationReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: ReplyText


class ConversationSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID4
    created_at: AwareDatetime
    updated_at: AwareDatetime
    message_count: int = Field(ge=0)


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: UUID4
    session_id: UUID4
    role: Literal["user", "assistant"]
    content: ReplyText
    created_at: AwareDatetime
    sequence: int = Field(ge=1)


class ConversationHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID4
    created_at: AwareDatetime
    updated_at: AwareDatetime
    messages: list[ConversationMessage]


class ConversationTaskReferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID4
    available_task_ids: list[UUID4]
    unavailable_task_ids: list[str]


class ConversationSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: ConversationText
    request_id: UUID4 = Field(default_factory=uuid4)
    timezone: str | None = Field(default=None, max_length=100)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("timezone must be a valid IANA time zone") from exc
        return value


class ConversationExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID4
    reply: ReplyText
    user_message: ConversationMessage
    assistant_message: ConversationMessage


# Preserve the prior module-level name for integrations that imported it.
SYSTEM_PROMPT = ASSISTANT_SYSTEM_INSTRUCTIONS


def _provider_reply(runtime, history: list[dict[str, str]], message: str, task_context=None) -> str:
    return generate_assistant_reply(runtime, history, message, task_context=task_context)


def _raise_provider_error(exc: Exception) -> None:
    if isinstance(exc, LLMConfigurationError):
        raise HTTPException(
            status_code=503,
            detail={"code": "llm_unavailable", "message": "LLM provider is not configured"},
        ) from exc
    if isinstance(exc, LLMProviderError):
        raise HTTPException(
            status_code=503,
            detail={"code": "llm_provider_error", "message": exc.public_message},
        ) from exc


def _bounded_history(runtime, session_id: UUID, message: str) -> list[dict[str, str]]:
    return bounded_session_history(runtime, session_id, message)


def _ensure_session(runtime, session_id: UUID) -> dict[str, object]:
    session = runtime.conversation_store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail={"code": "conversation_not_found", "message": "Conversation session was not found"})
    return session


def _stored_exchange(session_id: UUID, messages: list[dict[str, object]]) -> ConversationExchange:
    user = next(item for item in messages if item["role"] == "user")
    assistant = next(item for item in messages if item["role"] == "assistant")
    return ConversationExchange(
        session_id=session_id,
        reply=assistant["content"],
        user_message=user,
        assistant_message=assistant,
    )


@router.post("", response_model=ConversationReply)
def converse(body: ConversationRequest, request: Request) -> ConversationReply:
    """Backward-compatible, stateless conversation endpoint."""
    runtime = request.app.state.runtime
    try:
        reminder_reply = handle_reminder_conversation(
            runtime, body.message,
            timezone_name=body.timezone or runtime.user_timezone,
            conversation_key=None,
        )
        if reminder_reply is not None:
            return ConversationReply(reply=reminder_reply)
        return ConversationReply(
            reply=_provider_reply(
                runtime,
                bounded_history([turn.model_dump() for turn in body.history], body.message),
                body.message,
            )
        )
    except (LLMConfigurationError, LLMProviderError) as exc:
        _raise_provider_error(exc)
        raise AssertionError("unreachable")
    except MemoryStoreError as exc:
        logger.error("Personal memory lookup failed: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "memory_storage_error", "message": "Personal memory storage is unavailable"}) from exc
    except ReminderStoreError as exc:
        logger.error("Reminder persistence failed: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "reminder_storage_error", "message": "Reminder storage is unavailable"}) from exc


@sessions_router.post("", response_model=ConversationSession, status_code=201)
def create_conversation(request: Request) -> ConversationSession:
    try:
        return ConversationSession.model_validate(request.app.state.runtime.conversation_store.create_session())
    except ConversationStoreError as exc:
        logger.error("Conversation persistence failed: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "conversation_storage_error", "message": "Conversation storage is unavailable"}) from exc


@sessions_router.get("", response_model=list[ConversationSession])
def list_conversations(request: Request) -> list[ConversationSession]:
    try:
        return [ConversationSession.model_validate(item) for item in request.app.state.runtime.conversation_store.list_sessions()]
    except ConversationStoreError as exc:
        logger.error("Conversation persistence failed: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "conversation_storage_error", "message": "Conversation storage is unavailable"}) from exc


@sessions_router.get("/{session_id}", response_model=ConversationHistory)
def get_conversation(session_id: UUID4, request: Request) -> ConversationHistory:
    runtime = request.app.state.runtime
    try:
        session = _ensure_session(runtime, session_id)
        messages = runtime.conversation_store.get_messages(session_id)
        return ConversationHistory(
            session_id=session_id,
            created_at=session["created_at"],
            updated_at=session["updated_at"],
            messages=messages,
        )
    except ConversationStoreError as exc:
        logger.error("Conversation persistence failed: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "conversation_storage_error", "message": "Conversation storage is unavailable"}) from exc


@sessions_router.get("/{session_id}/tasks", response_model=ConversationTaskReferences)
def get_conversation_task_references(session_id: UUID4, request: Request) -> ConversationTaskReferences:
    """Restore task links for the chat UI without duplicating task storage."""
    runtime = request.app.state.runtime
    try:
        _ensure_session(runtime, session_id)
        if not runtime.conversation_store.get_task_ids(session_id):
            recover_legacy_task_links(runtime, session_id)
        task_ids = runtime.conversation_store.get_task_ids(session_id)
        available: list[UUID] = []
        unavailable: list[str] = []
        for task_id in task_ids:
            try:
                task = runtime.manager.get(task_id)
            except (KeyError, ValueError):
                unavailable.append(task_id)
                continue
            available.append(task.task_id)
        return ConversationTaskReferences(
            session_id=session_id,
            available_task_ids=available,
            unavailable_task_ids=unavailable,
        )
    except ConversationStoreError as exc:
        logger.error("Conversation task references unavailable: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "conversation_storage_error", "message": "Conversation task references are unavailable"}) from exc


@sessions_router.delete("/{session_id}", status_code=204)
def delete_conversation(session_id: UUID4, request: Request) -> None:
    try:
        deleted = request.app.state.runtime.conversation_store.delete_session(session_id)
    except ConversationStoreError as exc:
        logger.error("Conversation persistence failed: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "conversation_storage_error", "message": "Conversation storage is unavailable"}) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail={"code": "conversation_not_found", "message": "Conversation session was not found"})


@sessions_router.post("/{session_id}/messages", response_model=ConversationExchange)
def send_conversation_message(session_id: UUID4, body: ConversationSendRequest, request: Request) -> ConversationExchange:
    runtime = request.app.state.runtime
    if looks_like_secret(body.message):
        raise HTTPException(
            status_code=422,
            detail={"code": "sensitive_content_rejected", "message": "Do not send passwords, API keys, or authentication secrets in conversation messages"},
        )
    try:
        _ensure_session(runtime, session_id)
        existing = runtime.conversation_store.find_exchange(session_id, body.request_id)
        if existing:
            if next(item["content"] for item in existing if item["role"] == "user") != body.message:
                raise HTTPException(status_code=409, detail={"code": "request_id_conflict", "message": "Request ID was already used for a different message"})
            return _stored_exchange(session_id, existing)
        reminder_reply = handle_reminder_conversation(
            runtime, body.message,
            timezone_name=body.timezone or runtime.user_timezone,
            conversation_key=str(session_id),
        )
        if reminder_reply is not None:
            if looks_like_secret(reminder_reply):
                raise HTTPException(status_code=503, detail={"code": "sensitive_output_blocked", "message": "The response was not saved because it appeared to contain a credential"})
            messages = runtime.conversation_store.append_exchange(
                session_id, body.request_id, body.message, reminder_reply
            )
            if next(item["content"] for item in messages if item["role"] == "user") != body.message:
                raise HTTPException(status_code=409, detail={"code": "request_id_conflict", "message": "Request ID was already used for a different message"})
            return _stored_exchange(session_id, messages)
        history = _bounded_history(runtime, session_id, body.message)
        task_context = resolve_document_followup(runtime, session_id, body.message)
        if task_context and task_context["status"] in {"ambiguous", "unavailable"}:
            reply = task_context["reply"]
        else:
            verified_document_task = task_context["task"] if task_context and task_context["status"] == "context" else None
            if verified_document_task is not None and not is_local_ollama_provider(runtime.get_conversation_provider()):
                reply = DOCUMENT_CONTEXT_LOCAL_PROVIDER_REQUIRED
            else:
                reply = _provider_reply(runtime, history, body.message, task_context=verified_document_task)
        if looks_like_secret(reply):
            raise HTTPException(
                status_code=503,
                detail={"code": "sensitive_output_blocked", "message": "The response was not saved because it appeared to contain a credential"},
            )
        messages = runtime.conversation_store.append_exchange(
            session_id, body.request_id, body.message, reply
        )
        # Concurrent retries can race at the provider; the unique request key
        # still guarantees one persisted pair and the first stored answer wins.
        if next(item["content"] for item in messages if item["role"] == "user") != body.message:
            raise HTTPException(status_code=409, detail={"code": "request_id_conflict", "message": "Request ID was already used for a different message"})
        return _stored_exchange(session_id, messages)
    except (LLMConfigurationError, LLMProviderError) as exc:
        _raise_provider_error(exc)
        raise AssertionError("unreachable")
    except MemoryStoreError as exc:
        logger.error("Personal memory lookup failed: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "memory_storage_error", "message": "Personal memory storage is unavailable"}) from exc
    except ReminderStoreError as exc:
        logger.error("Reminder persistence failed: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "reminder_storage_error", "message": "Reminder storage is unavailable"}) from exc
    except ConversationStoreError as exc:
        logger.error("Conversation persistence failed: exception_class=%s", type(exc.__cause__ or exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "conversation_storage_error", "message": "Conversation storage is unavailable"}) from exc
