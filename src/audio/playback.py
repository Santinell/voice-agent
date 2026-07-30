"""Queued playback for streamed responses and local activation earcons."""

from __future__ import annotations

import base64
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Protocol

import numpy as np
import sounddevice as sd

from audio.capture import PcmBlock


@dataclass(frozen=True)
class PcmItem:
    data: bytes


@dataclass(frozen=True)
class BarrierItem:
    callback: Callable[[], None]


@dataclass(frozen=True)
class StopItem:
    pass


QueueItem = PcmItem | BarrierItem | StopItem


class PlaybackStream(Protocol):
    def __enter__(self) -> PlaybackStream: ...

    def __exit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> None: ...

    def write(
        self,
        samples: np.ndarray[
            tuple[int] | tuple[int, int], np.dtype[np.int16]
        ],
    ) -> bool: ...


class AudioOutput(Protocol):
    """Realtime client's dependency on a speaker implementation."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def put_delta(self, b64: str) -> None: ...

    def put_pcm(self, samples: PcmBlock) -> None: ...

    def put_barrier(self, callback: Callable[[], None]) -> None: ...


class AudioPlayer:
    """Thread-safe ordered PCM player with completion barriers."""

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int = 1,
        out: IO[str] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._queue: queue.Queue[QueueItem] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._out = out
        self._on_error = on_error

    def start(self) -> None:
        """Start the background playback thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="audio-playback", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to stop after already queued audio."""
        if self._thread is None:
            return
        self._queue.put(StopItem())
        self._thread.join(timeout=2.0)
        self._thread = None

    def put_delta(self, b64: str) -> None:
        """Decode a streamed response audio delta and enqueue it."""
        self._queue.put(PcmItem(base64.b64decode(b64)))

    def put_pcm(self, samples: PcmBlock) -> None:
        """Enqueue local int16 PCM, used for the wake confirmation earcon."""
        pcm = np.asarray(samples, dtype=np.int16).reshape(-1)
        self._queue.put(PcmItem(pcm.tobytes()))

    def put_barrier(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` after all earlier queue items reach the stream."""
        self._queue.put(BarrierItem(callback))

    def _run(self) -> None:
        try:
            with sd.OutputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="int16",
            ) as stream:
                while True:
                    item = self._queue.get()
                    if not self._handle_item(stream, item):
                        return
        except Exception as exc:  # pragma: no cover - hardware-dependent
            if self._out is not None:
                print(f"[playback] error: {exc}", file=self._out)
            if self._on_error is not None:
                self._on_error(exc)

    def _handle_item(self, stream: PlaybackStream, item: QueueItem) -> bool:
        """Process one queue item; extracted for deterministic unit tests."""
        if isinstance(item, StopItem):
            return False
        if isinstance(item, BarrierItem):
            item.callback()
            return True
        samples = np.frombuffer(item.data, dtype=np.int16)
        if self._channels > 1:
            samples = samples.reshape(-1, self._channels)
        stream.write(samples)
        return True


def make_earcon(
    *,
    sample_rate: int,
    frequency_hz: float,
    end_frequency_hz: float | None = None,
    duration_ms: int,
    volume: float,
    trailing_silence_ms: int,
) -> PcmBlock:
    """Create a click-free sine cue followed by an output-latency guard."""
    tone_count = int(sample_rate * duration_ms / 1000)
    silence_count = int(sample_rate * trailing_silence_ms / 1000)
    if tone_count == 0:
        return np.zeros(silence_count, dtype=np.int16)

    time = np.arange(tone_count, dtype=np.float32) / sample_rate
    end_frequency = (
        frequency_hz if end_frequency_hz is None else end_frequency_hz
    )
    duration_sec = tone_count / sample_rate
    sweep_rate = (end_frequency - frequency_hz) / duration_sec
    phase = 2 * np.pi * (
        frequency_hz * time + 0.5 * sweep_rate * time * time
    )
    tone = np.sin(phase)
    fade_count = min(int(sample_rate * 0.01), tone_count // 2)
    if fade_count:
        envelope = np.ones(tone_count, dtype=np.float32)
        envelope[:fade_count] = np.linspace(0.0, 1.0, fade_count)
        envelope[-fade_count:] = np.linspace(1.0, 0.0, fade_count)
        tone *= envelope

    pcm = (tone * volume * 32767).clip(-32768, 32767).astype(np.int16)
    silence = np.zeros(silence_count, dtype=np.int16)
    return np.concatenate((pcm, silence))


def make_chime(kind: str, *, sample_rate: int) -> PcmBlock:
    """Attention cue for a fired timer/alarm/reminder, by ``kind``.

    Built from :func:`make_earcon` segments so each tone is click-free. Alarms
    repeat to be more insistent; reminders stay gentle; timers double-beep.
    """
    if kind == "alarm":
        beep = make_earcon(
            sample_rate=sample_rate,
            frequency_hz=1000,
            duration_ms=150,
            volume=0.4,
            trailing_silence_ms=60,
        )
        return np.concatenate((beep, beep, beep))
    if kind == "reminder":
        return make_earcon(
            sample_rate=sample_rate,
            frequency_hz=660,
            duration_ms=350,
            volume=0.3,
            trailing_silence_ms=120,
        )
    # timer: a short double beep.
    beep = make_earcon(
        sample_rate=sample_rate,
        frequency_hz=660,
        duration_ms=200,
        volume=0.35,
        trailing_silence_ms=60,
    )
    return np.concatenate((beep, beep))


__all__ = [
    "AudioOutput",
    "AudioPlayer",
    "BarrierItem",
    "PcmItem",
    "PlaybackStream",
    "QueueItem",
    "StopItem",
    "make_earcon",
    "make_chime",
]
