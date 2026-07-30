"""Application wiring — build all components from settings and connect them.

This is the single composition root: load settings, create the HTTP client
for tools, the audio player, the tool deps, and the realtime client.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

import httpx

from activation import ActivationGate
from audio.playback import AudioPlayer, make_earcon
from audio.resample import SoxrWakeResampler, WakeFrameBuffer
from audio.wakeword import OpenWakeWordDetector
from config import Settings
from realtime.client import RealtimeClient, ToolDeps, WakeWordRuntime
from tools.scheduling import Scheduler, SchedulerStore


def _resolve_tz(name: str) -> tzinfo:
    """Resolve an IANA name (or fall back to the system local timezone)."""
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # pragma: no cover - invalid tz name
            pass
    local = datetime.now().astimezone().tzinfo
    return local or UTC


def build_client(settings: Settings) -> RealtimeClient:
    """Wire the realtime client with its audio player and tool deps."""
    wake = settings.wake_word
    requires_wake = wake.model is not None
    if requires_wake and settings.sample_rate != 24_000:
        raise ValueError("wake-word mode requires SAMPLE_RATE=24000")
    if requires_wake and settings.output_sample_rate != 16_000:
        raise ValueError("wake-word mode requires OUTPUT_SAMPLE_RATE=16000")

    gate = ActivationGate(
        requires_wake=requires_wake,
        listen_timeout_sec=wake.activation_listen_timeout_sec,
        follow_up_window_sec=wake.follow_up_window_sec,
        max_active_sec=wake.max_active_sec,
    )
    player = AudioPlayer(
        sample_rate=settings.output_sample_rate,
        channels=settings.channels,
        on_error=lambda _exc: gate.fail_playback(),
    )

    runtime: WakeWordRuntime | None = None
    if requires_wake:
        runtime = WakeWordRuntime(
            detector=OpenWakeWordDetector(wake),
            resampler=SoxrWakeResampler(input_rate=settings.sample_rate),
            frames=WakeFrameBuffer(),
            earcon=make_earcon(
                sample_rate=settings.output_sample_rate,
                frequency_hz=wake.earcon_frequency_hz,
                duration_ms=wake.earcon_duration_ms,
                volume=wake.earcon_volume,
                trailing_silence_ms=wake.post_earcon_silence_ms,
            ),
            deactivation_earcon=make_earcon(
                sample_rate=settings.output_sample_rate,
                frequency_hz=wake.deactivation_earcon_start_hz,
                end_frequency_hz=wake.deactivation_earcon_end_hz,
                duration_ms=wake.deactivation_earcon_duration_ms,
                volume=wake.deactivation_earcon_volume,
                trailing_silence_ms=wake.post_deactivation_earcon_silence_ms,
            ),
        )

    scheduler = Scheduler(
        SchedulerStore(settings.schedule_db), tz=_resolve_tz(settings.timezone)
    )
    deps = ToolDeps(
        language=settings.language,
        geocoding_url=settings.geocoding_url,
        forecast_url=settings.forecast_url,
        http_client=httpx.Client(),
        scheduler=scheduler,
    )
    return RealtimeClient(
        settings=settings,
        deps=deps,
        player=player,
        activation_gate=gate,
        wake_runtime=runtime,
    )


__all__ = ["build_client"]
