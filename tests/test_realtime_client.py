"""Tests for the realtime client's tool-dispatch logic (no network / no audio).

We exercise the event-routing + tool-execution path with a fake connection
that records the events it is asked to send. This verifies the core loop:
function_call arguments accumulate → on response.done the tool runs → its
output is sent as function_call_output → response.create is requested.
"""

from __future__ import annotations

import base64
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pytest
from websockets.exceptions import ConnectionClosed

from activation import ActivationGate, ActivationState
from audio.playback import AudioPlayer
from audio.resample import WakeFrameBuffer
from audio.wakeword import WakeDetection
from config import Settings, WakeWordSettings
from realtime.client import (
    RealtimeClient,
    ToolDeps,
    WakeWordRuntime,
)
from tools.scheduling import (
    Recurrence,
    ScheduledEvent,
    Scheduler,
    SchedulerStore,
)

# ── fakes ───────────────────────────────────────────────────────────────────


@dataclass
class FakeConnection:
    """Records every event sent to it (no actual websocket)."""

    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(self, event: Any) -> None:
        # The real SDK casts a dict to a typed param; we keep the raw dict.
        self.sent.append(event)  # type: ignore[arg-type]


@dataclass
class FakeClock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakePlayer:
    item_types: list[str] = field(default_factory=list)
    pcm: list[np.ndarray[Any, Any]] = field(default_factory=list)
    deltas: list[str] = field(default_factory=list)
    barriers: list[Callable[[], None]] = field(default_factory=list)
    started: bool = False
    stopped: bool = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def put_delta(self, b64: str) -> None:
        self.item_types.append("delta")
        self.deltas.append(b64)

    def put_pcm(self, samples: np.ndarray[Any, Any]) -> None:
        self.item_types.append("pcm")
        self.pcm.append(samples.copy())

    def put_barrier(self, callback: Callable[[], None]) -> None:
        self.item_types.append("barrier")
        self.barriers.append(callback)

    def run_next_barrier(self) -> None:
        self.barriers.pop(0)()


@dataclass
class FakeDetector:
    detections: list[bool]
    processed: list[np.ndarray[Any, Any]] = field(default_factory=list)
    reset_count: int = 0

    def process(self, frame: np.ndarray[Any, Any]) -> WakeDetection:
        self.processed.append(frame.copy())
        detected = self.detections.pop(0) if self.detections else False
        return WakeDetection(detected, "test_wake" if detected else None, 0.9)

    def reset(self) -> None:
        self.reset_count += 1


@dataclass
class IdentityResampler:
    reset_count: int = 0

    def process(self, samples: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        return samples.copy()

    def reset(self) -> None:
        self.reset_count += 1


def _settings(**overrides: Any) -> Settings:
    """Deterministic settings for tests (does not read the real .env)."""
    defaults: dict[str, Any] = dict(
        s2s_url="ws://localhost:8765/v1/realtime",
        llm_base_url="http://localhost:8765/v1",
        llm_api_key="test-key",
        llm_model="glm-5",
        language="ru",
        geocoding_url="https://geocoding.example/v1/search",
        forecast_url="https://forecast.example/v1/forecast",
        sample_rate=24000,
        input_device=None,
        output_sample_rate=16000,
        channels=1,
        block_size=4800,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_scheduler() -> Scheduler:
    return Scheduler(SchedulerStore(_mem_conn()), tz=UTC)


def _mem_conn() -> sqlite3.Connection:
    """An in-memory connection carrying the scheduled_events schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE scheduled_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT    NOT NULL,
            label      TEXT,
            fire_at    TEXT    NOT NULL,
            weekdays   TEXT,
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    NOT NULL
        )
        """
    )
    return conn


def _make_client(**settings_overrides: Any) -> tuple[RealtimeClient, ToolDeps]:
    settings = _settings(**settings_overrides)
    deps = ToolDeps(
        language=settings.language,
        geocoding_url=settings.geocoding_url,
        forecast_url=settings.forecast_url,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))),
        scheduler=_make_scheduler(),
    )
    player = AudioPlayer(sample_rate=24000)
    client = RealtimeClient(settings=settings, deps=deps, player=player)
    return client, deps


