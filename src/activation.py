"""Thread-safe wake-word and follow-up-window state machine."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

Clock = Callable[[], float]


class ActivationState(StrEnum):
    """Client-side microphone gate states."""

    SLEEPING = "sleeping"
    EARCON = "earcon"
    ACTIVE = "active"
    FOLLOW_UP = "follow_up"
    DEACTIVATING = "deactivating"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ActivationSnapshot:
    """Identity used to reject stale playback callbacks."""

    activation_id: int
    interaction_revision: int


class ActivationGate:
    """Own the activation lifecycle shared by mic, recv and playback threads."""

    def __init__(
        self,
        *,
        requires_wake: bool,
        listen_timeout_sec: float,
        follow_up_window_sec: float,
        max_active_sec: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self._requires_wake = requires_wake
        self._listen_timeout_sec = listen_timeout_sec
        self._follow_up_window_sec = follow_up_window_sec
        self._max_active_sec = max_active_sec
        self._clock = clock
        self._lock = threading.RLock()
        self._state = (
            ActivationState.SLEEPING if requires_wake else ActivationState.ACTIVE
        )
        self._activation_id = 0
        self._interaction_revision = 0
        self._listen_deadline: float | None = None
        self._follow_up_deadline: float | None = None
        self._hard_deadline: float | None = None
        self._last_deactivation_reason: str | None = None

    @property
    def requires_wake(self) -> bool:
        return self._requires_wake

    @property
    def state(self) -> ActivationState:
        with self._lock:
            return self._state

    @property
    def last_deactivation_reason(self) -> str | None:
        with self._lock:
            return self._last_deactivation_reason

    def should_forward_audio(self) -> bool:
        with self._lock:
            return self._state in {
                ActivationState.ACTIVE,
                ActivationState.FOLLOW_UP,
            }

    def begin_earcon(self) -> int | None:
        """Reserve a new activation and close the mic gate during earcon."""
        with self._lock:
            if not self._requires_wake or self._state is not ActivationState.SLEEPING:
                return None
            self._activation_id += 1
            self._state = ActivationState.EARCON
            self._last_deactivation_reason = None
            return self._activation_id

    def finish_earcon(self, activation_id: int) -> bool:
        """Open the mic gate after the matching earcon playback barrier."""
        with self._lock:
            if (
                self._state is not ActivationState.EARCON
                or activation_id != self._activation_id
            ):
                return False
            now = self._clock()
            self._state = ActivationState.ACTIVE
            self._interaction_revision += 1
            self._listen_deadline = now + self._listen_timeout_sec
            self._hard_deadline = now + self._max_active_sec
            return True

    def fail_earcon(self) -> None:
        """Fail closed when the local output stream cannot play the cue."""
        with self._lock:
            if self._state is ActivationState.EARCON:
                self._deactivate("earcon_failed")

    def fail_playback(self) -> None:
        """Close a wake-gated mic if the output thread becomes unavailable."""
        with self._lock:
            if self._requires_wake and self._state is not ActivationState.STOPPED:
                self._deactivate("playback_failed")

    def note_user_speech(self) -> None:
        """Keep the current cycle active when server VAD sees user speech."""
        with self._lock:
            if self._state not in {
                ActivationState.ACTIVE,
                ActivationState.FOLLOW_UP,
            }:
                return
            self._state = ActivationState.ACTIVE
            self._interaction_revision += 1
            self._listen_deadline = None
            self._follow_up_deadline = None

    def note_response_started(self) -> None:
        """Invalidate completion callbacks belonging to an older response."""
        with self._lock:
            if self._state not in {
                ActivationState.ACTIVE,
                ActivationState.FOLLOW_UP,
            }:
                return
            self._state = ActivationState.ACTIVE
            self._interaction_revision += 1
            self._listen_deadline = None
            self._follow_up_deadline = None

    def note_response_activity(self) -> None:
        """A response proves that the post-wake command was accepted."""
        with self._lock:
            if self._state in {
                ActivationState.ACTIVE,
                ActivationState.FOLLOW_UP,
            }:
                self._listen_deadline = None

    def snapshot(self) -> ActivationSnapshot:
        with self._lock:
            return ActivationSnapshot(
                activation_id=self._activation_id,
                interaction_revision=self._interaction_revision,
            )

    def finish_response_if_current(self, snapshot: ActivationSnapshot) -> bool:
        """Start follow-up listening after final response audio is drained."""
        with self._lock:
            if not self._requires_wake:
                return False
            if self._state not in {
                ActivationState.ACTIVE,
                ActivationState.FOLLOW_UP,
            }:
                return False
            if (
                snapshot.activation_id != self._activation_id
                or snapshot.interaction_revision != self._interaction_revision
            ):
                return False
            if self._follow_up_window_sec == 0:
                self._begin_deactivation("response_complete")
            else:
                self._state = ActivationState.FOLLOW_UP
                self._listen_deadline = None
                self._follow_up_deadline = (
                    self._clock() + self._follow_up_window_sec
                )
            return True

    def check_timeouts(self) -> str | None:
        """Apply expired deadlines and return a new deactivation reason."""
        with self._lock:
            if not self._requires_wake or self._state is ActivationState.STOPPED:
                return None
            now = self._clock()
            if self._hard_deadline is not None and now >= self._hard_deadline:
                self._begin_deactivation("max_active_timeout")
                return "max_active_timeout"
            if self._listen_deadline is not None and now >= self._listen_deadline:
                self._begin_deactivation("listen_timeout")
                return "listen_timeout"
            if (
                self._follow_up_deadline is not None
                and now >= self._follow_up_deadline
            ):
                self._begin_deactivation("follow_up_timeout")
                return "follow_up_timeout"
            return None

    def finish_deactivation(self, activation_id: int) -> bool:
        """Enable wake detection after the matching local sleep cue."""
        with self._lock:
            if (
                self._state is not ActivationState.DEACTIVATING
                or activation_id != self._activation_id
            ):
                return False
            self._state = ActivationState.SLEEPING
            return True

    def stop(self) -> None:
        with self._lock:
            self._state = ActivationState.STOPPED
            self._clear_deadlines()

    def _deactivate(self, reason: str) -> None:
        self._state = ActivationState.SLEEPING
        self._last_deactivation_reason = reason
        self._clear_deadlines()

    def _begin_deactivation(self, reason: str) -> None:
        self._state = ActivationState.DEACTIVATING
        self._last_deactivation_reason = reason
        self._clear_deadlines()

    def _clear_deadlines(self) -> None:
        self._listen_deadline = None
        self._follow_up_deadline = None
        self._hard_deadline = None


__all__ = ["ActivationGate", "ActivationSnapshot", "ActivationState", "Clock"]
