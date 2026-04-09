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
# POINT tag parser — regex copied verbatim from the Clicky project
# (leanring-buddy/CompanionManager.swift, parsePointingCoordinates).
# See https://github.com/farzaa/clicky
# ────────────────────────────────────────────────────────────────────

POINT_REGEX = re.compile(
    r"\[POINT:(?:none|(\d+)\s*,\s*(\d+)(?::([^\]:\s][^\]:]*?))?(?::screen(\d+))?)\]\s*$"
)


class ClaudeCancelled(Exception):
    """Raised when an in-flight claude -p call is interrupted by cancel()."""


@dataclass
class ParsedResponse:
    """A Claude response with the POINT tag (if any) extracted."""
    spoken_text: str                # response with POINT tag stripped, trimmed
    point_x: int | None = None      # screenshot pixel x
    point_y: int | None = None      # screenshot pixel y
    label: str | None = None        # short description of the element, or "none"
    screen_number: int | None = None  # 1-based; None means cursor screen

    @property
    def has_coordinate(self) -> bool:
        return self.point_x is not None and self.point_y is not None


def parse_point(response_text: str) -> ParsedResponse:
    """Extract the trailing POINT tag from a Claude response.

    Returns the spoken text (tag removed, whitespace trimmed) plus the
    parsed coordinate/label/screen fields. Responses with no tag come
    back with all POINT fields None. `[POINT:none]` comes back with
    coordinate None but `label="none"`, mirroring Clicky's behavior.
    """
    match = POINT_REGEX.search(response_text)
    if not match:
        return ParsedResponse(spoken_text=response_text.strip())

    spoken = response_text[: match.start()].strip()

    x_group = match.group(1)
    y_group = match.group(2)
    if x_group is None or y_group is None:
        # [POINT:none]
        return ParsedResponse(spoken_text=spoken, label="none")

    label = match.group(3).strip() if match.group(3) else None
    screen = int(match.group(4)) if match.group(4) else None

    return ParsedResponse(
        spoken_text=spoken,
        point_x=int(x_group),
        point_y=int(y_group),
        label=label,
        screen_number=screen,
    )


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
