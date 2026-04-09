"""X11 screenshot + monitor enumeration.

Uses ffmpeg's x11grab backend to capture one PNG per connected monitor.
Enumerates monitors via `xrandr --query` and picks up the cursor's
current monitor via `xdotool getmouselocation`.

The generated ScreenCapture labels match Clicky's "primary focus" style
so Claude can use `[POINT:x,y:screenN]` correctly.
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
        image_path = out_dir / f"capture_{idx}.png"
        try:
            _capture_one(mon, image_path)
        except subprocess.CalledProcessError as exc:
            print(f"⚠️ screenshot: ffmpeg failed for {mon.name}: {exc.stderr!r}")
            continue
        except subprocess.TimeoutExpired:
            print(f"⚠️ screenshot: ffmpeg timed out for {mon.name}")
            continue

        is_cursor = (mon is cursor_mon)
        focus_note = " — cursor is on this screen (primary focus)" if is_cursor else ""
        label = (
            f"screen {idx} of {total} ({mon.name}){focus_note} "
            f"(image dimensions: {mon.width}x{mon.height} pixels)"
        )
        captures.append(ScreenCapture(
            image_path=str(image_path),
            label=label,
            width=mon.width,
            height=mon.height,
            monitor_index=idx,
            monitor_x=mon.x,
            monitor_y=mon.y,
            is_cursor_screen=is_cursor,
        ))
    return captures
