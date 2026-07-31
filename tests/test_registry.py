"""Tests for the tool registry — schemas and dispatch."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from tools import registry
from tools.scheduling import Recurrence, ScheduledEvent

_T0 = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)


class _FakeScheduler:
    """Records calls and returns deterministic events for dispatch tests."""

    def __init__(self) -> None:
        self.tz = UTC
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def add_timer(self, seconds: float, label: str | None) -> ScheduledEvent:
        self.calls.append(("add_timer", (seconds, label)))
        return ScheduledEvent(
            id=1,
            kind="timer",
            label=label,
            fire_at_utc=_T0,
            recurrence=Recurrence.once(),
            enabled=True,
            created_at=_T0,
        )

    def add_alarm(
        self,
        hour: int,
        minute: int,
        recurrence: Recurrence,
        label: str | None,
    ) -> ScheduledEvent:
        self.calls.append(("add_alarm", (hour, minute, recurrence, label)))
        return ScheduledEvent(
            id=2,
            kind="alarm",
            label=label,
            fire_at_utc=datetime(2026, 7, 30, hour, minute, tzinfo=UTC),
            recurrence=recurrence,
            enabled=True,
            created_at=_T0,
        )

    def add_reminder(
        self,
        hour: int,
        minute: int,
        message: str,
        recurrence: Recurrence,
    ) -> ScheduledEvent:
        self.calls.append(("add_reminder", (hour, minute, message, recurrence)))
        return ScheduledEvent(
            id=3,
            kind="reminder",
            label=message,
            fire_at_utc=datetime(2026, 7, 30, hour, minute, tzinfo=UTC),
            recurrence=recurrence,
            enabled=True,
            created_at=_T0,
        )


class _FakeDeps:
    """Minimal ToolDeps for dispatch tests (no real network)."""

    def __init__(
        self,
        *,
        language: str = "ru",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.language = language
        self.geocoding_url = "https://geocoding.example/v1/search"
        self.forecast_url = "https://forecast.example/v1/forecast"
        # A client whose transport always 404s — weather tests inject their own.
        # web_search/read_url tests can pass a routing transport.
        t = transport or httpx.MockTransport(lambda _: httpx.Response(404))
        self.http_client = httpx.Client(transport=t)
        self.fetch_client = httpx.Client(transport=t)
        self.scheduler = _FakeScheduler()
        self.exa_api_key = ""
        self.reader_api_key = ""


_ALL_TOOLS = {
    registry.TOOL_CALCULATE,
    registry.TOOL_GET_WEATHER,
    registry.TOOL_GET_TIME,
    registry.TOOL_SET_TIMER,
    registry.TOOL_SET_ALARM,
    registry.TOOL_SET_REMINDER,
    registry.TOOL_WEB_SEARCH,
    registry.TOOL_READ_URL,
}


# ── schema construction ─────────────────────────────────────────────────────


def test_tool_schemas_have_stable_names() -> None:
    names = {s.name for s in registry.tool_schemas("ru")}
    assert names == _ALL_TOOLS


def test_tool_schemas_render_to_realtime_dict() -> None:
    schemas = registry.realtime_tools("en")
    assert len(schemas) == len(_ALL_TOOLS)
    for s in schemas:
        assert s["type"] == "function"
        assert "name" in s
        assert "description" in s
        assert "parameters" in s
        assert isinstance(s["parameters"], dict)


def test_tool_schemas_are_localized() -> None:
    ru = {s.name: s.description for s in registry.tool_schemas("ru")}
    en = {s.name: s.description for s in registry.tool_schemas("en")}
    # Russian descriptions contain Cyrillic; English ones don't.
    assert any("\u0430" <= ch <= "\u044f" for ch in ru[registry.TOOL_CALCULATE])
    assert not any("\u0430" <= ch <= "\u044f" for ch in en[registry.TOOL_CALCULATE])
    assert any("\u0430" <= ch <= "\u044f" for ch in ru[registry.TOOL_SET_TIMER])
    assert not any("\u0430" <= ch <= "\u044f" for ch in en[registry.TOOL_SET_TIMER])


def test_tool_schemas_show_stt_word_form_examples() -> None:
    # STT delivers times as number words; the descriptions must teach passing
    # them verbatim in `time` so the tool (not the LLM) parses "двадцать
    # тридцать" → 20:30 instead of the model's 23:30 mis-read.
    ru = {s.name: s.description for s in registry.tool_schemas("ru")}
    assert "двадцать тридцать" in ru[registry.TOOL_SET_ALARM]
    assert "time=" in ru[registry.TOOL_SET_ALARM]
    assert "двадцать тридцать" in ru[registry.TOOL_SET_REMINDER]
    assert "time=" in ru[registry.TOOL_SET_REMINDER]
    assert "девяносто секунд" in ru[registry.TOOL_SET_TIMER]


# ── dispatch: calculator ────────────────────────────────────────────────────


def test_dispatch_calculate() -> None:
    out = registry.dispatch(registry.TOOL_CALCULATE, '{"expression": "6 * 7"}', _FakeDeps())
    assert out == "Результат: 42"


def test_dispatch_calculate_en() -> None:
    deps = _FakeDeps(language="en")
    out = registry.dispatch(registry.TOOL_CALCULATE, '{"expression": "6 * 7"}', deps)
    assert out == "Result: 42"


# ── dispatch: weather ───────────────────────────────────────────────────────


_MOSCOW_GEO = {
    "results": [
        {
            "name": "Moscow",
            "latitude": 55.75,
            "longitude": 37.62,
            "country": "Russia",
            "admin1": "Moscow",
        }
    ]
}
_MOSCOW_FORECAST = {
    "current": {
        "temperature_2m": 20.0,
        "weather_code": 0,
        "wind_speed_10m": 3.0,
        "relative_humidity_2m": 50,
    }
}


def _weather_deps(geo: dict[str, Any] | None, fore: dict[str, Any] | None) -> _FakeDeps:
    deps = _FakeDeps()

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            return httpx.Response(200, json=geo or {})
        if "forecast" in request.url.host:
            return httpx.Response(200, json=fore or {})
        return httpx.Response(404)

    deps.http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return deps


def test_dispatch_weather_success() -> None:
    deps = _weather_deps(_MOSCOW_GEO, _MOSCOW_FORECAST)
    out = registry.dispatch(registry.TOOL_GET_WEATHER, '{"city": "Moscow"}', deps)
    assert "Moscow" in out
    assert "20°C" in out
    assert "ясно" in out


def test_dispatch_weather_not_found() -> None:
    deps = _weather_deps({"results": []}, _MOSCOW_FORECAST)
    out = registry.dispatch(registry.TOOL_GET_WEATHER, '{"city": "Atlantis"}', deps)
    assert "Не нашёл" in out
    assert "Atlantis" in out


def test_dispatch_get_time_returns_localized_sentence() -> None:
    # _FakeDeps uses tz=UTC; just assert the sentence shape, not the exact clock.
    out = registry.dispatch(registry.TOOL_GET_TIME, "{}", _FakeDeps())
    assert "Сейчас" in out
    assert re.search(r"\b\d{2}:\d{2}\b", out)


# ── dispatch: argument parsing & error paths ────────────────────────────────


def test_dispatch_handles_empty_arguments() -> None:
    # Empty JSON → args={}, calculator gets empty expression → localized empty msg.
    out = registry.dispatch(registry.TOOL_CALCULATE, "", _FakeDeps())
    assert out == "Пустое выражение."


def test_dispatch_handles_invalid_json() -> None:
    out = registry.dispatch(registry.TOOL_CALCULATE, "not-json{", _FakeDeps())
    assert "Некорректные аргументы" in out
    assert registry.TOOL_CALCULATE in out


def test_dispatch_handles_non_object_json() -> None:
    out = registry.dispatch(registry.TOOL_CALCULATE, "[1,2,3]", _FakeDeps())
    assert "Некорректные аргументы" in out


def test_dispatch_unknown_tool_returns_localized_message() -> None:
    out = registry.dispatch("does_not_exist", "{}", _FakeDeps())
    assert "Неизвестный инструмент" in out
    assert "does_not_exist" in out


def test_dispatch_never_raises_on_tool_error() -> None:
    # Division by zero inside calculator → localized message, not exception.
    out = registry.dispatch(registry.TOOL_CALCULATE, '{"expression": "1/0"}', _FakeDeps())
    assert "Деление на ноль" in out


# ── dispatch: timer / alarm / reminder ───────────────────────────────────────


def test_dispatch_set_timer() -> None:
    deps = _FakeDeps()
    out = registry.dispatch(registry.TOOL_SET_TIMER, '{"minutes": 30, "label": "чай"}', deps)
    assert "Таймер" in out and "30 мин" in out and "08:00" in out
    assert deps.scheduler.calls == [("add_timer", (1800, "чай"))]


def test_dispatch_set_timer_en_and_seconds() -> None:
    deps = _FakeDeps(language="en")
    out = registry.dispatch(registry.TOOL_SET_TIMER, '{"seconds": 90}', deps)
    assert "Timer" in out and "1m 30s" in out


def test_dispatch_set_timer_hours_and_minutes() -> None:
    # "полтора часа" → hours=1, minutes=30
    deps = _FakeDeps()
    out = registry.dispatch(registry.TOOL_SET_TIMER, '{"hours": 1, "minutes": 30}', deps)
    assert "1 ч 30 мин" in out
    assert deps.scheduler.calls == [("add_timer", (5400, None))]


def test_dispatch_set_timer_hours_en() -> None:
    deps = _FakeDeps(language="en")
    out = registry.dispatch(registry.TOOL_SET_TIMER, '{"hours": 2}', deps)
    assert "2h" in out


def test_dispatch_set_timer_bad_duration() -> None:
    out = registry.dispatch(registry.TOOL_SET_TIMER, '{"minutes": 0}', _FakeDeps())
    assert "положительное время" in out


def test_dispatch_set_alarm_weekdays() -> None:
    deps = _FakeDeps()
    out = registry.dispatch(
        registry.TOOL_SET_ALARM,
        '{"hour": 8, "minute": 0, "recurrence": "weekdays"}',
        deps,
    )
    assert "Будильник" in out and "08:00" in out and "по будням" in out


def test_dispatch_set_alarm_bad_clock() -> None:
    out = registry.dispatch(registry.TOOL_SET_ALARM, '{"hour": 25, "minute": 0}', _FakeDeps())
    assert "Некорректное время" in out


def test_dispatch_set_alarm_weekly_requires_weekdays() -> None:
    out = registry.dispatch(
        registry.TOOL_SET_ALARM,
        '{"hour": 8, "minute": 0, "recurrence": "weekly"}',
        _FakeDeps(),
    )
    assert "повтора" in out


def test_dispatch_set_reminder_daily() -> None:
    deps = _FakeDeps()
    out = registry.dispatch(
        registry.TOOL_SET_REMINDER,
        '{"hour": 19, "minute": 30, "message": "выпить таблетку", "recurrence": "daily"}',
        deps,
    )
    assert "Напоминание" in out and "19:30" in out and "каждый день" in out
    assert "выпить таблетку" in out


def test_dispatch_set_reminder_no_message() -> None:
    out = registry.dispatch(
        registry.TOOL_SET_REMINDER,
        '{"hour": 19, "minute": 30, "message": "  "}',
        _FakeDeps(),
    )
    assert "о чём напомнить" in out


def test_dispatch_set_reminder_weekdays_in_24h_format() -> None:
    # 13:00 must stay 13:00 (not 1 PM) — 24-hour rendering by default.
    out = registry.dispatch(
        registry.TOOL_SET_REMINDER,
        '{"hour": 13, "minute": 0, "message": "поесть"}',
        _FakeDeps(),
    )
    assert "13:00" in out


# ── dispatch: spoken `time` parsing (the model must not convert words) ───────


def test_dispatch_set_reminder_parses_spoken_time() -> None:
    # The raw words win over a (wrong) model-supplied hour: "Двадцать тридцать"
    # must parse to 20:30, not the LLM's 23:30 mis-read.
    deps = _FakeDeps()
    out = registry.dispatch(
        registry.TOOL_SET_REMINDER,
        '{"time": "Двадцать тридцать", "hour": 23, "minute": 30, "message": "поужинать"}',
        deps,
    )
    assert "20:30" in out and "поужинать" in out
    assert deps.scheduler.calls == [("add_reminder", (20, 30, "поужинать", Recurrence.once()))]


def test_dispatch_set_alarm_parses_spoken_time() -> None:
    deps = _FakeDeps()
    out = registry.dispatch(
        registry.TOOL_SET_ALARM,
        '{"time": "восемь ноль ноль", "recurrence": "weekdays"}',
        deps,
    )
    assert "08:00" in out and "по будням" in out
    assert deps.scheduler.calls == [("add_alarm", (8, 0, Recurrence.weekdays(), None))]


def test_dispatch_set_reminder_english_spoken_time() -> None:
    deps = _FakeDeps(language="en")
    out = registry.dispatch(
        registry.TOOL_SET_REMINDER,
        '{"time": "nineteen thirty", "message": "take a pill"}',
        deps,
    )
    assert "19:30" in out and "take a pill" in out
