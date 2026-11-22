"""Approval policy for consequential external actions."""
from __future__ import annotations

from typing import Any


SENSITIVE_TERMS = ("purchase", "buy", "pay", "payment", "send", "email", "message", "submit", "apply", "delete", "publish", "book", "transfer")


def requires_approval(action: str, metadata: dict[str, Any] | None = None) -> bool:
    meta = metadata or {}
    if meta.get("requires_approval") or meta.get("irreversible") or meta.get("sensitive"):
        return True
    text = action.lower()
    return any(term in text for term in SENSITIVE_TERMS)


def approval_granted(*, required: bool, approved: bool) -> bool:
    return not required or approved
