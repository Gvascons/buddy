"""X11 screenshot + monitor enumeration.

Uses ffmpeg's x11grab backend to capture PNGs. By default it crops to
the **active application window** (e.g. Blender, DaVinci Resolve) via
`xdotool getactivewindow`, because the `claude -p --tools Read` path
aggressively downsizes images to ~500px on the long edge regardless
of what we send. Cropping to one app means that 500px worth of pixel
budget is all spent on the UI the user is asking about — not wasted
on desktop background or other windows.

Falls back to full-monitor capture (multi-monitor aware, with screen
labels) when:
  - xdotool reports no active window
  - the active window is buddy's own control panel
  - the active window is too small (tooltip, popup)
  - the user forces it via BUDDY_CAPTURE_MODE=monitor
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from buddy import config
from buddy.claude_adapter import ScreenCapture


DISPLAY = os.environ.get("DISPLAY", ":0")

# Pre-resize target for images sent to Claude. The `claude -p --tools Read`
# harness downsizes any image aggressively — but empirical testing shows
# that anything ≤800 px on the long edge survives at 1:1 resolution, while
# anything larger gets at least 2× further downsized. So we cap the long
# edge at 800 and save as JPEG quality 92 to keep file size small.
CLAUDE_MAX_LONG_EDGE = int(os.environ.get("BUDDY_SCREENSHOT_MAX_EDGE", "800"))
JPEG_QUALITY = 92


# ────────────────────────────────────────────────────────────────────
# Monitor enumeration
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Monitor:
    name: str       # e.g. "DP-1"
    x: int          # root-window offset
    y: int
    width: int
    height: int
    is_primary: bool


# Parses lines like:
#   "DP-1 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 597mm x 336mm"
#   "HDMI-1 connected 1920x1080+2560+0 (...)"
_XRANDR_LINE = re.compile(
    r"^(?P<name>\S+)\s+connected\s+(?P<primary>primary\s+)?(?P<w>\d+)x(?P<h>\d+)\+(?P<x>\d+)\+(?P<y>\d+)"
)


def enumerate_monitors() -> list[Monitor]:
    """Return the list of connected monitors, primary first."""
    try:
        result = subprocess.run(
            ["xrandr", "--query"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    monitors: list[Monitor] = []
    for line in result.stdout.splitlines():
        m = _XRANDR_LINE.match(line)
        if not m:
            continue
        monitors.append(Monitor(
            name=m.group("name"),
            x=int(m.group("x")),
            y=int(m.group("y")),
            width=int(m.group("w")),
            height=int(m.group("h")),
            is_primary=bool(m.group("primary")),
        ))

    if not monitors:
        # Fallback: synthesize a single monitor from xdpyinfo so a
        # minimal setup still works.
        monitors = [_monitor_from_xdpyinfo()]

    # Sort: primary first, then by x offset, then by name.
    monitors.sort(key=lambda mon: (not mon.is_primary, mon.x, mon.name))
    return monitors


def _monitor_from_xdpyinfo() -> Monitor:
    try:
        result = subprocess.run(
            ["xdpyinfo"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if "dimensions:" in line:
                m = re.search(r"(\d+)x(\d+)", line)
                if m:
                    return Monitor(
                        name="default",
                        x=0,
                        y=0,
                        width=int(m.group(1)),
                        height=int(m.group(2)),
                        is_primary=True,
                    )
    except Exception:
        pass
    return Monitor(name="default", x=0, y=0, width=1920, height=1080, is_primary=True)


def cursor_monitor(monitors: Sequence[Monitor]) -> Monitor:
    """Return the monitor currently containing the mouse cursor."""
    try:
        result = subprocess.run(
            ["xdotool", "getmouselocation"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        m = re.search(r"x:(\d+)\s+y:(\d+)", result.stdout)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            for mon in monitors:
                if mon.x <= cx < mon.x + mon.width and mon.y <= cy < mon.y + mon.height:
                    return mon
    except Exception:
        pass
    return monitors[0]


def root_window_bounds(monitors: Sequence[Monitor]) -> tuple[int, int, int, int]:
    """Return (x, y, width, height) covering the union of all monitors.

    Used to size the full-root transparent cursor overlay.
    """
    if not monitors:
        return (0, 0, 1920, 1080)
    min_x = min(m.x for m in monitors)
    min_y = min(m.y for m in monitors)
    max_x = max(m.x + m.width for m in monitors)
    max_y = max(m.y + m.height for m in monitors)
    return (min_x, min_y, max_x - min_x, max_y - min_y)


# ────────────────────────────────────────────────────────────────────
# Screenshot capture
# ────────────────────────────────────────────────────────────────────

def _ensure_screenshot_dir() -> Path:
    config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return config.SCREENSHOT_DIR


def _capture_one(monitor: Monitor, output_path: Path) -> None:
    """Call ffmpeg to capture one monitor to `output_path` (PNG)."""
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-f", "x11grab",
        "-video_size", f"{monitor.width}x{monitor.height}",
        "-i", f"{DISPLAY}+{monitor.x},{monitor.y}",
        "-frames:v", "1",
        "-update", "1",
        "-y",
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True, timeout=10, check=True)


def _capture_region(
    x: int, y: int, width: int, height: int, output_path: Path,
) -> None:
    """Call ffmpeg to capture a root-relative rectangle to `output_path`."""
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-f", "x11grab",
        "-video_size", f"{width}x{height}",
        "-i", f"{DISPLAY}+{x},{y}",
        "-frames:v", "1",
        "-update", "1",
        "-y",
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True, timeout=10, check=True)


def _resize_for_claude(png_path: Path, jpg_path: Path) -> tuple[int, int]:
    """Resize the PNG at `png_path` to ≤CLAUDE_MAX_LONG_EDGE long edge
    and save as JPEG at `jpg_path`. Returns (out_width, out_height).

    We use Pillow with LANCZOS resampling for quality. If the source
    is already under the cap we still re-save as JPEG to benefit from
    the smaller file size (Claude CLI seems to downsize PNG more
    aggressively than JPEG at similar absolute pixel counts).

    When config.GRID_ENABLED is True, we also overlay a labeled grid
    on the final JPEG (Set-of-Marks prompting — see _draw_som_grid).
    """
    from PIL import Image

    with Image.open(png_path) as img:
        img.load()  # force decode before we mess with the file
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > CLAUDE_MAX_LONG_EDGE:
            scale = CLAUDE_MAX_LONG_EDGE / long_edge
            new_w = int(round(w * scale))
            new_h = int(round(h * scale))
            resized = img.resize((new_w, new_h), Image.LANCZOS)
        else:
            new_w, new_h = w, h
            resized = img.copy()
        # JPEG wants RGB, not RGBA
        if resized.mode != "RGB":
            resized = resized.convert("RGB")
        if config.GRID_ENABLED:
            resized = _draw_som_grid(resized, config.GRID_ROWS, config.GRID_COLS)
        resized.save(jpg_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
    try:
        png_path.unlink()
    except FileNotFoundError:
        pass
    return new_w, new_h


def _draw_som_grid(img, rows: int, cols: int):
    """Overlay a labeled Set-of-Marks grid on the given Pillow image.

    Returns a new RGB Pillow image with thin translucent grid lines
    and cell labels ("A1", "B1", ..., up to the Nth row and Mth column).
    Claude's vision model is much better at identifying labeled cells
    than at returning raw pixel coordinates, so this overlay is the
    single biggest accuracy boost available without a round-trip
    second Claude call.
    """
    from PIL import Image, ImageDraw, ImageFont

    rgba = img.convert("RGBA")
    overlay = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    width, height = rgba.size
    cell_w = width / cols
    cell_h = height / rows

    # Grid lines — thin, semi-transparent lime
    line_color = (50, 255, 120, 170)
    for r in range(1, rows):
        y = int(round(r * cell_h))
        draw.line([(0, y), (width, y)], fill=line_color, width=1)
    for c in range(1, cols):
        x = int(round(c * cell_w))
        draw.line([(x, 0), (x, height)], fill=line_color, width=1)

    # Pick a readable font. Try DejaVu first (ubuntu default), fall
    # back to Pillow's bitmap default if not available.
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 11,
        )
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 11)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Cell labels — top-left of each cell, with a dark backdrop so
    # they're readable against any background.
    label_bg = (0, 0, 0, 180)
    label_fg = (230, 255, 230, 255)
    for r in range(rows):
        for c in range(cols):
            label = f"{chr(ord('A') + c)}{r + 1}"
            x = int(round(c * cell_w)) + 3
            y = int(round(r * cell_h)) + 1
            # Measure text so the backdrop is tight
            try:
                bbox = draw.textbbox((x, y), label, font=font)
            except AttributeError:
                # Older Pillow fallback
                text_w, text_h = draw.textsize(label, font=font)  # type: ignore
                bbox = (x, y, x + text_w, y + text_h)
            padded = (bbox[0] - 1, bbox[1] - 1, bbox[2] + 1, bbox[3] + 1)
            draw.rectangle(padded, fill=label_bg)
            draw.text((x, y), label, fill=label_fg, font=font)

    composited = Image.alpha_composite(rgba, overlay).convert("RGB")
    return composited


# ────────────────────────────────────────────────────────────────────
# Active window detection
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ActiveWindow:
    window_id: int
    title: str
    x: int
    y: int
    width: int
    height: int


# Window classes we never want to crop to — desktop shells, panels.
# These are WM_CLASS values, which are much more reliable than titles
# (titles change every time a Chrome tab switches).
_IGNORED_WINDOW_CLASSES = frozenset({
    "xfce4-panel",
    "gnome-shell",
    "polybar",
    "plasmashell",
    "xfdesktop",
    "plank",
})

# Our own buddy windows are identified by X11 window ID, not by title
# or class — that's the only 100% reliable way, because Chrome or any
# other app might have a tab title containing "buddy" (e.g. when the
# user has the buddy GitHub repo open in a browser tab).
# app.py populates this set at startup via register_own_window_id().
_OWN_WINDOW_IDS: set[int] = set()


def register_own_window_id(window_id: int) -> None:
    """Called at startup to mark a GTK window's XID as 'ours' so the
    active-window crop never tries to screenshot buddy's own UI."""
    if window_id:
        _OWN_WINDOW_IDS.add(int(window_id))


