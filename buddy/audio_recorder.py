"""Microphone capture for push-to-talk.

Captures at the device's native 48kHz (PipeWire's canonical rate) and
decimates 3:1 to 16kHz before handing the buffer to whisper. Capturing
at 48kHz sidesteps a real ALSA/PipeWire gotcha: many systems route the
sounddevice "default" endpoint to a silent or mis-routed node when a
16kHz rate is requested, even though the underlying hardware supports
it perfectly at 48kHz.

Optional BUDDY_MIC_DEVICE env var lets you force a specific sounddevice
index or name substring — handy if the default still picks the wrong
thing (e.g. BUDDY_MIC_DEVICE=pipewire or BUDDY_MIC_DEVICE=4).

The PortAudio callback runs on its own thread; we only append bytes to
a locked bytearray so there's no allocation or GIL thrash in the audio
hot path.
"""

from __future__ import annotations

import math
import os
import threading

import numpy as np
import sounddevice as sd


# Capture rate — native for virtually every PipeWire/ALSA endpoint.
CAPTURE_SAMPLE_RATE = 48000

# Whisper expects this rate; we decimate CAPTURE_SAMPLE_RATE / DECIMATE_FACTOR
# in stop() before returning bytes.
WHISPER_SAMPLE_RATE = 16000
DECIMATE_FACTOR = CAPTURE_SAMPLE_RATE // WHISPER_SAMPLE_RATE  # 3

CHANNELS = 1
DTYPE = "int16"
BLOCK_SIZE = 4800  # 100ms per block at 48kHz


def _resolve_device() -> int | str | None:
    """Pick a sounddevice input based on BUDDY_MIC_DEVICE or auto-detect.

    Priority:
      1. BUDDY_MIC_DEVICE env var (integer index or case-insensitive
         substring of the device name)
      2. A device whose name contains "pipewire" — PipeWire's own
         sounddevice endpoint is the most reliable on modern Ubuntu
      3. None → sounddevice's built-in default
    """
    override = os.environ.get("BUDDY_MIC_DEVICE", "").strip()
    if override:
        # Numeric index?
        try:
            return int(override)
        except ValueError:
            pass
        # Name substring match
        for idx, info in enumerate(sd.query_devices()):
            if info["max_input_channels"] > 0 and override.lower() in info["name"].lower():
                print(f"🎙  mic: BUDDY_MIC_DEVICE matched device {idx}: {info['name']}")
                return idx
        print(f"⚠️ mic: BUDDY_MIC_DEVICE='{override}' matched nothing; using default")

    # Auto-detect: prefer "pipewire" if it exists, it handles routing for us
    try:
        for idx, info in enumerate(sd.query_devices()):
            if info["max_input_channels"] > 0 and "pipewire" in info["name"].lower():
                print(f"🎙  mic: auto-selected pipewire device {idx}")
                return idx
    except Exception:
        pass

    return None  # fall back to sounddevice default


class AudioRecorder:
    """One-shot push-to-talk recorder.

    Usage:
        recorder = AudioRecorder()
        recorder.start()
        ...                       # user holds hotkey
        pcm_bytes = recorder.stop()   # 16kHz int16 PCM, ready for whisper
    """

    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._latest_rms: float = 0.0
        self._device: int | str | None = _resolve_device()

    def start(self) -> None:
        """Open the audio stream and begin buffering PCM."""
        if self._stream is not None:
            return
        with self._buffer_lock:
            self._buffer.clear()
        self._latest_rms = 0.0

        def callback(indata, frames, time_info, status):
            if status:
                # Overflow / underflow — log but keep going
                print(f"⚠️ audio: {status}")
            # indata is shape (frames, channels), dtype int16.
            # Compute RMS on a float32 copy so we can drive a waveform later.
            samples = indata[:, 0].astype(np.float32)
            if samples.size:
                mean_square = float(np.mean(samples * samples))
                rms = math.sqrt(mean_square) / 32768.0
                self._latest_rms = rms
            with self._buffer_lock:
                self._buffer.extend(indata.tobytes())

        self._stream = sd.InputStream(
            samplerate=CAPTURE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=BLOCK_SIZE,
            callback=callback,
            device=self._device,
        )
        self._stream.start()

    def stop(self) -> bytes:
        """Close the stream and return 16kHz int16 PCM bytes (decimated)."""
        if self._stream is None:
            return b""
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None
        with self._buffer_lock:
            raw = bytes(self._buffer)
            self._buffer.clear()
        return _decimate_to_whisper_rate(raw)

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    @property
    def current_level(self) -> float:
        """Latest RMS level in [0, 1) — for the control panel waveform."""
        return self._latest_rms


def _decimate_to_whisper_rate(raw_48k_bytes: bytes) -> bytes:
    """Convert int16 PCM from 48kHz mono to 16kHz mono.

    Averages every `DECIMATE_FACTOR` consecutive samples. This acts as
    a crude anti-alias filter before decimation — not as clean as a
    proper polyphase FIR, but more than good enough for voice STT.
    """
    if not raw_48k_bytes:
        return b""
    arr = np.frombuffer(raw_48k_bytes, dtype=np.int16).astype(np.float32)
    # Trim to a multiple of DECIMATE_FACTOR so we can reshape cleanly
    trim_len = (arr.size // DECIMATE_FACTOR) * DECIMATE_FACTOR
    if trim_len == 0:
        return b""
    arr = arr[:trim_len].reshape(-1, DECIMATE_FACTOR).mean(axis=1)
    return arr.astype(np.int16).tobytes()
