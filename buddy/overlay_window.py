"""Full-screen transparent cursor overlay.

A single GTK4 Gtk.Window the size of the union of all monitors. Drawn
in Cairo. Click-through via an empty input region. Always-on-top via
`_NET_WM_STATE_ABOVE`. Runs a 60fps tick from `GLib.timeout_add` that
steps a quadratic Bezier flight ported from Clicky's quadratic
Bezier animation (OverlayWindow.swift, animateBezierFlightArc).

Design note: the blue triangle is **agent-operated only**. It is
never drawn while idle — your real mouse cursor is never shadowed.
The triangle only appears during a flight toward a [POINT:...] tag
Claude emitted, holds at the target with a label bubble, then fades
out. This is different from Clicky, which keeps a "buddy" triangle
following your cursor at all times.

Usage (from the GTK main thread only):

    overlay = CursorOverlay(monitors)
    overlay.show()                      # maps the window; triangle still invisible
    overlay.fly_to(x, y, label="render button")
    # → triangle swoops in near target, lands, shows label, fades out
    overlay.hide()                      # for screenshot capture
"""

from __future__ import annotations

import enum
import math
from typing import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

from buddy import xlib_helpers
from buddy.screenshot import Monitor, root_window_bounds


FRAME_INTERVAL_MS = 16          # ~60fps
POINTING_HOLD_SECONDS = 2.5     # how long to sit at the target before returning
BUBBLE_WIDTH = 180
BUBBLE_PADDING = 12

CURSOR_BLUE = (0.30, 0.55, 1.00, 0.95)
CURSOR_GLOW = (0.30, 0.55, 1.00, 0.35)
BUBBLE_BG = (0.10, 0.12, 0.16, 0.92)
BUBBLE_BORDER = (0.30, 0.55, 1.00, 0.75)
BUBBLE_TEXT = (1.0, 1.0, 1.0, 1.0)


class NavMode(enum.Enum):
    HIDDEN = "hidden"              # not visible at all
    IDLE_INVISIBLE = "idle"        # window is mapped but triangle is not drawn
    FLYING_TO_TARGET = "flying"    # mid-Bezier, triangle visible
    POINTING_AT_TARGET = "pointing"  # at destination, bubble showing
    FADING_OUT = "fading"          # triangle + bubble fading to zero alpha


