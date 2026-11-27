"""Content-minimized, credential-safe execution diagnostics."""
from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel


_SAFE_CODE = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
_SAFE_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9.+-]{1,80}/[A-Za-z0-9.+-]{1,80}$")
_SENSITIVE_KEY = re.compile(r"(?i)(?:authorization|cookie|password|secret|token|credential|private.?key|api.?key|headers?)")


def _uuid4(value: Any) -> str | None:
    try:
        parsed = UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
    return str(parsed) if parsed.version == 4 else None


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        host = parts.hostname.encode("idna").decode("ascii").lower()
        try:
            address = ipaddress.ip_address(host)
            host = f"[{host}]" if address.version == 6 else host
        except ValueError:
            pass
        port = parts.port
        netloc = host + (f":{port}" if port is not None else "")
        return urlunsplit((parts.scheme.lower(), netloc, parts.path[:512] or "/", "", ""))[:1024]
    except (ValueError, UnicodeError):
        return None


def _excerpt_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    encoded = value.encode("utf-8", errors="replace")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "characters": len(value)}


def _result_count(data: dict[str, Any]) -> int | None:
    for key in ("result_count", "total", "count"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    results = data.get("results")
    return len(results) if isinstance(results, list) else None


def safe_tool_response_diagnostic(tool_name: str, response: Any, *, stage: str) -> dict[str, Any]:
    """Return outcome-recoverable metadata without retaining page/document text or secrets."""
    if isinstance(response, BaseModel):
        try:
            payload = response.model_dump(mode="json")
        except Exception:
            try:
                payload = response.model_dump()
            except Exception:
                payload = {}
    elif isinstance(response, dict):
        payload = response
    else:
        payload = {}

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    data = data if isinstance(data, dict) else {}
    results = data.get("results")
    result_items: list[dict[str, Any]] = []
    document_ids: set[str] = set()
    direct_id = _uuid4(data.get("document_id"))
    if direct_id:
        document_ids.add(direct_id)
    if isinstance(results, list):
        for item in results[:100]:
            if not isinstance(item, dict):
                continue
            document_id = _uuid4(item.get("document_id"))
            if document_id:
                document_ids.add(document_id)
            entry: dict[str, Any] = {}
            if document_id:
                entry["document_id"] = document_id
            evidence_id = _uuid4(item.get("evidence_id"))
            if evidence_id:
                entry["evidence_id"] = evidence_id
            for key, limit in (("filename", 160), ("source_name", 160), ("page", 16), ("section", 160)):
                value = item.get(key)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    entry[key] = str(value)[:limit] if isinstance(value, str) else value
            score = item.get("relevance_score", item.get("score"))
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                entry["relevance_score"] = float(score)
            source_url = _safe_url(item.get("source_url") or item.get("url"))
            if source_url:
                entry["source_url"] = source_url
            excerpt = _excerpt_metadata(item.get("excerpt"))
            if excerpt:
                entry["excerpt"] = excerpt
            result_items.append(entry)

    evidence_ids: list[str] = []
    for evidence in payload.get("evidence", []) if isinstance(payload.get("evidence"), list) else []:
        evidence_id = _uuid4(evidence.get("evidence_id")) if isinstance(evidence, dict) else None
        if evidence_id:
            evidence_ids.append(evidence_id)

    observation = data.get("page_observation")
    page_summary = None
    screenshot_reference = None
    if isinstance(observation, dict):
        page_summary = {}
        safe_url = _safe_url(observation.get("url"))
        if safe_url:
            page_summary["url"] = safe_url
        title_meta = _excerpt_metadata(observation.get("title"))
        text_meta = _excerpt_metadata(observation.get("visible_text"))
        if title_meta:
            page_summary["title"] = title_meta
        if text_meta:
            page_summary["visible_text"] = text_meta
        digest = observation.get("screenshot_sha256")
        if isinstance(digest, str) and re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            screenshot_reference = f"sha256:{digest.lower()}"
            page_summary["screenshot_sha256"] = digest.lower()
        if not page_summary:
            page_summary = None

    for evidence in payload.get("evidence", []) if isinstance(payload.get("evidence"), list) else []:
        if not isinstance(evidence, dict):
            continue
        name = evidence.get("source_name")
        excerpt = evidence.get("excerpt")
        if isinstance(name, str) and "screenshot" in name.casefold() and isinstance(excerpt, str):
            match = re.search(r"\b[a-fA-F0-9]{64}\b", excerpt)
            if match:
                screenshot_reference = f"sha256:{match.group(0).lower()}"
                break

    safe_fields: dict[str, Any] = {}
    for key in ("failure_code", "extraction_status", "analysis_status", "file_type", "page_count", "chunk_count", "status"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and key in {"page_count", "chunk_count"} and value >= 0:
            safe_fields[key] = value
        elif isinstance(value, str):
            token = value[:100]
            if key == "file_type" and _SAFE_MEDIA_TYPE.fullmatch(token):
                safe_fields[key] = token
            elif key != "file_type" and _SAFE_CODE.fullmatch(token):
                safe_fields[key] = token

    # Field names are useful for identifying result normalization defects, but
    # values such as prompts, excerpts, cookies, and authorization are excluded.
    field_names = sorted(
        str(key)[:80] for key in data
        if isinstance(key, str) and not _SENSITIVE_KEY.search(key)
    )[:80]
    success = payload.get("success")
    diagnostic = {
        "stage": stage[:32],
        "tool_name": tool_name[:100],
        "response_type": type(response).__name__[:80],
        "success": success if isinstance(success, bool) else None,
        "result_count": _result_count(data),
        "document_ids": sorted(document_ids),
        "results": result_items,
        "evidence_ids": sorted(set(evidence_ids)),
        "screenshot_reference": screenshot_reference,
        "page_observation": page_summary,
        "safe_fields": safe_fields,
        "data_field_names": field_names,
    }
    return diagnostic


def result_error_category(tool_name: str, diagnostic: dict[str, Any], *, phase: str) -> str | None:
    """Map execution facts to stable diagnostic categories without raw error text."""
    if phase == "persistence":
        return "persistence_failure"
    if phase == "unknown":
        return "unknown_external_outcome"
    if tool_name == "document.search":
        if diagnostic.get("success") is not True:
            return "search_extraction_failure"
        if diagnostic.get("result_count") == 0:
            return "search_no_results"
    if tool_name.startswith("browser.") and diagnostic.get("success") is not True:
        return "browser_operation_failure"
    return None
