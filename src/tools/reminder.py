"""``set_reminder`` tool — schedule a spoken reminder, optionally repeating.

STT transcribes clock times as number words ("напоминай каждый день в
девятнадцать тридцать выпить таблетку", "поставь напоминание в двадцать
тридцать поужинать"). The model passes them verbatim in ``time`` and
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

_MSG_NO_MESSAGE = LocaleStr(
    ru="Не указано, о чём напомнить (параметр message).",
    en="No reminder text given (message parameter).",
)
_MSG_BAD_RECURRENCE = LocaleStr(
    ru="Некорректные параметры повтора. Доступно: once, daily, weekdays, "
    "weekends, weekly (тогда укажите weekdays).",
    en="Invalid recurrence parameters. Options: once, daily, weekdays, "
    "weekends, weekly (then also provide weekdays).",
)

_CONFIRM = LocaleStr(
    ru="Напоминание «{message}» на {time}{recurrence} — поставлено.",
    en="Reminder «{message}» at {time}{recurrence} — set.",
)


def set_reminder(args: dict[str, Any], language: str, scheduler: Scheduler) -> str:
    """Schedule a reminder from ``time`` (or ``hour``/``minute``) + message."""
    clock = resolve_clock(args, language)
    if isinstance(clock, str):
        return clock
    hour, minute = clock

    message = (args.get("message") or "").strip()
    if not message:
        return _MSG_NO_MESSAGE.render(language)

    try:
        recurrence = Recurrence.from_params(args.get("recurrence"), args.get("weekdays"))
    except ValueError:
        return _MSG_BAD_RECURRENCE.render(language)

    event = scheduler.add_reminder(hour, minute, message, recurrence)
    suffix = describe_recurrence(recurrence, language)
    return _CONFIRM.render(
        language,
        message=message,
        time=format_local(event.fire_at_utc, scheduler.tz),
        recurrence=f", {suffix}" if suffix else "",
    )


REMINDER_PARAMS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "time": {
            "type": "string",
            "description": (
                "Time as words exactly as the user said it, NOT digits: "
                "'двадцать тридцать', 'девятнадцать тридцать', 'восемь ноль ноль'."
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
        "message": {
            "type": "string",
            "description": "What to remind about, e.g. 'take my pill'.",
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
    },
    "required": ["message"],
    "anyOf": [
        {"required": ["time"]},
        {"required": ["hour", "minute"]},
    ],
}


__all__ = ["set_reminder", "REMINDER_PARAMS"]