# Minimum window size to be considered "a real app window" vs a popup.
_MIN_WINDOW_WIDTH = 600
_MIN_WINDOW_HEIGHT = 400


def _active_window_class() -> str:
    """Return the WM_CLASS of the currently active window, lowercased.

    Uses `xdotool getactivewindow getwindowclassname` which returns
    the instance name portion of WM_CLASS (e.g. "firefox", "blender").
    Returns empty string on failure.
    """
    try:
        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowclassname"],
            capture_output=True, text=True, timeout=2, check=True,
        )
        return result.stdout.strip().lower()
    except Exception:
        return ""


def active_window() -> ActiveWindow | None:
    """Query xdotool for the currently focused X11 window.

    Returns None if:
      - no window is focused
      - the focused window is one of buddy's own (tracked by XID)
      - the focused window's WM_CLASS is a desktop shell / panel
      - the focused window is too small to be a real app
    """
    try:
        geo = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowgeometry", "--shell"],
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    fields: dict[str, str] = {}
    for line in geo.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip()] = v.strip()

    try:
        window_id = int(fields["WINDOW"])
        x = int(fields["X"])
        y = int(fields["Y"])
        width = int(fields["WIDTH"])
        height = int(fields["HEIGHT"])
    except (KeyError, ValueError):
        return None

    # Skip one of our own buddy windows (control panel, overlay) by
    # exact X11 window ID. This is the only reliable filter — title
    # substrings like "buddy" can match any app that has the word in
    # its title (e.g. a Chrome tab showing the buddy GitHub repo).
    if window_id in _OWN_WINDOW_IDS:
        return None

    # Skip desktop shells and taskbars by their WM_CLASS.
    wm_class = _active_window_class()
    if wm_class in _IGNORED_WINDOW_CLASSES:
        return None

    # Skip tooltips, popups, and tiny transient windows.
    if width < _MIN_WINDOW_WIDTH or height < _MIN_WINDOW_HEIGHT:
        return None

    # Get the window title for the label we pass to Claude (purely
    # informational — doesn't affect routing).
    try:
        title = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout.strip()
    except Exception:
        title = ""

    return ActiveWindow(
        window_id=window_id,
        title=title,
        x=x, y=y,
        width=width,
        height=height,
    )


