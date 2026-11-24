"""Bounded, provenance-preserving context assembly for conversation replies."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from fastapi import HTTPException

from backend.app.assistant.instructions import ASSISTANT_SYSTEM_INSTRUCTIONS


_PROVIDER_CONNECTIVITY_QUERY = re.compile(
    r"(?:\b(?:connected|connection|reachable|responding|available)\b.{0,80}\b(?:local\s+(?:language\s+)?model|ollama|qwen|llm)\b"
    r"|\b(?:local\s+(?:language\s+)?model|ollama|qwen|llm)\b.{0,80}\b(?:connected|connection|reachable|responding|available)\b)",
    re.IGNORECASE,
)


def _positive_setting(name: str, default: int, *, minimum: int = 1, maximum: int = 100000) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "invalid_conversation_config", "message": "Conversation limits are misconfigured"},
        ) from exc
    if value < minimum or value > maximum:
        raise HTTPException(
            status_code=500,
            detail={"code": "invalid_conversation_config", "message": "Conversation limits are misconfigured"},
        )
    return value


def bounded_history(
    history: list[dict[str, str]],
    message: str,
    *,
    max_turns: int | None = None,
    max_chars: int | None = None,
) -> list[dict[str, str]]:
    """Drop oldest complete exchanges without truncating any message text."""
    turn_limit = max_turns or _positive_setting("CONVERSATION_MAX_TURNS", 12, maximum=100)
    char_limit = max_chars or _positive_setting(
        "CONVERSATION_MAX_CONTEXT_CHARS", 12000, minimum=4000, maximum=64000
    )
    bounded = [dict(turn) for turn in history[-(turn_limit * 2):]]
    while bounded and sum(len(turn["content"]) for turn in bounded) + len(message) > char_limit:
        bounded = bounded[2:]
    return bounded


def bounded_session_history(runtime: Any, session_id: Any, message: str) -> list[dict[str, str]]:
    turn_limit = _positive_setting("CONVERSATION_MAX_TURNS", 12, maximum=100)
    stored = runtime.conversation_store.get_messages(session_id, limit=turn_limit * 2)
    history = [{"role": item["role"], "content": item["content"]} for item in stored]
    return bounded_history(history, message, max_turns=turn_limit)


def _approved_memory_context(runtime: Any, history: list[dict[str, str]], message: str) -> list[dict[str, Any]]:
    # The store enforces approved+confirmed status and the user's retrieval toggle.
    matches = runtime.memory_store.retrieve_relevant(
        " ".join([*(turn["content"] for turn in history[-4:]), message]), limit=5
    )
    selected: list[dict[str, Any]] = []
    used_chars = 0
    for item in matches:
        content = item["content"]
        if used_chars + len(content) > 3000:
            continue
        selected.append(item)
        used_chars += len(content)
    return selected


def build_conversation_context(
    runtime: Any,
    history: list[dict[str, str]],
    message: str,
    task_context: dict[str, Any] | None = None,
) -> str:
    """Serialize context as data in the user prompt, never as system instructions."""
    tools = [
        {
            "tool_name": definition.tool_name,
            "description": definition.description,
            "requires_approval": definition.requires_approval,
        }
        for definition in runtime.registry.list_tools()
    ]
    payload = {
        "history": history,
        "relevant_approved_memories": _approved_memory_context(runtime, history, message),
        "registered_task_tools": tools,
        # Task results are quoted, validated evidence data, never instructions
        # or authorization. Only the caller's current turn can request action.
        "previous_verified_document_task": task_context,
        "current_user_message": message,
    }
    return json.dumps(payload, ensure_ascii=False)


def generate_assistant_reply(
    runtime: Any,
    history: list[dict[str, str]],
    message: str,
    task_context: dict[str, Any] | None = None,
) -> str:
    provider = runtime.get_conversation_provider()
    generated = provider.generate_text(
        system=ASSISTANT_SYSTEM_INSTRUCTIONS,
        prompt=build_conversation_context(runtime, history, message, task_context),
        max_tokens=256,
        reasoning_effort="none" if getattr(provider, "provider_name", None) == "ollama" else None,
    )
    if task_context is not None:
        generated = _ensure_verified_document_citation(generated, task_context)
    # A successful bounded generation is the connectivity check. Keep the
    # conclusion deterministic so the model cannot mistake configuration for
    # reachability or claim local Ollama when another provider answered.
    if _PROVIDER_CONNECTIVITY_QUERY.search(message):
        provider_name = getattr(provider, "provider_name", None)
        if provider_name == "ollama":
            model = getattr(provider, "model", None)
            model_detail = f" using configured model {model}" if isinstance(model, str) and model else ""
            return (
                f"Yes. Ollama responded successfully to a live request{model_detail}. "
                "That confirms the connection worked for this reply; availability can change."
            )
        return (
            "The configured language model provider responded successfully to a live request. "
            "I cannot confirm from this response that it is a local model."
        )
    return generated


def _ensure_verified_document_citation(reply: str, task_context: dict[str, Any]) -> str:
    """Keep at least one exact, validated source quote and evidence ID visible."""
    findings = task_context.get("findings")
    source_name = task_context.get("source_name")
    if not isinstance(findings, list) or not isinstance(source_name, str):
        return reply
    cited = [
        item for item in findings
        if isinstance(item, dict)
        and isinstance(item.get("quote"), str)
        and item["quote"] in reply
        and isinstance(item.get("evidence_id"), str)
    ]
    if cited:
        missing_ids = [item for item in cited if item["evidence_id"] not in reply]
        if not missing_ids:
            return reply
        citations = ", ".join(dict.fromkeys(item["evidence_id"] for item in missing_ids))
        return f"{reply.rstrip()}\n\nVerified source: {source_name} (evidence ID: {citations})."

    # The model may omit its citations or quote only a paraphrase. Include up
    # to three source-validated findings so the user can inspect exact text.
    excerpts = []
    for item in findings[:3]:
        if not isinstance(item, dict) or not isinstance(item.get("quote"), str) or not isinstance(item.get("evidence_id"), str):
            continue
        excerpts.append(
            f'> "{item["quote"]}"\n> Source: {source_name}; evidence ID: {item["evidence_id"]}'
        )
    if not excerpts:
        return reply
    return f"{reply.rstrip()}\n\nVerified source excerpts:\n" + "\n".join(excerpts)
