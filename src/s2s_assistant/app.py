"""Application wiring — build all components from settings and connect them.

This is the single composition root: load settings, create the HTTP client
for tools, the audio player, the tool deps, and the realtime client.
"""

from __future__ import annotations

import httpx

from .audio.playback import AudioPlayer
from .config import Settings
from .realtime.client import RealtimeClient, ToolDeps


def build_client(settings: Settings) -> RealtimeClient:
    """Wire the realtime client with its audio player and tool deps."""
    http_client = httpx.Client()
    deps = ToolDeps(
        language=settings.language,
        geocoding_url=settings.geocoding_url,
        forecast_url=settings.forecast_url,
        http_client=http_client,
    )
    # Playback uses the TTS pipeline rate (16 kHz), NOT the mic rate (24 kHz).
    player = AudioPlayer(
        sample_rate=settings.output_sample_rate, channels=settings.channels
    )
    return RealtimeClient(settings=settings, deps=deps, player=player)


__all__ = ["build_client"]
