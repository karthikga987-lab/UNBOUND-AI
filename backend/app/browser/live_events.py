"""Bounded, task-scoped live browser event buffer for the React Live View."""
from __future__ import annotations

import base64
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import struct
import threading
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4


MAX_LIVE_EVENTS_PER_TASK = 120
MAX_LIVE_TASKS = 64
MAX_LIVE_SCREENSHOT_BYTES = 512 * 1024


def _safe_url(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
        netloc = f"[{host}]" if ":" in host else host
        if port is not None:
            netloc += f":{port}"
        return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", "", ""))[:1024]
    except (ValueError, UnicodeError):
        return None


class BrowserLiveEventStore:
    """Transient event stream; task ID is the only lookup authority."""

    def __init__(self, *, max_events_per_task: int = MAX_LIVE_EVENTS_PER_TASK, max_tasks: int = MAX_LIVE_TASKS) -> None:
        if max_events_per_task < 4 or max_tasks < 1:
            raise ValueError("live event store limits are invalid")
        self._max_events_per_task = max_events_per_task
        self._max_tasks = max_tasks
        self._events: OrderedDict[UUID, list[dict]] = OrderedDict()
        self._lock = threading.RLock()
        self._sequence: dict[UUID, int] = {}
        self._session: dict[UUID, str] = {}

    def latest_session_id(self, task_id: UUID) -> str | None:
        with self._lock:
            return self._session.get(task_id)

    def publish(
        self,
        task_id: UUID,
        event_type: str,
        description: str,
        *,
        step_id: str | None = None,
        session_id: str | UUID | None = None,
        url: str | None = None,
        title: str | None = None,
        provider: str | None = None,
        screenshot_png: bytes | None = None,
    ) -> dict:
        if not isinstance(task_id, UUID) or task_id.version != 4:
            raise ValueError("a valid task ID is required for live browser events")
        if not isinstance(event_type, str) or not event_type or len(event_type) > 64:
            raise ValueError("live event type is invalid")
        if not isinstance(description, str) or not description:
            raise ValueError("live event description is required")
        safe_session = str(session_id)[:80] if session_id else None
        safe_title = title[:300] if isinstance(title, str) else None
        safe_provider = provider[:64] if isinstance(provider, str) else None
        safe_url = _safe_url(url)
        screenshot = None
        if screenshot_png is not None:
            if (
                not isinstance(screenshot_png, bytes)
                or not screenshot_png.startswith(b"\x89PNG\r\n\x1a\n")
                or len(screenshot_png) > MAX_LIVE_SCREENSHOT_BYTES
            ):
                raise ValueError("live screenshot is not a bounded PNG")
            screenshot = {
                "mime_type": "image/png",
                "data": base64.b64encode(screenshot_png).decode("ascii"),
                "width": struct.unpack(">I", screenshot_png[16:20])[0] if len(screenshot_png) >= 24 else None,
                "height": struct.unpack(">I", screenshot_png[20:24])[0] if len(screenshot_png) >= 24 else None,
                "sha256": hashlib.sha256(screenshot_png).hexdigest(),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
        with self._lock:
            sequence = self._sequence.get(task_id, 0) + 1
            self._sequence[task_id] = sequence
            if safe_session:
                self._session[task_id] = safe_session
            event = {
                "contract_version": "1.0",
                "event_id": str(uuid4()),
                "task_id": str(task_id),
                "session_id": safe_session or self._session.get(task_id),
                "sequence": sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": event_type,
                "description": description[:300],
                "step_id": step_id[:120] if isinstance(step_id, str) else None,
                "url": safe_url,
                "metadata": {
                    key: value for key, value in {
                        "stage": event_type,
                        "page_title": safe_title,
                        "provider": safe_provider,
                        "screenshot_sha256": screenshot["sha256"] if screenshot else None,
                    }.items() if value is not None
                },
            }
            if screenshot is not None:
                event["screenshot"] = screenshot
            events = self._events.setdefault(task_id, [])
            # Keep at most one screenshot payload per task. The latest screenshot
            # remains available to a reconnecting Live View without accumulating
            # large base64 frames in memory.
            if screenshot is not None:
                events[:] = [item for item in events if "screenshot" not in item]
            events.append(event)
            if len(events) > self._max_events_per_task:
                del events[:len(events) - self._max_events_per_task]
            self._events.move_to_end(task_id)
            while len(self._events) > self._max_tasks:
                old_task, _ = self._events.popitem(last=False)
                self._sequence.pop(old_task, None)
                self._session.pop(old_task, None)
            return dict(event)

    def after(self, task_id: UUID, sequence: int = 0) -> list[dict]:
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            return []
        with self._lock:
            return [dict(event) for event in self._events.get(task_id, ()) if event["sequence"] > sequence]

    def clear(self, task_id: UUID) -> None:
        with self._lock:
            self._events.pop(task_id, None)
            self._sequence.pop(task_id, None)
            self._session.pop(task_id, None)
