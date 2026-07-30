"""Minimal openWakeWord model declarations used by the adapter."""

from typing import Any

import numpy as np


class Model:
    def __init__(
        self,
        *,
        wakeword_models: list[str],
        inference_framework: str,
        melspec_model_path: str,
        embedding_model_path: str,
        vad_threshold: float,
        enable_speex_noise_suppression: bool,
    ) -> None: ...

    def predict(self, frame: np.ndarray[Any, Any]) -> dict[str, float]: ...

    def reset(self) -> None: ...
