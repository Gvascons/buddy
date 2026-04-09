"""TTS backend factory.

buddy supports two local TTS backends:

- `kokoro` (default): Kokoro 82M via kokoro-onnx. Noticeably more
  natural voice than piper. ~2 s time-to-first-audio on CPU per
  sentence. Runs entirely on your machine.
- `piper`: ~150 ms time-to-first-audio on CPU, much more responsive
  but slightly robotic. Best if you want the snappiest possible
  voice coworker feel and don't mind the less-natural voice.

Pick the backend via the `BUDDY_TTS_BACKEND` env var. Default is
`kokoro` because the quality bump is worth the ~2 s extra latency
per turn for most learning-focused use cases. Override with
`BUDDY_TTS_BACKEND=piper` if you want the snappier voice.
"""

from __future__ import annotations

import os
from typing import Protocol


class TTSBackend(Protocol):
    """The interface every TTS backend must implement."""

    @property
    def is_available(self) -> bool: ...
    def warmup(self) -> None: ...
    def speak(self, text: str, on_started=None) -> None: ...
    def stop(self) -> None: ...
    @property
    def is_speaking(self) -> bool: ...


DEFAULT_BACKEND = "kokoro"


def make_tts() -> TTSBackend:
    """Construct the TTS backend selected by BUDDY_TTS_BACKEND."""
    backend = os.environ.get("BUDDY_TTS_BACKEND", DEFAULT_BACKEND).strip().lower()
    if backend == "piper":
        from buddy.tts_piper import PiperTTS
        print("🔊 tts: piper (fast, ~150ms first-audio latency)")
        return PiperTTS()
    else:
        from buddy.tts_kokoro import KokoroTTS
        print("🔊 tts: kokoro (higher quality, ~2s first-audio latency)")
        return KokoroTTS()
