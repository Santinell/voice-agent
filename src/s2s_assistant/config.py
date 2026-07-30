"""Application configuration loaded from environment.

All tunables live here. Defaults point at a locally running
huggingface/speech-to-speech Realtime server.

Priority: environment / .env > hardcoded default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a hard dep, but be defensive
    pass


@dataclass(frozen=True)
class Settings:
    """Resolved application settings."""

    # speech-to-speech Realtime server the client connects to.
    s2s_url: str
    # OpenAI-compatible base URL + key used BY the s2s SERVER for its LLM.
    # The client forwards these via session config; the client itself only
    # needs the realtime websocket (s2s_url).
    llm_base_url: str
    llm_api_key: str
    llm_model: str

    # Client-side behaviour.
    language: str  # "ru" | "en"
    # Open-Meteo endpoints (geocoding + forecast). No API key required.
    geocoding_url: str
    forecast_url: str

    # Audio: 24 kHz PCM int16 is the OpenAI Realtime wire format for the mic.
    sample_rate: int
    # Input device (microphone) name substring or index; None = system default.
    # Set to the AEC virtual source name (e.g. "echo-cancel") to feed the mic
    # signal with acoustic echo already removed.
    input_device: str | None
    # TTS playback sample rate. The s2s server streams response audio at its
    # PIPELINE_SAMPLE_RATE (16 kHz), NOT the mic rate — so playback must open at
    # 16 kHz or the voice plays back 1.5× too fast and high-pitched.
    output_sample_rate: int
    channels: int
    block_size: int  # frames per mic/speaker block

    @classmethod
    def from_env(cls) -> Settings:
        def _get(key: str, default: str) -> str:
            return os.environ.get(key, default)

        return cls(
            s2s_url=_get("S2S_URL", "ws://localhost:8765/v1/realtime"),
            llm_base_url=_get("OPENAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4"),
            llm_api_key=_get("OPENAI_API_KEY", ""),
            llm_model=_get("LLM_MODEL", "glm-5"),
            language=_get("LANGUAGE", "ru").lower(),
            geocoding_url=_get(
                "GEOCODING_URL", "https://geocoding-api.open-meteo.com/v1/search"
            ),
            forecast_url=_get("FORECAST_URL", "https://api.open-meteo.com/v1/forecast"),
            sample_rate=int(_get("SAMPLE_RATE", "24000")),
            input_device=_get("INPUT_DEVICE", "") or None,
            output_sample_rate=int(_get("OUTPUT_SAMPLE_RATE", "16000")),
            channels=1,
            block_size=int(_get("BLOCK_SIZE", "4800")),  # 200 ms @ 24 kHz
        )


__all__ = ["Settings"]
