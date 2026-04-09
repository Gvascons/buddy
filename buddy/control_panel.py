"""Small floating GTK4 control panel.

Shows the current voice state, last transcript, last response, and a
model picker. Always-on-top via python-xlib. Non-intrusive — the panel
is the only thing that stays on screen when the cursor overlay is in
transient mode.

Ported from the screen-copilot project's overlay.py
(window chrome, always-on-top, drag-to-move).
See https://github.com/Gvascons/screen-copilot
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Adw, Gdk, GLib  # noqa: E402

from buddy import config, xlib_helpers
from buddy.state_machine import VoiceState


PANEL_CSS = """
window.buddy-panel {
    background: rgba(18, 20, 24, 0.96);
    border-radius: 16px;
    border: 1px solid rgba(80, 130, 230, 0.35);
}
.buddy-title {
    font-weight: 700;
    font-size: 13px;
    color: rgba(255, 255, 255, 0.95);
}
.buddy-subtitle {
    font-size: 11px;
    color: rgba(180, 200, 255, 0.75);
}
.buddy-transcript {
    color: rgba(200, 215, 240, 0.95);
    font-size: 12px;
}
.buddy-response {
    color: rgba(255, 255, 255, 0.95);
    font-size: 12px;
}
.buddy-status-dot {
    min-width: 10px;
    min-height: 10px;
    border-radius: 5px;
    background: rgba(120, 130, 150, 0.8);
}
.buddy-status-dot.listening { background: #4fc3ff; }
.buddy-status-dot.processing { background: #f5c063; }
.buddy-status-dot.responding { background: #63e6be; }
.buddy-header {
    padding: 8px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
.buddy-body {
    padding: 10px 14px 12px 14px;
}
.buddy-quit-button {
    min-width: 22px;
    min-height: 22px;
    padding: 0;
    background: transparent;
    color: rgba(255, 255, 255, 0.55);
    border: none;
}
.buddy-quit-button:hover {
    color: rgba(255, 255, 255, 0.9);
}
"""


class ControlPanel:
    """Thin window showing state + last exchange + model picker."""

    def __init__(
        self,
        application: Adw.Application,
        *,
        on_quit: Callable[[], None],
        on_model_changed: Callable[[str], None],
        on_clear_history: Callable[[], None],
    ) -> None:
        self._on_quit = on_quit
        self._on_model_changed = on_model_changed
        self._on_clear_history = on_clear_history

        self._install_css()

        self.window = Gtk.Window(application=application)
        self.window.set_title("buddy")
        self.window.set_default_size(320, 200)
        self.window.set_decorated(False)
        self.window.set_resizable(True)
        self.window.add_css_class("buddy-panel")

        self.window.connect("realize", self._on_realize)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self._build_header())
        root.append(self._build_body())
        self.window.set_child(root)

        # state
        self._state = VoiceState.IDLE
        self._set_status_label("idle — hold ctrl+alt+space to speak")

    # ── CSS ──────────────────────────────────────────────────────────

    _css_installed = False

    def _install_css(self) -> None:
        if ControlPanel._css_installed:
            return
        provider = Gtk.CssProvider()
        provider.load_from_string(PANEL_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        ControlPanel._css_installed = True

    # ── header (title + drag + quit) ─────────────────────────────────

    def _build_header(self) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.add_css_class("buddy-header")

        self.status_dot = Gtk.Box()
        self.status_dot.add_css_class("buddy-status-dot")
        self.status_dot.set_valign(Gtk.Align.CENTER)
        header.append(self.status_dot)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label(label="buddy", xalign=0)
        title.add_css_class("buddy-title")
        self.subtitle = Gtk.Label(label=f"● {config.DEFAULT_CLAUDE_MODEL}", xalign=0)
        self.subtitle.add_css_class("buddy-subtitle")
        title_box.append(title)
        title_box.append(self.subtitle)
        title_box.set_hexpand(True)
        header.append(title_box)

        quit_button = Gtk.Button(label="×")
        quit_button.add_css_class("buddy-quit-button")
        quit_button.connect("clicked", lambda *_: self._on_quit())
        header.append(quit_button)

        # Drag-to-move on the header
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        header.add_controller(drag)

        return header

    # ── body (state label, transcript, response, model picker) ──────

    def _build_body(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.add_css_class("buddy-body")

        self.status_label = Gtk.Label(label="", xalign=0)
        self.status_label.add_css_class("buddy-subtitle")
        self.status_label.set_wrap(True)
        body.append(self.status_label)

        self.transcript_label = Gtk.Label(label="", xalign=0)
        self.transcript_label.add_css_class("buddy-transcript")
        self.transcript_label.set_wrap(True)
        self.transcript_label.set_xalign(0)
        self.transcript_label.set_selectable(True)
        body.append(self.transcript_label)

        self.response_label = Gtk.Label(label="", xalign=0)
        self.response_label.add_css_class("buddy-response")
        self.response_label.set_wrap(True)
        self.response_label.set_xalign(0)
        self.response_label.set_selectable(True)
        body.append(self.response_label)

        # Model picker + clear button
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_margin_top(4)

        model_label = Gtk.Label(label="model:", xalign=0)
        model_label.add_css_class("buddy-subtitle")
        controls.append(model_label)

        self.model_dropdown = Gtk.DropDown.new_from_strings(
            list(config.AVAILABLE_CLAUDE_MODELS)
        )
        default_index = list(config.AVAILABLE_CLAUDE_MODELS).index(
            config.DEFAULT_CLAUDE_MODEL
        )
        self.model_dropdown.set_selected(default_index)
        self.model_dropdown.connect("notify::selected", self._on_model_dropdown_changed)
        controls.append(self.model_dropdown)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        controls.append(spacer)

        clear_button = Gtk.Button(label="clear")
        clear_button.add_css_class("buddy-quit-button")
        clear_button.connect("clicked", lambda *_: self._on_clear_history())
        controls.append(clear_button)

        body.append(controls)
        return body

    # ── realize / hints ──────────────────────────────────────────────

    def _on_realize(self, _widget) -> None:
        xlib_helpers.apply_overlay_hints(self.window, click_through=False)

    # ── drag ─────────────────────────────────────────────────────────

    def _on_drag_begin(self, gesture, x, y) -> None:
        native = self.window.get_native()
        if native is None:
            return
        surface = native.get_surface()
        if surface is None:
            return
        try:
            surface.begin_move(
                gesture.get_device(),
                int(gesture.get_current_button()),
                x, y,
                Gdk.CURRENT_TIME,
            )
        except Exception:
            pass

    # ── public API (call from main thread only) ─────────────────────

    def present(self) -> None:
        self.window.present()
        xlib_helpers.apply_overlay_hints(self.window, click_through=False)

    def set_state(self, state: VoiceState) -> None:
        self._state = state
        for cls in ("listening", "processing", "responding"):
            self.status_dot.remove_css_class(cls)
        if state == VoiceState.LISTENING:
            self.status_dot.add_css_class("listening")
            self._set_status_label("listening…")
        elif state == VoiceState.PROCESSING:
            self.status_dot.add_css_class("processing")
            self._set_status_label("thinking…")
        elif state == VoiceState.RESPONDING:
            self.status_dot.add_css_class("responding")
            self._set_status_label("speaking…")
        else:
            self._set_status_label("idle — hold ctrl+alt+space to speak")

    def set_transcript(self, transcript: str) -> None:
        display = transcript.strip()
        self.transcript_label.set_text(f"you: {display}" if display else "")

    def set_response(self, response: str) -> None:
        self.response_label.set_text(f"buddy: {response}" if response else "")

    def set_error(self, error_text: str) -> None:
        self.response_label.set_text(f"⚠️ {error_text}")

    def _set_status_label(self, text: str) -> None:
        self.status_label.set_text(text)

    def _on_model_dropdown_changed(self, dropdown, _pspec) -> None:
        idx = dropdown.get_selected()
        model = list(config.AVAILABLE_CLAUDE_MODELS)[idx]
        self.subtitle.set_text(f"● {model}")
        self._on_model_changed(model)
