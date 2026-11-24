"""Thin openWakeWord adapter used by the shared microphone lifecycle."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class OpenWakeWordDetector:
    """Convert fixed 80 ms PCM blocks into the service's detector interface."""

    frame_length = 1280  # 80 ms at 16 kHz, mono

    def __init__(
        self,
        *,
        model_path: str | Path,
        melspec_model_path: str | Path,
        embedding_model_path: str | Path,
        threshold: float,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        if model_factory is None:
            try:
                from openwakeword.model import Model
            except ImportError as exc:
                raise RuntimeError("Install the optional openWakeWord runtime with scripts/wake-word.ps1 install-runtime.") from exc
            model_factory = Model
        self.threshold = threshold
        self.model_name = Path(model_path).stem
        self.last_score = 0.0
        self._model = model_factory(
            wakeword_models=[str(model_path)],
            inference_framework="onnx",
            melspec_model_path=str(melspec_model_path),
            embedding_model_path=str(embedding_model_path),
        )

    def process(self, frame: Any) -> int:
        """Return 0 only when the configured model exceeds its explicit threshold."""
        if self._model is None:
            return -1
        predictions = self._model.predict(frame)
        if not isinstance(predictions, dict) or self.model_name not in predictions:
            raise RuntimeError("openWakeWord returned an unexpected prediction shape.")
        self.last_score = float(predictions[self.model_name])
        return 0 if self.last_score >= self.threshold else -1

    def delete(self) -> None:
        if self._model is not None:
            reset = getattr(self._model, "reset", None)
            if callable(reset):
                reset()
            self._model = None


class SoundDeviceRecorder:
    """Synchronous local mono PCM capture using the default Windows input device."""

    def __init__(self, *, frame_length: int, stream_factory: Callable[..., Any] | None = None) -> None:
        if stream_factory is None:
            try:
                import sounddevice as sd
            except ImportError as exc:
                raise RuntimeError("Install the optional microphone runtime with scripts/wake-word.ps1 install-runtime.") from exc
            stream_factory = sd.RawInputStream
        self._stream = stream_factory(
            samplerate=16_000,
            channels=1,
            dtype="int16",
            blocksize=frame_length,
        )

    def start(self) -> None:
        self._stream.start()

    def read(self):
        raw_data, overflowed = self._stream.read(self._stream.blocksize)
        if overflowed:
            # Preserve the stream and process the returned audio. Occasional
            # overflows are not permission to discard the wake listener.
            import logging

            logging.getLogger(__name__).debug("wake audio input overflow")
        import numpy as np

        return np.frombuffer(raw_data, dtype=np.int16).copy()

    def stop(self) -> None:
        self._stream.stop()

    def delete(self) -> None:
        self._stream.close()
