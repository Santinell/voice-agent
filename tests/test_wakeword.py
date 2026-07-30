"""Deterministic model resolution and detector gating tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from audio import wakeword
from audio.wakeword import (
    OpenWakeWordDetector,
    model_name_pattern,
    resolve_wake_model_paths,
)


@dataclass
class FakeOpenWakeWord:
    MODELS: dict[str, dict[str, str]] = field(default_factory=dict)
    FEATURE_MODELS: dict[str, dict[str, str]] = field(default_factory=dict)
    VAD_MODELS: dict[str, dict[str, str]] = field(default_factory=dict)
    __file__: str = "openwakeword/__init__.py"


def _registered_model(package_path: Path) -> dict[str, str]:
    return {
        "download_url": "release/hey_jarvis_v0.1.tflite",
        "model_path": str(package_path.with_suffix(".tflite")),
    }


def _resolve(
    module: FakeOpenWakeWord,
    model_dir: Path,
    requested_model: str,
    framework: str = "onnx",
) -> list[Path]:
    return resolve_wake_model_paths(
        openwakeword_module=module,
        model_dir=model_dir,
        inference_framework=framework,
        requested_model=requested_model,
    )


def test_direct_model_path_has_highest_priority(tmp_path: Path) -> None:
    direct = tmp_path / "outside" / "custom.onnx"
    direct.parent.mkdir()
    direct.touch()

    assert _resolve(FakeOpenWakeWord(), tmp_path / "models", str(direct)) == [direct.resolve()]


def test_builtin_name_resolves_downloaded_model_in_model_dir(
    tmp_path: Path,
) -> None:
    package_path = tmp_path / "package" / "hey_jarvis_v0.1.onnx"
    module = FakeOpenWakeWord(MODELS={"hey_jarvis": _registered_model(package_path)})
    local = tmp_path / "models" / "hey_jarvis_v0.1.onnx"
    local.parent.mkdir()
    local.touch()

    assert _resolve(module, local.parent, "hey_jarvis") == [local.resolve()]


@pytest.mark.parametrize("filename", ["hey_findus.onnx", "hey_findus_v2.onnx"])
def test_manual_model_resolves_by_filename_prefix(tmp_path: Path, filename: str) -> None:
    local = tmp_path / filename
    local.touch()

    assert _resolve(FakeOpenWakeWord(), tmp_path, "hey_findus") == [local.resolve()]


def test_short_name_does_not_match_a_longer_prefix(tmp_path: Path) -> None:
    (tmp_path / "hey_jarvis.onnx").touch()
    with pytest.raises(FileNotFoundError):
        _resolve(FakeOpenWakeWord(), tmp_path, "jarvis")
    assert not model_name_pattern("jarvis").match("hey_jarvis.onnx")


def test_resolution_respects_inference_framework(tmp_path: Path) -> None:
    onnx = tmp_path / "hey_findus.onnx"
    tflite = tmp_path / "hey_findus.tflite"
    onnx.touch()
    tflite.touch()

    assert _resolve(FakeOpenWakeWord(), tmp_path, "hey_findus", "onnx") == [onnx.resolve()]
    assert _resolve(FakeOpenWakeWord(), tmp_path, "hey_findus", "tflite") == [tflite.resolve()]


def test_registry_and_directory_scan_do_not_duplicate_path(tmp_path: Path) -> None:
    package_path = tmp_path / "package" / "hey_jarvis_v0.1.onnx"
    module = FakeOpenWakeWord(MODELS={"hey_jarvis": _registered_model(package_path)})
    local = tmp_path / "hey_jarvis_v0.1.onnx"
    local.touch()

    assert _resolve(module, tmp_path, "hey_jarvis") == [local.resolve()]


def test_builtin_falls_back_to_package_resources(tmp_path: Path) -> None:
    package_path = tmp_path / "package" / "hey_jarvis_v0.1.onnx"
    package_path.parent.mkdir()
    package_path.touch()
    module = FakeOpenWakeWord(MODELS={"hey_jarvis": _registered_model(package_path)})

    assert _resolve(module, tmp_path / "models", "hey_jarvis") == [package_path.resolve()]


def test_missing_explicit_path_has_diagnostic_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.onnx"
    with pytest.raises(FileNotFoundError, match="path does not exist"):
        _resolve(FakeOpenWakeWord(), tmp_path, str(missing))


class FakePredictionModel:
    def __init__(self, predictions: list[dict[str, float]]) -> None:
        self._predictions: Iterator[dict[str, float]] = iter(predictions)
        self.reset_called = False

    def predict(self, frame: np.ndarray[Any, Any]) -> dict[str, float]:
        del frame
        return next(self._predictions)

    def reset(self) -> None:
        self.reset_called = True


def _detector(
    predictions: list[dict[str, float]],
    *,
    threshold: float = 0.5,
    patience: int = 1,
    cooldown_sec: float = 1.5,
) -> tuple[OpenWakeWordDetector, FakePredictionModel]:
    detector = OpenWakeWordDetector.__new__(OpenWakeWordDetector)
    detector.model_name = "test"
    detector.threshold = threshold
    detector.gain = 1.0
    detector.cooldown_sec = cooldown_sec
    detector.patience = patience
    detector._last_detection = 0.0
    detector._consecutive_hits = 0
    model = FakePredictionModel(predictions)
    detector._model = model
    return detector, model


def test_threshold_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    detector, _ = _detector([{"wake": 0.5}])
    monkeypatch.setattr(wakeword.time, "monotonic", lambda: 100.0)

    detection = detector.process(np.zeros(1280, dtype=np.int16))

    assert detection.detected
    assert detection.model_name == "wake"
    assert detection.score == 0.5


def test_first_detection_is_not_blocked_by_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector, _ = _detector([{"wake": 0.9}], cooldown_sec=2.0)
    detector._last_detection = float("-inf")
    monkeypatch.setattr(wakeword.time, "monotonic", lambda: 0.1)

    assert detector.process(np.zeros(1280, dtype=np.int16)).detected


def test_patience_resets_after_negative_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector, _ = _detector(
        [{"wake": 0.8}, {"wake": 0.1}, {"wake": 0.8}, {"wake": 0.8}],
        patience=2,
    )
    monkeypatch.setattr(wakeword.time, "monotonic", lambda: 100.0)
    frame = np.zeros(1280, dtype=np.int16)

    assert not detector.process(frame).detected
    assert not detector.process(frame).detected
    assert not detector.process(frame).detected
    assert detector.process(frame).detected


def test_cooldown_suppresses_repeat_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector, _ = _detector(
        [{"wake": 0.9}, {"wake": 0.9}, {"wake": 0.9}],
        cooldown_sec=2.0,
    )
    moments = iter([100.0, 101.0, 102.0])
    monkeypatch.setattr(wakeword.time, "monotonic", lambda: next(moments))
    frame = np.zeros(1280, dtype=np.int16)

    assert detector.process(frame).detected
    assert not detector.process(frame).detected
    assert detector.process(frame).detected


def test_reset_clears_patience_and_resets_upstream_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector, model = _detector(
        [{"wake": 0.9}, {"wake": 0.9}, {"wake": 0.9}],
        patience=2,
    )
    monkeypatch.setattr(wakeword.time, "monotonic", lambda: 100.0)
    frame = np.zeros(1280, dtype=np.int16)
    assert not detector.process(frame).detected

    detector.reset()

    assert model.reset_called
    assert not detector.process(frame).detected
    assert detector.process(frame).detected


def test_gain_clips_without_int16_overflow() -> None:
    frame = np.array([20_000, -20_000], dtype=np.int16)
    gained = OpenWakeWordDetector._apply_gain(frame, 4.0)
    np.testing.assert_array_equal(gained, np.array([32_767, -32_768], dtype=np.int16))