def capture_active_window(
    monitors: Sequence[Monitor] | None = None,
) -> list[ScreenCapture]:
    """Capture just the currently focused app window.

    Returns a single ScreenCapture with `monitor_x/y` set to the window's
    root-relative origin, so POINT coordinates emitted by Claude (which
    are in the cropped image's pixel space) resolve back to the correct
    root-window pixel via the existing coords.resolve_point() logic.

    Returns [] if no suitable active window is detected; the caller
    should then fall back to capture_all_monitors().
    """
    window = active_window()
    if window is None:
        return []
    if monitors is None:
        monitors = enumerate_monitors()

    out_dir = _ensure_screenshot_dir()
    png_path = out_dir / "capture_active.png"
    jpg_path = out_dir / "capture_active.jpg"
    try:
        _capture_region(window.x, window.y, window.width, window.height, png_path)
    except subprocess.CalledProcessError as exc:
        print(f"⚠️ screenshot: ffmpeg failed for active window: {exc.stderr!r}")
        return []
    except subprocess.TimeoutExpired:
        print("⚠️ screenshot: ffmpeg timed out for active window")
        return []

    # Resize to ≤800 long edge + JPEG encode, so Claude sees 1:1
    try:
        claude_w, claude_h = _resize_for_claude(png_path, jpg_path)
    except Exception as exc:
        print(f"⚠️ screenshot: resize failed: {exc}")
        return []

    grid_note = (
        f" a {config.GRID_COLS}x{config.GRID_ROWS} labeled grid is "
        f"overlaid on this image."
        if config.GRID_ENABLED else ""
    )
    label = (
        f'cropped to the active application window "{window.title or "(untitled)"}" '
        f"(image dimensions: {claude_w}x{claude_h} pixels). "
        "this is the full screenshot — there is nothing outside this crop."
        f"{grid_note}"
    )

    return [ScreenCapture(
        image_path=str(jpg_path),
        label=label,
        width=claude_w,
        height=claude_h,
        source_width=window.width,
        source_height=window.height,
        monitor_index=1,
        monitor_x=window.x,
        monitor_y=window.y,
        is_cursor_screen=True,
    )]


