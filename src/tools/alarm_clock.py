"""``set_alarm`` tool — schedule an alarm at a clock time, optionally repeating.

STT transcribes clock times as number words ("каждый будний день в восемь ноль
ноль", "в двадцать тридцать"). The model passes them verbatim in ``time`` and
``scheduling.parse_clock_text`` converts them to hour/minute deterministically
(the LLM itself mis-reads "двадцать тридцать" as 23:30). Times are 24-hour.
"""

from __future__ import annotations

from typing import Any

from localization import LocaleStr

from .scheduling import (
    Recurrence,
    Scheduler,
    describe_recurrence,
    format_local,
    resolve_clock,
)

_MSG_BAD_RECURRENCE = LocaleStr(
    ru="Некорректные параметры повтора. Доступно: once, daily, weekdays, "
    "weekends, weekly (тогда укажите weekdays).",
    en="Invalid recurrence parameters. Options: once, daily, weekdays, "
    "weekends, weekly (then also provide weekdays).",
)

_CONFIRM = LocaleStr(
    ru="Будильник на {time}{recurrence}{label} — установлен.",
    en="Alarm for {time}{recurrence}{label} — set.",
)


def set_alarm(args: dict[str, Any], language: str, scheduler: Scheduler) -> str:
    """Schedule an alarm from ``time`` (or fallback ``hour``/``minute``)."""
    clock = resolve_clock(args, language)
    if isinstance(clock, str):
        return clock
    hour, minute = clock

    try:
        recurrence = Recurrence.from_params(
            args.get("recurrence"), args.get("weekdays")
        )
    except ValueError:
        return _MSG_BAD_RECURRENCE.render(language)

    label = args.get("label") or None
    event = scheduler.add_alarm(hour, minute, recurrence, label)
    suffix = describe_recurrence(recurrence, language)
    return _CONFIRM.render(
        language,
        time=format_local(event.fire_at_utc, scheduler.tz),
        recurrence=f", {suffix}" if suffix else "",
        label=f" «{label}»" if label else "",
    )


ALARM_PARAMS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "time": {
            "type": "string",
            "description": (
                "Time as words exactly as the user said it, NOT digits: "
                "'двадцать тридцать', 'восемь ноль ноль', 'девятнадцать тридцать'."
            ),
        },
        "hour": {
            "type": "integer",
            "minimum": 0,
            "maximum": 23,
            "description": "Fallback hour (0–23) only if 'time' is not given.",
        },
        "minute": {
            "type": "integer",
            "minimum": 0,
            "maximum": 59,
            "description": "Fallback minute (0–59) only if 'time' is not given.",
        },
        "recurrence": {
            "type": "string",
            "enum": ["once", "daily", "weekdays", "weekends", "weekly"],
            "default": "once",
            "description": (
                "once (default), daily, weekdays (Mon–Fri), weekends, "
                "or weekly (then set 'weekdays')."
            ),
        },
        "weekdays": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 6},
            "description": "Required only for recurrence=weekly. 0=Monday … 6=Sunday.",
        },
        "label": {
            "type": "string",
            "description": "Optional alarm name, e.g. 'work'.",
        },
    },
    "anyOf": [
        {"required": ["time"]},
        {"required": ["hour", "minute"]},
    ],
}


__all__ = ["set_alarm", "ALARM_PARAMS"]
