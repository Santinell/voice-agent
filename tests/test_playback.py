"""Ordered speaker queue and earcon tests without audio hardware."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from audio import playback
from audio.playback import (
    AudioPlayer,
    BarrierItem,
    PcmItem,
    StopItem,
    make_earcon,
)


@dataclass
class FakeStream:
    writes: list[np.ndarray[Any, Any]] = field(default_factory=list)

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def write(self, samples: np.ndarray[Any, Any]) -> bool:
        self.writes.append(samples.copy())
        return False


def test_pcm_and_barrier_are_processed_in_order() -> None:
    player = AudioPlayer(sample_rate=16_000)
    stream = FakeStream()
    order: list[str] = []

    assert player._handle_item(stream, PcmItem(np.array([1, 2], dtype=np.int16).tobytes()))
    order.append("pcm")
    assert player._handle_item(stream, BarrierItem(lambda: order.append("barrier")))

    np.testing.assert_array_equal(stream.writes[0], [1, 2])
    assert order == ["pcm", "barrier"]
    assert not player._handle_item(stream, StopItem())


def test_background_player_drains_before_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream()
    monkeypatch.setattr(playback.sd, "OutputStream", lambda **_kwargs: stream)
    barrier_done = threading.Event()
    player = AudioPlayer(sample_rate=16_000)
    player.start()
    player.put_pcm(np.array([10, 20], dtype=np.int16))
    player.put_barrier(barrier_done.set)
    player.stop()

    assert barrier_done.is_set()
    np.testing.assert_array_equal(stream.writes[0], [10, 20])


def test_playback_error_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("output unavailable")

    def fail(**_kwargs: object) -> FakeStream:
        raise error

    seen: list[Exception] = []
    monkeypatch.setattr(playback.sd, "OutputStream", fail)
    player = AudioPlayer(sample_rate=16_000, on_error=seen.append)
    player._run()

    assert seen == [error]


def test_make_earcon_includes_faded_tone_and_trailing_silence() -> None:
    earcon = make_earcon(
        sample_rate=16_000,
        frequency_hz=880,
        duration_ms=120,
        volume=0.25,
        trailing_silence_ms=120,
    )

    assert earcon.dtype == np.int16
    assert earcon.shape == (3840,)
    assert earcon[0] == 0
    assert np.any(earcon[:1920])
    assert not np.any(earcon[1920:])
    assert int(np.max(np.abs(earcon.astype(np.int32)))) <= int(32767 * 0.25)


def test_zero_duration_earcon_is_only_silence() -> None:
    earcon = make_earcon(
        sample_rate=16_000,
        frequency_hz=880,
        duration_ms=0,
        volume=0.25,
        trailing_silence_ms=50,
    )
    assert earcon.shape == (800,)
    assert not np.any(earcon)


def test_deactivation_earcon_is_a_descending_chirp() -> None:
    sample_rate = 16_000
    tone_samples = int(sample_rate * 0.18)
    earcon = make_earcon(
        sample_rate=sample_rate,
        frequency_hz=660,
        end_frequency_hz=440,
        duration_ms=180,
        volume=0.2,
        trailing_silence_ms=100,
    )

    first_half = earcon[: tone_samples // 2].astype(np.float32)
    second_half = earcon[tone_samples // 2 : tone_samples].astype(np.float32)
    frequencies = np.fft.rfftfreq(first_half.size, 1 / sample_rate)
    first_peak = frequencies[int(np.argmax(np.abs(np.fft.rfft(first_half))))]
    second_peak = frequencies[int(np.argmax(np.abs(np.fft.rfft(second_half))))]

    assert earcon.shape == (4480,)
    assert first_peak > second_peak
    assert not np.any(earcon[tone_samples:])
