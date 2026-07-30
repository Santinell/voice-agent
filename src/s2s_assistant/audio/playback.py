"""Speaker playback for Realtime ``response.output_audio.delta`` chunks.

The server streams response audio as base64 PCM deltas. We decode them and
push raw int16 bytes onto a thread-safe queue consumed by a single playback
stream, so audio plays contiguously regardless of how the deltas arrive.

A dedicated consumer thread is intentional: ``sounddevice.OutputStream.write``
blocks when the device buffer is full, which would stall the websocket event
loop. Decoupling via a queue keeps both pipelines independent.
"""

from __future__ import annotations

import base64
import queue
import threading
from typing import IO

import numpy as np
import sounddevice as sd

# Sentinel pushed onto the queue to tell the consumer thread to stop.
_STOP = object()


class AudioPlayer:
    """Thread-safe player that decodes base64 PCM and writes it to the speaker."""

    def __init__(
        self, *, sample_rate: int, channels: int = 1, out: IO[str] | None = None
    ) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._queue: queue.Queue[object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._out = out

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background playback thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="audio-playback", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to stop and wait for it to drain."""
        if self._thread is None:
            return
        self._queue.put(_STOP)
        self._thread.join(timeout=2.0)
        self._thread = None

    # ── producer side ───────────────────────────────────────────────────────

    def put_delta(self, b64: str) -> None:
        """Decode a base64 PCM delta and enqueue it for playback."""
        raw = base64.b64decode(b64)
        self._queue.put(raw)

    # ── consumer thread ─────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            with sd.OutputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="int16",
            ) as stream:
                while True:
                    item = self._queue.get()
                    if item is _STOP:
                        break
                    self._play(stream, item)  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover - hardware-dependent
            if self._out is not None:
                print(f"[playback] error: {exc}", file=self._out)

    def _play(self, stream: sd.OutputStream, raw: bytes) -> None:
        samples = np.frombuffer(raw, dtype=np.int16)
        # Reshape to (frames, channels) for the OutputStream; mono stays 1-D ok.
        if self._channels > 1:
            samples = samples.reshape(-1, self._channels)
        stream.write(samples)


__all__ = ["AudioPlayer"]
