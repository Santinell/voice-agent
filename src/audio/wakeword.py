"""Local openWakeWord adapter and deterministic model resolution."""

from __future__ import annotations

import os
import re
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from audio.capture import PcmBlock
from config import WakeWordSettings


class OpenWakeWordModule(Protocol):
    @property
    def MODELS(self) -> Mapping[str, Mapping[str, str]]: ...

    @property
    def FEATURE_MODELS(self) -> Mapping[str, Mapping[str, str]]: ...

    @property
    def VAD_MODELS(self) -> Mapping[str, Mapping[str, str]]: ...

    @property
    def __file__(self) -> str: ...


@dataclass(frozen=True)
class WakeDetection:
    detected: bool
    model_name: str | None
    score: float


class WakeDetector(Protocol):
    def process(self, frame: PcmBlock) -> WakeDetection: ...

    def reset(self) -> None: ...


def model_name_pattern(name: str) -> re.Pattern[str]:
    """Match an exact model prefix plus version or extension boundary."""
    normalized = re.escape(name.replace(" ", "_"))
    return re.compile(rf"^{normalized}(_v|\.)")


def _looks_like_path(value: str) -> bool:
    path = Path(value)
    return (
        "/" in value
        or "\\" in value
        or value.startswith((".", "~"))
        or path.suffix.lower() in {".onnx", ".tflite"}
    )


def resolve_wake_model_paths(
    *,
    openwakeword_module: OpenWakeWordModule,
    model_dir: Path,
    inference_framework: str,
    requested_model: str,
) -> list[Path]:
    """Resolve direct, application-local, then package-provided weights."""
    direct = Path(requested_model).expanduser()
    if direct.is_file():
        return [direct.resolve()]
    if _looks_like_path(requested_model):
        raise FileNotFoundError(f"Wake model path does not exist: {direct}")

    extension = f".{inference_framework}"
    pattern = model_name_pattern(requested_model)
    resolved: list[Path] = []

    # Registered model filename stored in the application-owned directory.
    for metadata in openwakeword_module.MODELS.values():
        filename = Path(metadata["download_url"]).with_suffix(extension).name
        if pattern.match(filename):
            candidate = (model_dir / filename).resolve()
            if candidate.is_file():
                resolved.append(candidate)

    # Custom/manual model absent from the upstream registry.
    if model_dir.is_dir():
        for candidate in sorted(model_dir.glob(f"*{extension}")):
            path = candidate.resolve()
            if pattern.match(candidate.name) and path not in resolved:
                resolved.append(path)

    if resolved:
        return resolved

    # Exact built-in name installed in openWakeWord package resources.
    builtin = openwakeword_module.MODELS.get(requested_model)
    if builtin is not None:
        package_path = Path(builtin["model_path"]).with_suffix(extension).resolve()
        if package_path.is_file():
            return [package_path]

    raise FileNotFoundError(
        f"Wake model {requested_model!r} was not found as a path, "
        f"in {model_dir}, or in openWakeWord package resources"
    )


def _asset_path(
    *,
    metadata: Mapping[str, str],
    model_dir: Path,
    extension: str,
) -> Path | None:
    local = model_dir / Path(metadata["download_url"]).with_suffix(extension).name
    if local.is_file():
        return local.resolve()
    package = Path(metadata["model_path"]).with_suffix(extension)
    return package.resolve() if package.is_file() else None


def _ensure_package_vad(openwakeword_module: OpenWakeWordModule, model_dir: Path) -> None:
    """Copy a prepared local VAD to the package location required upstream."""
    metadata = openwakeword_module.VAD_MODELS["silero_vad"]
    expected = Path(metadata["model_path"])
    if expected.is_file():
        return
    local = model_dir / Path(metadata["download_url"]).name
    if not local.is_file():
        raise FileNotFoundError("silero_vad.onnx is required for wake-word VAD")
    expected.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local, expected)


