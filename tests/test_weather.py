"""Tests for the weather tool — uses a stubbed httpx.Client (no network)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from s2s_assistant.tools import weather

_GEO_URL = "https://geocoding.example/v1/search"
_FORE_URL = "https://forecast.example/v1/forecast"


class _StubTransport(httpx.BaseTransport):
    """Routes requests to canned responses keyed by URL host + params.

    By default the geocoder returns the same canned response regardless of the
    ``language`` param. Pass ``geo_by_language`` to vary the geocoder response
    per language — this mirrors the real Open-Meteo behaviour where a Russian
    city name is only resolved with ``language=ru``.
    """

    def __init__(
        self,
        geo_response: dict[str, Any] | None,
        forecast_response: dict[str, Any] | None,
        *,
        geo_status: int = 200,
        forecast_status: int = 200,
        geo_by_language: dict[str, dict[str, Any] | None] | None = None,
    ) -> None:
        self._geo = geo_response
        self._fore = forecast_response
        self._geo_status = geo_status
        self._fore_status = forecast_status
        self._geo_by_language = geo_by_language or {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            lang = request.url.params.get("language")
            if lang in self._geo_by_language:
                return self._json(self._geo_by_language[lang], self._geo_status)
            return self._json(self._geo, self._geo_status)
        if "forecast" in request.url.host:
            return self._json(self._fore, self._fore_status)
        return httpx.Response(404, text="not found")

    @staticmethod
    def _json(payload: dict[str, Any] | None, status: int) -> httpx.Response:
        return httpx.Response(status, json=payload if payload is not None else {})


def _client(transport: httpx.BaseTransport) -> httpx.Client:
    return httpx.Client(transport=transport)


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
        "temperature_2m": 18.4,
        "weather_code": 1,
        "wind_speed_10m": 4.2,
        "relative_humidity_2m": 63,
    }
}


def test_get_weather_success_ru() -> None:
    client = _client(_StubTransport(_MOSCOW_GEO, _MOSCOW_FORECAST))
    out = weather.get_weather(
        "Москва", language="ru", geocoding_url=_GEO_URL, forecast_url=_FORE_URL, client=client
    )
    assert "Москва" not in out  # the display name uses the English geocoder name
    assert "Moscow" in out
    assert "18°C" in out
    assert "преимущественно ясно" in out
    assert "ветер 4 м/с" in out
    assert "влажность 63%" in out


def test_get_weather_success_en() -> None:
    client = _client(_StubTransport(_MOSCOW_GEO, _MOSCOW_FORECAST))
    out = weather.get_weather(
        "Moscow", language="en", geocoding_url=_GEO_URL, forecast_url=_FORE_URL, client=client
    )
    assert "Moscow" in out
    assert "18°C" in out
    assert "mainly clear" in out
    assert "wind 4 m/s" in out
    assert "humidity 63%" in out


# Mirrors the real Open-Meteo quirk that surfaced in the wild: a Russian city
# name like "Екатеринбург" resolves ONLY with language=ru (en returns nothing).
# The tool must try the user's language first and succeed on the first try.
_EKAT_RU_GEO = {
    "results": [
        {
            "name": "Екатеринбург",
            "latitude": 56.84,
            "longitude": 60.61,
            "country": "Russia",
            "admin1": "Sverdlovsk Oblast",
        }
    ]
}


def test_get_weather_russian_city_resolves_with_user_language() -> None:
    # Geocoder: ru → found, en → empty (exactly the bug that caused
    # "Не нашёл населённый пункт «Екатеринбург»" before the language fix).
    client = _client(
        _StubTransport(
            None,
            _MOSCOW_FORECAST,
            geo_by_language={"ru": _EKAT_RU_GEO, "en": {"results": []}},
        )
    )
    out = weather.get_weather(
        "Екатеринбург",
        language="ru",
        geocoding_url=_GEO_URL,
        forecast_url=_FORE_URL,
        client=client,
    )
    assert "Не нашёл" not in out
    assert "Екатеринбург" in out
    assert "преимущественно ясно" in out


def test_get_weather_falls_back_to_english_geocoder() -> None:
    # If the user's language misses (empty results), we retry with English.
    client = _client(
        _StubTransport(
            None,
            _MOSCOW_FORECAST,
            geo_by_language={"ru": {"results": []}, "en": _MOSCOW_GEO},
        )
    )
    out = weather.get_weather(
        "москва",
        language="ru",
        geocoding_url=_GEO_URL,
        forecast_url=_FORE_URL,
        client=client,
    )
    assert "Moscow" in out
    assert "Не нашёл" not in out


def test_get_weather_place_not_found() -> None:
    # Returns empty for BOTH languages so the fallback also misses — only then
    # does the tool report the city as not found.
    client = _client(
        _StubTransport(
            {"results": []},
            _MOSCOW_FORECAST,
            geo_by_language={"ru": {"results": []}, "en": {"results": []}},
        )
    )
    out = weather.get_weather(
        "Атлантида", language="ru", geocoding_url=_GEO_URL, forecast_url=_FORE_URL, client=client
    )
    assert "Не нашёл" in out
    assert "Атлантида" in out


def test_get_weather_empty_city() -> None:
    client = _client(_StubTransport(_MOSCOW_GEO, _MOSCOW_FORECAST))
    out = weather.get_weather(
        "", language="ru", geocoding_url=_GEO_URL, forecast_url=_FORE_URL, client=client
    )
    assert "Не нашёл" in out


def test_get_weather_service_error_geocoding() -> None:
    # HTTP error from the geocoder → service-unavailable message, no raise.
    client = _client(_StubTransport(None, _MOSCOW_FORECAST, geo_status=500))
    out = weather.get_weather(
        "Moscow", language="ru", geocoding_url=_GEO_URL, forecast_url=_FORE_URL, client=client
    )
    assert "недоступен" in out


def test_get_weather_service_error_forecast() -> None:
    client = _client(_StubTransport(_MOSCOW_GEO, None, forecast_status=502))
    out = weather.get_weather(
        "Moscow", language="en", geocoding_url=_GEO_URL, forecast_url=_FORE_URL, client=client
    )
    assert "unavailable" in out


def test_get_weather_missing_temperature() -> None:
    # Geocoding resolves, but forecast lacks temperature → no-data message.
    bad_forecast = {"current": {"weather_code": 0}}
    client = _client(_StubTransport(_MOSCOW_GEO, bad_forecast))
    out = weather.get_weather(
        "Moscow", language="ru", geocoding_url=_GEO_URL, forecast_url=_FORE_URL, client=client
    )
    assert "Не удалось получить погоду" in out
    assert "Moscow" in out


@pytest.mark.parametrize(
    "code, ru_substr, en_substr",
    [
        (0, "ясно", "clear sky"),
        (3, "пасмурно", "overcast"),
        (61, "небольшой дождь", "slight rain"),
        (75, "сильный снегопад", "heavy snow fall"),
        (95, "гроза", "thunderstorm"),
        (9999, "код погоды 9999", "weather code 9999"),
    ],
)
def test_wmo_description_mapping(code: int, ru_substr: str, en_substr: str) -> None:
    assert ru_substr in weather._wmo_description(code, "ru")
    assert en_substr in weather._wmo_description(code, "en")


def test_geolocation_display_name_with_region_and_country() -> None:
    geo = weather.GeoLocation(
        name="Springfield", latitude=0.0, longitude=0.0, country="USA", admin1="Illinois"
    )
    assert geo.display_name == "Springfield, Illinois, USA"


def test_geolocation_display_name_dedupes_admin1_equal_to_name() -> None:
    # admin1 == name should not be duplicated (e.g. Moscow city, region Moscow).
    geo = weather.GeoLocation(
        name="Moscow", latitude=0.0, longitude=0.0, country="Russia", admin1="Moscow"
    )
    assert geo.display_name == "Moscow, Russia"


def test_json_of_forecast_is_fetched_with_current_fields() -> None:
    # The forecast call must request exactly the current fields we render.
    captured: dict[str, Any] = {}

    class _CapturingTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            if "geocoding" in host:
                return httpx.Response(200, json=_MOSCOW_GEO)
            if "forecast" in host:
                # Keep the raw QueryParams so repeated keys survive.
                captured["params"] = request.url.params
                return httpx.Response(200, json=_MOSCOW_FORECAST)
            return httpx.Response(404)

    client = _client(_CapturingTransport())  # type: ignore[arg-type]
    weather.get_weather(
        "Moscow", language="ru", geocoding_url=_GEO_URL, forecast_url=_FORE_URL, client=client
    )
    # httpx encodes the list param as repeated `current=<field>` pairs.
    fields = {
        v for k, v in captured["params"].multi_items() if k == "current"
    }
    assert {
        "temperature_2m",
        "relative_humidity_2m",
        "weather_code",
        "wind_speed_10m",
    } <= fields


def test_constants_have_expected_schema() -> None:
    # The exposed JSON schema must declare a single required 'city' string.
    schema = weather.WEATHER_PARAMS
    assert schema["type"] == "object"
    assert schema["properties"]["city"]["type"] == "string"
    assert schema["required"] == ["city"]
    # Guard against accidental drift in the literal.
    assert json.dumps(schema)  # serializable