class CursorOverlay:
    def __init__(self, monitors: Sequence[Monitor]) -> None:
        self.monitors = list(monitors)
        ox, oy, ow, oh = root_window_bounds(self.monitors)
        self.origin_x, self.origin_y = ox, oy
        self.overlay_width, self.overlay_height = ow, oh

        # ── window ────────────────────────────────────────────────────
        self.window = Gtk.Window()
        self.window.set_title("buddy cursor")
        self.window.set_decorated(False)
        self.window.set_resizable(False)
        self.window.set_default_size(ow, oh)
        self.window.set_can_focus(False)

        # Transparent background via CSS
        provider = Gtk.CssProvider()
        provider.load_from_string(
            "window.cursor-overlay { background: transparent; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.window.add_css_class("cursor-overlay")

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_draw_func(self._draw)
        self.drawing_area.set_content_width(ow)
        self.drawing_area.set_content_height(oh)
        self.window.set_child(self.drawing_area)

        self.window.connect("realize", self._on_realize)

        # ── visual state ──────────────────────────────────────────────
        self.cursor_x: float = ow / 2
        self.cursor_y: float = oh / 2
        self.rotation_degrees: float = -35.0   # Clicky's default
        self.scale: float = 1.0
        self.visible_alpha: float = 0.0        # fade-in/out
        self.mode: NavMode = NavMode.HIDDEN

        # Speech bubble
        self.bubble_text: str = ""
        self.bubble_chars_shown: int = 0
        self.bubble_alpha: float = 0.0

        # ── Bezier flight state ───────────────────────────────────────
        self._flight_start: tuple[float, float] = (0.0, 0.0)
        self._flight_end: tuple[float, float] = (0.0, 0.0)
        self._flight_control: tuple[float, float] = (0.0, 0.0)
        self._flight_frame: int = 0
        self._flight_total_frames: int = 0
        self._flight_on_complete: Callable[[], None] | None = None

        # Pointing hold
        self._pointing_frames_remaining: int = 0
        self._bubble_stream_frames: int = 0

        # Ticks
        self._tick_source: int | None = None

    # ────────────────────────────────────────────────────────────────
    # Lifecycle
    # ────────────────────────────────────────────────────────────────

    def _on_realize(self, _widget) -> None:
        xlib_helpers.apply_overlay_hints(self.window, click_through=True)

    def show(self) -> None:
        """Present the overlay window but keep the triangle invisible.

        The window itself must be mapped (so Cairo has a surface to
        draw on when a flight starts) but nothing is drawn until
        `fly_to()` is called. Your real mouse cursor is never shadowed.
        """
        if not self.window.get_visible():
            self.window.present()
        xlib_helpers.apply_overlay_hints(self.window, click_through=True)
        if self.mode == NavMode.HIDDEN:
            self.mode = NavMode.IDLE_INVISIBLE
            self.visible_alpha = 0.0
        self._start_tick()

    def hide(self) -> None:
        """Immediately hide the overlay — used before screenshot capture."""
        self._stop_tick()
        self.mode = NavMode.HIDDEN
        self.bubble_text = ""
        self.bubble_alpha = 0.0
        self.visible_alpha = 0.0
        self.window.set_visible(False)

    def reassert_above(self) -> None:
        """Re-apply always-on-top after a hide/show cycle."""
        xlib_helpers.apply_overlay_hints(self.window, click_through=True)

    # ────────────────────────────────────────────────────────────────
    # Flight API
    # ────────────────────────────────────────────────────────────────

    def fly_to(
        self,
        target_x: float,
        target_y: float,
        label: str | None = None,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        """Begin a quadratic Bezier flight to (target_x, target_y).

        Coordinates are overlay-local. Since the triangle is invisible
        when idle, the flight originates from a point near the target:
        the triangle swoops in from ~160px above-left, so you see it
        arrive rather than teleport. `label` is displayed in the
        bubble once the flight completes.
        """
        if self.mode == NavMode.HIDDEN:
            self.show()

        # Entrance point — slightly above-left of the target so the
        # flight has some arc, but short so it doesn't waste screen time
        # flying across the whole desktop.
        entry_x = target_x - 160.0
        entry_y = target_y - 120.0

        # Reset visual state
        self.cursor_x = entry_x
        self.cursor_y = entry_y
        self.visible_alpha = 0.0

        self._begin_flight(
            start=(entry_x, entry_y),
            end=(target_x, target_y),
            mode=NavMode.FLYING_TO_TARGET,
            on_complete=lambda: self._on_flight_landed(label, on_complete),
        )

    def return_to_idle(self) -> None:
        """Fade the triangle and bubble out, then go back to invisible idle."""
        self.mode = NavMode.FADING_OUT
        # visible_alpha will tick down toward 0 in _tick()

    def start_pointing(self, label: str | None) -> None:
        """Called automatically when a flight lands — shows the bubble and holds."""
        self.mode = NavMode.POINTING_AT_TARGET
        self.bubble_text = (label or "here").strip()
        self.bubble_chars_shown = 0
        self.bubble_alpha = 1.0
        self._bubble_stream_frames = 0
        # Hold for POINTING_HOLD_SECONDS worth of frames.
        self._pointing_frames_remaining = int(
            POINTING_HOLD_SECONDS * 1000 / FRAME_INTERVAL_MS
        )

    # ────────────────────────────────────────────────────────────────
    # Bezier flight internals — ported from OverlayWindow.swift:495-568
    # ────────────────────────────────────────────────────────────────

    def _begin_flight(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        mode: NavMode,
        on_complete: Callable[[], None] | None,
    ) -> None:
        self._flight_start = start
        self._flight_end = end

        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = math.hypot(dx, dy)

        # Flight duration scales with distance: short hops are quick,
        # long flights are more dramatic. Clamped to 0.6s–1.4s.
        flight_duration_seconds = min(max(distance / 800.0, 0.6), 1.4)
        frame_interval_seconds = 1.0 / 60.0
        self._flight_total_frames = max(
            1, int(flight_duration_seconds / frame_interval_seconds)
        )
        self._flight_frame = 0

        # Control point for the quadratic bezier arc. Offset the midpoint
        # upward (negative Y, since both GTK4 and screenshot pixels have
        # top-left origin) so the triangle flies in a parabolic arc.
        mid_x = (start[0] + end[0]) / 2.0
        mid_y = (start[1] + end[1]) / 2.0
        arc_height = min(distance * 0.2, 80.0)
        self._flight_control = (mid_x, mid_y - arc_height)

        self._flight_on_complete = on_complete
        self.mode = mode
        self.bubble_text = ""
        self.bubble_alpha = 0.0
        self._start_tick()

    def _step_flight(self) -> None:
        self._flight_frame += 1

        if self._flight_frame > self._flight_total_frames:
            self.cursor_x, self.cursor_y = self._flight_end
            self.scale = 1.0
            self.rotation_degrees = -35.0
            cb = self._flight_on_complete
            self._flight_on_complete = None
            if cb is not None:
                cb()
            return

        # Linear progress 0→1 over the flight duration
        linear_progress = self._flight_frame / self._flight_total_frames

        # Smoothstep easeInOut: 3t² - 2t³ (Hermite interpolation)
        t = linear_progress * linear_progress * (3.0 - 2.0 * linear_progress)

        # Quadratic bezier: B(t) = (1-t)²·P0 + 2(1-t)t·P1 + t²·P2
        one_minus_t = 1.0 - t
        p0, p1, p2 = self._flight_start, self._flight_control, self._flight_end

        bezier_x = (
            one_minus_t * one_minus_t * p0[0]
            + 2.0 * one_minus_t * t * p1[0]
            + t * t * p2[0]
        )
        bezier_y = (
            one_minus_t * one_minus_t * p0[1]
            + 2.0 * one_minus_t * t * p1[1]
            + t * t * p2[1]
        )
        self.cursor_x = bezier_x
        self.cursor_y = bezier_y

        # Rotation: face the direction of travel by computing the tangent
        # to the bezier curve. B'(t) = 2(1-t)(P1-P0) + 2t(P2-P1)
        tangent_x = (
            2.0 * one_minus_t * (p1[0] - p0[0])
            + 2.0 * t * (p2[0] - p1[0])
        )
        tangent_y = (
            2.0 * one_minus_t * (p1[1] - p0[1])
            + 2.0 * t * (p2[1] - p1[1])
        )
        # +90° offset because the triangle's "tip" points up at 0° rotation,
        # and atan2 returns 0° for rightward movement
        self.rotation_degrees = math.degrees(math.atan2(tangent_y, tangent_x)) + 90.0

        # Scale pulse: sin curve peaks at midpoint of the flight.
        # Grows to ~1.3x at the apex, then shrinks back to 1.0x on landing.
        scale_pulse = math.sin(linear_progress * math.pi)
        self.scale = 1.0 + scale_pulse * 0.3

    def _on_flight_landed(
        self,
        label: str | None,
        user_on_complete: Callable[[], None] | None,
    ) -> None:
        self.start_pointing(label)
        if user_on_complete is not None:
            user_on_complete()

    # ────────────────────────────────────────────────────────────────
    # Tick loop
    # ────────────────────────────────────────────────────────────────

    def _start_tick(self) -> None:
        if self._tick_source is None:
            self._tick_source = GLib.timeout_add(FRAME_INTERVAL_MS, self._tick)

    def _stop_tick(self) -> None:
        if self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None

    def _tick(self) -> bool:
        if self.mode == NavMode.HIDDEN:
            self._tick_source = None
            return False

        # When idle-invisible, the triangle is not drawn at all — we
        # still run the tick so fly_to() can start immediately when
        # called, but we skip the redraw to save cycles.
        if self.mode == NavMode.IDLE_INVISIBLE:
            return True

        # Fade in during flight, fade out during fade-out mode
        if self.mode == NavMode.FADING_OUT:
            self.visible_alpha = max(0.0, self.visible_alpha - 0.08)
            if self.visible_alpha <= 0.0:
                self.mode = NavMode.IDLE_INVISIBLE
                self.bubble_text = ""
                self.bubble_alpha = 0.0
        elif self.visible_alpha < 1.0:
            self.visible_alpha = min(1.0, self.visible_alpha + 0.18)

        if self.mode == NavMode.FLYING_TO_TARGET:
            self._step_flight()
        elif self.mode == NavMode.POINTING_AT_TARGET:
            self._step_pointing()
        # FADING_OUT needs no position step; the draw() call just
        # honors the decreasing visible_alpha.

        self.drawing_area.queue_draw()
        return True  # keep ticking

    def _step_pointing(self) -> None:
        # Character-by-character reveal (roughly 40 chars/sec)
        if self.bubble_chars_shown < len(self.bubble_text):
            self._bubble_stream_frames += 1
            if self._bubble_stream_frames >= 2:  # 2 frames ≈ 32ms per char
                self._bubble_stream_frames = 0
                self.bubble_chars_shown += 1

        self._pointing_frames_remaining -= 1
        if self._pointing_frames_remaining <= 0:
            self.return_to_idle()

    # ────────────────────────────────────────────────────────────────
    # Drawing
    # ────────────────────────────────────────────────────────────────

    def _draw(self, _area, cr, _width: int, _height: int) -> None:
        # Only draw when there's something to show. IDLE_INVISIBLE
        # explicitly skips so your real mouse cursor is never shadowed.
        if self.mode in (NavMode.HIDDEN, NavMode.IDLE_INVISIBLE):
            return
        if self.visible_alpha <= 0:
            return
        alpha = self.visible_alpha

        # Triangle cursor
        cr.save()
        cr.translate(self.cursor_x, self.cursor_y)
        cr.rotate(math.radians(self.rotation_degrees))
        cr.scale(self.scale, self.scale)

        # Soft glow underlay
        cr.arc(0, 0, 18, 0, 2 * math.pi)
        r, g, b, a = CURSOR_GLOW
        cr.set_source_rgba(r, g, b, a * alpha)
        cr.fill()

        # Filled triangle — equilateral, tip pointing "up" at the
        # unrotated base (see OverlayWindow.swift:55-71)
        size = 16.0
        height = size * math.sqrt(3.0) / 2.0
        cr.move_to(0, -height / 1.5)
        cr.line_to(-size / 2, height / 3)
        cr.line_to(size / 2, height / 3)
        cr.close_path()

        r, g, b, a = CURSOR_BLUE
        cr.set_source_rgba(r, g, b, a * alpha)
        cr.fill_preserve()

        cr.set_source_rgba(1.0, 1.0, 1.0, 0.7 * alpha)
        cr.set_line_width(1.5)
        cr.stroke()
        cr.restore()

        if self.mode == NavMode.POINTING_AT_TARGET and self.bubble_text:
            self._draw_bubble(cr, alpha)

    def _draw_bubble(self, cr, alpha: float) -> None:
        shown = self.bubble_text[: self.bubble_chars_shown]
        if not shown:
            return

        # Position bubble above-right of cursor
        bx = self.cursor_x + 26
        by = self.cursor_y - 44

        # Rough text width (we don't have pango here yet)
        text_width = min(BUBBLE_WIDTH, max(40, len(shown) * 8))
        text_height = 22
        w = text_width + BUBBLE_PADDING * 2
        h = text_height + BUBBLE_PADDING

        cr.save()
        cr.translate(bx, by)

        # Rounded rectangle body
        radius = 10
        cr.new_sub_path()
        cr.arc(w - radius, radius, radius, -math.pi / 2, 0)
        cr.arc(w - radius, h - radius, radius, 0, math.pi / 2)
        cr.arc(radius, h - radius, radius, math.pi / 2, math.pi)
        cr.arc(radius, radius, radius, math.pi, 3 * math.pi / 2)
        cr.close_path()

        r, g, b, a = BUBBLE_BG
        cr.set_source_rgba(r, g, b, a * alpha * self.bubble_alpha)
        cr.fill_preserve()

        r, g, b, a = BUBBLE_BORDER
        cr.set_source_rgba(r, g, b, a * alpha * self.bubble_alpha)
        cr.set_line_width(1.5)
        cr.stroke()

        # Text
        r, g, b, a = BUBBLE_TEXT
        cr.set_source_rgba(r, g, b, a * alpha * self.bubble_alpha)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(13)
        cr.move_to(BUBBLE_PADDING, BUBBLE_PADDING + 12)
        cr.show_text(shown)
        cr.restore()