def _make_wake_client(
    *,
    detections: list[bool] | None = None,
    follow_up_window_sec: float = 10.0,
) -> tuple[
    RealtimeClient,
    FakeConnection,
    FakePlayer,
    FakeDetector,
    IdentityResampler,
    FakeClock,
]:
    wake = WakeWordSettings(
        model="test_wake",
        follow_up_window_sec=follow_up_window_sec,
    )
    settings = _settings(block_size=4, wake_word=wake)
    deps = ToolDeps(
        language=settings.language,
        geocoding_url=settings.geocoding_url,
        forecast_url=settings.forecast_url,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))),
        scheduler=_make_scheduler(),
    )
    clock = FakeClock()
    gate = ActivationGate(
        requires_wake=True,
        listen_timeout_sec=wake.activation_listen_timeout_sec,
        follow_up_window_sec=wake.follow_up_window_sec,
        max_active_sec=wake.max_active_sec,
        clock=clock,
    )
    detector = FakeDetector(detections if detections is not None else [True])
    resampler = IdentityResampler()
    player = FakePlayer()
    runtime = WakeWordRuntime(
        detector=detector,
        resampler=resampler,
        frames=WakeFrameBuffer(frame_size=4),
        earcon=np.array([100, 0], dtype=np.int16),
        deactivation_earcon=np.array([-100, 0], dtype=np.int16),
    )
    client = RealtimeClient(
        settings=settings,
        deps=deps,
        player=player,
        activation_gate=gate,
        wake_runtime=runtime,
    )
    return client, FakeConnection(), player, detector, resampler, clock


def _event(event_type: str, **fields: Any) -> SimpleNamespace:
    """Build a minimal server-event-like object with a ``type`` attr."""
    return SimpleNamespace(type=event_type, **fields)


# ── routing: unhandled events are ignored ───────────────────────────────────


def test_unhandled_event_is_ignored() -> None:
    client, _ = _make_client()
    conn = FakeConnection()
    client._handle(conn, _event("rate_limits.updated", rate_limits=[]))
    assert conn.sent == []


# ── function-call accumulation + dispatch ───────────────────────────────────


def test_function_call_accumulates_then_dispatches_on_response_done() -> None:
    client, _ = _make_client()
    conn = FakeConnection()

    # 1. model emits a function_call item
    client._handle(
        conn,
        _event(
            "response.output_item.added",
            item=SimpleNamespace(type="function_call", call_id="c1", name="calculate"),
        ),
    )
    # 2. arguments stream in as deltas
    client._handle(
        conn,
        _event("response.function_call_arguments.delta", call_id="c1", delta='{"exp'),
    )
    client._handle(
        conn,
        _event(
            "response.function_call_arguments.delta",
            call_id="c1",
            delta='ression":"6*7"}',
        ),
    )
    # 3. arguments.done finalizes them
    client._handle(
        conn,
        _event(
            "response.function_call_arguments.done",
            call_id="c1",
            arguments='{"expression":"6*7"}',
        ),
    )
    # Nothing sent yet — dispatch waits for response.done.
    assert conn.sent == []

    # 4. response.done triggers dispatch
    client._handle(conn, _event("response.done", response={}))
    # Expect: conversation.item.create (function_call_output) + response.create
    assert [e["type"] for e in conn.sent] == [
        "conversation.item.create",
        "response.create",
    ]
    item = conn.sent[0]["item"]
    assert item["type"] == "function_call_output"
    assert item["call_id"] == "c1"
    assert "42" in item["output"]  # calculator result


