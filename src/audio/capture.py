"""Microphone capture and Realtime wire encoding.

The OpenAI Realtime protocol expects raw 24 kHz 16-bit mono PCM, base64
encoded, delivered via ``input_audio_buffer.append``. Capture intentionally
yields raw PCM first so the local wake-word detector can inspect it before the
activation gate decides whether the block may be sent.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator

import numpy as np
import sounddevice as sd

PcmBlock = np.ndarray[tuple[int], np.dtype[np.int16]]


def encode_block(
    samples: np.ndarray[tuple[int] | tuple[int, int], np.dtype[np.int16]],
) -> str:
    """Encode an int16 PCM block to the base64 string the protocol expects."""
    return base64.b64encode(samples.tobytes()).decode("ascii")


def capture_pcm_blocks(
    *,
    sample_rate: int,
    block_size: int,
    channels: int = 1,
    device: int | str | None = None,
) -> Iterator[PcmBlock]:
    """Yield copied, flat PCM blocks from the microphone forever.

    ``device`` selects the input device (index or name substring); ``None``
    uses the system default. Runs until the consumer stops iterating (e.g. on
    Ctrl-C). Audio device errors propagate — the caller decides how to surface
    them.
    """
    with sd.InputStream(
        samplerate=sample_rate,
        blocksize=block_size,
        channels=channels,
        dtype="int16",
        device=device,
    ) as stream:
        while True:
            # read() blocks until a full block is available.
            block, overflowed = stream.read(block_size)
            if overflowed:
                # PortAudio keeps the stream usable after an overflow. The
                # caller observes gaps through regular logging/telemetry.
                pass
            yield np.asarray(block, dtype=np.int16).reshape(-1).copy()


def capture_blocks(
    *,
    sample_rate: int,
    block_size: int,
    channels: int = 1,
    device: int | str | None = None,
) -> Iterator[str]:
    """Backward-compatible encoded wrapper around :func:`capture_pcm_blocks`."""
    for block in capture_pcm_blocks(
        sample_rate=sample_rate,
        block_size=block_size,
        channels=channels,
        device=device,
    ):
        yield encode_block(block)


__all__ = ["PcmBlock", "capture_blocks", "capture_pcm_blocks", "encode_block"]
