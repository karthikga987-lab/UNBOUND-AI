from __future__ import annotations

import time
import threading

import pytest
from fastapi.testclient import TestClient

from backend.app.api.runtime import TaskRuntime
from backend.app.conversation.store import SQLiteConversationStore
from backend.app.main import create_app
from backend.app.wake_word.service import PHRASE, PorcupineWakeWordService, WakeWordUnavailable


class FakeEngine:
    frame_length = 512

    def __init__(self, detection_after: int | None = None):
        self.detection_after = detection_after
        self.read_count = 0
        self.deleted = 0

    def process(self, frame):
        self.read_count += 1
        return 0 if self.detection_after and self.read_count >= self.detection_after else -1

    def delete(self):
        self.deleted += 1


class FakeRecorder:
    def __init__(self, engine, *, fail_first_read=False):
        self.engine = engine
        self.fail_first_read = fail_first_read
        self.started = 0
        self.stopped = 0
        self.deleted = 0

    def start(self):
        self.started += 1

    def read(self):
        if self.fail_first_read:
            self.fail_first_read = False
            raise OSError("synthetic device interruption")
        time.sleep(0.002)
        return [0] * self.engine.frame_length

    def stop(self):
        self.stopped += 1

    def delete(self):
        self.deleted += 1


def configured_service(tmp_path, *, detection_after=None, recorder_builder=None, engine_builder=None, access_key="test-key"):
    model = tmp_path / "hey-unbound.ppn"
    model.write_bytes(b"synthetic-model-placeholder")
    engines = []
    recorders = []

    def engine_factory(**kwargs):
        if engine_builder:
            return engine_builder(**kwargs)
        engine = FakeEngine(detection_after)
        engines.append(engine)
        return engine

    def recorder_factory(**kwargs):
        engine = engines[-1]
        recorder = recorder_builder(engine, **kwargs) if recorder_builder else FakeRecorder(engine)
        recorders.append(recorder)
        return recorder

    service = PorcupineWakeWordService(
        engine_factory=engine_factory,
        recorder_factory=recorder_factory,
        access_key=lambda: access_key,
        model_path=lambda: model,
        recovery_delay=0.001,
    )
    return service, model, engines, recorders


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_missing_key_and_model_fail_without_opening_microphone(tmp_path):
    recorder_calls = []
    service = PorcupineWakeWordService(
        engine_factory=lambda **kwargs: FakeEngine(),
        recorder_factory=lambda **kwargs: recorder_calls.append(True),
        access_key=lambda: None,
        model_path=lambda: tmp_path / "missing.ppn",
    )
    with pytest.raises(WakeWordUnavailable, match="PICOVOICE_ACCESS_KEY"):
        service.start()
    assert recorder_calls == []
    service._access_key = lambda: "test-key"
    with pytest.raises(WakeWordUnavailable, match="Hey UNBOUND .ppn"):
        service.validate()
    assert recorder_calls == []


def test_invalid_model_initialization_failure_and_python_version_are_reported(tmp_path):
    service, model, _, recorders = configured_service(tmp_path, engine_builder=lambda **kwargs: (_ for _ in ()).throw(ValueError("bad model")))
    with pytest.raises(WakeWordUnavailable, match="could not initialize"):
        service.validate()
    assert recorders == []
    model.write_text("still only a placeholder", encoding="utf-8")
    with pytest.raises(WakeWordUnavailable):
        service.validate()
    with pytest.raises(WakeWordUnavailable, match="Python 3.9"):
        service._ensure_supported_python((3, 8, 19))


def test_validation_initializes_model_without_opening_microphone(tmp_path):
    service, _, engines, recorders = configured_service(tmp_path)
    result = service.validate()
    assert result == {"valid": True, "phrase": PHRASE, "microphone_opened": False}
    assert engines[0].deleted == 1
    assert recorders == []


def test_detection_releases_microphone_before_publishing_event_and_stops_cleanly(tmp_path):
    service, _, engines, recorders = configured_service(tmp_path, detection_after=1)
    assert service.start()["status"] == "listening"
    assert wait_until(lambda: service.status()["status"] == "activated")
    event = service.event_after(0)
    assert event == {"event_id": 1, "phrase": "hey unbound", "confidence": 1.0}
    assert recorders[0].deleted == 1
    assert engines[0].deleted == 1
    assert service.event_after(1) is None
    service.stop()
    service.stop()


def test_explicit_stop_releases_active_audio_resources_idempotently(tmp_path):
    service, _, engines, recorders = configured_service(tmp_path)
    service.start()
    assert wait_until(lambda: engines and engines[0].read_count > 0)
    assert service.stop()["status"] == "disabled"
    assert service.stop()["status"] == "disabled"
    assert recorders[0].deleted == 1
    assert engines[0].deleted == 1


