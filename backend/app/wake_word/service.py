"""Opt-in Porcupine service with exclusive local microphone ownership."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import platform
import sys
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)
PHRASE = "hey unbound"
DEFAULT_SENSITIVITY = 0.55
COOLDOWN_SECONDS = 2.0
MAX_AUDIO_RECOVERY_ATTEMPTS = 2
CLIENT_LEASE_SECONDS = 5.0


class WakeWordUnavailable(RuntimeError):
    """The exact custom model or runtime prerequisite is missing or invalid."""


def default_model_path() -> Path:
    return Path(__file__).resolve().parents[3] / "backend" / "models" / "wake-word" / "hey-unbound.ppn"


def default_openwakeword_model_path() -> Path:
    return Path(__file__).resolve().parents[3] / "backend" / "models" / "wake-word" / "hey-unbound.onnx"


def default_openwakeword_feature_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "backend" / "models" / "wake-word" / "features"


class PorcupineWakeWordService:
    """Owns one Porcupine handle and recorder only after an explicit start call."""

    def __init__(
        self,
        *,
        engine_factory: Callable[..., Any] | None = None,
        recorder_factory: Callable[..., Any] | None = None,
        access_key: Callable[[], str | None] | None = None,
        model_path: Callable[[], Path] | None = None,
        sensitivity: Callable[[], float] | None = None,
        recovery_delay: float = 0.25,
    ) -> None:
        self._engine_factory = engine_factory or self._default_engine_factory
        self._recorder_factory = recorder_factory or self._default_recorder_factory
        self._access_key = access_key or (lambda: os.getenv("PICOVOICE_ACCESS_KEY"))
        self._model_path = model_path or (lambda: Path(os.getenv("UNBOUND_WAKE_MODEL") or default_model_path()))
        self._sensitivity = sensitivity or (lambda: float(os.getenv("UNBOUND_WAKE_SENSITIVITY", str(DEFAULT_SENSITIVITY))))
        self._recovery_delay = recovery_delay
        self._uses_default_runtime = engine_factory is None and recorder_factory is None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._engine: Any | None = None
        self._recorder: Any | None = None
        self._status = "disabled"
        self._detail = "Wake-word monitoring is off. Start it explicitly to open the microphone."
        self._event_id = 0
        self._event: dict[str, object] | None = None
        self._last_detection_at = 0.0
        self._client_last_seen = 0.0

    @staticmethod
    def _default_engine_factory(*, access_key: str, keyword_path: str, sensitivity: float):
        try:
            import pvporcupine
        except ImportError as exc:
            raise WakeWordUnavailable("Optional wake dependencies are missing; run scripts/wake-word.ps1 install.") from exc
        return pvporcupine.create(access_key=access_key, keyword_paths=[keyword_path], sensitivities=[sensitivity])

    @staticmethod
    def _default_recorder_factory(*, frame_length: int):
        try:
            from pvrecorder import PvRecorder
        except ImportError as exc:
            raise WakeWordUnavailable("Optional microphone recorder dependency is missing; run scripts/wake-word.ps1 install.") from exc
        return PvRecorder(frame_length=frame_length, device_index=-1)

    def _configuration(self) -> tuple[str, Path, float]:
        self._ensure_supported_python(sys.version_info[:3])
        if self._uses_default_runtime and (os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64", "arm64", "aarch64"}):
            raise WakeWordUnavailable("The optional wake runtime is supported here only on Windows x64/ARM64.")
        key = self._access_key()
        if not key:
            raise WakeWordUnavailable("Set PICOVOICE_ACCESS_KEY in the backend process environment.")
        model = self._model_path().expanduser().resolve()
        if model.suffix.lower() != ".ppn" or not model.is_file():
            raise WakeWordUnavailable("Set UNBOUND_WAKE_MODEL to the custom Hey UNBOUND .ppn model file.")
        try:
            sensitivity = self._sensitivity()
        except (TypeError, ValueError) as exc:
            raise WakeWordUnavailable("UNBOUND_WAKE_SENSITIVITY must be a number from 0.0 to 1.0.") from exc
        if not 0.0 <= sensitivity <= 1.0:
            raise WakeWordUnavailable("UNBOUND_WAKE_SENSITIVITY must be a number from 0.0 to 1.0.")
        return key, model, sensitivity

    @staticmethod
    def _ensure_supported_python(version: tuple[int, ...]) -> None:
        if version < (3, 9):
            raise WakeWordUnavailable("Porcupine requires Python 3.9 or newer.")

    def _create_engine(self):
        key, model, sensitivity = self._configuration()
        try:
            return self._engine_factory(access_key=key, keyword_path=str(model), sensitivity=sensitivity)
        except WakeWordUnavailable:
            raise
        except Exception as exc:
            logger.warning("wake engine initialization failed exception_class=%s", type(exc).__name__)
            raise WakeWordUnavailable("Porcupine could not initialize this key/model/runtime combination.") from exc

    def validate(self) -> dict[str, object]:
        """Initialize and release the detector without opening the microphone."""
        engine = self._create_engine()
        try:
            return {"valid": True, "phrase": PHRASE, "microphone_opened": False}
        finally:
            engine.delete()

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "status": self._status,
                "enabled": self._status in {"listening", "recovering", "activated"},
                "local_audio": True,
                "phrase": PHRASE,
                "detail": self._detail,
                "event_id": self._event_id,
            }

    def event_after(self, event_id: int) -> dict[str, object] | None:
        with self._lock:
            if self._status == "listening":
                self._client_last_seen = time.monotonic()
            if self._event and int(self._event["event_id"]) > event_id:
                return dict(self._event)
            return None

    def _client_lease_expired(self) -> bool:
        with self._lock:
            if self._status != "listening":
                return False
            expired = time.monotonic() - self._client_last_seen > CLIENT_LEASE_SECONDS
            if expired:
                self._status = "disabled"
                self._detail = "Wake UI disconnected; local microphone monitoring was stopped and released."
                self._stop.set()
            return expired

    def start(self) -> dict[str, object]:
        with self._lock:
            if self._status == "listening":
                return self.status()
            if self._thread and self._thread.is_alive():
                raise WakeWordUnavailable("The previous wake listener is still shutting down; retry in a moment.")
            self._stop.clear()
            try:
                self._engine = self._create_engine()
                self._recorder = self._create_recorder(self._engine)
                self._recorder.start()
            except WakeWordUnavailable as exc:
                self._release_resources()
                self._status, self._detail = "unavailable", str(exc)
                raise
            except Exception as exc:
                logger.warning("wake microphone start failed exception_class=%s", type(exc).__name__)
                self._release_resources()
                self._status, self._detail = "error", "Could not open the microphone. Check Windows microphone permissions and input devices."
                raise WakeWordUnavailable(self._detail) from exc
            self._status = "listening"
            self._detail = "Listening locally for Hey UNBOUND. Audio is not saved or sent to speech recognition."
            self._client_last_seen = time.monotonic()
            self._thread = threading.Thread(target=self._run, name="unbound-wake-word", daemon=True)
            self._thread.start()
            return self.status()

    def _create_recorder(self, engine):
        try:
            return self._recorder_factory(frame_length=engine.frame_length)
        except WakeWordUnavailable:
            raise
        except Exception as exc:
            logger.warning("wake microphone initialization failed exception_class=%s", type(exc).__name__)
            raise WakeWordUnavailable("Could not initialize the microphone. Check Windows microphone permissions and input devices.") from exc

    def stop(self) -> dict[str, object]:
        with self._lock:
            thread = self._thread
            self._stop.set()
            recorder = self._recorder
            if recorder is not None:
                try:
                    recorder.stop()
                except Exception:
                    pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        with self._lock:
            if thread and thread.is_alive():
                self._status = "error"
                self._detail = "Wake listener did not stop promptly; restart the backend if microphone ownership remains busy."
            else:
                self._release_resources()
                if self._status != "activated":
                    self._status, self._detail = "disabled", "Wake-word monitoring is off and the microphone has been released."
            self._thread = None if not (thread and thread.is_alive()) else thread
            return self.status()

    def _run(self) -> None:
        recovery_attempts = 0
        try:
            while not self._stop.is_set():
                if self._client_lease_expired():
                    return
                try:
                    with self._lock:
                        recorder, engine = self._recorder, self._engine
                    if recorder is None or engine is None:
                        return
                    frame = recorder.read()
                    if self._stop.is_set() or self._client_lease_expired():
                        return
                    keyword_index = engine.process(frame)
                    if self._stop.is_set():
                        return
                    if keyword_index >= 0:
                        now = time.monotonic()
                        if now - self._last_detection_at < COOLDOWN_SECONDS:
                            continue
                        self._last_detection_at = now
                        self._release_resources()
                        with self._lock:
                            self._event_id += 1
                            self._event = {
                                "event_id": self._event_id,
                                "phrase": PHRASE,
                                "confidence": float(getattr(engine, "last_score", 1.0)),
                            }
                            self._status = "activated"
                            self._detail = "Hey UNBOUND detected locally. Microphone released; waiting for the browser voice handoff."
                        return
                except Exception as exc:
                    if self._stop.is_set():
                        return
                    recovery_attempts += 1
                    logger.warning("wake audio read failed exception_class=%s attempt=%d", type(exc).__name__, recovery_attempts)
                    if recovery_attempts > MAX_AUDIO_RECOVERY_ATTEMPTS:
                        with self._lock:
                            self._status = "error"
                            self._detail = "Microphone input was interrupted. Restart wake listening after checking the audio device."
                        return
                    with self._lock:
                        old_recorder = self._recorder
                        try:
                            if old_recorder:
                                old_recorder.stop()
                                old_recorder.delete()
                            self._recorder = self._create_recorder(self._engine)
                            self._recorder.start()
                            self._status = "recovering"
                            self._detail = "Microphone interrupted; reconnecting locally."
                        except Exception as recovery_error:
                            logger.warning("wake microphone recovery failed exception_class=%s", type(recovery_error).__name__)
                            self._status = "error"
                            self._detail = "Microphone reconnection failed. Check Windows microphone permissions and input devices."
                            return
                    time.sleep(self._recovery_delay)
                    with self._lock:
                        if not self._stop.is_set():
                            self._status = "listening"
                            self._detail = "Listening locally for Hey UNBOUND. Audio is not saved or sent to speech recognition."
        finally:
            if self.status()["status"] != "activated":
                self._release_resources()

    def _release_resources(self) -> None:
        # Caller may be the API stop thread or the detector thread; make cleanup idempotent.
        with self._lock:
            recorder, engine = self._recorder, self._engine
            self._recorder = None
            self._engine = None
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass
            try:
                recorder.delete()
            except Exception:
                pass
        if engine is not None:
            try:
                engine.delete()
            except Exception:
                pass


class OpenWakeWordService(PorcupineWakeWordService):
    """openWakeWord implementation reusing the proven opt-in lifecycle.

    Porcupine remains available as a legacy, explicitly constructed service
    until the open model has passed target-device evaluation. This is the
    runtime selected by TaskRuntime for new installations.
    """

    def __init__(
        self,
        *,
        engine_factory: Callable[..., Any] | None = None,
        recorder_factory: Callable[..., Any] | None = None,
        model_path: Callable[[], Path] | None = None,
        feature_dir: Callable[[], Path] | None = None,
        model_phrase: Callable[[Path], str | None] | None = None,
        threshold: Callable[[], float] | None = None,
        recovery_delay: float = 0.25,
    ) -> None:
        self._feature_dir = feature_dir or (
            lambda: Path(os.getenv("UNBOUND_WAKE_FEATURE_DIR") or default_openwakeword_feature_dir())
        )
        self._model_phrase = model_phrase or self._read_model_phrase
        super().__init__(
            engine_factory=engine_factory,
            recorder_factory=recorder_factory,
            access_key=lambda: None,
            model_path=model_path or (
                lambda: Path(os.getenv("UNBOUND_WAKE_MODEL") or default_openwakeword_model_path())
            ),
            sensitivity=threshold or (
                lambda: float(os.getenv("UNBOUND_WAKE_THRESHOLD", "0.65"))
            ),
            recovery_delay=recovery_delay,
        )

    @staticmethod
    def _default_engine_factory(*, access_key: str | None, keyword_path: str, sensitivity: float):
        from backend.app.wake_word.openwakeword_detector import OpenWakeWordDetector

        feature_dir = Path(os.getenv("UNBOUND_WAKE_FEATURE_DIR") or default_openwakeword_feature_dir())
        return OpenWakeWordDetector(
            model_path=keyword_path,
            melspec_model_path=feature_dir / "melspectrogram.onnx",
            embedding_model_path=feature_dir / "embedding_model.onnx",
            threshold=sensitivity,
        )

    @staticmethod
    def _default_recorder_factory(*, frame_length: int):
        from backend.app.wake_word.openwakeword_detector import SoundDeviceRecorder

        return SoundDeviceRecorder(frame_length=frame_length)

    def _configuration(self) -> tuple[str, Path, float]:
        self._ensure_supported_python(sys.version_info[:3])
        if self._uses_default_runtime and (
            os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64", "arm64", "aarch64"}
        ):
            raise WakeWordUnavailable("The optional local wake runtime is supported here only on Windows x64/ARM64.")
        model = self._model_path().expanduser().resolve()
        if model.suffix.lower() != ".onnx" or not model.is_file():
            raise WakeWordUnavailable("Set UNBOUND_WAKE_MODEL to the trained Hey UNBOUND .onnx model file.")
        try:
            model_phrase = " ".join(str(self._model_phrase(model) or "").casefold().split())
        except Exception as exc:
            logger.warning("openwakeword model metadata validation failed exception_class=%s", type(exc).__name__)
            raise WakeWordUnavailable("The custom ONNX model is malformed or has no phrase metadata.") from exc
        if model_phrase != PHRASE:
            raise WakeWordUnavailable("The ONNX model metadata must identify the exact phrase 'hey unbound'.")
        features = self._feature_dir().expanduser().resolve()
        if not (features / "melspectrogram.onnx").is_file() or not (features / "embedding_model.onnx").is_file():
            raise WakeWordUnavailable("Wake feature models are missing; run scripts/wake-word.ps1 prepare after installing the training environment.")
        try:
            threshold = self._sensitivity()
        except (TypeError, ValueError) as exc:
            raise WakeWordUnavailable("UNBOUND_WAKE_THRESHOLD must be a number from 0.0 to 1.0.") from exc
        if not 0.0 <= threshold <= 1.0:
            raise WakeWordUnavailable("UNBOUND_WAKE_THRESHOLD must be a number from 0.0 to 1.0.")
        return "", model, threshold

    @staticmethod
    def _read_model_phrase(model_path: Path) -> str | None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise WakeWordUnavailable("Install the optional openWakeWord runtime with scripts/wake-word.ps1 install-runtime.") from exc
        session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        metadata = session.get_modelmeta().custom_metadata_map
        return metadata.get("word")

    def _create_engine(self):
        key, model, threshold = self._configuration()
        try:
            return self._engine_factory(access_key=key, keyword_path=str(model), sensitivity=threshold)
        except WakeWordUnavailable:
            raise
        except Exception as exc:
            logger.warning("openwakeword model initialization failed exception_class=%s", type(exc).__name__)
            raise WakeWordUnavailable("openWakeWord could not load the model and feature pipeline.") from exc

    @staticmethod
    def _ensure_supported_python(version: tuple[int, ...]) -> None:
        if version < (3, 10):
            raise WakeWordUnavailable("openWakeWord requires Python 3.10 or newer.")
