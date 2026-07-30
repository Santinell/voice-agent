"""Wake activation and follow-up state-machine tests."""

from __future__ import annotations

from dataclasses import dataclass

from activation import ActivationGate, ActivationState


@dataclass
class FakeClock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _gate(
    clock: FakeClock,
    *,
    requires_wake: bool = True,
    follow_up_window_sec: float = 10.0,
) -> ActivationGate:
    return ActivationGate(
        requires_wake=requires_wake,
        listen_timeout_sec=8.0,
        follow_up_window_sec=follow_up_window_sec,
        max_active_sec=90.0,
        clock=clock,
    )


def _activate(gate: ActivationGate) -> int:
    activation_id = gate.begin_earcon()
    assert activation_id is not None
    assert gate.state is ActivationState.EARCON
    assert gate.finish_earcon(activation_id)
    assert gate.state is ActivationState.ACTIVE
    return activation_id


def _finish_deactivation(gate: ActivationGate) -> None:
    assert gate.state is ActivationState.DEACTIVATING
    activation_id = gate.snapshot().activation_id
    assert not gate.finish_deactivation(activation_id + 1)
    assert gate.finish_deactivation(activation_id)
    assert gate.state is ActivationState.SLEEPING


def test_wake_gate_starts_sleeping_and_opens_only_after_matching_earcon() -> None:
    gate = _gate(FakeClock())
    assert gate.state is ActivationState.SLEEPING
    assert not gate.should_forward_audio()

    activation_id = gate.begin_earcon()
    assert activation_id is not None
    assert gate.state is ActivationState.EARCON
    assert not gate.should_forward_audio()
    assert not gate.finish_earcon(activation_id + 1)
    assert gate.state is ActivationState.EARCON

    assert gate.finish_earcon(activation_id)
    assert gate.should_forward_audio()


def test_always_on_gate_never_rearms_or_times_out() -> None:
    clock = FakeClock()
    gate = _gate(clock, requires_wake=False)
    assert gate.state is ActivationState.ACTIVE
    assert gate.begin_earcon() is None

    snapshot = gate.snapshot()
    assert not gate.finish_response_if_current(snapshot)
    clock.advance(1_000)
    assert gate.check_timeouts() is None
    assert gate.state is ActivationState.ACTIVE


def test_listen_timeout_fails_closed() -> None:
    clock = FakeClock()
    gate = _gate(clock)
    _activate(gate)
    clock.advance(8.0)
    assert gate.check_timeouts() == "listen_timeout"
    assert gate.state is ActivationState.DEACTIVATING
    assert not gate.should_forward_audio()
    _finish_deactivation(gate)


def test_response_activity_cancels_short_listen_timeout() -> None:
    clock = FakeClock()
    gate = _gate(clock)
    _activate(gate)
    gate.note_response_activity()
    clock.advance(9.0)
    assert gate.check_timeouts() is None
    assert gate.state is ActivationState.ACTIVE


def test_final_response_opens_follow_up_window() -> None:
    clock = FakeClock()
    gate = _gate(clock, follow_up_window_sec=10.0)
    _activate(gate)
    gate.note_response_started()
    snapshot = gate.snapshot()

    assert gate.finish_response_if_current(snapshot)
    assert gate.state is ActivationState.FOLLOW_UP
    assert gate.should_forward_audio()

    clock.advance(9.9)
    assert gate.check_timeouts() is None
    clock.advance(0.1)
    assert gate.check_timeouts() == "follow_up_timeout"
    assert gate.state is ActivationState.DEACTIVATING
    assert not gate.should_forward_audio()
    _finish_deactivation(gate)


def test_follow_up_speech_returns_to_active_and_invalidates_old_callback() -> None:
    clock = FakeClock()
    gate = _gate(clock)
    _activate(gate)
    response = gate.snapshot()
    assert gate.finish_response_if_current(response)
    assert gate.state is ActivationState.FOLLOW_UP
    old_snapshot = gate.snapshot()
    gate.note_user_speech()

    assert gate.state is ActivationState.ACTIVE
    assert not gate.finish_response_if_current(old_snapshot)
    assert gate.state is ActivationState.ACTIVE


def test_response_created_invalidates_cancelled_response_barrier() -> None:
    gate = _gate(FakeClock())
    _activate(gate)
    cancelled_response = gate.snapshot()
    gate.note_response_started()
    assert not gate.finish_response_if_current(cancelled_response)


def test_zero_follow_up_window_requires_new_wake_after_response() -> None:
    gate = _gate(FakeClock(), follow_up_window_sec=0.0)
    _activate(gate)
    snapshot = gate.snapshot()
    assert gate.finish_response_if_current(snapshot)
    assert gate.state is ActivationState.DEACTIVATING
    assert gate.last_deactivation_reason == "response_complete"
    _finish_deactivation(gate)


def test_max_active_timeout_is_a_hard_safety_cap() -> None:
    clock = FakeClock()
    gate = _gate(clock)
    _activate(gate)
    gate.note_response_activity()
    clock.advance(90.0)
    assert gate.check_timeouts() == "max_active_timeout"
    assert gate.state is ActivationState.DEACTIVATING
    _finish_deactivation(gate)


def test_earcon_failure_returns_to_sleeping() -> None:
    gate = _gate(FakeClock())
    assert gate.begin_earcon() is not None
    gate.fail_earcon()
    assert gate.state is ActivationState.SLEEPING
    assert gate.last_deactivation_reason == "earcon_failed"


def test_playback_failure_closes_an_active_wake_gate() -> None:
    gate = _gate(FakeClock())
    _activate(gate)
    gate.fail_playback()
    assert gate.state is ActivationState.SLEEPING
    assert gate.last_deactivation_reason == "playback_failed"


def test_playback_failure_does_not_disable_always_on_mic() -> None:
    gate = _gate(FakeClock(), requires_wake=False)
    gate.fail_playback()
    assert gate.state is ActivationState.ACTIVE


def test_stop_is_terminal_for_forwarding() -> None:
    gate = _gate(FakeClock())
    _activate(gate)
    gate.stop()
    assert gate.state is ActivationState.STOPPED
    assert not gate.should_forward_audio()
    assert gate.begin_earcon() is None
