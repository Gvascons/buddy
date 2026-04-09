"""Claude backend types and CLI adapter + factory.

buddy supports two interchangeable Claude backends:

- **CLI** (`claude -p` subprocess) — works with a Claude MAX / Pro
  subscription, no API key required. The CLI's Read tool aggressively
  downsizes images to ~500 pixels on the long edge before the vision
  model sees them, which caps pointing precision on small UI
  elements.
- **API** (Anthropic Python SDK) — uses your `ANTHROPIC_API_KEY` to
  talk to the Messages API directly. Images go through as base64
  content blocks at full resolution, letting Claude see up to
  ~1568 px on the long edge. 3× better pointing accuracy in
  practice, and much lower latency.

`make_claude()` picks automatically based on whether
`ANTHROPIC_API_KEY` is set in the environment.

Both backends share:
- `ParsedResponse` / `parse_point` for POINT tag parsing
- `ScreenCapture` for describing captured screenshots
- `ClaudeAdapterBase` for history management + prompt building
- `ClaudeCancelled` exception for mid-turn interrupts
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Sequence

from buddy import config


# ────────────────────────────────────────────────────────────────────
# POINT tag parser — Clicky-style raw pixel coordinates.
# Regex originally copied from Clicky's CompanionManager.swift parse
# routine; simplified to just the pixel form after proving that
# Set-of-Marks grid-cell pointing is less accurate in practice on the
# CLI path (see git history for the experiment).
# See https://github.com/farzaa/clicky
# ────────────────────────────────────────────────────────────────────

POINT_REGEX = re.compile(
    r"\[POINT:"
    r"(?:none|(?P<x>\d+)\s*,\s*(?P<y>\d+))"           # either 'none' or x,y pixel integers
    r"(?::(?P<label>[^\]:\s][^\]:]*?))?"              # optional short label
    r"(?::screen(?P<screen>\d+))?"                    # optional :screenN
    r"\]\s*$"
)


class ClaudeCancelled(Exception):
    """Raised when an in-flight Claude call is interrupted by `cancel()`."""


@dataclass
class ParsedResponse:
    """A Claude response with the POINT tag (if any) extracted.

    `has_coordinate` is True when Claude emitted a real x/y pixel pair.
    `label="none"` with no coordinate means Claude explicitly said
    `[POINT:none]` (no pointing appropriate). Missing POINT tag leaves
    everything None except spoken_text.
    """
    spoken_text: str
    point_x: int | None = None
    point_y: int | None = None
    label: str | None = None
    screen_number: int | None = None

    @property
    def has_coordinate(self) -> bool:
        return self.point_x is not None and self.point_y is not None


def parse_point(response_text: str) -> ParsedResponse:
    """Extract the trailing POINT tag from a Claude response.

    Supported forms:
      [POINT:none]
      [POINT:x,y:label:screenN]    (label and :screenN are optional)

    Returns a ParsedResponse with the POINT tag stripped from the
    `spoken_text`. Responses with no tag come back with everything
    None except spoken_text.
    """
    match = POINT_REGEX.search(response_text)
    if not match:
        return ParsedResponse(spoken_text=response_text.strip())

    spoken = response_text[: match.start()].strip()
    x_group = match.group("x")
    y_group = match.group("y")
    label_group = match.group("label")
    screen_group = match.group("screen")

    label = label_group.strip() if label_group else None
    screen = int(screen_group) if screen_group else None

    if x_group is not None and y_group is not None:
        return ParsedResponse(
            spoken_text=spoken,
            point_x=int(x_group),
            point_y=int(y_group),
            label=label,
            screen_number=screen,
        )
    # [POINT:none] matched but no coordinate groups fired
    return ParsedResponse(spoken_text=spoken, label="none")


# ────────────────────────────────────────────────────────────────────
# Screenshot handle — passed from screenshot.py to Claude adapters
# ────────────────────────────────────────────────────────────────────

@dataclass
class ScreenCapture:
    """One screenshot as passed to a Claude adapter.

    `width`/`height` are the dimensions of the saved JPEG, which is
    the coordinate space Claude's POINT(x,y) tags operate in.

    `source_width`/`source_height` are the dimensions of the original
    region that was cropped (the active window or the monitor). If the
    image was resized before being handed to Claude, these two pairs
    differ and `coords.resolve_point()` scales by the ratio.

    `monitor_x`/`monitor_y` are the root-window coordinates of the
    top-left of that source region. Adding them to a scaled-back
    POINT gives the root-window pixel to fly the cursor to.
    """
    image_path: str
    label: str
    width: int
    height: int
    source_width: int
    source_height: int
    monitor_index: int
    monitor_x: int
    monitor_y: int
    is_cursor_screen: bool


# ────────────────────────────────────────────────────────────────────
# CLI output scrubbing — the `claude -p` CLI sometimes leaks tool-use
# XML tags and stray base64 into stdout. Only used by the CLI adapter.
# ────────────────────────────────────────────────────────────────────

_XML_TAG_RE = re.compile(r"<[^>]*>")
_BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")


def _scrub_cli_artifacts(text: str) -> str:
    text = _XML_TAG_RE.sub("", text)
    text = _BASE64_BLOB_RE.sub("", text)
    return text.strip()


# ────────────────────────────────────────────────────────────────────
# Shared base class for Claude adapters
# ────────────────────────────────────────────────────────────────────

class ClaudeAdapterBase:
    """Shared history tracking, prompt building, and cancellation
    bookkeeping. Subclasses override `ask()` and `cancel()`.
    """

    def __init__(
        self,
        model: str = config.DEFAULT_CLAUDE_MODEL,
        max_history: int = config.MAX_HISTORY_EXCHANGES,
    ) -> None:
        self.model = model
        self._max_history = max_history
        self._history: list[tuple[str, str]] = []
        self._cancelled = threading.Event()

    # ── history ──────────────────────────────────────────────────────

    def clear_history(self) -> None:
        self._history.clear()

    def history_length(self) -> int:
        return len(self._history)

    def _record_turn(self, user_transcript: str, assistant_spoken: str) -> None:
        self._history.append((user_transcript, assistant_spoken))
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    def _build_history_block(self) -> str:
        if not self._history:
            return ""
        lines = []
        for user_text, assistant_text in self._history:
            trimmed = assistant_text
            if len(trimmed) > 800:
                trimmed = trimmed[:800] + "…"
            lines.append(f"[previous user]: {user_text}")
            lines.append(f"[previous you]: {trimmed}")
        return "conversation so far:\n" + "\n".join(lines) + "\n\n---\n\n"

    # ── subclass hooks ───────────────────────────────────────────────

    def ask(
        self,
        transcript: str,
        captures: Sequence[ScreenCapture] = (),
    ) -> ParsedResponse:
        raise NotImplementedError

    def cancel(self) -> None:
        self._cancelled.set()


# ────────────────────────────────────────────────────────────────────
# CLI adapter — `claude -p` subprocess
# ────────────────────────────────────────────────────────────────────

class ClaudeCLIAdapter(ClaudeAdapterBase):
    """Uses the `claude -p` subprocess. No API key required — works
    with any Claude Pro/Max subscription. Slower (~8 s per turn
    including subprocess cold-start) and vision is capped at ~500 px
    wide after the CLI's internal downscale.
    """

    def __init__(
        self,
        model: str = config.DEFAULT_CLAUDE_MODEL,
        max_history: int = config.MAX_HISTORY_EXCHANGES,
        binary: str = "claude",
        timeout_seconds: float = 120.0,
    ) -> None:
        super().__init__(model=model, max_history=max_history)
        self._binary = binary
        self._timeout = timeout_seconds
        self._current_proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()

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
        prompt = self._build_prompt(transcript, captures)
        cmd = [
            self._binary, "-p",
            "--model", self.model,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Read",
            "--system-prompt", config.SYSTEM_PROMPT,
            prompt,
        ]

        print(
            f"🤖 claude-cli: asking ({self.model}, {len(captures)} images, "
            f"{len(self._history)} history)"
        )
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
        self._record_turn(transcript, parsed.spoken_text)
        return parsed

    def cancel(self) -> None:
        super().cancel()
        with self._proc_lock:
            proc = self._current_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


# ────────────────────────────────────────────────────────────────────
# Backend factory
# ────────────────────────────────────────────────────────────────────

def make_claude() -> ClaudeAdapterBase:
    """Construct the best available Claude backend.

    - If `ANTHROPIC_API_KEY` is set in the environment and the
      `anthropic` SDK is importable, returns the API adapter.
    - Otherwise falls back to the `claude -p` CLI adapter.

    Override `BUDDY_CLAUDE_BACKEND=cli` to force the CLI path even
    with an API key set.
    """
    forced = os.environ.get("BUDDY_CLAUDE_BACKEND", "").strip().lower()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    if forced == "cli":
        print("🤖 claude: forced CLI backend (BUDDY_CLAUDE_BACKEND=cli)")
        return ClaudeCLIAdapter()
    if forced == "api" or has_key:
        try:
            from buddy.claude_api_adapter import ClaudeAPIAdapter
            print(
                "🤖 claude: using Anthropic API (higher vision resolution, "
                "faster turns — via ANTHROPIC_API_KEY)"
            )
            return ClaudeAPIAdapter()
        except ImportError as exc:
            print(
                f"⚠️ claude: anthropic SDK not available ({exc}), "
                f"falling back to CLI"
            )
    print("🤖 claude: using `claude -p` CLI (works with any Claude Pro/Max sub)")
    return ClaudeCLIAdapter()
