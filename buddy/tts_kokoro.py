"""Local text-to-speech via Kokoro (onnx-runtime variant).

Kokoro is an 82M-parameter Apache-2.0 TTS model. We use the
`kokoro-onnx` package which runs it on CPU via onnxruntime — no
PyTorch dependency. Output quality is noticeably better than piper,
but synthesis is much slower: roughly 0.6–0.8x real-time on CPU,
where piper is ~15-20x real-time.

To hide the synthesis latency as much as possible we split the text
into sentences and synthesize them one at a time on a worker thread,
pushing PCM samples into sounddevice the moment each sentence
finishes. That way the second sentence is synthesizing while the
first one is playing, so only the first sentence imposes a hard
pre-roll delay.

Must be called from a worker thread — `speak()` blocks until done
or `stop()` is called.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd


KOKORO_DIR = Path.home() / ".local" / "share" / "buddy" / "kokoro"
DEFAULT_MODEL = KOKORO_DIR / "kokoro-v1.0.int8.onnx"
DEFAULT_VOICES = KOKORO_DIR / "voices-v1.0.bin"
DEFAULT_VOICE_NAME = "af_heart"       # warm American female
DEFAULT_LANG = "en-us"

KOKORO_SAMPLE_RATE = 24000
KOKORO_CHANNELS = 1

# Split at sentence boundaries so we can start playback sooner.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    """Split `text` into sentences, keeping each one short enough that
    kokoro can start synthesizing the first chunk quickly."""
    cleaned = text.strip()
    if not cleaned:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(cleaned) if p.strip()]
    # If no punctuation-based split worked, fall back to the whole thing.
    return parts or [cleaned]


class KokoroTTS:
    def __init__(
        self,
        model_path: Path = DEFAULT_MODEL,
        voices_path: Path = DEFAULT_VOICES,
        voice: str = DEFAULT_VOICE_NAME,
        lang: str = DEFAULT_LANG,
    ) -> None:
        self._model_path = Path(model_path)
        self._voices_path = Path(voices_path)
        self._voice = voice
        self._lang = lang
        self._stop_event = threading.Event()
        self._engine_lock = threading.Lock()
        self._engine = None  # lazy — first speak() loads the model

    @property
    def is_available(self) -> bool:
        return self._model_path.exists() and self._voices_path.exists()

    def _ensure_engine(self) -> None:
        """Load the Kokoro model on first use. ~300ms one-time cost."""
        with self._engine_lock:
            if self._engine is not None:
                return
            if not self.is_available:
                print(
                    f"⚠️ kokoro: model or voices missing. See README for "
                    f"the one-line download command."
                )
                return
            from kokoro_onnx import Kokoro
            print(f"🧠 kokoro: loading {self._model_path.name}…")
            self._engine = Kokoro(str(self._model_path), str(self._voices_path))
            print("🧠 kokoro: ready")

    def speak(
        self,
        text: str,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        """Synthesize and play. Blocks the calling thread until done or
        stop() is called. Uses sentence-level chunking so playback of
        the first sentence starts as soon as it's synthesized, while
        the second sentence renders in parallel.
        """
        if not text or not text.strip():
            return
        if not self.is_available:
            print(f"⚠️ kokoro: not available (missing model at {self._model_path})")
            return

        self.stop()
        self._stop_event.clear()
        self._ensure_engine()
        if self._engine is None:
            return

        sentences = _split_sentences(text)
        first_played = False

        with sd.OutputStream(
            samplerate=KOKORO_SAMPLE_RATE,
            channels=KOKORO_CHANNELS,
            dtype="float32",
        ) as stream:
            for sentence in sentences:
                if self._stop_event.is_set():
                    return
                try:
                    audio, _sr = self._engine.create(
                        sentence,
                        voice=self._voice,
                        speed=1.0,
                        lang=self._lang,
                    )
                except Exception as exc:
                    print(f"⚠️ kokoro synth error: {exc}")
                    continue

                if self._stop_event.is_set():
                    return

                if not first_played:
                    first_played = True
                    if on_started is not None:
                        try:
                            on_started()
                        except Exception as exc:
                            print(f"⚠️ kokoro on_started raised: {exc}")

                # Kokoro returns float32 in [-1, 1] at 24kHz
                if audio.dtype != np.float32:
                    audio = audio.astype(np.float32)
                stream.write(audio)

    def stop(self) -> None:
        """Interrupt an in-progress speak() call. Safe from any thread."""
        self._stop_event.set()

    @property
    def is_speaking(self) -> bool:
        return not self._stop_event.is_set() and self._engine is not None
