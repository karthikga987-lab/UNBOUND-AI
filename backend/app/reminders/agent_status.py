"""Read-only status-file helpers shared by the agent and existing health route."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AGENT_DIR_NAME = "UNBOUND_AI"
INSTALL_FILE_NAME = "notification-agent.install.json"
HEARTBEAT_FILE_NAME = "notification-agent.heartbeat.json"
TASK_NAME = "UNBOUND Local Reminder Notifications"


def agent_directory() -> Path:
    local = os.getenv("LOCALAPPDATA")
    if local:
        return Path(local) / AGENT_DIR_NAME
    return Path.home() / ".unbound-ai"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def notification_agent_status(*, now: datetime | None = None) -> dict[str, Any]:
    directory = agent_directory()
    install_marker = _read_json(directory / INSTALL_FILE_NAME)
    scheduled = _scheduled_task_exists() if install_marker is not None else False
    # A marker without a task is definitively uninstalled. If Windows could not
    # answer the task query, report unknown instead of claiming either state.
    installed = (scheduled if scheduled is not None else None) if install_marker is not None else False
    heartbeat = _read_json(directory / HEARTBEAT_FILE_NAME)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    running = False
    if heartbeat:
        try:
            seen = datetime.fromisoformat(str(heartbeat["heartbeat_at"]))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            interval = max(15, int(heartbeat.get("poll_interval_seconds", 60)))
            running = 0 <= (current - seen).total_seconds() <= interval * 3 + 15
        except (KeyError, TypeError, ValueError):
            running = False
    return {"installed": installed, "running": running}


def _scheduled_task_exists() -> bool | None:
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", TASK_NAME],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


def write_agent_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{os.urandom(4).hex()}.tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
