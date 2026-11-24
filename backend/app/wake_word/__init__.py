"""Opt-in local wake-word detection components."""
"""Optional local wake-word detection for UNBOUND."""

from backend.app.wake_word.service import OpenWakeWordService, PorcupineWakeWordService, WakeWordUnavailable

__all__ = ["OpenWakeWordService", "PorcupineWakeWordService", "WakeWordUnavailable"]
