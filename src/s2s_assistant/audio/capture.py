"""Microphone capture → base64 PCM chunks for the Realtime ``input_audio``.

The OpenAI Realtime protocol expects raw 24 kHz 16-bit mono PCM, base64
encoded, delivered via ``input_audio_buffer.append``. We read the default
input device in blocks and yield base64 strings ready to send.

Keeping this thin: all the cleverness (VAD, turn detection, barge-in) lives
in the s2s server. We just shovel mic audio into the session.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator

import numpy as np
import sounddevice as sd


def encode_block(samples: np.ndarray[tuple[int, int], np.dtype[np.int16]]) -> str:
    """Encode an int16 PCM block to the base64 string the protocol expects."""
    return base64.b64encode(samples.tobytes()).decode("ascii")


def capture_blocks(
    *,
    sample_rate: int,
    block_size: int,
    channels: int = 1,
    device: int | str | None = None,
) -> Iterator[str]:
    """Yield base64-encoded PCM blocks from the microphone, forever.

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
            block, _overflowed = stream.read(block_size)
            yield encode_block(block)


__all__ = ["encode_block", "capture_blocks"]
