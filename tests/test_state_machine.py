"""Tests for the voice state machine."""

from buddy.state_machine import StateMachine, VoiceState


def test_initial_state_is_idle():
    sm = StateMachine()
    assert sm.state == VoiceState.IDLE


def test_allowed_idle_to_listening():
    sm = StateMachine()
    assert sm.transition(VoiceState.LISTENING) is True
    assert sm.state == VoiceState.LISTENING


def test_disallowed_idle_to_processing():
    sm = StateMachine()
    assert sm.transition(VoiceState.PROCESSING) is False
    assert sm.state == VoiceState.IDLE


def test_full_happy_path():
    sm = StateMachine()
    events: list[tuple[VoiceState, VoiceState]] = []
    sm.add_observer(lambda old, new: events.append((old, new)))

    assert sm.transition(VoiceState.LISTENING)
    assert sm.transition(VoiceState.PROCESSING)
    assert sm.transition(VoiceState.RESPONDING)
    assert sm.transition(VoiceState.IDLE)
    assert sm.state == VoiceState.IDLE
    assert events == [
        (VoiceState.IDLE, VoiceState.LISTENING),
        (VoiceState.LISTENING, VoiceState.PROCESSING),
        (VoiceState.PROCESSING, VoiceState.RESPONDING),
        (VoiceState.RESPONDING, VoiceState.IDLE),
    ]


def test_responding_interrupted_by_new_listening():
    sm = StateMachine()
    sm.transition(VoiceState.LISTENING)
    sm.transition(VoiceState.PROCESSING)
    sm.transition(VoiceState.RESPONDING)
    assert sm.transition(VoiceState.LISTENING) is True
    assert sm.state == VoiceState.LISTENING


def test_self_loop_ignored():
    sm = StateMachine()
    events: list[tuple[VoiceState, VoiceState]] = []
    sm.add_observer(lambda old, new: events.append((old, new)))
    assert sm.transition(VoiceState.IDLE) is False
    assert events == []


def test_force_skips_allowed_edge_check():
    sm = StateMachine()
    sm.transition(VoiceState.LISTENING)
    sm.transition(VoiceState.PROCESSING)
    # PROCESSING → LISTENING is not allowed normally
    assert sm.transition(VoiceState.LISTENING) is False
    sm.force(VoiceState.LISTENING)
    assert sm.state == VoiceState.LISTENING


def test_listening_to_idle_allowed():
    sm = StateMachine()
    sm.transition(VoiceState.LISTENING)
    assert sm.transition(VoiceState.IDLE)
    assert sm.state == VoiceState.IDLE