def test_function_call_done_carries_name_without_added() -> None:
    # s2s emits ONLY response.function_call_arguments.done for a function call
    # (no response.output_item.added), and that .done event carries the tool
    # `name`. The client must read the name from .done and dispatch correctly —
    # otherwise the tool is reported "unknown" and the model never gets weather.
    client, _ = _make_client()
    conn = FakeConnection()
    client._handle(
        conn,
        _event(
            "response.function_call_arguments.done",
            call_id="c2",
            name="get_weather",
            arguments='{"city":"Moscow"}',
        ),
    )
    client._handle(conn, _event("response.done", response={}))
    assert [e["type"] for e in conn.sent] == [
        "conversation.item.create",
        "response.create",
    ]
    item = conn.sent[0]["item"]
    assert item["call_id"] == "c2"
    # Weather ran (mock transport 404 → service-unavailable message), NOT
    # "unknown tool" — proving the name was read from the .done event.
    assert "Неизвестный инструмент" not in item["output"]
    assert "Сервис погоды недоступен" in item["output"]


def test_function_call_done_without_name_is_treated_as_unknown() -> None:
    # Defence: if neither .added nor .done carries a name, we cannot know the
    # tool — dispatch yields the "unknown tool" message rather than crashing.
    client, _ = _make_client()
    conn = FakeConnection()
    client._handle(
        conn,
        _event(
            "response.function_call_arguments.done",
            call_id="c2b",
            arguments='{"expression":"2+2"}',
        ),
    )
    client._handle(conn, _event("response.done", response={}))
    item = conn.sent[0]["item"]
    assert "Неизвестный инструмент" in item["output"]


def test_audio_delta_goes_to_player() -> None:
    # We can't easily assert on the speaker, but we can ensure no crash and
    # that no event is sent in response (audio is one-way out).
    client, _ = _make_client()
    conn = FakeConnection()
    client._handle(conn, _event("response.output_audio.delta", delta="dGhpcyBpcyBmYWtlIGF1ZGlv"))
    assert conn.sent == []


def test_multiple_tool_outputs_request_one_continuation() -> None:
    client, _ = _make_client()
    conn = FakeConnection()
    for call_id, expression in (("c5", "2+2"), ("c6", "3+3")):
        client._handle(
            conn,
            _event(
                "response.function_call_arguments.done",
                call_id=call_id,
                name="calculate",
                arguments=f'{{"expression":"{expression}"}}',
            ),
        )

    client._handle(conn, _event("response.done", response={}))

    assert [event["type"] for event in conn.sent].count("conversation.item.create") == 2
    assert [event["type"] for event in conn.sent].count("response.create") == 1


def test_pending_is_cleared_after_response_done() -> None:
    client, _ = _make_client()
    conn = FakeConnection()
    client._handle(
        conn,
        _event(
            "response.output_item.added",
            item=SimpleNamespace(type="function_call", call_id="c3", name="calculate"),
        ),
    )
    client._handle(
        conn,
        _event(
            "response.function_call_arguments.done",
            call_id="c3",
            arguments='{"expression":"1+1"}',
        ),
    )
    client._handle(conn, _event("response.done", response={}))
    assert client._pending == {}


def test_unknown_tool_returns_localized_output() -> None:
    client, _ = _make_client()
    conn = FakeConnection()
    client._handle(
        conn,
        _event(
            "response.output_item.added",
            item=SimpleNamespace(type="function_call", call_id="c4", name="nope"),
        ),
    )
    client._handle(
        conn,
        _event("response.function_call_arguments.done", call_id="c4", arguments="{}"),
    )
    client._handle(conn, _event("response.done", response={}))
    item = conn.sent[0]["item"]
    assert "Неизвестный инструмент" in item["output"]


# ── session configuration ───────────────────────────────────────────────────


def test_configure_session_sends_tools_and_instructions() -> None:
    client, _ = _make_client()
    conn = FakeConnection()
    client._configure_session(conn)  # type: ignore[arg-type]
    assert len(conn.sent) == 1
    event = conn.sent[0]
    assert event["type"] == "session.update"
    session = event["session"]
    assert "instructions" in session
    tool_names = {t["name"] for t in session["tools"]}
    assert {"calculate", "get_weather", "set_timer", "set_alarm", "set_reminder"} <= tool_names
    assert session["input_audio_format"] == "pcm16"
    assert session["output_audio_format"] == "pcm16"
    # The instructions must oblige the model to call the scheduling tools
    # instead of merely claiming "I'll remind you" (a regression where the
    # reminder was never stored).
    instructions = session["instructions"]
    assert "set_reminder" in instructions
    assert "через инструменты" in instructions


