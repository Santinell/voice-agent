"""``set_timer`` tool — start a countdown that fires once after a duration.

STT transcribes durations as Russian number words, e.g. "засеки полтора часа"
→ ``hours=1, minutes=30``; "засеки пол часа" → ``minutes=30``; "засеки
девяносто секунд" → ``seconds=90``. The shared
:class:`~tools.scheduling.Scheduler` stores the event and rings
it via the realtime client.
"""

from __future__ import annotations

from typing import Any

from localization import LocaleStr

from .scheduling import Scheduler, format_local

_MSG_BAD_DURATION = LocaleStr(
    ru="Укажите положительное время для таймера (hours, minutes и/или seconds).",
    en="Specify a positive duration for the timer (hours, minutes and/or seconds).",
)

_CONFIRM = LocaleStr(
    ru="Таймер{label} на {duration} — поставлен. Звонок в {time}.",
    en="Timer{label} for {duration} — set. Rings at {time}.",
)

_HOURS = LocaleStr(ru="{h} ч", en="{h}h")
_MINUTES = LocaleStr(ru="{m} мин", en="{m}m")
_SECONDS = LocaleStr(ru="{s} с", en="{s}s")


def _format_duration(total_seconds: float, language: str) -> str:
    """Render a duration as the largest non-trivial parts, e.g. ``1 ч 30 мин``."""
    total = int(round(total_seconds))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(_HOURS.render(language, h=hours))
    if minutes:
        parts.append(_MINUTES.render(language, m=minutes))
    # Always show at least one part (covers sub-minute and zero guards).
    if seconds or not parts:
        parts.append(_SECONDS.render(language, s=seconds))
    return " ".join(parts)


def set_timer(args: dict[str, Any], language: str, scheduler: Scheduler) -> str:
    """Parse ``hours``/``minutes``/``seconds``/``label`` and schedule a timer."""
    try:
        hours = int(args.get("hours") or 0)
        minutes = int(args.get("minutes") or 0)
        seconds = int(args.get("seconds") or 0)
    except (TypeError, ValueError):
        return _MSG_BAD_DURATION.render(language)
    total = hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        return _MSG_BAD_DURATION.render(language)

    label = args.get("label") or None
    event = scheduler.add_timer(total, label)
    return _CONFIRM.render(
        language,
        label=f" «{label}»" if label else "",
        duration=_format_duration(total, language),
        time=format_local(event.fire_at_utc, scheduler.tz),
    )


TIMER_PARAMS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "hours": {
            "type": "integer",
            "minimum": 0,
            "description": "Whole hours. Example: 1 for 'an hour and a half' (with minutes=30).",
        },
        "minutes": {
            "type": "integer",
            "minimum": 0,
            "description": "Whole minutes to count down. Example: 30 for 'half an hour'.",
        },
        "seconds": {
            "type": "integer",
            "minimum": 0,
            "description": "Additional seconds. Use for short timers, e.g. 90.",
        },
        "label": {
            "type": "string",
            "description": "Optional name for the timer, e.g. 'tea'.",
        },
    },
    "anyOf": [
        {"required": ["hours"]},
        {"required": ["minutes"]},
        {"required": ["seconds"]},
    ],
}


__all__ = ["set_timer", "TIMER_PARAMS"]
