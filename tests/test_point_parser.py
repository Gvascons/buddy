"""Tests for the POINT-tag parser.

Must match Clicky's Swift behavior
(see CompanionManager.swift:782-823).
"""

from buddy.claude_adapter import parse_point


def test_no_tag_leaves_text_untouched():
    result = parse_point("hello there, no pointing happening.")
    assert result.spoken_text == "hello there, no pointing happening."
    assert not result.has_coordinate
    assert result.label is None
    assert result.screen_number is None


def test_point_none_strips_tag_and_sets_none_label():
    result = parse_point("html is hypertext markup language. [POINT:none]")
    assert result.spoken_text == "html is hypertext markup language."
    assert not result.has_coordinate
    assert result.label == "none"
    assert result.screen_number is None


def test_full_point_with_label():
    result = parse_point("click the render button. [POINT:1680,420:render button]")
    assert result.spoken_text == "click the render button."
    assert result.has_coordinate
    assert result.point_x == 1680
    assert result.point_y == 420
    assert result.label == "render button"
    assert result.screen_number is None


def test_point_without_label():
    result = parse_point("there it is. [POINT:100,200]")
    assert result.spoken_text == "there it is."
    assert result.has_coordinate
    assert result.point_x == 100
    assert result.point_y == 200
    assert result.label is None
    assert result.screen_number is None


def test_point_with_label_and_screen():
    result = parse_point(
        "that's over on your other monitor. [POINT:400,300:terminal:screen2]"
    )
    assert result.spoken_text == "that's over on your other monitor."
    assert result.has_coordinate
    assert result.point_x == 400
    assert result.point_y == 300
    assert result.label == "terminal"
    assert result.screen_number == 2


def test_point_must_be_at_end():
    # A POINT-like string that isn't at the end should NOT be extracted.
    result = parse_point("that [POINT:1,2:foo] is inline text afterwards.")
    assert "[POINT:1,2:foo]" in result.spoken_text
    assert not result.has_coordinate


def test_trailing_whitespace_tolerated():
    result = parse_point("click here. [POINT:5,6:button]   \n")
    assert result.spoken_text == "click here."
    assert result.has_coordinate
    assert result.point_x == 5
    assert result.point_y == 6
    assert result.label == "button"


def test_point_with_spaces_around_comma():
    result = parse_point("see the bar. [POINT:10 , 20:bar]")
    assert result.has_coordinate
    assert result.point_x == 10
    assert result.point_y == 20
    assert result.label == "bar"


# ── Set-of-Marks grid cell parsing ────────────────────────────────


def test_point_grid_cell_single_digit():
    result = parse_point("click the render button. [POINT:H6:render button]")
    assert result.spoken_text == "click the render button."
    assert result.has_coordinate
    assert result.cell == "H6"
    assert result.point_x is None
    assert result.point_y is None
    assert result.label == "render button"
    assert result.screen_number is None


def test_point_grid_cell_double_digit():
    result = parse_point("see the bottom corner. [POINT:J12:save]")
    assert result.has_coordinate
    assert result.cell == "J12"
    assert result.label == "save"


def test_point_grid_cell_with_screen():
    result = parse_point(
        "on your other monitor. [POINT:D4:terminal:screen2]"
    )
    assert result.has_coordinate
    assert result.cell == "D4"
    assert result.label == "terminal"
    assert result.screen_number == 2


def test_point_grid_cell_without_label():
    result = parse_point("there. [POINT:A1]")
    assert result.has_coordinate
    assert result.cell == "A1"
    assert result.label is None


def test_point_grid_cell_first_cell():
    result = parse_point("top-left corner. [POINT:A1:home]")
    assert result.cell == "A1"


def test_point_grid_cell_last_possible_cell():
    result = parse_point("bottom right. [POINT:Z99:edge]")
    assert result.cell == "Z99"


def test_pixel_format_still_works_alongside_cell_format():
    """Both [POINT:x,y:label] and [POINT:H6:label] should parse cleanly."""
    pixel_result = parse_point("a. [POINT:100,200:foo]")
    cell_result = parse_point("b. [POINT:C4:foo]")
    assert pixel_result.point_x == 100 and pixel_result.cell is None
    assert cell_result.cell == "C4" and cell_result.point_x is None
