"""Composition-root tests for enabling wake mode from the model field."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

import app
from activation import ActivationState
from audio.wakeword import WakeDetection
from config import Settings, WakeWordSettings


def _settings(wake: WakeWordSettings, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "s2s_url": "ws://localhost:8765/v1/realtime",
        "llm_base_url": "http://localhost:8765/v1",
        "llm_api_key": "test",
        "llm_model": "test-model",
        "language": "ru",
        "geocoding_url": "https://geocoding.invalid",
        "forecast_url": "https://forecast.invalid",
        "sample_rate": 24_000,
        "input_device": None,
        "output_sample_rate": 16_000,
        "channels": 1,
        "block_size": 1920,
        "wake_word": wake,
    }
    values.update(overrides)
    return Settings(**values)


@dataclass
class FakeDetector:
    reset_count: int = 0

    def process(self, frame: np.ndarray[Any, Any]) -> WakeDetection:
        del frame
        return WakeDetection(False, None, 0.0)

    def reset(self) -> None:
        self.reset_count += 1


def test_empty_model_builds_original_always_on_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_detector(_wake: WakeWordSettings) -> FakeDetector:
        raise AssertionError("detector must stay lazy in always-on mode")

    monkeypatch.setattr(app, "OpenWakeWordDetector", unexpected_detector)
    client = app.build_client(_settings(WakeWordSettings(model=None)))
    try:
        assert client.activation_gate.state is ActivationState.ACTIVE
        assert not client.activation_gate.requires_wake
        assert client.wake_runtime is None
    finally:
        client.deps.http_client.close()


def test_non_empty_model_builds_sleeping_wake_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = FakeDetector()
    monkeypatch.setattr(app, "OpenWakeWordDetector", lambda _wake: detector)
    client = app.build_client(_settings(WakeWordSettings(model="test_wake")))
    try:
        assert client.activation_gate.state is ActivationState.SLEEPING
        assert client.activation_gate.requires_wake
        assert client.wake_runtime is not None
        assert client.wake_runtime.detector is detector
        assert client.wake_runtime.earcon.dtype == np.int16
        assert client.wake_runtime.deactivation_earcon.dtype == np.int16
        assert not np.array_equal(
            client.wake_runtime.earcon,
            client.wake_runtime.deactivation_earcon,
        )
    finally:
        client.deps.http_client.close()


@pytest.mark.parametrize(
    ("override", "value", "message"),
    [
        ("sample_rate", 16_000, "SAMPLE_RATE=24000"),
        ("output_sample_rate", 24_000, "OUTPUT_SAMPLE_RATE=16000"),
    ],
)
def test_wake_mode_rejects_incompatible_audio_rates(
    override: str, value: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        app.build_client(
            _settings(
                WakeWordSettings(model="test_wake"),
                **{override: value},
            )
        )
