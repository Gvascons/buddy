"""Map Claude POINT-tag coordinates to overlay pixel positions.

Claude emits POINT tags in one of two forms:

  [POINT:x,y:label]      raw pixel coordinates in the image space
  [POINT:H6:label]       Set-of-Marks grid cell name

For the raw-pixel form, the (x, y) is already in the screenshot's
pixel space (width/height as sent to Claude). For the cell form we
first translate the cell name into the pixel center of that cell,
using the known grid dimensions from config. In both cases we then:

  1. Clamp to the image bounds
  2. Scale back to the source-region pixel space (the image sent to
     Claude may be a 800 px downscale of a bigger crop)
  3. Add the monitor/window root offset to get root-window pixels
  4. Subtract the overlay's root origin to get overlay-local pixels

This mirrors Clicky's CompanionManager.swift:640-675 logic (cursor
screen as the fallback for missing :screenN suffix).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from buddy import config
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


def cell_to_pixel(
    cell: str,
    image_width: int,
    image_height: int,
    rows: int,
    cols: int,
) -> tuple[int, int] | None:
    """Translate a Set-of-Marks cell name (e.g. "H6") into the pixel
    center of that cell within an image of size (image_width × image_height)
    that was overlaid with a (cols × rows) grid.

    Returns None if the cell name is malformed or out of range.

    Column letters are A-Z (so columns max out at 26). Row numbers
    are 1-indexed from the top.
    """
    if not cell or not isinstance(cell, str):
        return None
    cell = cell.strip().upper()
    if len(cell) < 2 or not cell[0].isalpha():
        return None
    col_letter = cell[0]
    row_part = cell[1:]
    if not row_part.isdigit():
        return None
    col_idx = ord(col_letter) - ord("A")            # 0-based
    row_idx = int(row_part) - 1                     # 0-based
    if col_idx < 0 or col_idx >= cols:
        return None
    if row_idx < 0 or row_idx >= rows:
        return None
    cell_w = image_width / cols
    cell_h = image_height / rows
    center_x = int(round(col_idx * cell_w + cell_w / 2))
    center_y = int(round(row_idx * cell_h + cell_h / 2))
    return (center_x, center_y)


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

    # Turn whichever POINT form Claude emitted into pixel coords in
    # the image-file pixel space (target.width × target.height).
    if parsed.cell is not None:
        resolved = cell_to_pixel(
            parsed.cell,
            target.width,
            target.height,
            rows=config.GRID_ROWS or 1,
            cols=config.GRID_COLS or 1,
        )
        if resolved is None:
            # Cell name wasn't valid for the current grid — fall back
            # to the image center so the triangle at least goes
            # somewhere sensible instead of nowhere.
            print(
                f"⚠️ coords: cell {parsed.cell!r} out of range for "
                f"{config.GRID_COLS}x{config.GRID_ROWS} grid; using center"
            )
            sx = target.width // 2
            sy = target.height // 2
        else:
            sx, sy = resolved
    elif parsed.point_x is not None and parsed.point_y is not None:
        sx = int(parsed.point_x)
        sy = int(parsed.point_y)
    else:
        return None

    # Clamp to the image-file pixel bounds
    sx = max(0, min(sx, target.width - 1))
    sy = max(0, min(sy, target.height - 1))

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
