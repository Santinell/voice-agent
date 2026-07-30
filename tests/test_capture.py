"""Tests for the pure parts of audio capture/playback (no hardware)."""

from __future__ import annotations

import base64

import numpy as np

from s2s_assistant.audio import capture


def test_encode_block_roundtrips_pcm() -> None:
    samples = np.array([[0], [100], [-100], [32767], [-32768]], dtype=np.int16)
    encoded = capture.encode_block(samples)
    decoded = np.frombuffer(base64.b64decode(encoded), dtype=np.int16)
    np.testing.assert_array_equal(decoded, samples.flatten())


def test_encode_block_empty() -> None:
    empty = np.zeros((0, 1), dtype=np.int16)
    assert capture.encode_block(empty) == ""
