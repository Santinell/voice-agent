"""Tests for the realtime client's tool-dispatch logic (no network / no audio).

We exercise the event-routing + tool-execution path with a fake connection
that records the events it is asked to send. This verifies the core loop:
function_call arguments accumulate → on response.done the tool runs → its
output is sent as function_call_output → response.create is requested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from s2s_assistant.audio.playback import AudioPlayer
from s2s_assistant.config import Settings
from s2s_assistant.realtime.client import RealtimeClient, ToolDeps

# ── fakes ───────────────────────────────────────────────────────────────────


@dataclass
class FakeConnection:
    """Records every event sent to it (no actual websocket)."""

    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(self, event: Any) -> None:
        # The real SDK casts a dict to a typed param; we keep the raw dict.
        self.sent.append(event)  # type: ignore[arg-type]


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


def _make_client(**settings_overrides: Any) -> tuple[RealtimeClient, ToolDeps]:
    settings = _settings(**settings_overrides)
    deps = ToolDeps(
        language=settings.language,
        geocoding_url=settings.geocoding_url,
        forecast_url=settings.forecast_url,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))),
    )
    player = AudioPlayer(sample_rate=24000)
    client = RealtimeClient(settings=settings, deps=deps, player=player)
    return client, deps


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
            item=SimpleNamespace(
                type="function_call", call_id="c1", name="calculate"
            ),
        ),
    )
    # 2. arguments stream in as deltas
    client._handle(
        conn,
        _event(
            "response.function_call_arguments.delta", call_id="c1", delta='{"exp'
        ),
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
    client._handle(
        conn, _event("response.output_audio.delta", delta="dGhpcyBpcyBmYWtlIGF1ZGlv")
    )
    assert conn.sent == []


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
        _event(
            "response.function_call_arguments.done", call_id="c4", arguments="{}"
        ),
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
    assert {"calculate", "get_weather"} <= tool_names
    assert session["input_audio_format"] == "pcm16"
    assert session["output_audio_format"] == "pcm16"


def test_configure_session_localizes_instructions() -> None:
    # English deps → English prompt mentioning "voice assistant".
    settings = _settings(language="en")
    deps_en = ToolDeps(
        language="en",
        geocoding_url="x",
        forecast_url="x",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))),
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
