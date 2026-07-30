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

from localization import LocaleStr

from . import alarm_clock, calculator, reminder, scheduling, timer, weather

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

_TIMER_DESC = LocaleStr(
    ru="Таймер: засечь время. Запускает обратный отсчёт (hours/minutes/seconds); "
    "по истечении ассистент сообщает об этом. Длительность приходит от STT "
    "числительными: «полтора часа» → hours=1, minutes=30; «пол часа» → "
    "minutes=30; «девяносто секунд» → seconds=90.",
    en="Timer: start a countdown (hours/minutes/seconds); when it elapses the "
    "assistant announces it. STT sends durations as words: 'an hour and a half' "
    "→ hours=1, minutes=30; 'half an hour' → minutes=30; 'ninety seconds' → "
    "seconds=90.",
)

_ALARM_DESC = LocaleStr(
    ru="Будильник. Звонит в указанное время суток, по умолчанию однократно; "
    "поддерживает повтор (once/daily/weekdays/weekends/weekly). ВАЖНО: время "
    "передавай в поле time числительными ровно так, как сказал пользователь, НЕ "
    "переводи в цифры: «двадцать тридцать», «восемь ноль ноль», «девятнадцать "
    "тридцать». Пример: «каждый будний день в восемь ноль ноль» → time='восемь "
    "ноль ноль', recurrence=weekdays.",
    en="Alarm clock. Rings at a given time of day, once by default; supports "
    "recurrence (once/daily/weekdays/weekends/weekly). IMPORTANT: pass the time "
    "in the 'time' field as words exactly as the user said it, do NOT convert to "
    "digits: 'twenty thirty', 'eight hundred', 'nineteen thirty'. Example: "
    "'every weekday at eight hundred' → time='eight hundred', "
    "recurrence=weekdays.",
)

_REMINDER_DESC = LocaleStr(
    ru="Напоминание. В указанное время ассистент озвучит сообщение; поддерживает "
    "повтор (once/daily/weekdays/weekends/weekly). ВАЖНО: время передавай в поле "
    "time числительными ровно так, как сказал пользователь, НЕ переводи в цифры: "
    "«двадцать тридцать», «девятнадцать тридцать», «восемь ноль ноль». Пример: "
    "«напоминай каждый день в девятнадцать тридцать выпить таблетку» → "
    "time='девятнадцать тридцать', message='выпить таблетку', recurrence=daily.",
    en="Reminder. At the given time the assistant speaks the message; supports "
    "recurrence (once/daily/weekdays/weekends/weekly). IMPORTANT: pass the time "
    "in the 'time' field as words exactly as the user said it, do NOT convert to "
    "digits: 'twenty thirty', 'nineteen thirty', 'eight hundred'. Example: "
    "'remind me every day at nineteen thirty to take a pill' → time='nineteen "
    "thirty', message='take a pill', recurrence=daily.",
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
TOOL_SET_TIMER = "set_timer"
TOOL_SET_ALARM = "set_alarm"
TOOL_SET_REMINDER = "set_reminder"


# ── dependencies a tool may need at dispatch time ───────────────────────────


class ToolDeps(Protocol):
    """What tools need to run. Kept explicit to document real dependencies."""

    language: str
    geocoding_url: str
    forecast_url: str
    http_client: httpx.Client
    scheduler: scheduling.Scheduler


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
        ToolSchema(
            name=TOOL_SET_TIMER,
            description=_TIMER_DESC.render(language),
            parameters=timer.TIMER_PARAMS,
        ),
        ToolSchema(
            name=TOOL_SET_ALARM,
            description=_ALARM_DESC.render(language),
            parameters=alarm_clock.ALARM_PARAMS,
        ),
        ToolSchema(
            name=TOOL_SET_REMINDER,
            description=_REMINDER_DESC.render(language),
            parameters=reminder.REMINDER_PARAMS,
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
    if name == TOOL_SET_TIMER:
        return timer.set_timer(args, deps.language, deps.scheduler)
    if name == TOOL_SET_ALARM:
        return alarm_clock.set_alarm(args, deps.language, deps.scheduler)
    if name == TOOL_SET_REMINDER:
        return reminder.set_reminder(args, deps.language, deps.scheduler)

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
    "TOOL_SET_TIMER",
    "TOOL_SET_ALARM",
    "TOOL_SET_REMINDER",
    "tool_schemas",
    "realtime_tools",
    "dispatch",
]
