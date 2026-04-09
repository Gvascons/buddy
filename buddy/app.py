"""Top-level Adw.Application that wires together every piece.

Owns:
- StateMachine
- WhisperSTT (loaded in a background thread on startup)
- ClaudeAdapter
- TTS backend (selected by make_tts() based on BUDDY_TTS_BACKEND)
- AudioRecorder
- GlobalPushToTalk
- ControlPanel (small floating window)
- CursorOverlay (full-root transparent, only shown during interactions)

The push-to-talk pipeline runs on per-request worker threads; all GTK
mutations marshal back via GLib.idle_add.
"""

from __future__ import annotations

import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from buddy import config, coords
from buddy.audio_recorder import AudioRecorder
from buddy.claude_adapter import ClaudeAdapter
from buddy.control_panel import ControlPanel
from buddy.hotkey import GlobalPushToTalk
from buddy.overlay_window import CursorOverlay
from buddy.screenshot import capture_for_prompt, enumerate_monitors
from buddy.state_machine import StateMachine, VoiceState
from buddy.stt_whisper import WhisperSTT
from buddy.tts import make_tts


class BuddyApp:
    def __init__(self) -> None:
        self.application = Adw.Application(application_id="so.buddy.Buddy")
        self.application.connect("activate", self._on_activate)

        # Core components (constructed in _on_activate so GTK is ready)
        self.state = StateMachine()
        self.recorder = AudioRecorder()
        self.claude = ClaudeAdapter()
        self.tts = make_tts()
        self.whisper: WhisperSTT | None = None
        self.monitors = enumerate_monitors()

        self.control_panel: ControlPanel | None = None
        self.overlay: CursorOverlay | None = None
        self.hotkey: GlobalPushToTalk | None = None

        # Runtime
        self._cursor_mode = config.DEFAULT_CURSOR_MODE
        self._transient_hide_source: int | None = None

    # ────────────────────────────────────────────────────────────────
    # Activation
    # ────────────────────────────────────────────────────────────────

    def _on_activate(self, _app) -> None:
        # Control panel first — gives the user something to look at while
        # whisper loads in the background.
        self.control_panel = ControlPanel(
            self.application,
            on_quit=self._quit,
            on_model_changed=self._set_model,
            on_clear_history=self._clear_history,
        )
        self.control_panel.present()

        # State observer updates the control panel + overlay
        self.state.add_observer(self._on_state_change)

        # Overlay window (hidden until needed in transient mode)
        self.overlay = CursorOverlay(self.monitors)
        # Present once so it gets an XID, then immediately hide
        self.overlay.window.present()
        GLib.timeout_add(50, self._hide_overlay_initially)

        # Register our own window XIDs so the active-window crop
        # never tries to screenshot buddy's own UI. Delayed so the
        # windows are fully realized + mapped before we ask for IDs.
        GLib.timeout_add(400, self._register_own_windows)

        # Hotkey listener
        self.hotkey = GlobalPushToTalk(
            on_press=self._on_hotkey_press,
            on_release=self._on_hotkey_release,
        )
        self.hotkey.start()

        # Warm whisper in the background so startup isn't blocking.
        threading.Thread(
            target=self._load_and_warm_whisper,
            daemon=True,
            name="whisper-warmup",
        ).start()

        # Warm the TTS engine in parallel so the first push-to-talk
        # doesn't pay the model-load + graph-compile cost on top of
        # everything else. This is especially important for kokoro,
        # whose onnx graph compilation adds ~300-500ms to the first
        # synthesis.
        threading.Thread(
            target=self._warm_tts,
            daemon=True,
            name="tts-warmup",
        ).start()

    def _hide_overlay_initially(self) -> bool:
        if self.overlay is not None and self._cursor_mode == "transient":
            self.overlay.hide()
        return False

    def _register_own_windows(self) -> bool:
        """Tell screenshot.py which X11 window IDs belong to us so the
        active-window crop ignores buddy's own control panel and
        cursor overlay.
        """
        from buddy import screenshot, xlib_helpers
        registered: list[int] = []
        if self.control_panel is not None:
            xid = xlib_helpers.get_xid(self.control_panel.window)
            if xid:
                screenshot.register_own_window_id(xid)
                registered.append(xid)
        if self.overlay is not None:
            xid = xlib_helpers.get_xid(self.overlay.window)
            if xid:
                screenshot.register_own_window_id(xid)
                registered.append(xid)
        if registered:
            hexes = ", ".join(f"0x{x:x}" for x in registered)
            print(f"🪟 registered own window ids: {hexes}")
        return False

    def _load_and_warm_whisper(self) -> None:
        try:
            GLib.idle_add(self._update_status, "loading whisper model…")
            whisper = WhisperSTT()
            whisper.warmup()
            self.whisper = whisper
            GLib.idle_add(self._update_status, "ready — hold ctrl+alt+space to speak")
        except Exception as exc:
            GLib.idle_add(self._update_status, f"whisper failed: {exc}")

    def _warm_tts(self) -> None:
        """Preload + warmup the TTS backend in a background thread so
        the first real speak() call doesn't pay the model-load cost.
        """
        try:
            self.tts.warmup()
        except Exception as exc:
            print(f"⚠️ tts warmup failed: {exc}")

    def _update_status(self, text: str) -> bool:
        if self.control_panel is not None:
            self.control_panel._set_status_label(text)
        return False

    # ────────────────────────────────────────────────────────────────
    # Hotkey callbacks (run on pynput listener thread)
    # ────────────────────────────────────────────────────────────────

    def _on_hotkey_press(self) -> None:
        GLib.idle_add(self._handle_hotkey_press)

    def _on_hotkey_release(self) -> None:
        GLib.idle_add(self._handle_hotkey_release)

    def _handle_hotkey_press(self) -> bool:
        """Main thread: start recording.

        Also handles interrupts: if the user presses the hotkey while
        buddy is already in PROCESSING or RESPONDING, this cancels
        whatever's running (the claude subprocess and/or TTS) and
        transitions back to IDLE before starting a fresh recording.
        """
        if self.whisper is None:
            if self.control_panel:
                self.control_panel._set_status_label("still loading whisper… wait a sec")
            return False

        # Cancel any in-flight TTS, pending hide, and (if claude is
        # still thinking) the claude subprocess itself.
        self.tts.stop()
        self.claude.cancel()
        self._cancel_transient_hide()

        # Whatever state we were in, force back to IDLE so the allowed-
        # edge check below lets us enter LISTENING. The old worker
        # thread (if any) will see its state has moved on when it
        # tries to post a result back, and will drop it.
        if self.state.state != VoiceState.IDLE:
            self.state.force(VoiceState.IDLE)

        # Show the overlay if in transient mode
        if self.overlay is not None:
            self.overlay.show()

        try:
            self.recorder.start()
        except Exception as exc:
            print(f"⚠️ audio: failed to start: {exc}")
            if self.control_panel:
                self.control_panel.set_error(f"mic failed: {exc}")
            return False

        self.state.transition(VoiceState.LISTENING)
        if self.control_panel:
            self.control_panel.set_transcript("")
            self.control_panel.set_response("")
        return False

    def _handle_hotkey_release(self) -> bool:
        """Main thread: stop recording, kick pipeline worker."""
        if self.state.state != VoiceState.LISTENING:
            return False

        pcm = self.recorder.stop()
        if len(pcm) < config.MIN_RECORDING_BYTES:
            print(f"⚠️ audio: recording too short ({len(pcm)} bytes)")
            self.state.transition(VoiceState.IDLE)
            self._schedule_transient_hide()
            return False

        self.state.transition(VoiceState.PROCESSING)

        threading.Thread(
            target=self._pipeline_worker,
            args=(pcm,),
            daemon=True,
            name="buddy-pipeline",
        ).start()
        return False

    # ────────────────────────────────────────────────────────────────
    # Pipeline worker (runs on a background thread)
    # ────────────────────────────────────────────────────────────────

    def _pipeline_worker(self, pcm_bytes: bytes) -> None:
        """Runs on a daemon worker thread. Each turn gets its own
        worker, and if the user interrupts mid-turn the worker simply
        discovers (either via a ClaudeCancelled exception or a state
        check) that its work is no longer wanted and bails out.
        """
        from buddy.claude_adapter import ClaudeCancelled
        try:
            assert self.whisper is not None

            # 1. Transcribe
            transcript = self.whisper.transcribe(pcm_bytes)
            if not self._worker_still_wanted():
                print("⚠️ pipeline: cancelled after whisper")
                return
            if not transcript:
                print("⚠️ whisper: empty transcript")
                GLib.idle_add(self._fail_and_reset, "didn't catch that — try again")
                return
            print(f"📝 transcript: {transcript}")
            GLib.idle_add(self._set_transcript_label, transcript)

            # 2. Capture screens (hide overlay first)
            self._hide_overlay_for_capture()
            time.sleep(0.05)
            captures = capture_for_prompt(self.monitors)
            self._restore_overlay_after_capture()
            if not self._worker_still_wanted():
                print("⚠️ pipeline: cancelled after screenshot")
                return

            # 3. Ask Claude
            parsed = self.claude.ask(transcript, captures)
            print(f"💬 claude: {parsed.spoken_text!r}")
            if parsed.has_coordinate:
                if parsed.cell is not None:
                    coord_str = f"cell {parsed.cell}"
                else:
                    coord_str = f"pixel ({parsed.point_x},{parsed.point_y})"
                print(
                    f"   pointing at {coord_str} "
                    f"{parsed.label!r} screen={parsed.screen_number}"
                )

            if not self._worker_still_wanted():
                print("⚠️ pipeline: cancelled after claude")
                return

            # 4. Trigger cursor + TTS on main thread
            GLib.idle_add(self._handle_response, parsed, captures)
        except ClaudeCancelled:
            # User hit the hotkey mid-turn — this is expected, not an error.
            print("⚠️ pipeline: claude call cancelled by user")
        except Exception as exc:
            print(f"⚠️ pipeline: {exc}")
            if self._worker_still_wanted():
                GLib.idle_add(self._fail_and_reset, str(exc))

    def _worker_still_wanted(self) -> bool:
        """True if the currently-running pipeline worker should keep
        going. If the user pressed the hotkey again mid-turn, the
        state machine will have been forced back to IDLE / LISTENING
        and any in-flight worker thread should drop its result.
        """
        return self.state.state == VoiceState.PROCESSING

    def _hide_overlay_for_capture(self) -> None:
        """Hide overlay synchronously so the next ffmpeg grab is clean."""
        done = threading.Event()

        def hide() -> bool:
            if self.overlay is not None:
                self.overlay.hide()
            done.set()
            return False

        GLib.idle_add(hide)
        done.wait(timeout=1.0)

    def _restore_overlay_after_capture(self) -> None:
        def show() -> bool:
            if self.overlay is not None:
                self.overlay.show()
                self.overlay.reassert_above()
            return False
        GLib.idle_add(show)

    def _handle_response(self, parsed, captures) -> bool:
        """Main thread: kick the TTS worker. The cursor flight is
        scheduled for the moment the first audio sample is about to
        play, via the on_started callback — so with Kokoro's ~2s
        first-audio latency the cursor and the voice stay in sync.
        """
        if self.control_panel:
            self.control_panel.set_response(parsed.spoken_text)

        # Pre-resolve the overlay target so the on_started callback
        # doesn't have to do any cross-thread work besides calling
        # overlay.fly_to().
        target = None
        if parsed.has_coordinate and self.overlay is not None:
            target = coords.resolve_point(
                parsed,
                captures,
                overlay_origin_x=self.overlay.origin_x,
                overlay_origin_y=self.overlay.origin_y,
            )
            # Show the overlay window early so fly_to() doesn't have
            # to wait for it to map when audio starts.
            self.overlay.show()

        # TTS on a worker thread — the on_started callback fires on
        # that worker thread the moment the first audio sample is
        # about to be written to the output device. We marshal it
        # back to the main thread, which is where we start the
        # cursor flight and flip the state machine to RESPONDING.
        threading.Thread(
            target=self._tts_worker,
            args=(parsed.spoken_text, target, parsed.label),
            daemon=True,
            name="buddy-tts",
        ).start()
        return False

    def _tts_worker(self, text: str, target, label: str | None) -> None:
        def on_started() -> None:
            GLib.idle_add(self._audio_starting, target, label)

        try:
            self.tts.speak(text, on_started=on_started)
        except Exception as exc:
            print(f"⚠️ tts: {exc}")
        finally:
            GLib.idle_add(self._tts_finished)

    def _audio_starting(self, target, label: str | None) -> bool:
        """Main thread: called at the moment audio actually begins.

        Starts the cursor flight (so it arrives at the target while
        the voice is still explaining it) and transitions the state
        machine to RESPONDING.
        """
        if self.state.state == VoiceState.PROCESSING:
            self.state.transition(VoiceState.RESPONDING)
        if target is not None and self.overlay is not None:
            self.overlay.fly_to(
                target.overlay_x,
                target.overlay_y,
                label=label,
            )
        return False

    def _tts_finished(self) -> bool:
        # Only transition if we're still in RESPONDING — the user may
        # have started a new push-to-talk mid-TTS.
        if self.state.state == VoiceState.RESPONDING:
            self.state.transition(VoiceState.IDLE)
        self._schedule_transient_hide()
        return False

    def _fail_and_reset(self, error_text: str) -> bool:
        if self.control_panel:
            self.control_panel.set_error(error_text)
        # Force back to idle regardless of current state
        self.state.force(VoiceState.IDLE)
        self._schedule_transient_hide()
        return False

    def _set_transcript_label(self, text: str) -> bool:
        if self.control_panel:
            self.control_panel.set_transcript(text)
        return False

    # ────────────────────────────────────────────────────────────────
    # State observer
    # ────────────────────────────────────────────────────────────────

    def _on_state_change(self, old: VoiceState, new: VoiceState) -> None:
        if self.control_panel is not None:
            self.control_panel.set_state(new)

    # ────────────────────────────────────────────────────────────────
    # Transient overlay hide
    # ────────────────────────────────────────────────────────────────

    def _schedule_transient_hide(self) -> None:
        if self._cursor_mode != "transient":
            return
        self._cancel_transient_hide()
        delay_ms = int(config.TRANSIENT_HIDE_DELAY * 1000)
        self._transient_hide_source = GLib.timeout_add(
            delay_ms,
            self._do_transient_hide,
        )

    def _cancel_transient_hide(self) -> None:
        if self._transient_hide_source is not None:
            GLib.source_remove(self._transient_hide_source)
            self._transient_hide_source = None

    def _do_transient_hide(self) -> bool:
        self._transient_hide_source = None
        if self.state.state != VoiceState.IDLE:
            return False
        if self.overlay is None:
            return False
        # Only hide if the overlay is already idle-invisible or hidden,
        # i.e. no flight / bubble / fade-out is in progress.
        from buddy.overlay_window import NavMode
        if self.overlay.mode not in (NavMode.IDLE_INVISIBLE, NavMode.HIDDEN):
            return False
        self.overlay.hide()
        return False

    # ────────────────────────────────────────────────────────────────
    # Control panel callbacks
    # ────────────────────────────────────────────────────────────────

    def _set_model(self, model: str) -> None:
        print(f"⚙  model: switching to {model}")
        self.claude.model = model

    def _clear_history(self) -> None:
        self.claude.clear_history()
        if self.control_panel:
            self.control_panel.set_transcript("")
            self.control_panel.set_response("history cleared.")

    def _quit(self) -> None:
        self.tts.stop()
        if self.hotkey is not None:
            self.hotkey.stop()
        self.application.quit()

    # ────────────────────────────────────────────────────────────────
    # Entry
    # ────────────────────────────────────────────────────────────────

    def run(self) -> int:
        return self.application.run([])


def run_app() -> int:
    app = BuddyApp()
    return app.run()
