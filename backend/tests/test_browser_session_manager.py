from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from uuid import uuid4

import pytest

from backend.app.browser.session_manager import BrowserSessionManager, BrowserSessionManagerError
from backend.app.schemas.contracts import ToolCall


class FakeController:
    def __init__(self, *, start_error=None, initially_started=False, close_error=None):
        self.is_started = initially_started
        self.start_error = start_error
        self.close_error = close_error
        self.start_count = 0
        self.close_count = 0

    def start(self):
        self.start_count += 1
        if self.start_error:
            raise RuntimeError(self.start_error)
        self.is_started = True
        return self

    def close(self):
        self.close_count += 1
        self.is_started = False
        if self.close_error:
            raise RuntimeError(self.close_error)


def _tool_call(task_id=None):
    return ToolCall(
        task_id=task_id or uuid4(),
        step_id="browser-step",
        tool_name="browser.navigate",
        arguments={},
    )


def test_session_creation_reuse_and_task_isolation():
    controllers = []

    def factory():
        controller = FakeController()
        controllers.append(controller)
        return controller

    manager = BrowserSessionManager(factory)
    call_a = _tool_call()
    call_b = _tool_call()

    session_a = manager.create_session(call_a)
    reused_a = manager.create_session(call_a)
    session_b = manager.create_session(call_b)

    assert session_a.session_id == reused_a.session_id
    assert session_a.controller is reused_a.controller
    assert session_a.session_id != session_b.session_id
    assert session_a.controller is not session_b.controller
    assert len(controllers) == 2
    assert manager.get_session_for_tool_call(call_a) == session_a
    assert manager.get_session_for_tool_call(call_b) == session_b


def test_cross_task_session_reference_is_rejected_and_creation_requires_tool_call():
    manager = BrowserSessionManager(FakeController)
    call_a = _tool_call()
    call_b = _tool_call()
    session_a = manager.create_session(call_a)
    session_b = manager.create_session(call_b)

    assert not manager.validate_session(call_b, session_a.session_id)
    assert manager.validate_session(call_a, session_a.session_id)
    assert manager.get_session_for_tool_call(call_b) == session_b
    with pytest.raises(BrowserSessionManagerError, match="trusted ToolCall"):
        manager.create_session(call_a.task_id)
    non_browser_call = ToolCall(
        task_id=call_a.task_id, step_id="other-tool", tool_name="document.search", arguments={}
    )
    with pytest.raises(BrowserSessionManagerError, match="not authorized"):
        manager.create_session(non_browser_call)


def test_invalid_closed_and_expired_sessions_are_safely_removed():
    now = [100.0]
    controller = FakeController()
    replacement = FakeController()
    controllers = iter([controller, replacement])
    manager = BrowserSessionManager(lambda: next(controllers), ttl_seconds=10, clock=lambda: now[0])
    call = _tool_call()
    session = manager.create_session(call)

    assert manager.get_session(uuid4()) is None
    controller.is_started = False
    assert manager.get_session_for_tool_call(call) is None
    assert controller.close_count == 1

    # A stopped controller can be replaced; idle expiry also closes it.
    second = manager.create_session(call)
    now[0] += 11
    assert manager.get_session_for_tool_call(call) is None
    assert replacement.close_count == 1
    assert not manager.validate_session(call, second.session_id)
    assert session.session_id != second.session_id


def test_cleanup_is_idempotent_and_supports_task_cancellation_hook():
    controller = FakeController(close_error="sensitive cookie diagnostic")
    manager = BrowserSessionManager(lambda: controller)
    call = _tool_call()
    manager.create_session(call)

    assert manager.close_session(call) is False  # close failed but state is detached
    assert manager.close_task_session(call.task_id) is False
    assert controller.close_count == 1
    # A subsequent task/session cleanup is harmless.
    manager.close_all()


@pytest.mark.parametrize("hook_name", ["on_task_completed", "on_task_cancelled", "on_task_error"])
def test_task_lifecycle_hooks_close_only_the_call_owners_session(hook_name):
    controllers = []

    def factory():
        controller = FakeController()
        controllers.append(controller)
        return controller

    manager = BrowserSessionManager(factory)
    call_a, call_b = _tool_call(), _tool_call()
    manager.create_session(call_a)
    manager.create_session(call_b)

    assert getattr(manager, hook_name)(call_a) is True
    assert manager.get_session_for_tool_call(call_a) is None
    assert manager.get_session_for_tool_call(call_b) is not None
    assert [controller.close_count for controller in controllers] == [1, 0]


def test_concurrent_creation_reuses_one_session_and_leases_serialize_access():
    controller = FakeController()
    factory_calls = []

    def factory():
        factory_calls.append(1)
        time.sleep(0.02)
        return controller

    manager = BrowserSessionManager(factory)
    call = _tool_call()
    barrier = threading.Barrier(8)
    active = [0]
    maximum = [0]
    measure_lock = threading.Lock()

    def access():
        barrier.wait()
        with manager.session_lease(call) as session:
            assert session is not None
            with measure_lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            time.sleep(0.01)
            with measure_lock:
                active[0] -= 1
            return session.session_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        session_ids = list(pool.map(lambda _: access(), range(8)))

    assert len(set(session_ids)) == 1
    assert len(factory_calls) == 1
    assert maximum[0] == 1


def test_startup_failure_closes_partial_controller_and_returns_safe_error(caplog):
    secret = "private-browser-storage-diagnostic"
    controller = FakeController(start_error=secret, close_error=secret)
    manager = BrowserSessionManager(lambda: controller)

    with pytest.raises(BrowserSessionManagerError) as error:
        manager.create_session(_tool_call())

    assert str(error.value) == "Browser session could not be started safely"
    assert secret not in str(error.value)
    assert secret not in caplog.text
    assert controller.start_count == 1
    assert controller.close_count == 1


def test_controller_factory_cannot_share_one_controller_between_tasks():
    controller = FakeController()
    manager = BrowserSessionManager(lambda: controller)
    call_a = _tool_call()
    call_b = _tool_call()
    manager.create_session(call_a)

    with pytest.raises(BrowserSessionManagerError, match="another task"):
        manager.create_session(call_b)
    assert controller.close_count == 0
    assert manager.get_session_for_tool_call(call_a) is not None


def test_shutdown_closes_all_task_sessions_once():
    controllers = [FakeController(), FakeController()]
    manager = BrowserSessionManager(iter(controllers).__next__)
    calls = [_tool_call(), _tool_call()]
    for call in calls:
        manager.create_session(call)

    manager.shutdown()
    manager.shutdown()

    assert [controller.close_count for controller in controllers] == [1, 1]
    assert all(manager.get_session_for_tool_call(call) is None for call in calls)
