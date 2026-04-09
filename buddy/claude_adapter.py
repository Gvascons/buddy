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
    """One monitor's screenshot as passed to Claude.

    The `label` must include the pixel dimensions so Claude's POINT
    coordinates are in the correct space.
    """
    image_path: str
    label: str              # human-readable: "screen 1 of 2 — cursor on this screen (primary focus) (image dimensions: 2560x1440 pixels)"
    width: int              # pixels of the saved screenshot
    height: int
    monitor_index: int      # 1-based, matches :screenN tag
    monitor_x: int          # root-window offset
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
        """Send one turn to Claude. Blocks until the subprocess returns.

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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(
                stderr or f"claude exited with code {result.returncode}"
            )

        raw_text = _scrub_cli_artifacts(result.stdout)
        parsed = parse_point(raw_text)

        # Save to history using the stripped spoken text (no POINT tag).
        self._history.append((transcript, parsed.spoken_text))
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        return parsed