def test_configure_session_localizes_instructions() -> None:
    # English deps → English prompt mentioning "voice assistant".
    settings = _settings(language="en")
    deps_en = ToolDeps(
        language="en",
        geocoding_url="x",
        forecast_url="x",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))),
        scheduler=_make_scheduler(),
    )
    player = AudioPlayer(sample_rate=24000)
    client = RealtimeClient(settings=settings, deps=deps_en, player=player)
    conn = FakeConnection()
    client._configure_session(conn)  # type: ignore[arg-type]
    instructions = conn.sent[0]["session"]["instructions"]
    assert "voice assistant" in instructions


# ── http base url derivation ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ws_url, expected",
    [
        ("ws://localhost:8765/v1/realtime", "http://localhost:8765/v1/realtime"),
        ("wss://example.com/v1/realtime", "https://example.com/v1/realtime"),
        ("http://no-scheme-change", "http://no-scheme-change"),
    ],
)
def test_http_base_url_derivation(ws_url: str, expected: str) -> None:
    client, _ = _make_client(s2s_url=ws_url)
    assert client._http_base_url() == expected


# ── wake-word audio gate and dialogue window ────────────────────────────────


def test_always_on_mode_still_forwards_every_mic_block() -> None:
    client, _ = _make_client()
    conn = FakeConnection()
    block = np.array([1, 2, 3, 4], dtype=np.int16)

    client._process_mic_block(conn, block)  # type: ignore[arg-type]

    assert len(conn.sent) == 1
    assert conn.sent[0]["type"] == "input_audio_buffer.append"
    decoded = np.frombuffer(base64.b64decode(conn.sent[0]["audio"]), dtype=np.int16)
    np.testing.assert_array_equal(decoded, block)


def test_sleeping_trigger_and_earcon_blocks_never_reach_server() -> None:
    client, conn, player, detector, _, _ = _make_wake_client()
    trigger = np.array([1, 2, 3, 4], dtype=np.int16)
    during_earcon = np.array([5, 6, 7, 8], dtype=np.int16)

    client._process_mic_block(conn, trigger)  # type: ignore[arg-type]

    assert conn.sent == []
    assert client.activation_gate.state is ActivationState.EARCON
    assert len(detector.processed) == 1
    assert player.item_types == ["pcm", "barrier"]
    np.testing.assert_array_equal(player.pcm[0], [100, 0])

    client._process_mic_block(conn, during_earcon)  # type: ignore[arg-type]
    assert conn.sent == []
    assert len(detector.processed) == 1


def test_first_command_block_is_sent_only_after_earcon_barrier() -> None:
    client, conn, player, _, _, _ = _make_wake_client()
    client._process_mic_block(  # type: ignore[arg-type]
        conn, np.array([1, 2, 3, 4], dtype=np.int16)
    )
    command = np.array([10, 20, 30, 40], dtype=np.int16)
    client._process_mic_block(conn, command)  # type: ignore[arg-type]
    assert conn.sent == []

    player.run_next_barrier()
    assert client.activation_gate.state is ActivationState.ACTIVE
    client._process_mic_block(conn, command)  # type: ignore[arg-type]

    assert len(conn.sent) == 1
    decoded = np.frombuffer(base64.b64decode(conn.sent[0]["audio"]), dtype=np.int16)
    np.testing.assert_array_equal(decoded, command)


def test_final_playback_opens_follow_up_window_without_new_wake() -> None:
    client, conn, player, _, _, _ = _make_wake_client()
    client._process_mic_block(  # type: ignore[arg-type]
        conn, np.array([1, 2, 3, 4], dtype=np.int16)
    )
    player.run_next_barrier()
    client._handle(conn, _event("response.created"))
    client._handle(conn, _event("response.done", response={"status": "completed"}))
    assert client.activation_gate.state is ActivationState.ACTIVE
    assert len(player.barriers) == 1

    player.run_next_barrier()

    assert client.activation_gate.state is ActivationState.FOLLOW_UP
    clarification = np.array([7, 8, 9, 10], dtype=np.int16)
    client._process_mic_block(conn, clarification)  # type: ignore[arg-type]
    assert conn.sent[-1]["type"] == "input_audio_buffer.append"


