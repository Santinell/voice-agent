"""Streaming resampler and wake-frame buffering tests."""

from __future__ import annotations

import numpy as np

from audio.resample import SoxrWakeResampler, WakeFrameBuffer


def test_frame_buffer_builds_exact_frames_from_arbitrary_chunks() -> None:
    buffer = WakeFrameBuffer(frame_size=4)

    assert buffer.push(np.array([1, 2, 3], dtype=np.int16)) == []
    frames = buffer.push(np.array([4, 5, 6, 7, 8, 9], dtype=np.int16))

    assert len(frames) == 2
    np.testing.assert_array_equal(frames[0], [1, 2, 3, 4])
    np.testing.assert_array_equal(frames[1], [5, 6, 7, 8])
    np.testing.assert_array_equal(
        buffer.push(np.array([10, 11, 12], dtype=np.int16))[0],
        [9, 10, 11, 12],
    )


def test_frame_buffer_reset_discards_pending_samples() -> None:
    buffer = WakeFrameBuffer(frame_size=4)
    assert buffer.push(np.array([1, 2, 3], dtype=np.int16)) == []
    buffer.reset()
    assert buffer.push(np.array([4], dtype=np.int16)) == []


def test_streaming_resampler_has_no_cumulative_drift_and_preserves_zero() -> None:
    resampler = SoxrWakeResampler(input_rate=24_000)
    outputs = [resampler.process(np.zeros(1920, dtype=np.int16)) for _ in range(100)]
    combined = np.concatenate(outputs)

    assert combined.dtype == np.int16
    assert np.count_nonzero(combined) == 0
    # soxr retains a short fixed filter tail; it must not accumulate per block.
    assert abs(combined.size - 100 * 1280) < 512


def test_resampler_output_builds_regular_openwakeword_frames() -> None:
    resampler = SoxrWakeResampler(input_rate=24_000)
    buffer = WakeFrameBuffer()
    frames = []
    for _ in range(20):
        frames.extend(buffer.push(resampler.process(np.zeros(1920, dtype=np.int16))))

    assert len(frames) >= 19
    assert all(frame.shape == (1280,) for frame in frames)


def test_resampler_preserves_sine_frequency() -> None:
    input_rate = 24_000
    frequency = 1_000
    sample_count = input_rate * 2
    phase = np.arange(sample_count) / input_rate
    source = (np.sin(2 * np.pi * frequency * phase) * 12_000).astype(np.int16)
    resampler = SoxrWakeResampler(input_rate=input_rate)
    chunks = [
        resampler.process(source[offset : offset + 1920]) for offset in range(0, source.size, 1920)
    ]
    output = np.concatenate(chunks)
    spectrum = np.abs(np.fft.rfft(output[1000:].astype(np.float32)))
    frequencies = np.fft.rfftfreq(output[1000:].size, 1 / 16_000)

    assert abs(float(frequencies[int(np.argmax(spectrum))]) - frequency) < 2


def test_resampler_reset_starts_a_fresh_stream() -> None:
    block = np.arange(1920, dtype=np.int16)
    resampler = SoxrWakeResampler(input_rate=24_000)
    first = resampler.process(block)
    resampler.process(block)
    resampler.reset()

    np.testing.assert_array_equal(resampler.process(block), first)
