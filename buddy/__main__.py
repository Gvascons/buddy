"""buddy entry point.

Usage:

    python -m buddy                      # full GTK app (push-to-talk)
    python -m buddy --smoke              # M0 smoke test: record 5s, ask Claude,
                                           speak reply (no GTK, no hotkey, no
                                           screenshot, no POINT)
    python -m buddy --doctor             # check dependencies and exit
"""

from __future__ import annotations

import argparse
import sys
import time


def smoke_test() -> int:
    """Minimal round-trip: record → whisper → claude → piper."""
    from buddy import audio_recorder, claude_adapter, stt_whisper, tts_piper

    print("🎙  buddy smoke test — record 5 seconds after the beep.")
    print("    (loads whisper first, which can take a few seconds)")
    whisper = stt_whisper.WhisperSTT()
    whisper.warmup()

    claude = claude_adapter.ClaudeAdapter()
    tts = tts_piper.PiperTTS()

    recorder = audio_recorder.AudioRecorder()
    print("▶  recording for 5s… speak now.")
    recorder.start()
    time.sleep(5.0)
    pcm = recorder.stop()
    print(f"⏹  captured {len(pcm)} bytes of PCM")

    if len(pcm) < 1000:
        print("❌ recording too short. try again.")
        return 1

    # Quick sanity check on the signal level
    import numpy as np
    arr = np.frombuffer(pcm, dtype=np.int16)
    peak = int(np.abs(arr).max()) if arr.size else 0
    print(f"📊 peak amplitude: {peak}/32767 ({peak/32767*100:.1f}%)")
    if peak < 300:
        print("⚠️ audio is essentially silent — try: BUDDY_MIC_DEVICE=pipewire python -m buddy --smoke")
        print("   or list devices with: python3 -c 'import sounddevice as sd; print(sd.query_devices())'")

    print("🧠 transcribing…")
    transcript = whisper.transcribe(pcm)
    if not transcript:
        print("❌ empty transcript. check mic permissions.")
        return 1
    print(f"📝 transcript: {transcript!r}")

    print("🤖 asking claude…")
    t0 = time.time()
    parsed = claude.ask(transcript)
    elapsed = time.time() - t0
    print(f"💬 claude ({elapsed:.1f}s): {parsed.spoken_text}")
    if parsed.has_coordinate:
        print(
            f"   [POINT:{parsed.point_x},{parsed.point_y}"
            f":{parsed.label}:screen{parsed.screen_number}]"
        )

    print("🔊 piper speaking…")
    tts.speak(parsed.spoken_text)
    print("✅ done.")
    return 0


def doctor() -> int:
    """Verify dependencies — print what's missing, exit 0 if all good."""
    import os
    import shutil

    print("🩺 buddy doctor")
    problems: list[str] = []

    for binary, purpose in [
        ("claude", "LLM (Claude MAX subscription via `claude -p`)"),
        ("ffmpeg", "screenshot capture"),
        ("piper", "local neural TTS"),
        ("xrandr", "monitor enumeration"),
        ("xdotool", "cursor position queries"),
        ("xprop", "window queries"),
        ("xwininfo", "window geometry"),
    ]:
        path = shutil.which(binary)
        if path:
            print(f"  ✅ {binary:10s}  {path}   — {purpose}")
        else:
            print(f"  ❌ {binary:10s}  MISSING  — {purpose}")
            problems.append(binary)

    # Python imports
    for module, pip_name in [
        ("gi", "PyGObject"),
        ("cairo", "pycairo"),
        ("sounddevice", "sounddevice"),
        ("numpy", "numpy"),
        ("faster_whisper", "faster-whisper"),
        ("pynput", "pynput"),
        ("Xlib", "python-xlib"),
        ("PIL", "Pillow"),
        ("kokoro_onnx", "kokoro-onnx"),
    ]:
        try:
            __import__(module)
            print(f"  ✅ python:{module:15s}  installed  — pip: {pip_name}")
        except ImportError:
            print(f"  ❌ python:{module:15s}  MISSING    — pip install {pip_name}")
            problems.append(pip_name)

    # TTS model files — only require the files for whichever backend is active
    from buddy import config as buddy_config
    active_backend = os.environ.get("BUDDY_TTS_BACKEND", "kokoro").strip().lower()
    if active_backend == "piper":
        if buddy_config.PIPER_MODEL_PATH.exists():
            print(f"  ✅ piper voice    {buddy_config.PIPER_MODEL_PATH}")
        else:
            print(f"  ❌ piper voice    MISSING at {buddy_config.PIPER_MODEL_PATH}")
            problems.append("piper voice model")
    else:
        from buddy.tts_kokoro import DEFAULT_MODEL, DEFAULT_VOICES
        if DEFAULT_MODEL.exists():
            print(f"  ✅ kokoro model   {DEFAULT_MODEL}")
        else:
            print(f"  ❌ kokoro model   MISSING at {DEFAULT_MODEL}")
            problems.append("kokoro onnx model")
        if DEFAULT_VOICES.exists():
            print(f"  ✅ kokoro voices  {DEFAULT_VOICES}")
        else:
            print(f"  ❌ kokoro voices  MISSING at {DEFAULT_VOICES}")
            problems.append("kokoro voices file")

    # $DISPLAY
    if os.environ.get("DISPLAY"):
        print(f"  ✅ $DISPLAY       {os.environ['DISPLAY']}")
    else:
        print("  ❌ $DISPLAY       NOT SET  — buddy needs an X11 session, not Wayland")
        problems.append("X11 session")

    if problems:
        print(f"\n❌ {len(problems)} issue(s). See README for install steps.")
        return 1
    print("\n✅ all good.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="buddy")
    parser.add_argument("--smoke", action="store_true",
                        help="Record 5s, transcribe, ask Claude, speak reply. Skips GTK.")
    parser.add_argument("--doctor", action="store_true",
                        help="Check dependencies and exit.")
    args = parser.parse_args()

    if args.doctor:
        return doctor()
    if args.smoke:
        return smoke_test()

    # Full GTK app
    from buddy.app import run_app
    return run_app()


if __name__ == "__main__":
    sys.exit(main())