def test_follow_up_timeout_closes_gate_and_resets_wake_pipeline() -> None:
    client, conn, player, detector, resampler, clock = _make_wake_client()
    block = np.array([1, 2, 3, 4], dtype=np.int16)
    client._process_mic_block(conn, block)  # type: ignore[arg-type]
    player.run_next_barrier()
    client._process_mic_block(conn, block)  # type: ignore[arg-type]
    client._handle(conn, _event("response.done", response={"status": "completed"}))
    player.run_next_barrier()
    sent_before_timeout = len(conn.sent)

    clock.advance(10)
    client._process_mic_block(conn, block)  # type: ignore[arg-type]

    assert client.activation_gate.state is ActivationState.DEACTIVATING
    assert len(conn.sent) == sent_before_timeout
    assert detector.reset_count == 0
    assert resampler.reset_count == 0
    np.testing.assert_array_equal(player.pcm[-1], [-100, 0])

    player.run_next_barrier()
    assert client.activation_gate.state is ActivationState.SLEEPING
    client._process_mic_block(conn, block)  # type: ignore[arg-type]
    assert detector.reset_count == 1
    assert resampler.reset_count == 1
    assert len(detector.processed) == 2


def test_zero_follow_up_plays_deactivation_cue_after_final_audio() -> None:
    client, conn, player, _, _, _ = _make_wake_client(follow_up_window_sec=0)
    client._process_mic_block(  # type: ignore[arg-type]
        conn, np.array([1, 2, 3, 4], dtype=np.int16)
    )
    player.run_next_barrier()
    client._handle(conn, _event("response.done", response={"status": "completed"}))

    player.run_next_barrier()

    assert client.activation_gate.state is ActivationState.DEACTIVATING
    np.testing.assert_array_equal(player.pcm[-1], [-100, 0])
    player.run_next_barrier()
    assert client.activation_gate.state is ActivationState.SLEEPING


def test_follow_up_speech_starts_a_new_turn_without_wake() -> None:
    client, conn, player, _, _, _ = _make_wake_client()
    client._process_mic_block(  # type: ignore[arg-type]
        conn, np.array([1, 2, 3, 4], dtype=np.int16)
    )
    player.run_next_barrier()
    client._handle(conn, _event("response.done", response={"status": "completed"}))
    player.run_next_barrier()
    assert client.activation_gate.state is ActivationState.FOLLOW_UP

    client._handle(conn, _event("input_audio_buffer.speech_started"))

    assert client.activation_gate.state is ActivationState.ACTIVE


def test_barge_in_invalidates_old_response_completion_barrier() -> None:
    client, conn, player, _, _, _ = _make_wake_client()
    client._process_mic_block(  # type: ignore[arg-type]
        conn, np.array([1, 2, 3, 4], dtype=np.int16)
    )
    player.run_next_barrier()
    client._handle(conn, _event("response.created"))
    client._handle(conn, _event("response.done", response={"status": "completed"}))
    assert len(player.barriers) == 1

    client._handle(conn, _event("input_audio_buffer.speech_started"))
    player.run_next_barrier()

    assert client.activation_gate.state is ActivationState.ACTIVE
    assert client.activation_gate.should_forward_audio()


@pytest.mark.parametrize("status", ["cancelled", "failed", "incomplete"])
def test_non_final_response_status_does_not_schedule_rearm(status: str) -> None:
    client, conn, player, _, _, _ = _make_wake_client()
    client._process_mic_block(  # type: ignore[arg-type]
        conn, np.array([1, 2, 3, 4], dtype=np.int16)
    )
    player.run_next_barrier()

    client._handle(conn, _event("response.done", response={"status": status}))

    assert player.barriers == []
    assert client.activation_gate.state is ActivationState.ACTIVE


# ── scheduled-event firing (timer / alarm / reminder) ───────────────────────


