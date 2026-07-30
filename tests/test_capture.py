"""Tests for the pure parts of audio capture/playback (no hardware)."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from audio import capture


def test_encode_block_roundtrips_pcm() -> None:
    samples = np.array([[0], [100], [-100], [32767], [-32768]], dtype=np.int16)
    encoded = capture.encode_block(samples)
    decoded = np.frombuffer(base64.b64decode(encoded), dtype=np.int16)
    np.testing.assert_array_equal(decoded, samples.flatten())


def test_encode_block_empty() -> None:
    empty = np.zeros((0, 1), dtype=np.int16)
    assert capture.encode_block(empty) == ""


@dataclass
class FakeInputStream:
    block: np.ndarray[Any, Any]
    overflowed: bool = False

    def __enter__(self) -> FakeInputStream:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def read(self, frames: int) -> tuple[np.ndarray[Any, Any], bool]:
        assert frames == self.block.shape[0]
        return self.block, self.overflowed


@pytest.mark.parametrize("overflowed", [False, True])
def test_capture_yields_flat_copied_pcm_and_survives_overflow(
    monkeypatch: pytest.MonkeyPatch, overflowed: bool
) -> None:
    source = np.array([[1], [2], [3]], dtype=np.int16)
    stream = FakeInputStream(source, overflowed)
    received_kwargs: dict[str, object] = {}

    def open_stream(**kwargs: object) -> FakeInputStream:
        received_kwargs.update(kwargs)
        return stream

    monkeypatch.setattr(capture.sd, "InputStream", open_stream)
    blocks = capture.capture_pcm_blocks(
        sample_rate=24_000,
        block_size=3,
        channels=1,
        device="test-device",
    )
    first = next(blocks)
    source[:] = 9

    np.testing.assert_array_equal(first, [1, 2, 3])
    assert first.dtype == np.int16
    assert first.shape == (3,)
    assert received_kwargs["samplerate"] == 24_000
    assert received_kwargs["device"] == "test-device"
    np.testing.assert_array_equal(next(blocks), [9, 9, 9])
    blocks.close()
