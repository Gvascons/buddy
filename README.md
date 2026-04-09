# buddy

A push-to-talk voice coworker for Linux that watches your screen and flies a blue triangle cursor to the menu, button, or panel you're asking about. Designed for learning Blender, DaVinci Resolve, Godot, and similar creative software without leaving the app.

Spiritual successor to [Clicky](https://github.com/farzaa/clicky) for Linux/X11, with architectural borrowings from [screen-copilot](https://github.com/Gvascons/screen-copilot). Voice input, TTS, and the whole GUI run locally; only the LLM call leaves your machine.

## What it does

1. You hold **Ctrl+Alt+Space** and ask your question out loud.
2. `faster-whisper` transcribes your voice locally (mic → text, no network).
3. buddy hides its own overlay, crops a fresh screenshot of your active app window (or falls back to per-monitor captures), and restores the overlay.
4. Claude looks at the screenshot and answers in one or two spoken sentences. Two interchangeable backends:
    - **Anthropic API** when `ANTHROPIC_API_KEY` is set — higher vision resolution, streaming responses, ~1-3 s per turn, ~$0.0005 per turn with haiku.
    - **`claude -p` CLI** otherwise — works with any Claude Pro/Max subscription, no API key, ~5-10 s per turn.
5. Kokoro (default) or Piper speaks the reply. All local, no cloud TTS.
6. If Claude's response ends with a `[POINT:x,y:label]` tag, a blue triangle flies along a Bezier arc to those coordinates and shows the label.

## Requirements

- **Ubuntu 22.04 / 24.04** (or any Linux running an **X11** session — not Wayland)
- **Python 3.10+**
- Either:
  - **Claude Code CLI**, logged into a Pro/Max subscription (verify: `claude -p "hi"`), **or**
  - An **`ANTHROPIC_API_KEY`** environment variable set (recommended for speed + accuracy)
- About 1 GB of free RAM for whisper + kokoro + buddy

## Install

### 1. System packages

```bash
sudo apt update
sudo apt install -y \
    python3-gi python3-gi-cairo \
    gir1.2-gtk-4.0 gir1.2-adw-1 \
    libgirepository1.0-dev libcairo2-dev \
    ffmpeg x11-utils xdotool portaudio19-dev
```

### 2. Python package

```bash
cd buddy
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e .
```

The `--system-site-packages` flag is important so the venv can see `python3-gi`.

### 3. TTS

buddy ships with two local TTS backends and picks one at startup via
the `BUDDY_TTS_BACKEND` env var:

| Backend | Default? | Quality | Time to first audio | Notes |
|---|---|---|---|---|
| **Kokoro 82M** (`kokoro`) | **Yes** | Very natural | ~2 s on CPU | Apache 2.0, runs via `kokoro-onnx`, no PyTorch needed |
| Piper (`piper`) | no | Slightly robotic | ~300 ms on CPU | Very responsive, fast to synthesize |

You can use either one. If you just want to try buddy quickly, piper is
simpler to install (no Python dependency on `kokoro-onnx`) and will
feel snappier. If you want the best voice quality and are willing to
wait ~2 seconds extra per turn, use kokoro (the default).

#### 3a. Kokoro (default backend)

`pip install -e .` already pulled in `kokoro-onnx`. You just need the
model files:

```bash
mkdir -p ~/.local/share/buddy/kokoro

# 82M int8 ONNX model (~89 MB) — runs on CPU via onnxruntime
curl -L -o ~/.local/share/buddy/kokoro/kokoro-v1.0.int8.onnx \
    https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx

# Voice embeddings (~27 MB) — 54 speakers, multiple languages
curl -L -o ~/.local/share/buddy/kokoro/voices-v1.0.bin \
    https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

buddy uses the `af_heart` voice by default (warm American female).
Change it by editing `DEFAULT_VOICE_NAME` in `buddy/tts_kokoro.py` —
the full list of 54 voices is available via
`python3 -c "from kokoro_onnx import Kokoro; k = Kokoro('~/.local/share/buddy/kokoro/kokoro-v1.0.int8.onnx', '~/.local/share/buddy/kokoro/voices-v1.0.bin'); print(k.get_voices())"`.

#### 3b. Piper (optional, faster)

`piper` is not in `apt`. The binary ships with shared libraries and an
`espeak-ng-data/` directory that must stay next to it, so we install the
whole bundle into `~/.local/share/piper/` and drop a wrapper script in
`~/.local/bin/piper`.

```bash
# Download the latest release — check https://github.com/rhasspy/piper/releases
# for newer tags if 2023.11.14-2 has been superseded
curl -L -o /tmp/piper.tar.gz \
    https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz

# Extract the whole bundle
mkdir -p ~/.local/share/piper
tar -xzf /tmp/piper.tar.gz -C ~/.local/share/piper --strip-components=1

# Wrapper script so `piper` is on $PATH without breaking RPATH lookups
mkdir -p ~/.local/bin
cat > ~/.local/bin/piper <<EOF
#!/bin/sh
exec "$HOME/.local/share/piper/piper" "\$@"
EOF
chmod +x ~/.local/bin/piper

# Voice model — English, female, medium quality (~63MB)
mkdir -p ~/.local/share/buddy/piper
curl -L -o ~/.local/share/buddy/piper/en_US-amy-medium.onnx \
    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx
curl -L -o ~/.local/share/buddy/piper/en_US-amy-medium.onnx.json \
    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json
```

Make sure `~/.local/bin` is on your `$PATH`. Verify with:

```bash
which piper
echo "hello from piper" | piper \
    --model ~/.local/share/buddy/piper/en_US-amy-medium.onnx \
    --output_file /tmp/piper_test.wav
aplay /tmp/piper_test.wav
```

### 4. Verify everything

```bash
python -m buddy --doctor
```

This prints a checklist. Fix anything red before moving on.

## Run

```bash
# Full GTK app — push-to-talk coworker
python -m buddy

# Quick round-trip test without the GTK overlay
python -m buddy --smoke
```

On first launch the `faster-whisper base.en` model downloads into `~/.cache/huggingface/` (~140MB). The small floating control panel shows the state while that happens.

## Usage

- Hold **Ctrl+Alt+Space** and speak your question.
- Release to send.
- The blue triangle fades in, flies to whatever Claude references on screen, labels it, and flies back.
- The control panel in the corner shows the transcript, the response, and a model picker (haiku/sonnet/opus — haiku is the default because it's the fastest).
- Click **×** on the control panel to quit.

### Tips

- Keep questions short. "Where's the render button?" works better than a whole paragraph.
- If Claude's pointing is off by a few pixels, that's vision-model rounding — not much to do about it without adding an OCR pass. The label is usually more useful than the exact arrow anyway.
- Haiku is fast and cheap on your quota. Switch to sonnet or opus in the control panel for harder questions.
- You can interrupt Claude mid-response by pressing the hotkey again — TTS stops immediately.

## Architecture

```
              ┌────────────────────┐
              │  Ctrl+Alt+Space    │  (pynput global listener)
              └──────────┬─────────┘
                         │
               ┌─────────▼─────────┐
               │   GTK main thread │──► control_panel.py (floating panel)
               │  + state machine  │──► overlay_window.py (full-root, transparent,
               └─────────┬─────────┘    Cairo blue triangle + Bezier flight)
                         │
                 ┌───────▼───────┐
                 │ worker thread │  (one per push-to-talk turn)
                 └──┬──────────┬─┘
                    │          │
          ┌─────────▼──┐   ┌───▼──────────────┐
          │  whisper   │   │  ffmpeg x11grab  │
          │  (local)   │   │  → 800/1568 JPEG │
          └─────┬──────┘   └───┬──────────────┘
                │              │
                └──────┬───────┘
                       │
               ┌───────▼────────┐
               │    Claude      │  (make_claude() picks one)
               │ ┌────────────┐ │
               │ │ API (SDK)  │ │  ← when ANTHROPIC_API_KEY is set
               │ │ or         │ │     (~1-3 s, 1568 px vision)
               │ │ CLI (-p)   │ │  ← fallback (Claude Pro/Max sub)
               │ └────────────┘ │     (~5-10 s, 500 px vision)
               └───────┬────────┘
                       │
               ┌───────▼────────┐
               │ TTS (make_tts) │  (kokoro by default, piper via env var)
               └────────────────┘
```

Only the GTK main thread mutates widgets and state. Worker threads post results back via `GLib.idle_add`. The audio input callback runs on a PortAudio thread and only appends PCM to a locked bytearray.

## Key files

| File | Purpose |
|---|---|
| `buddy/app.py` | Top-level Adw.Application. Owns the state machine + worker thread pipeline. |
| `buddy/state_machine.py` | `IDLE → LISTENING → PROCESSING → RESPONDING → IDLE` transitions + observers. |
| `buddy/hotkey.py` | Global push-to-talk listener (pynput). |
| `buddy/audio_recorder.py` | sounddevice 48 kHz → 16 kHz decimator for whisper. |
| `buddy/stt_whisper.py` | `faster-whisper` wrapper with background warmup. |
| `buddy/claude_adapter.py` | Shared `ParsedResponse` / POINT parser + CLI adapter + `make_claude()` factory. |
| `buddy/claude_api_adapter.py` | Anthropic Python SDK adapter with streaming + mid-flight cancel. |
| `buddy/tts.py` | `make_tts()` factory (kokoro by default, piper via `BUDDY_TTS_BACKEND=piper`). |
| `buddy/tts_kokoro.py` | Kokoro 82M (ONNX) with producer/consumer pipeline for seamless sentence playback. |
| `buddy/tts_piper.py` | Piper subprocess → sounddevice, with interrupt. |
| `buddy/screenshot.py` | `ffmpeg -f x11grab` + xrandr + active-window crop + 800/1568 JPEG resize. |
| `buddy/overlay_window.py` | Full-root transparent GTK4 window, Cairo blue triangle, quadratic Bezier flight ported from Clicky's `OverlayWindow.swift`. |
| `buddy/control_panel.py` | Small floating Adw window with state dot, transcript, response, model picker. |
| `buddy/coords.py` | Claude-POINT → overlay-pixel coordinate mapping (pixel scaling + monitor offsets). |
| `buddy/xlib_helpers.py` | Always-on-top / click-through via `_NET_WM_STATE_ABOVE` ClientMessage (ported from screen-copilot). |
| `buddy/config.py` | Paths, defaults, and the system prompt (Clicky's voice prompt + a narrow-tab-strip hint). |
| `buddy/benchmark.py` | End-to-end latency benchmark with side-by-side TTS backend comparison. |

## Tests

```bash
pip install pytest
pytest tests/
```

26 tests covering the POINT parser, multi-monitor coordinate mapping, and state machine transitions. GTK widgets and subprocess calls are verified end-to-end by running the app.

## Choosing a Claude backend

**Both backends are fully supported and selected automatically by `make_claude()`.**

- **If `ANTHROPIC_API_KEY` is set** in the environment, buddy uses the Anthropic Python SDK directly.
    - Higher effective vision resolution (~1568 px long edge vs. the CLI's ~500 px)
    - Streaming responses — total turn ~3–5 s instead of ~8–13 s
    - Cost: ~$0.0005 per turn with haiku, ~$0.002 with sonnet — practically free for personal use
    - Get a key at https://console.anthropic.com/settings/keys, then `export ANTHROPIC_API_KEY=sk-ant-...`
- **Otherwise** buddy falls back to the `claude -p` CLI.
    - Works with any Claude Pro/Max subscription, no API key
    - Images are downsized aggressively by the CLI's Read tool, limiting pointing precision on small UI elements
    - Slower per turn due to subprocess cold-start

**Short version**: if you use buddy regularly, set an API key — you get both faster turns and better pointing accuracy for essentially nothing.

You can force a specific backend with `BUDDY_CLAUDE_BACKEND=cli` or `BUDDY_CLAUDE_BACKEND=api`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables the Anthropic API backend. Unset = CLI fallback. |
| `BUDDY_CLAUDE_BACKEND` | auto | Force a specific Claude backend: `api`, `cli`, or unset for auto. |
| `BUDDY_TTS_BACKEND` | `kokoro` | `kokoro` (higher quality, ~2 s first-audio latency) or `piper` (faster, ~300 ms, slightly robotic). |
| `BUDDY_MIC_DEVICE` | auto `pipewire` | Override which sounddevice input to use. Integer index or case-insensitive substring of the device name. List devices with `python3 -c 'import sounddevice as sd; print(sd.query_devices())'`. |
| `BUDDY_CAPTURE_MODE` | `auto` | `auto` crops to the active window (better accuracy). Set to `monitor` to force full-monitor capture. |
| `BUDDY_SCREENSHOT_MAX_EDGE` | 800 or 1568 | Max long-edge pixels sent to Claude. Auto-selected: 800 for CLI path, 1568 for API path. |
| `BUDDY_WHISPER_MODEL` | `base.en` | Swap the whisper model — `tiny.en` for low RAM, `small.en` for more accuracy. |
| `BUDDY_WHISPER_DEVICE` | `cpu` | Set to `cuda` if you have an NVIDIA GPU and `faster-whisper` picks it up. |
| `BUDDY_WHISPER_COMPUTE` | `int8` | Whisper compute type. Use `float16` on GPU for speed. |
| `BUDDY_PIPER_BINARY` | `piper` | Override the piper binary name/path. |

## Troubleshooting

- **"empty transcript" / mic captures silence** — on modern Ubuntu with PipeWire, sounddevice's "default" endpoint can land on a silent or mis-routed node. buddy auto-selects the `pipewire` sounddevice device to avoid this, but if it still doesn't work, list devices with `python3 -c 'import sounddevice as sd; print(sd.query_devices())'` and force one with e.g. `BUDDY_MIC_DEVICE=4 python -m buddy`. You can also check what PipeWire sees with `wpctl status` (look under Sources).
- **"whisper failed"** — the `faster-whisper` wheel sometimes needs `libstdc++6` updates. Try `pip install --upgrade faster-whisper`.
- **Hotkey does nothing** — confirm with `echo $XDG_SESSION_TYPE` that you're on `x11`, not `wayland`. If you're on Wayland, log out and pick "Ubuntu on Xorg" at the greeter.
- **Triangle is invisible** — your WM might not have a compositor running. On lightweight X11 setups (i3, openbox), install `picom` and run it.
- **`claude -p` hangs** — run `claude -p "hi"` standalone first. If that hangs too, re-authenticate your Claude CLI, or set `ANTHROPIC_API_KEY` to switch to the API backend.
- **Triangle points to the wrong place** — Claude's vision is approximate, and the CLI path caps it at ~500 px wide before the vision model sees it. If pointing is consistently off and you care, setting `ANTHROPIC_API_KEY` gets you ~3× more vision resolution and usually fixes it.
- **piper binary not found** — confirm `~/.local/bin` is in `$PATH` and the binary is executable. Run `which piper` to verify.
- **piper exits with a shared-library error** — the piper binary needs its sibling `.so` files and `espeak-ng-data/` to stay in the same directory. That's why the install extracts into `~/.local/share/piper/` with a wrapper script at `~/.local/bin/piper`.

## Limitations

- **X11 only.** Wayland doesn't allow arbitrary always-on-top + click-through windows or global hotkeys without portals.
- **Pointing accuracy** on the CLI backend is capped by the `claude -p` Read tool's ~500 px-wide downscale. The API backend is substantially better (~1568 px) but neither is pixel-perfect — the label in the bubble is often more useful than the exact arrow position.
- **English only by default.** Whisper model is `base.en` and the piper voice is `en_US-amy-medium`. Kokoro has multilingual voices available — swap via `buddy/tts_kokoro.py`'s `DEFAULT_VOICE_NAME` if you want another language.

## License

MIT. Goes well with an afternoon of actually learning Blender instead of bouncing between YouTube tabs.
