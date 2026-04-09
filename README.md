# buddy

A push-to-talk voice coworker for Linux that watches your screen and flies a blue triangle cursor to the menu, button, or panel you're asking about. Designed for learning Blender, DaVinci Resolve, Godot, and similar creative software without leaving the app.

Combines the "real coworker" feel of [Clicky](https://github.com/farzaa/clicky) with the zero-API-key simplicity of [screen-copilot](https://github.com/Gvascons/screen-copilot). Everything runs locally — the only cloud call is the `claude -p` CLI talking to your existing Claude MAX subscription.

## What it does

1. You hold **Ctrl+Alt+Space** and ask your question out loud.
2. `faster-whisper` transcribes your voice locally.
3. buddy hides its own overlay, grabs a fresh screenshot of every monitor, and restores the overlay.
4. `claude -p` looks at the screenshots and answers in one or two spoken sentences.
5. `piper` speaks the reply.
6. If Claude sees what you asked about, a blue triangle flies along a Bezier arc to the exact UI element and labels it.

No API keys. No cloud services beyond the `claude` CLI you already use.

## Requirements

- **Ubuntu 22.04 / 24.04** (or any Linux running an **X11** session — not Wayland)
- **Python 3.10+**
- **Claude Code CLI**, already logged into a Claude Pro/Max subscription (verify: `claude -p "hi"`)
- About 1 GB of free RAM for the whisper model + piper + claude CLI

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

### 3. Piper TTS (one-time download)

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
       │  Ctrl+Alt+Space    │  (pynput listener thread)
       └──────────┬─────────┘
                  │
        ┌─────────▼──────────┐
        │   GTK main thread  │────► control_panel.py (floating)
        │  + state machine   │────► overlay_window.py (full-root, transparent)
        └─────────┬──────────┘         Cairo blue triangle + Bezier flight
                  │
        ┌─────────▼──────────┐
        │  worker threads    │
        │  (one per turn)    │
        └─┬────────┬──────┬──┘
          │        │      │
   ┌──────▼──┐ ┌──▼───┐ ┌─▼──────┐
   │ whisper │ │ffmpeg│ │ claude │
   │ (local) │ │x11grab│ │  -p   │
   └────┬────┘ └──┬───┘ └───┬────┘
        │         │         │
        └─────────┴────┬────┘
                       │
                 ┌─────▼─────┐
                 │   piper   │
                 │  (local)  │
                 └───────────┘
```

Only the GTK main thread mutates widgets and state. Worker threads post results back via `GLib.idle_add`. The audio callback runs on a PortAudio thread and only appends PCM to a locked bytearray.

## Key files

| File | Purpose |
|---|---|
| `buddy/app.py` | Top-level Adw.Application. Owns state + threads. |
| `buddy/state_machine.py` | `IDLE → LISTENING → PROCESSING → RESPONDING → IDLE` transitions. |
| `buddy/hotkey.py` | Global push-to-talk listener (pynput). |
| `buddy/audio_recorder.py` | sounddevice → int16 PCM bytes, with RMS level. |
| `buddy/stt_whisper.py` | `faster-whisper` wrapper with warmup. |
| `buddy/claude_adapter.py` | `claude -p` subprocess + conversation history + POINT-tag parser (regex copied verbatim from Clicky). |
| `buddy/tts_piper.py` | Piper subprocess piped into sounddevice + interrupt. |
| `buddy/screenshot.py` | `ffmpeg -f x11grab` + xrandr multi-monitor enumeration. |
| `buddy/overlay_window.py` | Full-root transparent GTK4 window, Cairo triangle, quadratic Bezier flight ported from Clicky's `OverlayWindow.swift:495-568`. |
| `buddy/control_panel.py` | Small floating Adw window with state dot, transcript, response, model picker. |
| `buddy/coords.py` | Claude-POINT → overlay-pixel coordinate mapping. |
| `buddy/xlib_helpers.py` | Always-on-top / click-through via `_NET_WM_STATE_ABOVE` ClientMessage (ported from screen-copilot). |
| `buddy/config.py` | Defaults + the verbatim Clicky system prompt. |

## Tests

```bash
pip install pytest
pytest tests/
```

Covers the POINT parser, multi-monitor coordinate mapping, and state machine transitions. GTK widgets and subprocess calls are not unit-tested — they're verified end-to-end by running the app.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BUDDY_MIC_DEVICE` | auto-detects `pipewire` | Override which sounddevice input to use. Can be an integer index or a case-insensitive substring of the device name (e.g. `BUDDY_MIC_DEVICE=4` or `BUDDY_MIC_DEVICE=pipewire`). List devices with `python3 -c 'import sounddevice as sd; print(sd.query_devices())'`. |
| `BUDDY_WHISPER_MODEL` | `base.en` | Swap the whisper model — try `tiny.en` if you're low on RAM or `small.en` if you want better accuracy. |
| `BUDDY_WHISPER_DEVICE` | `cpu` | Set to `cuda` if you have an NVIDIA GPU and `faster-whisper` picks it up. |
| `BUDDY_WHISPER_COMPUTE` | `int8` | Whisper compute type. Use `float16` on GPU for speed. |
| `BUDDY_PIPER_BINARY` | `piper` | Override the piper binary name/path. |

## Troubleshooting

- **"empty transcript" / mic captures silence** — on modern Ubuntu with PipeWire, sounddevice's "default" endpoint can land on a silent or mis-routed node. buddy auto-selects the `pipewire` sounddevice device to avoid this, but if it still doesn't work, list devices with `python3 -c 'import sounddevice as sd; print(sd.query_devices())'` and force one with e.g. `BUDDY_MIC_DEVICE=4 python -m buddy`. You can also check what PipeWire sees with `wpctl status` (look under Sources).
- **"whisper failed"** — the `faster-whisper` wheel sometimes needs `libstdc++6` updates. Try `pip install --upgrade faster-whisper`.
- **Hotkey does nothing** — confirm with `echo $XDG_SESSION_TYPE` that you're on `x11`, not `wayland`. If you're on Wayland, log out and pick "Ubuntu on Xorg" at the greeter.
- **Triangle is invisible** — your WM might not have a compositor running. On lightweight X11 setups (i3, openbox), install `picom` and run it.
- **`claude -p` hangs** — run `claude -p "hi"` standalone first. If that hangs too, re-authenticate your Claude CLI.
- **Triangle points to the wrong place** — Claude's vision is approximate. If it's consistently off by a lot, try switching from haiku to sonnet in the control panel.
- **piper binary not found** — confirm `~/.local/bin` is in `$PATH` and the binary is executable. Run `which piper` to verify.
- **piper exits with a shared-library error** — the piper binary requires its sibling `.so` files and `espeak-ng-data/` to stay in the same directory. That's why the install instructions extract into `~/.local/share/piper/` and wrap it with a launcher script at `~/.local/bin/piper` — don't move just the binary out of the bundle.

## Limitations

- **X11 only.** Wayland doesn't allow arbitrary always-on-top + click-through windows or global hotkeys without portals.
- **Voice latency** is dominated by the `claude -p` subprocess round-trip (~1.5–4 seconds with haiku). This is why there's no streaming TTS — haiku is fast enough that a single synthesis per turn feels fine.
- **Pointing accuracy** is whatever Claude's vision produces. There's no OCR fallback yet.
- **English only.** Whisper model is `base.en` and piper voice is `en_US-amy-medium`. Swap them in `buddy/config.py` if you want another language.

## License

MIT. Goes well with an afternoon of actually learning Blender instead of bouncing between YouTube tabs.
