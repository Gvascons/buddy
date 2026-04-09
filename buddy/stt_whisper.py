"""Local speech-to-text via faster-whisper.

Loads a small English model (~140MB) at startup, warms it on 1 second
of silence so the first real push-to-talk isn't painfully slow, and
then runs synchronous transcription on full PCM buffers.
"""

from __future__ import annotations

import numpy as np

from buddy import config


class WhisperSTT:
    def __init__(
        self,
        model_name: str = config.WHISPER_MODEL_NAME,
        device: str = config.WHISPER_DEVICE,
        compute_type: str = config.WHISPER_COMPUTE_TYPE,
    ) -> None:
        # Import lazily so the rest of the app can at least `python -m buddy`
        # if faster-whisper isn't installed yet.
        from faster_whisper import WhisperModel

        print(f"🧠 whisper: loading {model_name} ({device}/{compute_type})…")
        self._model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self._warmed_up = False

    def warmup(self) -> None:
        """Run inference on 1 second of silence so the first real call is fast."""
        if self._warmed_up:
            return
        silence = np.zeros(16000, dtype=np.float32)
        segments, _info = self._model.transcribe(silence, beam_size=1, language="en")
        # Drain the generator to actually run the model.
        for _ in segments:
            pass
        self._warmed_up = True
        print("🧠 whisper: warmed up")

    def transcribe(self, pcm16_bytes: bytes) -> str:
        """Transcribe a buffer of 16kHz mono int16 PCM and return the text."""
        if not pcm16_bytes:
            return ""
        audio = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size == 0:
            return ""
        segments, _info = self._model.transcribe(
            audio,
            beam_size=1,
            language="en",
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text
