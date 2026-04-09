"""Voice coworker state machine.

Mirrors Clicky's CompanionVoiceState (CompanionManager.swift:17-21).
Only the GTK main thread should mutate state. Workers post transitions
back via GLib.idle_add(state.transition, NEW_STATE).
"""

from __future__ import annotations

import enum
from typing import Callable


class VoiceState(enum.Enum):
    IDLE = "idle"                # nothing happening
    LISTENING = "listening"      # hotkey held, recording mic
    PROCESSING = "processing"    # transcribe + screenshot + claude in flight
    RESPONDING = "responding"    # TTS playing, possibly cursor flying


# Allowed transitions. Any other transition is silently ignored (we log it)
# rather than raising, because race conditions between the hotkey thread,
# the worker thread, and the TTS thread can legitimately produce redundant
# requests like "go to IDLE" while we're already IDLE.
_ALLOWED_TRANSITIONS: dict[VoiceState, set[VoiceState]] = {
    VoiceState.IDLE: {VoiceState.LISTENING},
    VoiceState.LISTENING: {VoiceState.IDLE, VoiceState.PROCESSING},
    VoiceState.PROCESSING: {VoiceState.IDLE, VoiceState.RESPONDING},
    VoiceState.RESPONDING: {VoiceState.IDLE, VoiceState.LISTENING},
}


Observer = Callable[[VoiceState, VoiceState], None]


class StateMachine:
    """Holds the current state and notifies observers on transitions."""

    def __init__(self) -> None:
        self._state: VoiceState = VoiceState.IDLE
        self._observers: list[Observer] = []

    @property
    def state(self) -> VoiceState:
        return self._state

    def add_observer(self, observer: Observer) -> None:
        self._observers.append(observer)

    def transition(self, new_state: VoiceState) -> bool:
        """Attempt to transition to new_state.

        Returns True if the transition happened, False if it was ignored
        (e.g. self-loop or disallowed edge). Observers are fired only on
        real transitions.
        """
        old_state = self._state
        if new_state == old_state:
            return False
        allowed = _ALLOWED_TRANSITIONS.get(old_state, set())
        if new_state not in allowed:
            print(f"⚠️ state: ignoring {old_state.value} → {new_state.value}")
            return False

        self._state = new_state
        print(f"🟢 state: {old_state.value} → {new_state.value}")
        for observer in self._observers:
            try:
                observer(old_state, new_state)
            except Exception as exc:
                print(f"⚠️ state observer raised: {exc}")
        return True

    def force(self, new_state: VoiceState) -> None:
        """Emergency transition — skips the allowed-edge check.

        Used only for cleanup on fatal errors or shutdown.
        """
        old_state = self._state
        if new_state == old_state:
            return
        self._state = new_state
        print(f"🟠 state (forced): {old_state.value} → {new_state.value}")
        for observer in self._observers:
            try:
                observer(old_state, new_state)
            except Exception as exc:
                print(f"⚠️ state observer raised: {exc}")
