"""Tests for the tool registry — schemas and dispatch."""

from __future__ import annotations

from typing import Any

import httpx

from s2s_assistant.tools import registry


class _FakeDeps:
    """Minimal ToolDeps for dispatch tests (no real network)."""

    def __init__(self, *, language: str = "ru") -> None:
        self.language = language
        self.geocoding_url = "https://geocoding.example/v1/search"
        self.forecast_url = "https://forecast.example/v1/forecast"
        # A client whose transport always 404s — weather tests inject their own.
        self.http_client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(404))
        )


# ── schema construction ─────────────────────────────────────────────────────


def test_tool_schemas_have_stable_names() -> None:
    names = {s.name for s in registry.tool_schemas("ru")}
    assert names == {registry.TOOL_CALCULATE, registry.TOOL_GET_WEATHER}


def test_tool_schemas_render_to_realtime_dict() -> None:
    schemas = registry.realtime_tools("en")
    assert len(schemas) == 2
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


# ── dispatch: calculator ────────────────────────────────────────────────────


def test_dispatch_calculate() -> None:
    out = registry.dispatch(
        registry.TOOL_CALCULATE, '{"expression": "6 * 7"}', _FakeDeps()
    )
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
