"""Approval policy for consequential external actions."""
from __future__ import annotations

import re
from typing import Any


SENSITIVE_TERMS = (
    "purchase", "buy", "pay", "payment", "checkout", "place order", "confirm order",
    "send", "email", "message", "submit", "apply", "delete", "remove", "publish",
    "book", "transfer", "subscribe", "sign up",
)
_SENSITIVE_PATTERN = re.compile(r"\b(?:" + "|".join(re.escape(term) for term in SENSITIVE_TERMS) + r")\b", re.IGNORECASE)
_SAFE_CLICK_PATTERN = re.compile(
    r"\b(?:search|find|view|open|show|expand|filter|sort|refresh|download|read|details|next page|previous page|back)\b",
    re.IGNORECASE,
)


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)


def _click_target(arguments: dict[str, Any] | None) -> tuple[str, str, str]:
    args = arguments or {}
    target = args.get("target", args.get("locator"))
    if target is None:
        target = {key: args[key] for key in ("selector", "text", "name", "label", "role", "accessible_name", "aria_label", "href") if key in args}
    pieces = list(_strings(target))
    return " ".join(pieces), str(target.get("role", "")).lower() if isinstance(target, dict) else "", str(target.get("href", "")).lower() if isinstance(target, dict) else ""


def requires_approval(action: str, metadata: dict[str, Any] | None = None) -> bool:
    meta = metadata or {}
    if meta.get("requires_approval") or meta.get("irreversible") or meta.get("sensitive"):
        return True
    return _SENSITIVE_PATTERN.search(action) is not None


def action_requires_approval(
    *,
    tool_name: str,
    description: str,
    arguments: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Classify a concrete tool action; ambiguous generic clicks fail closed."""
    meta = metadata or {}
    if meta.get("requires_approval") or meta.get("irreversible") or meta.get("sensitive"):
        return True
    action_fields = {
        "action", "action_type", "operation", "operation_type", "intent", "target", "locator",
        "selector", "name", "label", "text", "role", "accessible_name", "aria_label", "href",
        "button", "button_text", "form_action", "submit", "submit_action", "confirm", "confirmation",
        "recipient", "destination",
    }
    action_context = [description]
    for key, value in arguments.items():
        if key.lower() in action_fields:
            action_context.extend(_strings(value))
    if _SENSITIVE_PATTERN.search(" ".join(action_context)):
        return True
    # A replay may perform actions stored behind a workflow ID, outside the call's
    # arguments. Until the registry can classify the trusted stored workflow,
    # require approval rather than trusting caller-supplied step summaries.
    if tool_name == "browser.replay":
        return True
    if tool_name == "browser.click":
        target_text, role, href = _click_target(arguments)
        if role in {"link", "navigation"} and href.startswith(("http://", "https://", "/", "#")):
            return False
        # A button/action is safe only when its accessible target clearly names a
        # read-only operation. Opaque selectors and unknown labels need approval.
        return _SAFE_CLICK_PATTERN.search(target_text) is None
    # Inspect structured action/target metadata while ignoring ordinary search
    # query/value payloads, which can mention sensitive topics without acting on them.
    return False


def approval_granted(*, required: bool, approved: bool) -> bool:
    return not required or approved
