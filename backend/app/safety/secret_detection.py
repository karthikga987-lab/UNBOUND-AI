"""Best-effort checks for obvious credential strings before local retention."""
from __future__ import annotations

import re

_SECRET_PATTERNS = (
    re.compile(
        r"\b(?:password|passwd|pwd|api[_ -]?key|access[_ -]?token|client[_ -]?secret|"
        r"refresh[_ -]?token|auth[_ -]?token)(?:\s*[:=]\s*|\s+is\s+)\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def looks_like_secret(value: str) -> bool:
    """Recognize common credential formats; this is intentionally not a DLP guarantee."""
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)
