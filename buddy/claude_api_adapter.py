"""Anthropic API Claude backend.

Uses the `anthropic` Python SDK to talk to the Messages API directly.
Key differences vs. the CLI adapter:

- **Images go through as base64 content blocks** instead of via the
  Read tool, which means the vision model sees them at the API's
  ~1568 px long-edge resolution instead of the CLI's ~500 px.
  This is the single biggest factor in pointing accuracy.
- **Streaming responses**. The response streams in text deltas, so
  we can parse the POINT tag and kick off TTS the moment the full
  response is available — typically ~1–3 s for haiku/sonnet with
  a single image, vs. the CLI's ~8 s per turn.
- **Clean cancellation**. The stream context manager lets us close
  the response mid-flight when the user re-presses the hotkey.

Costs are negligible for personal use — a typical voice turn is
~1500 input tokens + 1 image + ~80 output tokens. At haiku prices
(~$0.25 / M input, $1.25 / M output) that's ~$0.0005 per turn.
"""

from __future__ import annotations

import base64
import threading
from pathlib import Path
from typing import Sequence

from buddy import config
from buddy.claude_adapter import (
    ClaudeAdapterBase,
    ClaudeCancelled,
    ParsedResponse,
    ScreenCapture,
    parse_point,
)


# Short aliases → actual Anthropic model IDs (see the model selection
# guidance in the Claude docs for the latest mapping).
_MODEL_ID_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}


class ClaudeAPIAdapter(ClaudeAdapterBase):
    def __init__(
        self,
        model: str = config.DEFAULT_CLAUDE_MODEL,
        max_history: int = config.MAX_HISTORY_EXCHANGES,
    ) -> None:
        super().__init__(model=model, max_history=max_history)
        # Lazy-import so the rest of buddy runs without the SDK if
        # the user only needs the CLI path.
        import anthropic
        self._client = anthropic.Anthropic()
        self._stream_lock = threading.Lock()
        self._current_stream = None  # anthropic stream context manager

    # ── model id resolution ──────────────────────────────────────────

    def _resolve_model_id(self) -> str:
        return _MODEL_ID_MAP.get(self.model, self.model)

    # ── content block construction ───────────────────────────────────

    def _build_content_blocks(
        self,
        transcript: str,
        captures: Sequence[ScreenCapture],
    ) -> list[dict]:
        """Build the list of `content` blocks for the Messages API.

        Each screenshot becomes an `image` block (base64-encoded JPEG)
        followed by a short `text` block identifying it. The user's
        transcript + history block goes as the final `text` block.
        """
        content: list[dict] = []

        for cap in captures:
            image_bytes = Path(cap.image_path).read_bytes()
            image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": image_b64,
                },
            })
            content.append({
                "type": "text",
                "text": f"(image above: {cap.label})",
            })

        history_block = self._build_history_block()
        content.append({
            "type": "text",
            "text": (
                f"{history_block}"
                f"the user just said (via voice push-to-talk):\n"
                f'"{transcript}"\n\n'
                f"respond following all the rules in your system prompt. "
                f"remember to end with a [POINT:...] tag."
            ),
        })
        return content

    # ── streaming turn ───────────────────────────────────────────────

    def ask(
        self,
        transcript: str,
        captures: Sequence[ScreenCapture] = (),
    ) -> ParsedResponse:
        """Stream Claude's response, accumulate the full text, parse
        the POINT tag, record the turn in history, and return.

        Runs synchronously on the calling worker thread — the internal
        streaming is purely so we can abort mid-flight if the user
        hits the hotkey again.
        """
        import anthropic  # for exception types

        model_id = self._resolve_model_id()
        content = self._build_content_blocks(transcript, captures)
        print(
            f"🤖 claude-api: streaming ({self.model} → {model_id}, "
            f"{len(captures)} images, {len(self._history)} history)"
        )

        self._cancelled.clear()
        accumulated: list[str] = []

        try:
            stream_ctx = self._client.messages.stream(
                model=model_id,
                max_tokens=1024,
                system=config.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )
            with stream_ctx as stream:
                with self._stream_lock:
                    self._current_stream = stream
                try:
                    for delta in stream.text_stream:
                        if self._cancelled.is_set():
                            raise ClaudeCancelled(
                                "claude call was interrupted by the user"
                            )
                        accumulated.append(delta)
                finally:
                    with self._stream_lock:
                        self._current_stream = None
        except ClaudeCancelled:
            raise
        except anthropic.APIError as exc:
            if self._cancelled.is_set():
                raise ClaudeCancelled("claude call was interrupted by the user")
            raise RuntimeError(f"Anthropic API error: {exc}") from exc

        if self._cancelled.is_set():
            raise ClaudeCancelled("claude call was interrupted by the user")

        raw_text = "".join(accumulated).strip()
        parsed = parse_point(raw_text)
        self._record_turn(transcript, parsed.spoken_text)
        return parsed

    def cancel(self) -> None:
        """Abort an in-flight streaming request.

        Sets the cancel event so the stream loop bails on its next
        delta, and closes the underlying HTTP response so the SDK
        stops blocking on network I/O.
        """
        super().cancel()
        with self._stream_lock:
            stream = self._current_stream
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