def _make_firing_client() -> tuple[RealtimeClient, FakeConnection, FakePlayer]:
    settings = _settings()
    deps = ToolDeps(
        language=settings.language,
        geocoding_url=settings.geocoding_url,
        forecast_url=settings.forecast_url,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))),
        scheduler=_make_scheduler(),
    )
    player = FakePlayer()
    client = RealtimeClient(settings=settings, deps=deps, player=player)
    return client, FakeConnection(), player


def _sched_event(
    kind: str = "reminder",
    *,
    label: str | None = "выпить таблетку",
    fire_at: str = "2026-07-30T08:00:00+00:00",
    recurrence: Recurrence | None = None,
) -> ScheduledEvent:
    return ScheduledEvent(
        id=1,
        kind=kind,
        label=label,
        fire_at_utc=datetime.fromisoformat(fire_at),
        recurrence=recurrence or Recurrence.once(),
        enabled=True,
        created_at=datetime(2026, 7, 30, 7, 0, tzinfo=UTC),
    )


def test_fire_scheduled_plays_chime_then_injects_message() -> None:
    client, conn, player = _make_firing_client()
    client._conn = conn  # type: ignore[assignment]
    event = _sched_event("reminder")

    client._fire_scheduled(event)

    # Chime queued first, then a barrier that injects the message.
    assert player.item_types == ["pcm", "barrier"]
    assert len(player.pcm) == 1
    assert player.pcm[0].dtype == np.int16

    # Nothing sent until the chime has finished playing.
    assert conn.sent == []
    player.run_next_barrier()

    assert [e["type"] for e in conn.sent] == [
        "conversation.item.create",
        "response.create",
    ]
    item = conn.sent[0]["item"]
    assert item["type"] == "message"
    assert item["role"] == "user"
    text = item["content"][0]["text"]
    assert "выпить таблетку" in text


def test_fire_scheduled_alarm_includes_local_time() -> None:
    client, conn, player = _make_firing_client()
    client._conn = conn  # type: ignore[assignment]
    # 08:00 UTC with a UTC scheduler → rendered as "08:00" (24-hour).
    event = _sched_event("alarm", label=None, recurrence=Recurrence.daily())

    client._fire_scheduled(event)
    player.run_next_barrier()

    text = conn.sent[0]["item"]["content"][0]["text"]
    assert "08:00" in text
    assert "будильник" in text.lower() or "alarm" in text.lower()


def test_fire_scheduled_skips_send_when_not_running() -> None:
    # Without a live connection (e.g. before run / during teardown) firing must
    # not crash and must not send anything, while the chime still enqueues.
    client, _conn, player = _make_firing_client()
    # client._conn stays None.
    client._fire_scheduled(_sched_event("timer", label=None))

    assert player.item_types == ["pcm", "barrier"]
    player.run_next_barrier()  # _send_scheduled is a safe no-op (conn is None)


# ── recv loop shutdown behaviour ─────────────────────────────────────────────


class _FakeConnectionClosed(ConnectionClosed):
    """ConnectionClosed without the websockets close-frame bookkeeping."""

    def __init__(self) -> None:
        Exception.__init__(self, "received 1012 (service restart)")

    def __str__(self) -> str:
        return "received 1012 (service restart)"


def test_recv_loop_exits_quietly_on_connection_closed() -> None:
    # The start script kills the s2s server on Ctrl+C, which closes the socket;
    # the recv loop must treat that as a normal exit, not a raised error.
    client, _ = _make_client()

    def recv() -> Any:
        raise _FakeConnectionClosed()

    conn = SimpleNamespace(recv=recv, send=lambda event: None)
    client._recv_loop(conn)  # type: ignore[arg-type]  # must not raise


def test_recv_loop_exits_quietly_on_error_during_shutdown() -> None:
    client, _ = _make_client()

    def recv() -> Any:
        client._stop.set()  # signal handler ran while recv was blocked
        raise RuntimeError("boom")

    conn = SimpleNamespace(recv=recv, send=lambda event: None)
    client._recv_loop(conn)  # type: ignore[arg-type]  # must not raise