def capture_for_prompt(
    monitors: Sequence[Monitor] | None = None,
) -> list[ScreenCapture]:
    """Produce the list of ScreenCaptures to send to Claude for one turn.

    By default this crops to the active application window (which
    gives Claude ~4x more effective resolution per UI element since the
    claude CLI auto-downsizes images to ~500px long edge). Falls back
    to per-monitor captures if no real app window is focused, or when
    the user sets BUDDY_CAPTURE_MODE=monitor.
    """
    mode = os.environ.get("BUDDY_CAPTURE_MODE", "auto").strip().lower()
    if mode != "monitor":
        captures = capture_active_window(monitors)
        if captures:
            print(f"📸 capture: active window ({captures[0].label})")
            return captures
    print("📸 capture: full monitor(s)")
    return capture_all_monitors(monitors)


def capture_all_monitors(
    monitors: Sequence[Monitor] | None = None,
) -> list[ScreenCapture]:
    """Grab one PNG per connected monitor.

    The cursor's monitor is sorted first in the returned list so Claude
    treats it as the "primary focus". Labels embed the pixel dimensions
    so POINT coordinates are unambiguous.
    """
    if monitors is None:
        monitors = enumerate_monitors()
    if not monitors:
        return []

    out_dir = _ensure_screenshot_dir()
    cursor_mon = cursor_monitor(monitors)

    # Put cursor monitor first, preserving relative order of the rest.
    ordered: list[Monitor] = [cursor_mon]
    for mon in monitors:
        if mon is not cursor_mon and mon not in ordered:
            ordered.append(mon)

    total = len(ordered)
    captures: list[ScreenCapture] = []
    for idx, mon in enumerate(ordered, start=1):
        png_path = out_dir / f"capture_{idx}.png"
        jpg_path = out_dir / f"capture_{idx}.jpg"
        try:
            _capture_one(mon, png_path)
        except subprocess.CalledProcessError as exc:
            print(f"⚠️ screenshot: ffmpeg failed for {mon.name}: {exc.stderr!r}")
            continue
        except subprocess.TimeoutExpired:
            print(f"⚠️ screenshot: ffmpeg timed out for {mon.name}")
            continue

        try:
            claude_w, claude_h = _resize_for_claude(png_path, jpg_path)
        except Exception as exc:
            print(f"⚠️ screenshot: resize failed for {mon.name}: {exc}")
            continue

        is_cursor = (mon is cursor_mon)
        focus_note = " — cursor is on this screen (primary focus)" if is_cursor else ""
        grid_note = (
            f" a {config.GRID_COLS}x{config.GRID_ROWS} labeled grid is "
            f"overlaid on this image."
            if config.GRID_ENABLED else ""
        )
        label = (
            f"screen {idx} of {total} ({mon.name}){focus_note} "
            f"(image dimensions: {claude_w}x{claude_h} pixels)."
            f"{grid_note}"
        )
        captures.append(ScreenCapture(
            image_path=str(jpg_path),
            label=label,
            width=claude_w,
            height=claude_h,
            source_width=mon.width,
            source_height=mon.height,
            monitor_index=idx,
            monitor_x=mon.x,
            monitor_y=mon.y,
            is_cursor_screen=is_cursor,
        ))
    return captures
