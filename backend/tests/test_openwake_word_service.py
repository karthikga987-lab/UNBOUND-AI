from __future__ import annotations

import time

import pytest

from backend.app.wake_word.openwakeword_detector import OpenWakeWordDetector
from backend.app.wake_word.service import OpenWakeWordService, PHRASE, WakeWordUnavailable


class FakeModel:
    def __init__(self, values):
        self.values = iter(values)
        self.reset_calls = 0
        self.frames = []

    def predict(self, frame):
        self.frames.append(frame)
        return {"hey-unbound": next(self.values)}

    def reset(self):
        self.reset_calls += 1


class FakeEngine:
    frame_length = 1280
    last_score = 0.77

    def __init__(self, detect=False):
        self.detect = detect
        self.read_count = 0
        self.deleted = 0

    def process(self, frame):
        self.read_count += 1
        return 0 if self.detect else -1

    def delete(self):
        self.deleted += 1


class FakeRecorder:
    def __init__(self, engine):
        self.engine = engine
        self.started = 0
        self.stopped = 0
        self.deleted = 0

    def start(self):
        self.started += 1

    def read(self):
        time.sleep(0.002)
        return [0] * self.engine.frame_length

    def stop(self):
        self.stopped += 1

    def delete(self):
        self.deleted += 1


def make_service(tmp_path, *, detect=False, model_name="hey-unbound.onnx", threshold=0.64):
    model = tmp_path / model_name
    model.write_bytes(b"fake-onnx-placeholder")
    features = tmp_path / "features"
    features.mkdir()
    (features / "melspectrogram.onnx").write_bytes(b"fake-mel")
    (features / "embedding_model.onnx").write_bytes(b"fake-embedding")
    engines = []
    recorders = []

    def engine_factory(**kwargs):
        engine = FakeEngine(detect)
        engine.received = kwargs
        engines.append(engine)
        return engine

    def recorder_factory(**kwargs):
        recorder = FakeRecorder(engines[-1])
        recorders.append(recorder)
        return recorder

    service = OpenWakeWordService(
        engine_factory=engine_factory,
        recorder_factory=recorder_factory,
        model_path=lambda: model,
        feature_dir=lambda: features,
        model_phrase=lambda path: "different phrase" if path.name != "hey-unbound.onnx" else "hey unbound",
        threshold=lambda: threshold,
        recovery_delay=0.001,
    )
    return service, engines, recorders, model, features


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_detector_uses_model_threshold_and_exact_custom_onnx_path(tmp_path):
    model_path = tmp_path / "hey-unbound.onnx"
    features = tmp_path / "features"
    features.mkdir()
    mel = features / "melspectrogram.onnx"
    embedding = features / "embedding_model.onnx"
    created = {}
    model = FakeModel([0.4, 0.71])

    def factory(**kwargs):
        created.update(kwargs)
        return model

    detector = OpenWakeWordDetector(
        model_path=model_path,
        melspec_model_path=mel,
        embedding_model_path=embedding,
        threshold=0.65,
        model_factory=factory,
    )
    assert created["wakeword_models"] == [str(model_path)]
    assert created["inference_framework"] == "onnx"
    assert detector.process([0] * 1280) == -1
    assert detector.last_score == 0.4
    assert detector.process([1] * 1280) == 0
    assert detector.last_score == 0.71
    assert len(model.frames) == 2
    detector.delete()
    assert model.reset_calls == 1


def test_validation_loads_candidate_without_opening_microphone(tmp_path):
    service, engines, recorders, _, _ = make_service(tmp_path)
    result = service.validate()
    assert result == {"valid": True, "phrase": PHRASE, "microphone_opened": False}
    assert engines[0].received["sensitivity"] == 0.64
    assert engines[0].deleted == 1
    assert recorders == []


def test_missing_model_or_feature_models_fails_before_detector_or_mic(tmp_path):
    service, _, recorders, model, features = make_service(tmp_path)
    model.unlink()
    with pytest.raises(WakeWordUnavailable, match="trained Hey UNBOUND .onnx"):
        service.start()
    assert recorders == []

    model.write_bytes(b"fake")
    (features / "embedding_model.onnx").unlink()
    with pytest.raises(WakeWordUnavailable, match="feature models are missing"):
        service.validate()
    assert recorders == []


def test_threshold_must_be_bounded_and_numeric(tmp_path):
    service, _, _, _, _ = make_service(tmp_path, threshold=1.1)
    with pytest.raises(WakeWordUnavailable, match="UNBOUND_WAKE_THRESHOLD"):
        service.validate()

    service._sensitivity = lambda: float("nan")
    with pytest.raises(WakeWordUnavailable, match="UNBOUND_WAKE_THRESHOLD"):
        service.validate()


def test_mock_detection_publishes_score_after_resources_are_released(tmp_path):
    service, engines, recorders, _, _ = make_service(tmp_path, detect=True)
    service.start()
    assert wait_until(lambda: service.status()["status"] == "activated")
    assert service.event_after(0) == {"event_id": 1, "phrase": "hey unbound", "confidence": 0.77}
    assert recorders[0].deleted == 1
    assert engines[0].deleted == 1
    service.stop()


def test_standalone_model_name_mismatch_is_a_clear_error(tmp_path):
    service, _, _, _, _ = make_service(tmp_path, model_name="different-phrase.onnx", detect=True)
    with pytest.raises(WakeWordUnavailable, match="exact phrase"):
        service.validate()
