"""Claude CLI adapter.

Shells out to `claude -p` (Claude Code CLI, authenticated via the user's
Claude MAX subscription — no API keys required). Sends multi-image
prompts by embedding paths in the prompt text and using `--tools read`
so the CLI reads the screenshots itself. Parses Clicky-style
`[POINT:x,y:label:screenN]` tags out of the response.
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Sequence

from buddy import config


# ────────────────────────────────────────────────────────────────────
# POINT tag parser — based on the Clicky project's regex
# (leanring-buddy/CompanionManager.swift, parsePointingCoordinates)
# with a Set-of-Marks cell variant added on top.
# See https://github.com/farzaa/clicky
#
# Accepts three formats:
#   [POINT:none]                         — no pointing needed
#   [POINT:123,456:label:screen2]        — raw pixel coords (classic)
#   [POINT:H6:label:screen2]             — grid cell name (SoM)
# The label and screen suffix are optional in all coord variants.
# ────────────────────────────────────────────────────────────────────

POINT_REGEX = re.compile(
    r"\[POINT:"
    r"(?:"
    r"none"                                      # [POINT:none]
    r"|(?P<x>\d+)\s*,\s*(?P<y>\d+)"              # raw pixels
    r"|(?P<cell>[A-Z]\d+)"                       # cell name, e.g. "H6"
    r")"
    r"(?::(?P<label>[^\]:\s][^\]:]*?))?"         # optional label
    r"(?::screen(?P<screen>\d+))?"               # optional :screenN
    r"\]\s*$"
)


class ClaudeCancelled(Exception):
    """Raised when an in-flight claude -p call is interrupted by cancel()."""


@dataclass
class ParsedResponse:
    """A Claude response with the POINT tag (if any) extracted.

    At most ONE of `(point_x, point_y)` or `cell` is populated:
    - When Claude returns pixel coordinates, point_x/point_y are set.
    - When Claude returns a grid cell (Set-of-Marks mode), cell is set.
    - When there's no coordinate at all (or [POINT:none]), both are
      None and has_coordinate returns False.

    coords.resolve_point() translates either form into overlay-local
    pixel coordinates using the target ScreenCapture's dimensions.
    """
    spoken_text: str                # response with POINT tag stripped, trimmed
    point_x: int | None = None      # screenshot pixel x (raw-pixel mode)
    point_y: int | None = None      # screenshot pixel y (raw-pixel mode)
    cell: str | None = None         # grid cell label, e.g. "H6" (SoM mode)
    label: str | None = None        # short description of the element, or "none"
    screen_number: int | None = None  # 1-based; None means cursor screen

    @property
    def has_coordinate(self) -> bool:
        if self.cell is not None:
            return True
        return self.point_x is not None and self.point_y is not None


def parse_point(response_text: str) -> ParsedResponse:
    """Extract the trailing POINT tag from a Claude response.

    Returns the spoken text (tag removed, whitespace trimmed) plus
    parsed coordinate fields. Supports three formats:

      [POINT:none]                  → label="none", no coordinate
      [POINT:123,456:label:screenN] → point_x/point_y set
      [POINT:H6:label:screenN]      → cell set

    Responses with no tag come back with all POINT fields None.
    """
    match = POINT_REGEX.search(response_text)
    if not match:
        return ParsedResponse(spoken_text=response_text.strip())

    spoken = response_text[: match.start()].strip()

    x_group = match.group("x")
    y_group = match.group("y")
    cell_group = match.group("cell")
    label_group = match.group("label")
    screen_group = match.group("screen")

    label = label_group.strip() if label_group else None
    screen = int(screen_group) if screen_group else None

    if cell_group is not None:
        return ParsedResponse(
            spoken_text=spoken,
            cell=cell_group,
            label=label,
            screen_number=screen,
        )
    if x_group is not None and y_group is not None:
        return ParsedResponse(
            spoken_text=spoken,
            point_x=int(x_group),
            point_y=int(y_group),
            label=label,
            screen_number=screen,
        )
    # [POINT:none] — matched, but no coordinate capture fired
    return ParsedResponse(spoken_text=spoken, label="none")


# ────────────────────────────────────────────────────────────────────
# Claude CLI output scrubbing
# Ported from the screen-copilot project's response cleanup.
# See https://github.com/Gvascons/screen-copilot
# ────────────────────────────────────────────────────────────────────

_XML_TAG_RE = re.compile(r"<[^>]*>")
_BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")


def _scrub_cli_artifacts(text: str) -> str:
    """Remove tool-use tags and stray base64 that sometimes leak into stdout."""
    text = _XML_TAG_RE.sub("", text)
    text = _BASE64_BLOB_RE.sub("", text)
    return text.strip()


# ────────────────────────────────────────────────────────────────────
# Screenshot handle
# ────────────────────────────────────────────────────────────────────

@dataclass
class ScreenCapture:
    """One screenshot as passed to Claude.

    `width` / `height` are the dimensions of the image file on disk AND
    the coordinate space Claude's POINT(x,y) tags operate in — they
    must match the dimensions embedded in the label.

    `source_width` / `source_height` are the dimensions of the original
    region that was cropped (the app window or the monitor). If the
    image was resized before being handed to Claude, these two pairs
    differ and `coords.resolve_point()` scales by the ratio.

    `monitor_x` / `monitor_y` are the root-window coordinates of the
    top-left corner of that *source* region — adding them to the
    scaled-back POINT gives the root-window pixel to fly the cursor to.
    """
    image_path: str
    label: str              # "cropped to the active application window ... (image dimensions: 800x533 pixels)"
    width: int              # pixels of the saved screenshot (Claude's POINT space)
    height: int
    source_width: int       # pixels of the original unresized crop
    source_height: int
    monitor_index: int      # 1-based, matches :screenN tag
    monitor_x: int          # root-window offset of the source region
    monitor_y: int
    is_cursor_screen: bool


# ────────────────────────────────────────────────────────────────────
# Claude CLI driver
# ────────────────────────────────────────────────────────────────────

class ClaudeAdapter:
    def __init__(
        self,
        model: str = config.DEFAULT_CLAUDE_MODEL,
        max_history: int = config.MAX_HISTORY_EXCHANGES,
        binary: str = "claude",
        timeout_seconds: float = 120.0,
    ) -> None:
        self.model = model
        self._max_history = max_history
        self._binary = binary
        self._timeout = timeout_seconds
        # [(user_transcript, assistant_spoken_text), ...]
        self._history: list[tuple[str, str]] = []
        # In-flight subprocess so cancel() can kill it from the hotkey
        # handler when the user interrupts mid-turn.
        self._current_proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._cancelled = threading.Event()

    # ── history ──────────────────────────────────────────────────────

    def clear_history(self) -> None:
        self._history.clear()

    def history_length(self) -> int:
        return len(self._history)

    def _build_history_block(self) -> str:
        if not self._history:
            return ""
        lines = []
        for user_text, assistant_text in self._history:
            # Truncate long assistant turns so the context stays small.
            trimmed = assistant_text
            if len(trimmed) > 800:
                trimmed = trimmed[:800] + "…"
            lines.append(f"[previous user]: {user_text}")
            lines.append(f"[previous you]: {trimmed}")
        return "conversation so far:\n" + "\n".join(lines) + "\n\n---\n\n"

    # ── prompt construction ──────────────────────────────────────────

    def _build_prompt(self, transcript: str, captures: Sequence[ScreenCapture]) -> str:
        history_block = self._build_history_block()

        if captures:
            image_lines = ["screenshots for this turn:"]
            for cap in captures:
                image_lines.append(f"- {cap.label}")
                image_lines.append(f"  read this image at: {cap.image_path}")
            image_block = "\n".join(image_lines) + "\n\n"
        else:
            image_block = ""

        return (
            f"{history_block}"
            f"{image_block}"
            f"the user just said (via voice push-to-talk):\n"
            f'"{transcript}"\n\n'
            f"respond following all the rules in your system prompt. "
            f"remember to end with a [POINT:...] tag."
        )

    # ── subprocess call ──────────────────────────────────────────────

    def ask(
        self,
        transcript: str,
        captures: Sequence[ScreenCapture] = (),
    ) -> ParsedResponse:
        """Send one turn to Claude. Blocks until the subprocess returns
        or `cancel()` is called on another thread.

        Raises RuntimeError on nonzero exit code, TimeoutError on hard
        timeout, and ClaudeCancelled when interrupted mid-flight.

        Must be called from a worker thread, not the GTK main thread.
        """
        prompt = self._build_prompt(transcript, captures)

        cmd = [
            self._binary, "-p",
            "--model", self.model,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Read",
            "--system-prompt", config.SYSTEM_PROMPT,
            prompt,
        ]

        print(f"🤖 claude: asking ({self.model}, {len(captures)} images, {len(self._history)} history)")

        # Reset the cancel flag so a previous cancel doesn't affect us.
        self._cancelled.clear()

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._proc_lock:
            self._current_proc = proc

        try:
            try:
                stdout, stderr = proc.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
                raise TimeoutError(
                    f"claude subprocess exceeded {self._timeout}s timeout"
                )
        finally:
            with self._proc_lock:
                self._current_proc = None

        if self._cancelled.is_set():
            raise ClaudeCancelled("claude call was interrupted by the user")

        if proc.returncode != 0:
            raise RuntimeError(
                (stderr or "").strip() or f"claude exited with code {proc.returncode}"
            )

        raw_text = _scrub_cli_artifacts(stdout)
        parsed = parse_point(raw_text)

        # Save to history using the stripped spoken text (no POINT tag).
        self._history.append((transcript, parsed.spoken_text))
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        return parsed

    def cancel(self) -> None:
        """Kill any in-flight `claude -p` subprocess. Safe from any thread.

        Sets an internal flag so the ask() call can distinguish a
        user-triggered cancellation from a normal subprocess failure,
        and raise ClaudeCancelled instead of RuntimeError.
        """
        self._cancelled.set()
        with self._proc_lock:
            proc = self._current_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
