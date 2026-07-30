"""Stateful microphone resampling for the 16 kHz wake-word path."""

from __future__ import annotations

from typing import Protocol

import numpy as np
import soxr

from audio.capture import PcmBlock

WAKE_SAMPLE_RATE = 16_000
WAKE_FRAME_SIZE = 1_280


class AudioResampler(Protocol):
    def process(self, samples: PcmBlock) -> PcmBlock: ...

    def reset(self) -> None: ...


class SoxrWakeResampler:
    """Convert continuous mono int16 PCM while preserving filter state."""

    def __init__(self, *, input_rate: int, output_rate: int = WAKE_SAMPLE_RATE) -> None:
        self._input_rate = input_rate
        self._output_rate = output_rate
        self._stream = self._new_stream()

    def _new_stream(self) -> soxr.ResampleStream:
        return soxr.ResampleStream(
            self._input_rate,
            self._output_rate,
            1,
            dtype="float32",
            quality="HQ",
        )

    def process(self, samples: PcmBlock) -> PcmBlock:
        output = self._stream.resample_chunk(
            samples.astype(np.float32, copy=False), last=False
        )
        pcm = np.clip(np.rint(output), -32768, 32767).astype(np.int16)
        return pcm.reshape(-1)

    def reset(self) -> None:
        self._stream = self._new_stream()


class WakeFrameBuffer:
    """Accumulate resampler output into exact openWakeWord frames."""

    def __init__(self, frame_size: int = WAKE_FRAME_SIZE) -> None:
        self._frame_size = frame_size
        self._pending = np.zeros(0, dtype=np.int16)

    def push(self, samples: PcmBlock) -> list[PcmBlock]:
        combined = np.concatenate((self._pending, samples))
        count = combined.size // self._frame_size
        frames = [
            combined[index * self._frame_size : (index + 1) * self._frame_size]
            for index in range(count)
        ]
        self._pending = combined[count * self._frame_size :]
        return frames

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.int16)


__all__ = [
    "AudioResampler",
    "SoxrWakeResampler",
    "WAKE_FRAME_SIZE",
    "WAKE_SAMPLE_RATE",
    "WakeFrameBuffer",
]
