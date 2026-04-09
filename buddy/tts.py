"""TTS backend factory.

buddy supports two local TTS backends:

- `piper` (default): ~150 ms time-to-first-audio on CPU, slightly
  robotic but extremely responsive. Best for conversational voice.
- `kokoro`: Kokoro 82M via kokoro-onnx. Noticeably more natural
  voice, but ~2 s time-to-first-audio on CPU per sentence. Best
  when you care about voice quality more than latency.

Pick the backend via the `BUDDY_TTS_BACKEND` env var. Default is
`piper` because the latency gap is large enough that most voice
coworker turns feel broken with kokoro.
"""

from __future__ import annotations

import os
from typing import Protocol


class TTSBackend(Protocol):
    """The interface every TTS backend must implement."""

    @property
    def is_available(self) -> bool: ...
    def speak(self, text: str, on_started=None) -> None: ...
    def stop(self) -> None: ...
    @property
    def is_speaking(self) -> bool: ...


def make_tts() -> TTSBackend:
    """Construct the TTS backend selected by BUDDY_TTS_BACKEND."""
    backend = os.environ.get("BUDDY_TTS_BACKEND", "piper").strip().lower()
    if backend == "kokoro":
        from buddy.tts_kokoro import KokoroTTS
        print("🔊 tts: kokoro (higher quality, ~2s first-audio latency)")
        return KokoroTTS()
    else:
        from buddy.tts_piper import PiperTTS
        print("🔊 tts: piper (fast, ~150ms first-audio latency)")
        return PiperTTS()