class OpenWakeWordDetector:
    """Stateful wake detector with local/custom and built-in model support."""

    def __init__(self, settings: WakeWordSettings) -> None:
        if settings.model is None:
            raise ValueError("Wake detector requires a non-empty WAKE_WORD_MODEL")

        # Deferred imports keep always-on mode free of wake-word dependencies.
        import openwakeword
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        self.model_name = settings.model
        self.threshold = settings.threshold
        self.gain = settings.gain
        self.cooldown_sec = settings.cooldown_sec
        self.patience = settings.patience
        self._last_detection = float("-inf")
        self._consecutive_hits = 0

        model_dir = settings.model_dir
        model_dir.mkdir(parents=True, exist_ok=True)

        try:
            wake_paths = resolve_wake_model_paths(
                openwakeword_module=openwakeword,
                model_dir=model_dir,
                inference_framework=settings.inference_framework,
                requested_model=settings.model,
            )
        except FileNotFoundError:
            # Only built-ins are downloadable by name. The upstream helper also
            # prepares feature and VAD assets in our application-owned folder.
            if settings.model not in openwakeword.MODELS:
                raise
            download_models(model_names=[settings.model], target_directory=str(model_dir))
            wake_paths = resolve_wake_model_paths(
                openwakeword_module=openwakeword,
                model_dir=model_dir,
                inference_framework=settings.inference_framework,
                requested_model=settings.model,
            )

        extension = f".{settings.inference_framework}"
        melspec = _asset_path(
            metadata=openwakeword.FEATURE_MODELS["melspectrogram"],
            model_dir=model_dir,
            extension=extension,
        )
        embedding = _asset_path(
            metadata=openwakeword.FEATURE_MODELS["embedding"],
            model_dir=model_dir,
            extension=extension,
        )
        if melspec is None or embedding is None:
            # A manually supplied custom wake model still needs shared feature
            # models. An unknown name downloads only those shared assets.
            download_models(model_names=[settings.model], target_directory=str(model_dir))
            melspec = _asset_path(
                metadata=openwakeword.FEATURE_MODELS["melspectrogram"],
                model_dir=model_dir,
                extension=extension,
            )
            embedding = _asset_path(
                metadata=openwakeword.FEATURE_MODELS["embedding"],
                model_dir=model_dir,
                extension=extension,
            )
        if melspec is None or embedding is None:
            raise FileNotFoundError("openWakeWord feature models are missing")

        if settings.vad_threshold > 0:
            _ensure_package_vad(openwakeword, model_dir)

        self._model: Any = Model(
            wakeword_models=[str(path) for path in wake_paths],
            inference_framework=settings.inference_framework,
            melspec_model_path=str(melspec),
            embedding_model_path=str(embedding),
            vad_threshold=settings.vad_threshold,
            enable_speex_noise_suppression=settings.noise_suppression,
        )

        source = ", ".join(str(path) for path in wake_paths)
        # Standard logging is intentionally used instead of exposing model
        # paths through exceptions or user-facing output.
        import logging

        logging.getLogger("voice-agent.wakeword").info(
            "wake model ready: %s (%s)", settings.model, source
        )

    def process(self, frame: PcmBlock) -> WakeDetection:
        processed = self._apply_gain(frame, self.gain)
        prediction: dict[str, float] = self._model.predict(processed)
        best_name, best_score = max(prediction.items(), key=lambda item: item[1], default=("", 0.0))
        score = float(best_score)
        self._consecutive_hits = self._consecutive_hits + 1 if score >= self.threshold else 0

        now = time.monotonic()
        detected = (
            self._consecutive_hits >= self.patience
            and now - self._last_detection >= self.cooldown_sec
        )
        if detected:
            self._last_detection = now
            self._consecutive_hits = 0
        return WakeDetection(
            detected=detected,
            model_name=(best_name or None) if detected else None,
            score=score,
        )

    def reset(self) -> None:
        self._consecutive_hits = 0
        reset = getattr(self._model, "reset", None)
        if callable(reset):
            reset()

    @staticmethod
    def _apply_gain(frame: PcmBlock, gain: float) -> PcmBlock:
        if gain == 1.0:
            return frame
        return np.clip(frame.astype(np.float32) * gain, -32768, 32767).astype(np.int16)


def package_model_directory() -> Path:
    """Return upstream's resource directory; useful for diagnostics/tests."""
    import openwakeword

    return Path(os.path.dirname(os.path.abspath(openwakeword.__file__))) / "resources" / "models"


__all__ = [
    "OpenWakeWordDetector",
    "OpenWakeWordModule",
    "WakeDetection",
    "WakeDetector",
    "model_name_pattern",
    "package_model_directory",
    "resolve_wake_model_paths",
]
