"""Task-isolated lifecycle management for browser controllers.

Session creation and ownership are tied to a trusted shared ``ToolCall``. This
module does not accept task IDs from planner argument dictionaries.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
from threading import RLock
from time import monotonic
from typing import Callable, Iterator
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from backend.app.browser.controller import BrowserController
from backend.app.schemas.contracts import ToolCall


class BrowserSessionManagerError(RuntimeError):
    """Safe, non-diagnostic browser session lifecycle failure."""


@dataclass(frozen=True)
class BrowserSessionBinding:
    """Task-owned browser controller and opaque session identifier."""

    session_id: UUID
    controller: BrowserController


@dataclass
class _ManagedSession:
    task_id: UUID
    binding: BrowserSessionBinding
    expires_at: float


class BrowserSessionManager:
    """Create and isolate one Playwright controller per authorized task.

    ``get_session(task_id)`` is retained for the Phase 9 injected interface and
    is intended for trusted backend callers. Public browser adapters should use
    ``session_lease(tool_call)`` or ``get_session_for_tool_call(tool_call)`` so
    ownership always comes from the orchestrator's ToolCall.
    """

    def __init__(
        self,
        controller_factory: Callable[[], BrowserController] | None = None,
        *,
        ttl_seconds: float = 1800.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
            raise ValueError("ttl_seconds must be a positive finite number")
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive finite number")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._controller_factory = controller_factory or BrowserController
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._sessions: dict[UUID, _ManagedSession] = {}
        self._controller_owners: dict[int, UUID] = {}
        self._guard = RLock()
        self._task_locks: WeakValueDictionary[UUID, RLock] = WeakValueDictionary()

    def create_session(self, tool_call: ToolCall) -> BrowserSessionBinding:
        """Create or reuse the active session owned by this trusted ToolCall."""
        task_id = self._task_id_from_call(tool_call)
        task_lock = self._task_lock(task_id)
        with task_lock:
            existing = self.get_session(task_id)
            if existing is not None:
                return existing

            controller: BrowserController | None = None
            controller_id: int | None = None
            try:
                controller = self._controller_factory()
                if controller is None or not callable(getattr(controller, "start", None)):
                    raise TypeError("invalid controller factory result")
                controller_id = id(controller)
                with self._guard:
                    owner = self._controller_owners.get(controller_id)
                    if owner is not None and owner != task_id:
                        # This controller is live and belongs to another task;
                        # never close it while rejecting the invalid factory.
                        controller = None
                        raise BrowserSessionManagerError("Browser controller is already assigned to another task")
                    self._controller_owners[controller_id] = task_id

                controller.start()
                if not controller.is_started:
                    raise RuntimeError("browser controller did not start")
                binding = BrowserSessionBinding(session_id=uuid4(), controller=controller)
                managed = _ManagedSession(
                    task_id=task_id,
                    binding=binding,
                    expires_at=self._clock() + self._ttl_seconds,
                )
                with self._guard:
                    self._sessions[task_id] = managed
                return binding
            except BrowserSessionManagerError:
                self._close_quietly(controller)
                with self._guard:
                    if controller_id is not None and self._controller_owners.get(controller_id) == task_id:
                        self._controller_owners.pop(controller_id, None)
                raise
            except Exception:
                self._close_quietly(controller)
                with self._guard:
                    if controller_id is not None and self._controller_owners.get(controller_id) == task_id:
                        self._controller_owners.pop(controller_id, None)
                raise BrowserSessionManagerError("Browser session could not be started safely") from None

    def get_session(self, task_id: UUID) -> BrowserSessionBinding | None:
        """Retrieve a live session by its task key; stale sessions are closed."""
        if not isinstance(task_id, UUID) or task_id.version != 4:
            return None
        task_lock = self._task_lock(task_id)
        with task_lock:
            with self._guard:
                managed = self._sessions.get(task_id)
                if managed is None:
                    return None
                expired = self._clock() >= managed.expires_at
            try:
                live = managed.binding.controller.is_started
            except Exception:
                live = False
            if expired or not live:
                self._close_task_session_locked(task_id, expected_session_id=managed.binding.session_id)
                return None
            with self._guard:
                # Recheck identity in case a lifecycle operation replaced it.
                current = self._sessions.get(task_id)
                if current is not managed:
                    return None
                current.expires_at = self._clock() + self._ttl_seconds
                return current.binding

    def get_session_for_tool_call(self, tool_call: ToolCall) -> BrowserSessionBinding | None:
        """Resolve only the task identified by a trusted shared ToolCall."""
        try:
            task_id = self._task_id_from_call(tool_call)
        except BrowserSessionManagerError:
            return None
        return self.get_session(task_id)

    def get_or_create_session(self, tool_call: ToolCall) -> BrowserSessionBinding:
        """Resolve the task's current session or create an isolated one."""
        return self.create_session(tool_call)

    def validate_session(self, tool_call: ToolCall, session_id: UUID) -> bool:
        """Validate that an opaque session reference belongs to this task."""
        if not isinstance(session_id, UUID):
            return False
        binding = self.get_session_for_tool_call(tool_call)
        return binding is not None and binding.session_id == session_id

    @contextmanager
    def session_lease(
        self,
        tool_call: ToolCall,
        *,
        create: bool = True,
        expected_session_id: UUID | None = None,
    ) -> Iterator[BrowserSessionBinding | None]:
        """Serialize access and cleanup for one task's browser controller."""
        task_id = self._task_id_from_call(tool_call)
        task_lock = self._task_lock(task_id)
        with task_lock:
            binding = self.get_session(task_id)
            if binding is None and create:
                binding = self.create_session(tool_call)
            if expected_session_id is not None and (
                binding is None or binding.session_id != expected_session_id
            ):
                binding = None
            yield binding

    def close_session(self, tool_call: ToolCall) -> bool:
        """Close the session owned by a trusted ToolCall; repeated calls are safe."""
        return self.close_task_session(self._task_id_from_call(tool_call))

    def on_task_completed(self, tool_call: ToolCall) -> bool:
        """Lifecycle hook for orchestrator task completion."""
        return self.close_session(tool_call)

    def on_task_cancelled(self, tool_call: ToolCall) -> bool:
        """Lifecycle hook for orchestrator cancellation."""
        return self.close_session(tool_call)

    def on_task_error(self, tool_call: ToolCall) -> bool:
        """Lifecycle hook for terminal task errors."""
        return self.close_session(tool_call)

    def close_task_session(self, task_id: UUID) -> bool:
        """Lifecycle hook for trusted task completion, cancellation, or teardown."""
        if not isinstance(task_id, UUID) or task_id.version != 4:
            return False
        task_lock = self._task_lock(task_id)
        with task_lock:
            return self._close_task_session_locked(task_id)

    def close_expired_sessions(self) -> int:
        """Close all currently expired sessions; safe to call from a host scheduler."""
        now = self._clock()
        with self._guard:
            expired = [
                (task_id, managed.binding.session_id)
                for task_id, managed in self._sessions.items()
                if now >= managed.expires_at
            ]
        closed = 0
        for task_id, session_id in expired:
            task_lock = self._task_lock(task_id)
            with task_lock:
                if self._close_task_session_locked(task_id, expected_session_id=session_id):
                    closed += 1
        return closed

    def close_all(self) -> None:
        """Shutdown hook that closes all owned browser resources idempotently."""
        with self._guard:
            sessions = [
                (task_id, managed.binding.session_id)
                for task_id, managed in self._sessions.items()
            ]
        for task_id, session_id in sessions:
            task_lock = self._task_lock(task_id)
            with task_lock:
                self._close_task_session_locked(task_id, expected_session_id=session_id)

    shutdown = close_all

    def _close_task_session_locked(
        self,
        task_id: UUID,
        *,
        expected_session_id: UUID | None = None,
    ) -> bool:
        with self._guard:
            managed = self._sessions.get(task_id)
            if managed is None or (
                expected_session_id is not None
                and managed.binding.session_id != expected_session_id
            ):
                return False
            self._sessions.pop(task_id, None)
            self._controller_owners.pop(id(managed.binding.controller), None)
        return self._close_quietly(managed.binding.controller)

    @staticmethod
    def _task_id_from_call(tool_call: ToolCall) -> UUID:
        if not isinstance(tool_call, ToolCall):
            raise BrowserSessionManagerError("A trusted ToolCall is required for browser session access")
        if not tool_call.tool_name.startswith("browser."):
            raise BrowserSessionManagerError("ToolCall is not authorized for browser session access")
        task_id = tool_call.task_id
        if not isinstance(task_id, UUID) or task_id.version != 4:
            raise BrowserSessionManagerError("ToolCall task identity is invalid")
        return task_id

    def _task_lock(self, task_id: UUID) -> RLock:
        with self._guard:
            return self._task_locks.setdefault(task_id, RLock())

    @staticmethod
    def _close_quietly(controller: BrowserController | None) -> bool:
        if controller is None:
            return True
        try:
            controller.close()
            return True
        except Exception:
            # Browser diagnostics may contain URLs or page data; never return/log them.
            return False