def test_stop_wins_over_a_frame_returning_during_shutdown(tmp_path):
    entered_read = threading.Event()
    release_read = threading.Event()

    class BlockingRecorder(FakeRecorder):
        def read(self):
            entered_read.set()
            release_read.wait(1)
            return [0] * self.engine.frame_length

        def stop(self):
            self.stopped += 1
            release_read.set()

    service, _, engines, _ = configured_service(
        tmp_path,
        detection_after=1,
        recorder_builder=lambda engine, **kwargs: BlockingRecorder(engine),
    )
    service.start()
    assert entered_read.wait(1)
    assert service.stop()["status"] == "disabled"
    assert service.event_after(0) is None
    assert engines[0].read_count == 0


def test_repeated_detection_is_debounced_across_immediate_restart(tmp_path):
    service, _, _, recorders = configured_service(tmp_path, detection_after=1)
    service.start()
    assert wait_until(lambda: service.status()["status"] == "activated")
    service.stop()
    service.start()
    time.sleep(0.03)
    assert service.status()["status"] == "listening"
    assert service.event_after(0)["event_id"] == 1
    service.stop()
    assert recorders[1].stopped >= 1


def test_wake_listener_releases_microphone_when_voice_ui_lease_expires(tmp_path):
    service, _, engines, recorders = configured_service(tmp_path)
    service.start()
    assert wait_until(lambda: engines and engines[0].read_count > 0)
    service._client_last_seen = time.monotonic() - 6
    assert wait_until(lambda: service.status()["status"] == "disabled")
    assert recorders[0].deleted == 1
    assert engines[0].deleted == 1


def test_audio_interruption_attempts_bounded_recorder_recovery(tmp_path):
    service, _, engines, recorders = configured_service(
        tmp_path,
        detection_after=1,
        recorder_builder=lambda engine, **kwargs: FakeRecorder(engine, fail_first_read=len(recorders) == 0),
    )
    service.start()
    assert wait_until(lambda: service.status()["status"] == "activated")
    assert len(recorders) == 2
    assert recorders[0].deleted == 1
    assert engines[0].deleted == 1
    service.stop()


def test_microphone_permission_failure_is_clear_and_cleans_engine(tmp_path):
    class DeniedRecorder(FakeRecorder):
        def start(self):
            raise PermissionError("synthetic access denied")

    service, _, engines, _ = configured_service(
        tmp_path,
        recorder_builder=lambda engine, **kwargs: DeniedRecorder(engine),
    )
    with pytest.raises(WakeWordUnavailable, match="microphone.*permissions"):
        service.start()
    assert service.status()["status"] == "error"
    assert engines[0].deleted == 1


def test_api_reports_configuration_failure_and_application_shutdown_releases_service(tmp_path):
    service, _, _, _ = configured_service(tmp_path, access_key=None)
    runtime = TaskRuntime(conversation_store=SQLiteConversationStore(tmp_path / "wake-api.sqlite3"), wake_word_service=service)
    with TestClient(create_app(runtime)) as client:
        assert client.get("/api/wake-word/status").json()["status"] == "disabled"
        assert client.post("/api/wake-word/validate").status_code == 503
        assert client.post("/api/wake-word/start").json()["detail"]["code"] == "wake_word_unavailable"
    assert service.status()["status"] == "disabled"


def test_detection_event_is_local_state_and_does_not_create_or_execute_tasks(tmp_path):
    service, _, _, _ = configured_service(tmp_path, detection_after=1)
    runtime = TaskRuntime(conversation_store=SQLiteConversationStore(tmp_path / "wake-no-task.sqlite3"), wake_word_service=service)
    client = TestClient(create_app(runtime))
    client.post("/api/wake-word/start")
    assert wait_until(lambda: client.get("/api/wake-word/events").json()["event"] is not None)
    assert runtime.manager.repository._tasks == {}
    client.close()


def test_application_shutdown_releases_active_wake_microphone(tmp_path):
    service, _, engines, recorders = configured_service(tmp_path)
    runtime = TaskRuntime(conversation_store=SQLiteConversationStore(tmp_path / "wake-shutdown.sqlite3"), wake_word_service=service)
    with TestClient(create_app(runtime)) as client:
        assert client.post("/api/wake-word/start").status_code == 200
        assert wait_until(lambda: engines and engines[0].read_count > 0)
    assert service.status()["status"] == "disabled"
    assert recorders[0].deleted == 1
    assert engines[0].deleted == 1
