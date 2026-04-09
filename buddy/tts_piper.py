"""Local text-to-speech via piper.

Piper's CLI (`piper --model X.onnx --output_raw`) reads text from stdin
and writes 16-bit 22050 Hz mono raw PCM to stdout. We stream that directly
into sounddevice so we can cleanly interrupt playback when the user starts
a new push-to-talk.

Must be called from a worker thread — `speak()` blocks until playback ends
(or stop() is called).
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

from buddy import config


PIPER_SAMPLE_RATE = 22050
PIPER_CHANNELS = 1
PIPER_DTYPE = "int16"
_READ_CHUNK_BYTES = 4096


class PiperTTS:
    """Synchronous piper-backed TTS with an interruptible playback loop."""

    def __init__(
        self,
        model_path: Path = config.PIPER_MODEL_PATH,
        binary: str = config.PIPER_BINARY,
    ) -> None:
        self._model_path = Path(model_path)
        self._binary = binary
        self._stop_event = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()

    @property
    def is_available(self) -> bool:
        return self._model_path.exists()

    def warmup(self) -> None:
        """No-op for piper — subprocess spawn is the only cost, and
        there's nothing persistent to keep loaded between turns.
        """
        pass

    def speak(
        self,
        text: str,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        """Synthesize `text` and play it back. Blocks until done or stopped.

        `on_started` fires the moment the first audio chunk reaches
        sounddevice (so the caller can transition state → RESPONDING).
        """
        if not text or not text.strip():
            return
        if not self.is_available:
            print(
                f"⚠️ piper: model not found at {self._model_path}. "
                f"See README for the download one-liner."
            )
            return

        # Stop any prior playback before starting.
        self.stop()
        self._stop_event.clear()

        cmd = [
            self._binary,
            "--model", str(self._model_path),
            "--output_raw",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            print(f"⚠️ piper: binary '{self._binary}' not found on $PATH.")
            return

        with self._proc_lock:
            self._proc = proc

        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()

            first_chunk = True
            with sd.OutputStream(
                samplerate=PIPER_SAMPLE_RATE,
                channels=PIPER_CHANNELS,
                dtype=PIPER_DTYPE,
            ) as stream:
                while not self._stop_event.is_set():
                    chunk = proc.stdout.read(_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    if first_chunk:
                        first_chunk = False
                        if on_started is not None:
                            try:
                                on_started()
                            except Exception as exc:
                                print(f"⚠️ piper on_started raised: {exc}")
                    arr = np.frombuffer(chunk, dtype=np.int16)
                    if arr.size:
                        stream.write(arr)
        finally:
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            with self._proc_lock:
                self._proc = None

    def stop(self) -> None:
        """Interrupt an in-progress speak() call.

        Safe to call from any thread. The speak() loop checks the stop
        event each chunk and exits cleanly.
        """
        self._stop_event.set()
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

