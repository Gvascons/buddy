"""Local text-to-speech via Kokoro (onnx-runtime variant).

Kokoro is an 82M-parameter Apache-2.0 TTS model. We use the
`kokoro-onnx` package which runs it on CPU via onnxruntime — no
PyTorch dependency. Output quality is noticeably better than piper,
but synthesis is much slower: roughly 0.6–0.8x real-time on CPU,
where piper is ~15-20x real-time.

To make Kokoro sound like natural conversation we pipeline
synthesis and playback with a producer/consumer pattern:

    producer thread: splits text into sentences, synthesizes each
                     one, pushes the audio array onto a queue
    consumer (main): pulls arrays from the queue and writes them
                     to sounddevice in small chunks

Because Kokoro's real-time factor is ~0.7x, synthesizing sentence
N+1 takes LESS time than playing sentence N — so by the time the
consumer finishes playing one sentence, the next one is already
waiting in the queue. The gap between sentences collapses to zero.

The consumer writes audio in small (~170 ms) chunks and checks the
stop event between chunks, so `stop()` cuts playback within ~170 ms
of being called. It also explicitly aborts the OutputStream on stop,
which discards any samples still buffered in PortAudio.

Must be called from a worker thread — `speak()` blocks until done
or `stop()` is called.
"""

from __future__ import annotations

import queue
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

# Size of each write to sounddevice. Smaller = faster interrupt
# response, larger = fewer Python/CFFI crossings. 4096 samples at
# 24kHz is ~170 ms, which is imperceptible as a stop delay.
PLAYBACK_CHUNK_SAMPLES = 4096

# How long speak() waits for the producer to join before giving up
# and letting it finish orphaned. The producer's blocking point is
# `engine.create()` which is an uninterruptible native call, so if
# stop() fires mid-synthesis we'd otherwise wait up to 2 s for the
# current sentence to finish. A short timeout lets speak() return
# quickly; the orphaned producer will finish its current synth call
# and then exit on its own (its put() will be a no-op because the
# queue is about to be GC'd).
PRODUCER_JOIN_TIMEOUT_S = 0.1

# Sentinel used to signal end-of-stream from producer to consumer.
_END_OF_STREAM = object()

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
        # The sounddevice OutputStream currently in use by speak(),
        # so stop() can abort() it from any thread. Protected by a
        # lock because both speak() and stop() can touch it.
        self._current_stream: sd.OutputStream | None = None
        self._stream_lock = threading.Lock()

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

    def warmup(self) -> None:
        """Load the model and run one trivial synthesis so the first
        real `speak()` call doesn't pay any graph-compile cost.

        Safe to call from a background thread at app startup — the
        engine-load is guarded by a lock so concurrent speak() calls
        on the main worker will just wait.
        """
        self._ensure_engine()
        if self._engine is None:
            return
        try:
            # One silent throwaway synthesis to JIT the onnx graph.
            self._engine.create("ready.", voice=self._voice, lang=self._lang)
            print("🔊 kokoro: warmed up")
        except Exception as exc:
            print(f"⚠️ kokoro warmup failed: {exc}")

    def speak(
        self,
        text: str,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        """Synthesize and play text as one continuous, natural-sounding
        utterance. Blocks the calling thread until playback finishes
        or `stop()` is called.

        Uses a producer/consumer pipeline so the next sentence is
        synthesized while the current one is playing — this eliminates
        the long pauses that appear when sentences are synthesized
        serially on the playback thread.
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
        if not sentences:
            return

        # Bounded so an extremely long reply doesn't spike memory
        audio_queue: queue.Queue = queue.Queue(maxsize=4)

        def producer() -> None:
            """Synthesize sentences in order and push them onto the
            queue. Exits on stop_event or after the last sentence.
            """
            try:
                for sentence in sentences:
                    if self._stop_event.is_set():
                        break
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
                        break
                    if audio.dtype != np.float32:
                        audio = audio.astype(np.float32)
                    # put() blocks if the queue is full, which is the
                    # natural back-pressure — synthesis waits for the
                    # consumer to drain one sentence before making
                    # another. We use a short timeout so stop_event
                    # can unblock us.
                    while not self._stop_event.is_set():
                        try:
                            audio_queue.put(audio, timeout=0.1)
                            break
                        except queue.Full:
                            continue
            finally:
                # Always signal end of stream so the consumer wakes.
                try:
                    audio_queue.put_nowait(_END_OF_STREAM)
                except queue.Full:
                    # Consumer is already stopping; drain one slot.
                    try:
                        audio_queue.get_nowait()
                        audio_queue.put_nowait(_END_OF_STREAM)
                    except Exception:
                        pass

        producer_thread = threading.Thread(
            target=producer, daemon=True, name="kokoro-producer",
        )
        producer_thread.start()

        first_played = False
        stream: sd.OutputStream | None = None
        try:
            stream = sd.OutputStream(
                samplerate=KOKORO_SAMPLE_RATE,
                channels=KOKORO_CHANNELS,
                dtype="float32",
            )
            stream.start()
            with self._stream_lock:
                self._current_stream = stream

            while not self._stop_event.is_set():
                # Block waiting for the producer to deliver the next
                # sentence. Short timeout so stop_event can unblock.
                try:
                    item = audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if item is _END_OF_STREAM:
                    break

                # Fire on_started the moment the very first chunk is
                # about to be written to the output device.
                if not first_played:
                    first_played = True
                    if on_started is not None:
                        try:
                            on_started()
                        except Exception as exc:
                            print(f"⚠️ kokoro on_started raised: {exc}")

                audio_array: np.ndarray = item
                # Write in small chunks so stop() cuts playback quickly
                for start in range(0, len(audio_array), PLAYBACK_CHUNK_SAMPLES):
                    if self._stop_event.is_set():
                        break
                    chunk = audio_array[start:start + PLAYBACK_CHUNK_SAMPLES]
                    stream.write(chunk)
        finally:
            with self._stream_lock:
                self._current_stream = None
            if stream is not None:
                try:
                    if self._stop_event.is_set():
                        # Discard any audio still buffered in PortAudio
                        stream.abort()
                    else:
                        stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            producer_thread.join(timeout=PRODUCER_JOIN_TIMEOUT_S)

    def stop(self) -> None:
        """Interrupt an in-progress speak() call. Safe from any thread.

        Sets the stop event so both the producer and the consumer
        bail out of their loops, and explicitly aborts the current
        OutputStream so PortAudio throws away any buffered samples
        instead of letting them play out.
        """
        self._stop_event.set()
        with self._stream_lock:
            stream = self._current_stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass

