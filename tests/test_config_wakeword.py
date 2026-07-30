"""Wake-word environment configuration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import WakeWordSettings


def test_empty_model_keeps_wake_word_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAKE_WORD_MODEL", "   ")
    assert WakeWordSettings.from_env().model is None


def test_non_empty_model_enables_wake_word(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WAKE_WORD_MODEL", "  hey_findus  ")
    monkeypatch.setenv("WAKE_WORD_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("WAKE_WORD_THRESHOLD", "0.42")
    monkeypatch.setenv("WAKE_WORD_NOISE_SUPPRESSION", "false")
    monkeypatch.setenv("FOLLOW_UP_WINDOW_SEC", "12")

    settings = WakeWordSettings.from_env()

    assert settings.model == "hey_findus"
    assert settings.model_dir == tmp_path.resolve()
    assert settings.threshold == 0.42
    assert not settings.noise_suppression
    assert settings.follow_up_window_sec == 12


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("inference_framework", "invalid"),
        ("threshold", 0),
        ("gain", 0),
        ("patience", 0),
        ("vad_threshold", 1.1),
        ("earcon_volume", -0.1),
        ("deactivation_earcon_volume", 1.1),
        ("deactivation_earcon_end_hz", 0),
        ("cooldown_sec", -0.1),
        ("follow_up_window_sec", -1),
    ],
)
def test_invalid_wake_settings_fail_early(field: str, value: object) -> None:
    values: dict[str, object] = {field: value}
    with pytest.raises(ValueError):
        WakeWordSettings(**values)  # type: ignore[arg-type]
