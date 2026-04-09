"""Map Claude POINT-tag coordinates to overlay pixel positions.

Claude sees each screenshot with its pixel dimensions embedded in the
label, so the (x, y) it emits is already in that screenshot's pixel
space. Since we capture at native monitor resolution in v0, the
screenshot pixel space equals the monitor pixel space — all we need
to do is add the monitor's root-window offset.

This module also exposes a helper for resolving the `:screenN` suffix
to the right ScreenCapture, mirroring Clicky's
CompanionManager.swift:640-646 logic (cursor screen as the fallback).
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
    screen_index: int      # 1-based, the screen that was selected
    monitor_x: int         # root offset of that monitor
    monitor_y: int


def resolve_point(
    parsed: ParsedResponse,
    captures: Sequence[ScreenCapture],
    overlay_origin_x: int = 0,
    overlay_origin_y: int = 0,
) -> PointTarget | None:
    """Turn a POINT tag into overlay coordinates.

    `overlay_origin_x/y` is the root-window position of the top-left of
    our full-root transparent overlay window. On single-monitor setups
    and the usual multi-monitor arrangements it's (0, 0).

    Returns None if:
      - the response has no coordinate tag
      - the referenced screen index is out of range AND there's no
        cursor-screen fallback available
    """
    if not parsed.has_coordinate or not captures:
        return None

    # Pick the target capture: explicit :screenN or the cursor screen
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

    # Clamp Claude's POINT to the image-file pixel bounds
    sx = max(0, min(int(parsed.point_x), target.width - 1))
    sy = max(0, min(int(parsed.point_y), target.height - 1))

    # Rescale from image-file pixels to source-region pixels. When we
    # pre-resize captures to ~800px long edge before sending to Claude
    # (to avoid the CLI's aggressive auto-downsize), target.width is
    # the resized size and target.source_width is the real on-screen
    # region. For source ≤ 800px they're equal and this is a no-op.
    scale_x = target.source_width / target.width if target.width else 1.0
    scale_y = target.source_height / target.height if target.height else 1.0
    mx = sx * scale_x
    my = sy * scale_y

    # Translate to root-window coordinates by adding the region's root
    # offset, then to overlay-local coordinates by subtracting the
    # overlay origin.
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
