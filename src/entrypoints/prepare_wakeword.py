"""Download/validate configured wake-word assets without opening audio devices."""

from __future__ import annotations

from audio.wakeword import OpenWakeWordDetector
from config import Settings


def main() -> None:
    wake = Settings.from_env().wake_word
    if wake.model is None:
        raise SystemExit("WAKE_WORD_MODEL is empty; nothing to prepare")
    detector = OpenWakeWordDetector(wake)
    print(f"Wake-word model is ready: {detector.model_name}")


__all__ = ["main"]
