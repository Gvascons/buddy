"""Global push-to-talk hotkey via pynput.

Runs a listener in a daemon thread. When the user presses the chord
(default Ctrl+Alt+Space), `on_press` fires once; when any key in the
chord is released, `on_release` fires once. Re-presses while held are
ignored.

Callbacks run on the pynput listener thread — callers must marshal to
the GTK main thread via `GLib.idle_add`.
"""

from __future__ import annotations

from typing import Callable

from pynput import keyboard

from buddy import config


class GlobalPushToTalk:
    def __init__(
        self,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        hotkey_str: str = config.DEFAULT_HOTKEY,
    ) -> None:
        self._on_press_user = on_press
        self._on_release_user = on_release
        self._hotkey_str = hotkey_str
        self._listener: keyboard.Listener | None = None
        self._pressed: bool = False

        self._hotkey = keyboard.HotKey(
            keyboard.HotKey.parse(hotkey_str),
            self._handle_hotkey_activate,
        )

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(
            on_press=self._listener_on_press,
            on_release=self._listener_on_release,
        )
        self._listener.daemon = True
        self._listener.start()
        print(f"⌨  hotkey: listening for {self._hotkey_str}")

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    # ── pynput callbacks ─────────────────────────────────────────────

    def _listener_on_press(self, key):
        assert self._listener is not None
        canonical = self._listener.canonical(key)
        self._hotkey.press(canonical)

    def _listener_on_release(self, key):
        assert self._listener is not None
        canonical = self._listener.canonical(key)
        self._hotkey.release(canonical)
        # Edge: if any key in the chord is released after activation,
        # we fire the user release callback exactly once.
        if self._pressed:
            self._pressed = False
            try:
                self._on_release_user()
            except Exception as exc:
                print(f"⚠️ hotkey on_release raised: {exc}")

    def _handle_hotkey_activate(self) -> None:
        """Fires when all keys in the chord are held simultaneously."""
        if self._pressed:
            return  # debounce — already recording
        self._pressed = True
        try:
            self._on_press_user()
        except Exception as exc:
            print(f"⚠️ hotkey on_press raised: {exc}")
