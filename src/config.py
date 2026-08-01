"""Application configuration loaded from environment.

All tunables live here. Defaults point at a locally running
huggingface/speech-to-speech Realtime server.

Priority: environment / .env > hardcoded default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a hard dep, but be defensive
    pass


def _env_bool(value: str, *, default: bool) -> bool:
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _resolve_log_level(value: str) -> str:
    """Normalize a LOG_LEVEL env value to a valid ``logging`` level name.

    Case-insensitive; an empty or unknown name falls back to ``INFO`` rather than
    silently disabling logging (a typo shouldn't hide every log line).
    """
    name = value.strip().upper()
    return name if name in _VALID_LOG_LEVELS else "INFO"


@dataclass(frozen=True)
class WakeWordSettings:
    """Local wake-word gate configuration.

    A non-empty ``model`` enables the gate. ``None`` preserves the original
    always-on microphone behaviour.
    """

    model: str | None = None
    model_dir: Path = Path(".local/openwakeword")
    inference_framework: str = "onnx"
    threshold: float = 0.35
    gain: float = 1.0
    patience: int = 1
    cooldown_sec: float = 1.5
    vad_threshold: float = 0.5
    noise_suppression: bool = True
    earcon_frequency_hz: float = 880.0
    earcon_duration_ms: int = 120
    earcon_volume: float = 0.25
    post_earcon_silence_ms: int = 120
    deactivation_earcon_start_hz: float = 660.0
    deactivation_earcon_end_hz: float = 440.0
    deactivation_earcon_duration_ms: int = 180
    deactivation_earcon_volume: float = 0.2
    post_deactivation_earcon_silence_ms: int = 100
    activation_listen_timeout_sec: float = 8.0
    follow_up_window_sec: float = 10.0
    max_active_sec: float = 90.0

    def __post_init__(self) -> None:
        if self.inference_framework not in {"onnx", "tflite"}:
            raise ValueError("WAKE_WORD_INFERENCE_FRAMEWORK must be onnx or tflite")
        if not 0 < self.threshold <= 1:
            raise ValueError("WAKE_WORD_THRESHOLD must be in (0, 1]")
        if self.gain <= 0:
            raise ValueError("WAKE_WORD_GAIN must be positive")
        if self.patience < 1:
            raise ValueError("WAKE_WORD_PATIENCE must be at least 1")
        if not 0 <= self.vad_threshold <= 1:
            raise ValueError("WAKE_WORD_VAD_THRESHOLD must be in [0, 1]")
        if not 0 <= self.earcon_volume <= 1:
            raise ValueError("EARCON_VOLUME must be in [0, 1]")
        if not 0 <= self.deactivation_earcon_volume <= 1:
            raise ValueError("DEACTIVATION_EARCON_VOLUME must be in [0, 1]")
        if (
            self.earcon_frequency_hz <= 0
            or self.deactivation_earcon_start_hz <= 0
            or self.deactivation_earcon_end_hz <= 0
        ):
            raise ValueError("earcon frequencies must be positive")
        if (
            self.earcon_duration_ms < 0
            or self.post_earcon_silence_ms < 0
            or self.deactivation_earcon_duration_ms < 0
            or self.post_deactivation_earcon_silence_ms < 0
        ):
            raise ValueError("earcon durations cannot be negative")
        if self.cooldown_sec < 0:
            raise ValueError("WAKE_WORD_COOLDOWN_SEC cannot be negative")
        if (
            self.activation_listen_timeout_sec <= 0
            or self.follow_up_window_sec < 0
            or self.max_active_sec <= 0
        ):
            raise ValueError("wake-word timeouts are invalid")

    @classmethod
    def from_env(cls) -> WakeWordSettings:
        def _get(key: str, default: str) -> str:
            return os.environ.get(key, default)

        raw_model = _get("WAKE_WORD_MODEL", "").strip()
        return cls(
            model=raw_model or None,
            model_dir=Path(_get("WAKE_WORD_MODEL_DIR", ".local/openwakeword"))
            .expanduser()
            .resolve(),
            inference_framework=_get("WAKE_WORD_INFERENCE_FRAMEWORK", "onnx").lower(),
            threshold=float(_get("WAKE_WORD_THRESHOLD", "0.35")),
            gain=float(_get("WAKE_WORD_GAIN", "1.0")),
            patience=int(_get("WAKE_WORD_PATIENCE", "1")),
            cooldown_sec=float(_get("WAKE_WORD_COOLDOWN_SEC", "1.5")),
            vad_threshold=float(_get("WAKE_WORD_VAD_THRESHOLD", "0.5")),
            noise_suppression=_env_bool(
                _get("WAKE_WORD_NOISE_SUPPRESSION", "true"), default=True
            ),
            earcon_frequency_hz=float(_get("EARCON_FREQUENCY_HZ", "880")),
            earcon_duration_ms=int(_get("EARCON_DURATION_MS", "120")),
            earcon_volume=float(_get("EARCON_VOLUME", "0.25")),
            post_earcon_silence_ms=int(_get("POST_EARCON_SILENCE_MS", "120")),
            deactivation_earcon_start_hz=float(
                _get("DEACTIVATION_EARCON_START_HZ", "660")
            ),
            deactivation_earcon_end_hz=float(
                _get("DEACTIVATION_EARCON_END_HZ", "440")
            ),
            deactivation_earcon_duration_ms=int(
                _get("DEACTIVATION_EARCON_DURATION_MS", "180")
            ),
            deactivation_earcon_volume=float(
                _get("DEACTIVATION_EARCON_VOLUME", "0.2")
            ),
            post_deactivation_earcon_silence_ms=int(
                _get("POST_DEACTIVATION_EARCON_SILENCE_MS", "100")
            ),
            activation_listen_timeout_sec=float(
                _get("ACTIVATION_LISTEN_TIMEOUT_SEC", "8")
            ),
            follow_up_window_sec=float(_get("FOLLOW_UP_WINDOW_SEC", "10")),
            max_active_sec=float(_get("MAX_ACTIVE_SEC", "90")),
        )


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
    wake_word: WakeWordSettings = field(default_factory=WakeWordSettings)
    # Persistent store (SQLite): scheduled events, stored secrets. Migrated on
    # startup via yoyo; the connection is owned by the app and shared by stores.
    db_path: Path = Path(".local/agent.db")
    # IANA timezone (e.g. "Europe/Moscow") for scheduling clock times; empty =
    # system local. Times render and are interpreted in 24-hour format.
    timezone: str = ""
    # Console logging verbosity: a standard logging level name (DEBUG/INFO/
    # WARNING/ERROR/CRITICAL), case-insensitive. Invalid names fall back to INFO.
    log_level: str = "INFO"
    # Web tools (optional). All degrade gracefully to a free fallback:
    # FIRECRAWL_API_KEY → Firecrawl search & scrape (primary). JINA_API_KEY →
    # Jina search & reader; without it JinaClient auto-mints a trial key from
    # keygen.jina.ai. EXA_API_KEY → Exa search & page text (third fallback),
    # else DuckDuckGo HTML / direct fetch.
    firecrawl_api_key: str = ""
    exa_api_key: str = ""
    jina_api_key: str = ""

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
            block_size=int(_get("BLOCK_SIZE", "1920")),  # 80 ms @ 24 kHz
            wake_word=WakeWordSettings.from_env(),
            db_path=Path(_get("AGENT_DB", ".local/agent.db")).expanduser(),
            timezone=_get("TIMEZONE", "").strip(),
            log_level=_resolve_log_level(_get("LOG_LEVEL", "INFO")),
            exa_api_key=_get("EXA_API_KEY", "").strip(),
            jina_api_key=_get("JINA_API_KEY", "").strip(),
            firecrawl_api_key=_get("FIRECRAWL_API_KEY", "").strip(),
        )


__all__ = ["Settings", "WakeWordSettings"]
