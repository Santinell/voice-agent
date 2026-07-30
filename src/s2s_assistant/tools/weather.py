"""``get_weather`` tool — real current weather via Open-Meteo (no API key).

Pipeline:
  1. Geocoding API resolves a free-form city name (RU/EN) to lat/lon.
  2. Forecast API returns the current weather at that point.
  3. The numbers + WMO weather code are rendered into a localised sentence.

Open-Meteo is free, keyless and read-only — ideal first real tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx

from ..localization import LocaleStr

# ── localised strings ───────────────────────────────────────────────────────

_MSG_FOUND = LocaleStr(
    ru="Сейчас в {place}: {temp}°C, {desc}, ветер {wind} м/с, влажность {humidity}%.",
    en="Right now in {place}: {temp}°C, {desc}, wind {wind} m/s, humidity {humidity}%.",
)
_MSG_NOT_FOUND = LocaleStr(
    ru="Не нашёл населённый пункт «{city}».",
    en="Could not find a place called {city!r}.",
)
_MSG_NO_DATA = LocaleStr(
    ru="Не удалось получить погоду для {place}.",
    en="Could not get weather for {place}.",
)
_MSG_SERVICE = LocaleStr(
    ru="Сервис погоды недоступен. Попробуйте позже.",
    en="The weather service is unavailable. Try again later.",
)

# WMO weather interpretation codes (Open-Meteo) → short localised phrases.
_WMO_DESC: dict[int, LocaleStr] = {
    0: LocaleStr(ru="ясно", en="clear sky"),
    1: LocaleStr(ru="преимущественно ясно", en="mainly clear"),
    2: LocaleStr(ru="переменная облачность", en="partly cloudy"),
    3: LocaleStr(ru="пасмурно", en="overcast"),
    45: LocaleStr(ru="туман", en="fog"),
    48: LocaleStr(ru="изморозь", en="depositing rime fog"),
    51: LocaleStr(ru="слабая морось", en="light drizzle"),
    53: LocaleStr(ru="морось", en="moderate drizzle"),
    55: LocaleStr(ru="сильная морось", en="dense drizzle"),
    56: LocaleStr(ru="ледяная морось", en="light freezing drizzle"),
    57: LocaleStr(ru="сильная ледяная морось", en="dense freezing drizzle"),
    61: LocaleStr(ru="небольшой дождь", en="slight rain"),
    63: LocaleStr(ru="дождь", en="moderate rain"),
    65: LocaleStr(ru="сильный дождь", en="heavy rain"),
    66: LocaleStr(ru="ледяной дождь", en="light freezing rain"),
    67: LocaleStr(ru="сильный ледяной дождь", en="heavy freezing rain"),
    71: LocaleStr(ru="небольшой снег", en="slight snow fall"),
    73: LocaleStr(ru="снег", en="moderate snow fall"),
    75: LocaleStr(ru="сильный снегопад", en="heavy snow fall"),
    77: LocaleStr(ru="снежные зёрна", en="snow grains"),
    80: LocaleStr(ru="ливень", en="slight rain showers"),
    81: LocaleStr(ru="сильный ливень", en="moderate rain showers"),
    82: LocaleStr(ru="очень сильный ливень", en="violent rain showers"),
    85: LocaleStr(ru="снегопад", en="slight snow showers"),
    86: LocaleStr(ru="сильный снегопад", en="heavy snow showers"),
    95: LocaleStr(ru="гроза", en="thunderstorm"),
    96: LocaleStr(ru="гроза с градом", en="thunderstorm with slight hail"),
    99: LocaleStr(ru="сильная гроза с градом", en="thunderstorm with heavy hail"),
}


def _wmo_description(code: int, language: str) -> str:
    fallback = LocaleStr(ru=f"код погоды {code}", en=f"weather code {code}")
    return _WMO_DESC.get(code, fallback).render(language)


# ── geocoding result ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GeoLocation:
    name: str
    latitude: float
    longitude: float
    country: str | None = None
    admin1: str | None = None  # region/state — helps disambiguate names

    @property
    def display_name(self) -> str:
        """Human-friendly label, e.g. 'Moscow, Russia'."""
        bits = [self.name]
        if self.admin1 and self.admin1 != self.name:
            bits.append(self.admin1)
        if self.country:
            bits.append(self.country)
        return ", ".join(bits)


# ── API calls ───────────────────────────────────────────────────────────────

# Timeout budget: these are fast read-only APIs. A slow/hung request must not
# block the voice loop indefinitely.
_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


def _geocode(
    city: str, geocoding_url: str, *, language: str, client: httpx.Client
) -> GeoLocation | None:
    """Resolve a city name to coordinates. Returns the first match or None.

    Open-Meteo's geocoder matches names per ``language``: a Russian name like
    "Екатеринбург" is only found with ``language=ru`` (``language=en`` returns
    nothing), whereas English names work with any language. We therefore try the
    user's language first, then fall back to English for names the user may have
    given in a different script/language.
    """
    for lang in _unique_in_order(language, "en"):
        resp = client.get(
            geocoding_url,
            params={"name": city.strip(), "count": 1, "language": lang, "format": "json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        results: list[dict[str, Any]] = resp.json().get("results") or []
        if results:
            r = results[0]
            return GeoLocation(
                name=str(r.get("name", city)),
                latitude=float(r["latitude"]),
                longitude=float(r["longitude"]),
                country=r.get("country"),
                admin1=r.get("admin1"),
            )
    return None


def _unique_in_order(*values: str) -> list[str]:
    """De-duplicate while preserving order (e.g. ('ru', 'en') stays that way)."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _forecast(
    geo: GeoLocation, forecast_url: str, *, client: httpx.Client
) -> dict[str, Any] | None:
    """Fetch the current-weather fields from Open-Meteo."""
    resp = client.get(
        forecast_url,
        params={
            "latitude": geo.latitude,
            "longitude": geo.longitude,
            "current": [
                "temperature_2m",
                "relative_humidity_2m",
                "weather_code",
                "wind_speed_10m",
            ],
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    payload: dict[str, Any] = resp.json()
    current = payload.get("current")
    if not isinstance(current, dict):
        return None
    # Need at least a temperature to say something useful.
    if "temperature_2m" not in current:
        return None
    return cast(dict[str, Any], current)


def _format(geo: GeoLocation, current: dict[str, Any], language: str) -> str:
    temp = current.get("temperature_2m")
    code = current.get("weather_code")
    wind = current.get("wind_speed_10m")
    hum = current.get("relative_humidity_2m")

    return _MSG_FOUND.render(
        language,
        place=geo.display_name,
        temp=_round(temp),
        desc=_wmo_description(_as_int(code), language) if code is not None else "—",
        wind=_round(wind),
        humidity=_as_int(hum),
    )


def _round(v: Any) -> Any:
    """Round a numeric value to an int when finite, else pass through."""
    if isinstance(v, int | float):
        return round(v)
    return v


def _as_int(v: Any) -> int:
    return int(v) if v is not None else 0


# ── public entry point ──────────────────────────────────────────────────────


def get_weather(
    city: str,
    *,
    language: str,
    geocoding_url: str,
    forecast_url: str,
    client: httpx.Client,
) -> str:
    """Resolve ``city`` and return a localised current-weather sentence.

    Never raises on expected failures (not found, missing fields, HTTP errors):
    it returns a localised message so the LLM can relay it to the user.
    """
    if not city or not city.strip():
        return _MSG_NOT_FOUND.render(language, city=city)

    try:
        geo = _geocode(city, geocoding_url, language=language, client=client)
    except httpx.HTTPError:
        return _MSG_SERVICE.render(language)
    if geo is None:
        return _MSG_NOT_FOUND.render(language, city=city)

    try:
        current = _forecast(geo, forecast_url, client=client)
    except httpx.HTTPError:
        return _MSG_SERVICE.render(language)
    if current is None:
        return _MSG_NO_DATA.render(language, place=geo.display_name)

    return _format(geo, current, language)


# JSON-schema argument contract exposed to the LLM via the Realtime session.
WEATHER_PARAMS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "city": {
            "type": "string",
            "description": (
                "City or place name the user asked the weather for, "
                "in the user's language. Example: 'Moscow', 'Москва', 'Paris'."
            ),
        }
    },
    "required": ["city"],
}


__all__ = ["get_weather", "WEATHER_PARAMS", "GeoLocation"]
