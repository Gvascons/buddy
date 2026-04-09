"""Map Claude POINT-tag coordinates to overlay pixel positions.

Claude emits POINT tags in raw pixel form: `[POINT:x,y:label]` where
x, y are integer pixel coordinates in the image Claude saw (the
JPEG's pixel space).

`resolve_point()` translates that into overlay-local pixel coords:

  1. Clamp the POINT to the image bounds
  2. Scale back to the source-region pixel space (the image sent to
     Claude may be a downscale of a bigger window/monitor crop)
  3. Add the monitor/window root offset to get root-window pixels
  4. Subtract the overlay's root origin to get overlay-local pixels

The cursor screen is the fallback target when Claude omits the
`:screenN` suffix, mirroring Clicky's CompanionManager.swift logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from buddy.claude_adapter import ParsedResponse, ScreenCapture


@dataclass(frozen=True)
class PointTarget:
    """Where the overlay should fly to, in overlay-local pixels."""
    overlay_x: float
    overlay_y: float
    label: str | None
    screen_index: int       # 1-based, the screen that was selected
    monitor_x: int          # root offset of that monitor
    monitor_y: int


def resolve_point(
    parsed: ParsedResponse,
    captures: Sequence[ScreenCapture],
    overlay_origin_x: int = 0,
    overlay_origin_y: int = 0,
) -> PointTarget | None:
    """Turn a ParsedResponse into overlay-local coordinates.

    `overlay_origin_x/y` is the root-window position of the top-left
    of the full-root cursor overlay window. On the usual
    single-monitor and left-aligned multi-monitor setups it's (0, 0).

    Returns None if the response has no coordinate.
    """
    if not parsed.has_coordinate or not captures:
        return None

    # Pick the target capture — explicit :screenN wins, else the
    # cursor screen, else the first capture.
    target: ScreenCapture | None = None
    if parsed.screen_number is not None:
        if 1 <= parsed.screen_number <= len(captures):
            target = captures[parsed.screen_number - 1]
    if target is None:
        for cap in captures:
            if cap.is_cursor_screen:
                target = cap
                break
    if target is None:
        target = captures[0]

    # Clamp to the image-file pixel bounds
    sx = max(0, min(int(parsed.point_x), target.width - 1))
    sy = max(0, min(int(parsed.point_y), target.height - 1))

    # Rescale from image-file pixels to source-region pixels.
    # target.width is the saved JPEG size; target.source_width is
    # the real on-screen region that was cropped. For source ≤
    # image_size they're equal and this is a no-op.
    scale_x = target.source_width / target.width if target.width else 1.0
    scale_y = target.source_height / target.height if target.height else 1.0
    mx = sx * scale_x
    my = sy * scale_y

    # Translate into root-window coordinates by adding the region
    # offset, then into overlay-local by subtracting the overlay
    # origin.
    root_x = target.monitor_x + mx
    root_y = target.monitor_y + my
    overlay_x = root_x - overlay_origin_x
    overlay_y = root_y - overlay_origin_y

    return PointTarget(
        overlay_x=overlay_x,
        overlay_y=overlay_y,
        label=parsed.label,
        screen_index=target.monitor_index,
        monitor_x=target.monitor_x,
        monitor_y=target.monitor_y,
    )
