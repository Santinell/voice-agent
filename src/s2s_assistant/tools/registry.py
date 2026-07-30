"""Tool registry — the single source of truth for client-side tools.

This module:
  * defines each tool's OpenAI Realtime function schema (name, description,
    parameters) — bilingual descriptions so the same schema works in RU/EN;
  * dispatches an incoming function call (name + JSON args) to the matching
    implementation, returning a string the LLM relays to the user.

The Realtime client feeds ``realtime_tools()`` into ``session.update`` and
calls ``dispatch()`` when the server emits ``response.function_call_arguments``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, cast

import httpx

from ..localization import LocaleStr
from . import calculator, weather

# A parsed JSON object — the shape every tool argument payload has.
JsonObject: TypeAlias = dict[str, Any]

# ── shared, localised descriptions ──────────────────────────────────────────

_CALC_DESC = LocaleStr(
    ru="Калькулятор. Вычисляет арифметическое выражение (+, -, *, /, //, %, **, "
    "скобки, константы pi, e). Используй, когда пользователь просит посчитать.",
    en="Calculator. Evaluates an arithmetic expression (+, -, *, /, //, %, **, "
    "parentheses, constants pi, e). Use when the user asks to compute something.",
)

_WEATHER_DESC = LocaleStr(
    ru="Текущая погода в указанном городе. Возвращает температуру, описание, "
    "ветер и влажность. Используй, когда пользователь спрашивает о погоде.",
    en="Current weather in a given city. Returns temperature, description, "
    "wind and humidity. Use when the user asks about the weather.",
)

_MSG_BAD_ARGS = LocaleStr(
    ru="Некорректные аргументы инструмента {tool}: {detail}",
    en="Invalid arguments for tool {tool}: {detail}",
)
_MSG_UNKNOWN_TOOL = LocaleStr(
    ru="Неизвестный инструмент {tool}.",
    en="Unknown tool {tool}.",
)

# ── tool names: stable identifiers shared with the model ───────────────────

TOOL_CALCULATE = "calculate"
TOOL_GET_WEATHER = "get_weather"


# ── dependencies a tool may need at dispatch time ───────────────────────────


class ToolDeps(Protocol):
    """What tools need to run. Kept explicit to document real dependencies."""

    language: str
    geocoding_url: str
    forecast_url: str
    http_client: httpx.Client


@dataclass(frozen=True)
class ToolSchema:
    """A tool's OpenAI Realtime function schema."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Render as the ``tools[]`` item expected by ``session.update``."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def tool_schemas(language: str) -> list[ToolSchema]:
    """Build the tool schema list localised for ``language``."""
    return [
        ToolSchema(
            name=TOOL_CALCULATE,
            description=_CALC_DESC.render(language),
            parameters=calculator.CALCULATOR_PARAMS,
        ),
        ToolSchema(
            name=TOOL_GET_WEATHER,
            description=_WEATHER_DESC.render(language),
            parameters=weather.WEATHER_PARAMS,
        ),
    ]


def realtime_tools(language: str) -> list[dict[str, Any]]:
    """Schemas ready for the Realtime ``tools`` session field."""
    return [s.to_dict() for s in tool_schemas(language)]


# ── dispatch ────────────────────────────────────────────────────────────────


def dispatch(name: str, arguments: str, deps: ToolDeps) -> str:
    """Run the named tool with JSON ``arguments`` and return its string output.

    ``arguments`` is the raw JSON string delivered in
    ``response.function_call_arguments`` chunks. Returns a localised string in
    all cases (success, bad args, unknown tool) so the loop never crashes.
    """
    # Parse args first; a parse failure yields a localised error message.
    args = _parse_arguments(arguments, name, deps)
    if isinstance(args, str):  # parse-error message
        return args

    if name == TOOL_CALCULATE:
        return calculator.evaluate(args.get("expression", ""), deps.language)
    if name == TOOL_GET_WEATHER:
        return weather.get_weather(
            args.get("city", ""),
            language=deps.language,
            geocoding_url=deps.geocoding_url,
            forecast_url=deps.forecast_url,
            client=deps.http_client,
        )

    return _MSG_UNKNOWN_TOOL.render(deps.language, tool=name)


def _parse_arguments(arguments: str, name: str, deps: ToolDeps) -> JsonObject | str:
    """Parse the tool-call argument JSON.

    Returns a dict on success, or a localised error string on failure (so the
    caller can relay it to the model without exception handling at the call
    site).
    """
    if not arguments:
        return {}
    try:
        parsed: Any = json.loads(arguments)
    except json.JSONDecodeError as exc:
        return _MSG_BAD_ARGS.render(deps.language, tool=name, detail=str(exc))
    if not isinstance(parsed, dict):
        return _MSG_BAD_ARGS.render(deps.language, tool=name, detail="expected object")
    return cast(JsonObject, parsed)


__all__ = [
    "ToolDeps",
    "ToolSchema",
    "TOOL_CALCULATE",
    "TOOL_GET_WEATHER",
    "tool_schemas",
    "realtime_tools",
    "dispatch",
]
