"""End-to-end latency benchmark for the buddy pipeline.

Times every stage of a simulated push-to-talk turn:

  1. Whisper STT — transcribe a pre-generated audio file
  2. Screenshot capture + resize to 800px JPEG
  3. Claude CLI call (sonnet/haiku/opus) with the real screenshot
  4. TTS — first-audio latency and total synthesis time, per backend

Runs the pipeline with piper, then with kokoro (if installed), then
prints a side-by-side comparison.

Usage:

    python -m buddy.benchmark
    python -m buddy.benchmark --model haiku
    python -m buddy.benchmark --no-kokoro     # skip kokoro if you haven't
                                               # downloaded the model
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import wave
from pathlib import Path

import numpy as np

from buddy import config, screenshot
from buddy.audio_recorder import _decimate_to_whisper_rate  # type: ignore
from buddy.claude_adapter import ClaudeAdapter
from buddy.stt_whisper import WhisperSTT


TEST_TRANSCRIPT = "where is the color wheels panel in davinci resolve"
TEST_RESPONSE_TEXT = (
    "you'll want to jump to the color page at the bottom of the screen. "
    "click that tab and the color wheels panel shows up in the middle area."
)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

class Timer:
    """Tiny convenience context manager."""
    def __init__(self, name: str) -> None:
        self.name = name
        self.elapsed_ms: float = 0.0

    def __enter__(self) -> "Timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self.elapsed_ms = (time.perf_counter() - self._t0) * 1000.0


def _synth_with_piper_to_wav(text: str, out_path: Path) -> Path:
    """Use piper to synthesize `text` into a WAV file so we have a
    realistic audio input to benchmark whisper against.
    """
    import subprocess
    from buddy import config as cfg
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        cfg.PIPER_BINARY,
        "--model", str(cfg.PIPER_MODEL_PATH),
        "--output_file", str(out_path),
    ]
    subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, check=True)
    return out_path


def _wav_to_16k_pcm16_bytes(wav_path: Path) -> bytes:
    """Load a WAV file, resample to 16kHz mono int16, return raw bytes.
    Whisper expects exactly 16kHz mono int16 PCM.
    """
    with wave.open(str(wav_path), "rb") as wav:
        sr = wav.getframerate()
        n_channels = wav.getnchannels()
        sampwidth = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if sampwidth != 2:
        raise RuntimeError(f"expected 16-bit WAV, got {sampwidth*8}-bit")
    arr = np.frombuffer(raw, dtype=np.int16)
    if n_channels > 1:
        arr = arr.reshape(-1, n_channels).mean(axis=1).astype(np.int16)
    if sr == 16000:
        return arr.tobytes()
    if sr == 48000 and len(arr) % 3 == 0:
        # Fast path — the audio recorder uses the same 48→16 decimator
        return _decimate_to_whisper_rate(arr.tobytes())
    # Generic resample via linear interp
    target_len = int(round(len(arr) * 16000 / sr))
    xs = np.linspace(0, len(arr) - 1, target_len)
    resampled = np.interp(xs, np.arange(len(arr)), arr.astype(np.float32)).astype(np.int16)
    return resampled.tobytes()


# ─────────────────────────────────────────────────────────────────────
# Stage benchmarks
# ─────────────────────────────────────────────────────────────────────

def bench_whisper(whisper: WhisperSTT, pcm_bytes: bytes) -> tuple[str, float]:
    with Timer("whisper") as t:
        transcript = whisper.transcribe(pcm_bytes)
    return transcript, t.elapsed_ms


def bench_screenshot(monitors) -> tuple[list, float]:
    with Timer("screenshot") as t:
        captures = screenshot.capture_for_prompt(monitors)
    return captures, t.elapsed_ms


def bench_claude(claude: ClaudeAdapter, transcript: str, captures) -> tuple[str, float]:
    with Timer("claude") as t:
        parsed = claude.ask(transcript, captures)
    return parsed.spoken_text, t.elapsed_ms


def bench_tts_piper(text: str) -> tuple[float, float]:
    """Return (time_to_first_audio_ms, total_synth_ms) for piper.

    piper streams int16 PCM to stdout; we measure the wall-clock time
    from subprocess start until the first chunk arrives, then keep
    reading until EOF to get the total synth time. We do NOT actually
    play the audio — we just consume stdout, because what we care about
    is synthesis throughput.
    """
    import subprocess
    from buddy import config as cfg

    cmd = [
        cfg.PIPER_BINARY,
        "--model", str(cfg.PIPER_MODEL_PATH),
        "--output_raw",
    ]
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(text.encode("utf-8"))
    proc.stdin.close()

    first_chunk_ms: float | None = None
    total_bytes = 0
    while True:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        if first_chunk_ms is None:
            first_chunk_ms = (time.perf_counter() - t0) * 1000.0
        total_bytes += len(chunk)
    proc.wait()
    total_ms = (time.perf_counter() - t0) * 1000.0

    # Audio duration: piper outputs 22050 Hz mono int16
    audio_seconds = total_bytes / 2 / 22050
    return first_chunk_ms or total_ms, total_ms, audio_seconds  # type: ignore


def bench_tts_kokoro(text: str) -> tuple[float, float, float]:
    """Return (time_to_first_audio_ms, total_synth_ms, audio_seconds) for kokoro.

    Mirrors KokoroTTS.speak's sentence-level chunking but without
    actually playing audio — measures wall-clock time from model load
    until first sentence is synthesized, then keeps going.
    """
    from buddy.tts_kokoro import _split_sentences, DEFAULT_MODEL, DEFAULT_VOICES

    from kokoro_onnx import Kokoro

    # Don't count engine load in the TTS timing — it's a one-time cost
    # and app.py would keep the engine around across turns.
    engine = Kokoro(str(DEFAULT_MODEL), str(DEFAULT_VOICES))
    # Warm-up: one trivial synth so model weights are resident.
    engine.create("warmup", voice="af_heart", lang="en-us")

    sentences = _split_sentences(text)
    t0 = time.perf_counter()
    first_audio_ms: float | None = None
    total_audio_samples = 0
    for sentence in sentences:
        audio, sr = engine.create(sentence, voice="af_heart", lang="en-us")
        if first_audio_ms is None:
            first_audio_ms = (time.perf_counter() - t0) * 1000.0
        total_audio_samples += len(audio)
    total_ms = (time.perf_counter() - t0) * 1000.0
    audio_seconds = total_audio_samples / 24000
    return first_audio_ms or total_ms, total_ms, audio_seconds


# ─────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────

def format_ms(ms: float) -> str:
    if ms < 1000:
        return f"{ms:>6.0f} ms"
    return f"{ms/1000:>6.2f} s "


def run(
    *,
    claude_model: str,
    include_kokoro: bool,
    claude_warmup: bool = True,
) -> None:
    print("=" * 72)
    print(f" buddy pipeline benchmark — claude model: {claude_model}")
    print("=" * 72)

    # ── 1. Prep: load whisper, prep a test audio file
    print("\n◦ loading whisper…")
    whisper = WhisperSTT()
    whisper.warmup()

    print("◦ synthesizing test audio via piper (for whisper input)…")
    test_wav = config.SCREENSHOT_DIR.parent / "bench_test.wav"
    _synth_with_piper_to_wav(TEST_TRANSCRIPT, test_wav)
    pcm = _wav_to_16k_pcm16_bytes(test_wav)
    print(f"  test clip: {len(pcm)/32000:.1f}s of 16kHz PCM")

    # ── 2. Stage benchmarks (common to both TTS backends)
    monitors = screenshot.enumerate_monitors()
    claude = ClaudeAdapter(model=claude_model)

    if claude_warmup:
        print("\n◦ warming up claude (one throwaway call)…")
        with Timer("warmup") as t:
            claude.ask("say 'ready' and nothing else", [])
        print(f"  warmup: {format_ms(t.elapsed_ms)}")

    results: dict[str, float] = {}

    print("\n◦ stage 1: whisper STT")
    transcript, ms = bench_whisper(whisper, pcm)
    print(f"  transcript: {transcript!r}")
    print(f"  ⏱  {format_ms(ms)}")
    results["whisper_stt"] = ms

    print("\n◦ stage 2: screenshot + resize + JPEG encode")
    captures, ms = bench_screenshot(monitors)
    if captures:
        print(f"  {captures[0].label}")
    print(f"  ⏱  {format_ms(ms)}")
    results["screenshot"] = ms

    print(f"\n◦ stage 3: claude -p ({claude_model}) with image")
    response, ms = bench_claude(claude, transcript, captures)
    print(f"  response: {response[:120]!r}{'…' if len(response) > 120 else ''}")
    print(f"  ⏱  {format_ms(ms)}")
    results["claude"] = ms

    # ── 3. TTS backends — use TEST_RESPONSE_TEXT for a fair comparison
    #      regardless of what Claude actually said
    print(f"\n◦ stage 4: TTS on canned response ({len(TEST_RESPONSE_TEXT.split())} words)")
    print(f"  text: {TEST_RESPONSE_TEXT!r}")

    try:
        first_ms, total_ms, audio_s = bench_tts_piper(TEST_RESPONSE_TEXT)
        print(f"\n  PIPER")
        print(f"    first audio:      {format_ms(first_ms)}")
        print(f"    total synth:      {format_ms(total_ms)}")
        print(f"    audio duration:   {audio_s:.2f} s")
        print(f"    real-time factor: {total_ms/1000/audio_s:.2f}x")
        results["piper_first_ms"] = first_ms
        results["piper_total_ms"] = total_ms
        results["piper_audio_s"] = audio_s
    except Exception as exc:
        print(f"  ❌ piper failed: {exc}")

    if include_kokoro:
        try:
            first_ms, total_ms, audio_s = bench_tts_kokoro(TEST_RESPONSE_TEXT)
            print(f"\n  KOKORO")
            print(f"    first audio:      {format_ms(first_ms)}")
            print(f"    total synth:      {format_ms(total_ms)}")
            print(f"    audio duration:   {audio_s:.2f} s")
            print(f"    real-time factor: {total_ms/1000/audio_s:.2f}x")
            results["kokoro_first_ms"] = first_ms
            results["kokoro_total_ms"] = total_ms
            results["kokoro_audio_s"] = audio_s
        except Exception as exc:
            print(f"  ❌ kokoro failed: {exc}")
            import traceback
            traceback.print_exc()

    # ── 4. Simulated conversation summary
    print("\n" + "=" * 72)
    print(" simulated conversation — total wall-clock per turn")
    print("=" * 72)

    fixed_stages = results["whisper_stt"] + results["screenshot"] + results["claude"]
    print(f"  whisper + screenshot + claude:  {format_ms(fixed_stages)}")

    if "piper_first_ms" in results:
        piper_total = fixed_stages + results["piper_first_ms"]
        print(f"  + piper first audio:            {format_ms(piper_total)}  ← user hears first word")
    if "kokoro_first_ms" in results:
        kokoro_total = fixed_stages + results["kokoro_first_ms"]
        print(f"  + kokoro first audio:           {format_ms(kokoro_total)}  ← user hears first word")
        if "piper_first_ms" in results:
            delta = kokoro_total - piper_total
            print(f"  kokoro vs piper extra delay:    {format_ms(delta)}")

    print("\n  interpretation: 'user hears first word' is the number that")
    print("  controls whether a conversation feels snappy. Under ~2s = great,")
    print("  2-4s = noticeable but usable, above 4s = breaks the conversational")
    print("  feel and users will hate it.")


def main() -> int:
    parser = argparse.ArgumentParser(prog="buddy.benchmark")
    parser.add_argument("--model", default="haiku",
                        choices=["haiku", "sonnet", "opus"],
                        help="Claude model to benchmark (default: haiku)")
    parser.add_argument("--no-kokoro", action="store_true",
                        help="Skip kokoro TTS benchmark (e.g. if model isn't downloaded)")
    parser.add_argument("--no-warmup", action="store_true",
                        help="Skip the throwaway claude warmup call")
    args = parser.parse_args()
    run(
        claude_model=args.model,
        include_kokoro=not args.no_kokoro,
        claude_warmup=not args.no_warmup,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
