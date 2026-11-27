"""Safe, session-scoped access to completed, verified document task results."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from backend.app.schemas.contracts import TaskStatus

DOCUMENT_CONTEXT_LOCAL_PROVIDER_REQUIRED = (
    "I found the saved verified document result, but its excerpts may only be sent to local Ollama. "
    "The configured conversation provider is not a verified loopback Ollama endpoint, so I did not send document content. "
    "No file was reopened."
)


_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
_DOCUMENT_FOLLOWUP = re.compile(
    r"\b(document|file|evidence|excerpt|quotation|quote|source|accuracy|claim|finding|findings|limitation|methodology|conclusion|prove|proves|support(?:s|ed)?)\b",
    re.I,
)
_BROWSER_REQUEST = re.compile(
    r"\b(?:browser(?:less)?|website|webpage|web\s+page|browse|navigate|visit)\b|https?://",
    re.I,
)
_FILENAME_REFERENCE = re.compile(r"\b[\w.-]+\.(?:pdf|docx|txt)\b", re.I)


def is_local_ollama_provider(provider: Any) -> bool:
    if getattr(provider, "provider_name", None) != "ollama":
        return False
    base_url = getattr(provider, "base_url", None)
    if not isinstance(base_url, str):
        return False
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _verified_document_payload(task: Any) -> dict[str, Any] | None:
    if task.status != TaskStatus.COMPLETED:
        return None
    supported_tools = {"document.read_local_file", "document.analyze_uploaded"}
    step = next((item for item in task.plan if item.tool_name in supported_tools), None)
    if step is None:
        return None
    result = task.result if isinstance(task.result, dict) else {}
    verified_steps = result.get("verified")
    verified_step = next(
        (item for item in verified_steps if isinstance(item, dict) and item.get("step_id") == step.step_id),
        None,
    ) if isinstance(verified_steps, list) else None
    verification = verified_step.get("verification") if isinstance(verified_step, dict) else None
    tool_result = verification.get("tool_result") if isinstance(verification, dict) and verification.get("verified") is True else None
    if not isinstance(tool_result, dict) or tool_result.get("analysis_status") != "verified_quotes":
        return None
    analysis = tool_result.get("analysis")
    if not isinstance(analysis, dict) or not isinstance(analysis.get("findings"), list):
        return None
    tool_chunk_ids = tool_result.get("analysis_scope_limited_to_chunks")
    if not isinstance(tool_chunk_ids, list) or not all(isinstance(value, str) for value in tool_chunk_ids):
        return None
    allowed_chunk_ids = set(tool_chunk_ids)
    evidence_by_id = {
        str(item.evidence_id): item
        for item in task.evidence
        if str(item.task_id) == str(task.task_id) and item.source_name == tool_result.get("original_filename")
    }
    validated_findings: list[dict[str, str]] = []
    referenced_ids: set[str] = set()
    for finding in analysis["findings"]:
        if not isinstance(finding, dict):
            return None
        evidence_id = str(finding.get("evidence_id", ""))
        quote = finding.get("quote")
        source_chunk_id = finding.get("source_chunk_id")
        if (
            evidence_id not in evidence_by_id
            or not isinstance(quote, str) or not quote
            or source_chunk_id not in allowed_chunk_ids
        ):
            return None
        evidence = evidence_by_id[evidence_id]
        if not isinstance(evidence.excerpt, str) or quote not in evidence.excerpt:
            return None
        if not isinstance(finding.get("statement"), str) or not isinstance(finding.get("kind"), str):
            return None
        referenced_ids.add(evidence_id)
        validated_findings.append({
            "kind": finding["kind"],
            "statement": finding["statement"],
            "quote": quote,
            "evidence_id": evidence_id,
        })
    if not validated_findings:
        return None
    excerpts = [
        {"evidence_id": evidence_id, "source_name": evidence_by_id[evidence_id].source_name,
         "excerpt": evidence_by_id[evidence_id].excerpt}
        for evidence_id in sorted(referenced_ids)
    ]
    if sum(len(item["excerpt"] or "") for item in excerpts) > 12000:
        return None
    return {
        "task_id": str(task.task_id),
        "source_name": tool_result.get("original_filename"),
        "summary": analysis.get("summary", ""),
        "findings": validated_findings,
        "missing_or_unreadable": analysis.get("missing_or_unreadable", []),
        "evidence": excerpts,
        "source_scope": tool_result.get("analysis_scope"),
    }


def recover_legacy_task_links(runtime: Any, session_id: UUID) -> list[str]:
    """Recover IDs from the server's explicit task response format.

    Older clients stored the task ID only in the assistant reply. Persisting a
    reference is safe even after restart: the task remains unavailable until
    TaskManager can actually retrieve it, and no file operation is repeated.
    """
    messages = runtime.conversation_store.get_messages(session_id)
    user_requests = {item["content"] for item in messages if item["role"] == "user"}
    found: list[str] = []
    for message in messages:
        if message["role"] != "assistant":
            continue
        content = message["content"]
        candidates = set()
        for candidate in _UUID.findall(content):
            escaped = re.escape(candidate)
            if re.search(rf"task plan\s+{escaped}\s+is ready|/api/tasks/{escaped}/execute", content, re.I):
                candidates.add(candidate)
        for candidate in candidates:
            try:
                task = runtime.manager.get(candidate)
            except (KeyError, ValueError):
                runtime.conversation_store.link_task(session_id, candidate)
                found.append(candidate)
                continue
            if task.user_request in user_requests:
                runtime.conversation_store.link_task(session_id, task.task_id)
                found.append(str(task.task_id))
    return found


def resolve_document_followup(runtime: Any, session_id: UUID | None, message: str) -> dict[str, Any] | None:
    """Return verified task context or a safe clarification/unavailable state.

    This only reads TaskManager records and persisted task references. It never
    opens a file or authorizes a new task.
    """
    # Browser requests commonly mention evidence, sources, and excerpts too.
    # Those generic words must not attach a prior local-document result to a
    # new web task in the same conversation.
    if session_id is None or _BROWSER_REQUEST.search(message) or not _DOCUMENT_FOLLOWUP.search(message):
        return None
    task_ids = runtime.conversation_store.get_task_ids(session_id)
    if not task_ids:
        task_ids = recover_legacy_task_links(runtime, session_id)
    available: list[tuple[Any, dict[str, Any]]] = []
    unavailable_count = 0
    for task_id in task_ids:
        try:
            # The public TaskManager view deliberately redacts private verifier
            # tool_result data. This trusted, read-only assistant path needs
            # that data to validate saved evidence; it still emits only the
            # whitelisted quotes below and never exposes the raw task result.
            task = runtime.manager.get_for_execution(task_id)
        except (KeyError, ValueError):
            unavailable_count += 1
            continue
        payload = _verified_document_payload(task)
        if payload is not None:
            available.append((task, payload))

    if not available:
        if task_ids and unavailable_count:
            return {"status": "unavailable", "reply": (
                "The earlier document-analysis task is unavailable in the current backend runtime, so I cannot retrieve its verified evidence. "
                "I have not reopened the file. If you want a new analysis, submit the exact file request again and approve it."
            )}
        return None

    has_explicit_filename = bool(_FILENAME_REFERENCE.search(message))
    if has_explicit_filename:
        matched = [item for item in available if item[1]["source_name"].casefold() in message.casefold()]
        if not matched:
            names = ", ".join(dict.fromkeys(item[1]["source_name"] for item in available))
            return {"status": "ambiguous", "reply": (
                f"I don't have a verified analysis for the document named in your question. "
                f"This conversation has verified results for: {names}. Which document should I use?"
            )}
        available = matched

    if unavailable_count and len(task_ids) > 1 and not has_explicit_filename:
        names = ", ".join(dict.fromkeys(item[1]["source_name"] for item in available))
        suffix = f" Available verified results include: {names}." if names else ""
        return {"status": "ambiguous", "reply": (
            "This conversation has multiple prior task references, and at least one result is unavailable after a backend restart. "
            "Please name the document you mean; unavailable files will not be reopened." + suffix
        )}

    if len(available) > 1:
        named = [item for item in available if item[1]["source_name"].casefold() in message.casefold()]
        if len(named) == 1:
            available = named
        else:
            names = ", ".join(dict.fromkeys(item[1]["source_name"] for item in available))
            return {"status": "ambiguous", "reply": f"Which previously analyzed document do you mean: {names}? I will use only its verified saved result."}
    return {"status": "context", "task": available[-1][1]}
