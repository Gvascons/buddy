"""Tests for Claude-POINT → overlay coordinate mapping."""

from buddy.claude_adapter import ParsedResponse, ScreenCapture
from buddy.coords import resolve_point


def _capture(idx: int, mx: int, my: int, w: int, h: int, is_cursor: bool) -> ScreenCapture:
    """A ScreenCapture where the image Claude sees equals the source region
    (i.e. no pre-resize). Default case for the coordinate-mapping tests."""
    return ScreenCapture(
        image_path=f"/tmp/cap_{idx}.jpg",
        label=f"screen {idx} (image dimensions: {w}x{h} pixels)",
        width=w,
        height=h,
        source_width=w,
        source_height=h,
        monitor_index=idx,
        monitor_x=mx,
        monitor_y=my,
        is_cursor_screen=is_cursor,
    )


def test_single_monitor_identity_mapping():
    captures = [_capture(1, 0, 0, 2560, 1440, is_cursor=True)]
    parsed = ParsedResponse(
        spoken_text="here", point_x=500, point_y=600, label="foo", screen_number=None
    )
    target = resolve_point(parsed, captures)
    assert target is not None
    assert target.overlay_x == 500
    assert target.overlay_y == 600
    assert target.screen_index == 1


def test_multi_monitor_resolves_screen2_with_offset():
    # monitor 1 at (0,0), monitor 2 at (2560, 0)
    captures = [
        _capture(1, 0, 0, 2560, 1440, is_cursor=True),
        _capture(2, 2560, 0, 1920, 1080, is_cursor=False),
    ]
    parsed = ParsedResponse(
        spoken_text="over there",
        point_x=500,
        point_y=500,
        label="terminal",
        screen_number=2,
    )
    target = resolve_point(parsed, captures)
    assert target is not None
    assert target.overlay_x == 2560 + 500
    assert target.overlay_y == 0 + 500
    assert target.screen_index == 2


def test_missing_screen_number_falls_back_to_cursor_screen():
    captures = [
        _capture(1, 0, 0, 2560, 1440, is_cursor=False),
        _capture(2, 2560, 0, 1920, 1080, is_cursor=True),
    ]
    parsed = ParsedResponse(
        spoken_text="here",
        point_x=100,
        point_y=200,
        label="foo",
        screen_number=None,
    )
    target = resolve_point(parsed, captures)
    assert target is not None
    assert target.overlay_x == 2560 + 100
    assert target.overlay_y == 0 + 200
    assert target.screen_index == 2


def test_out_of_range_screen_number_falls_back_to_cursor_screen():
    captures = [
        _capture(1, 0, 0, 2560, 1440, is_cursor=True),
        _capture(2, 2560, 0, 1920, 1080, is_cursor=False),
    ]
    parsed = ParsedResponse(
        spoken_text="uh",
        point_x=10,
        point_y=20,
        label="foo",
        screen_number=99,
    )
    target = resolve_point(parsed, captures)
    assert target is not None
    # screen 99 doesn't exist, so we fall back to the cursor screen (screen 1)
    assert target.overlay_x == 10
    assert target.overlay_y == 20
    assert target.screen_index == 1


def test_coords_clamped_to_capture_bounds():
    captures = [_capture(1, 0, 0, 1920, 1080, is_cursor=True)]
    parsed = ParsedResponse(
        spoken_text="edge",
        point_x=5000,  # way outside
        point_y=5000,
        label="foo",
        screen_number=None,
    )
    target = resolve_point(parsed, captures)
    assert target is not None
    assert target.overlay_x == 1919
    assert target.overlay_y == 1079


def test_no_coordinate_returns_none():
    captures = [_capture(1, 0, 0, 1920, 1080, is_cursor=True)]
    parsed = ParsedResponse(spoken_text="no tag")
    assert resolve_point(parsed, captures) is None


def test_overlay_origin_subtracted():
    """If the overlay is positioned at (-100, -50) in root space, subtract it."""
    captures = [_capture(1, 0, 0, 1920, 1080, is_cursor=True)]
    parsed = ParsedResponse(
        spoken_text="hi", point_x=500, point_y=400, label="x", screen_number=None
    )
    target = resolve_point(parsed, captures, overlay_origin_x=-100, overlay_origin_y=-50)
    assert target is not None
    assert target.overlay_x == 500 - (-100)
    assert target.overlay_y == 400 - (-50)


def test_resized_image_scales_point_back_to_source():
    """Active window 1920x1080 resized to 800x450 for Claude.

    Claude emits POINT(400, 225) — the centre of the image it sees.
    That should resolve to the centre of the source window:
    (window.x + 960, window.y + 540) = (1000 + 960, 500 + 540).
    """
    capture = ScreenCapture(
        image_path="/tmp/cap_active.jpg",
        label="active window (image dimensions: 800x450 pixels)",
        width=800,                 # what Claude sees
        height=450,
        source_width=1920,         # real window pixels
        source_height=1080,
        monitor_index=1,
        monitor_x=1000,            # window's root-relative top-left
        monitor_y=500,
        is_cursor_screen=True,
    )
    parsed = ParsedResponse(
        spoken_text="middle", point_x=400, point_y=225, label="foo", screen_number=None
    )
    target = resolve_point(parsed, [capture])
    assert target is not None
    # 400/800 * 1920 + 1000 = 960 + 1000 = 1960
    assert target.overlay_x == 1960
    # 225/450 * 1080 + 500 = 540 + 500 = 1040
    assert target.overlay_y == 1040


def test_resized_image_clamps_to_image_bounds_not_source_bounds():
    """POINT is clamped to the Claude-visible dims, not the source dims."""
    capture = ScreenCapture(
        image_path="/tmp/cap_active.jpg",
        label="active window (image dimensions: 800x450 pixels)",
        width=800, height=450,
        source_width=1920, source_height=1080,
        monitor_index=1,
        monitor_x=0, monitor_y=0,
        is_cursor_screen=True,
    )
    parsed = ParsedResponse(
        spoken_text="edge", point_x=5000, point_y=5000,
        label="foo", screen_number=None,
    )
    target = resolve_point(parsed, [capture])
    assert target is not None
    # Clamped to (799, 449), then scaled up to source: (799/800 * 1920, 449/450 * 1080)
    assert round(target.overlay_x) == round(799 * 1920 / 800)
    assert round(target.overlay_y) == round(449 * 1080 / 450)
