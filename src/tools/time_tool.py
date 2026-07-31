"""``get_time`` tool — current time, date and weekday in the user's timezone.

Returns one localised sentence such as "Сейчас пятница, 31 июля, 17:25" /
"It's Friday, July 31, 17:25". The model then relays it to the user, so the
voice agent can answer "который час?", "какое сегодня число?", "какой день
недели?" without keeping any clock of its own.

Day/month names are hardcoded per language rather than read via ``strftime("%A")``:
the project never relies on the system locale (it can be ``C``/``en`` on the
server), and explicit tables keep the output — and the tests — deterministic.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any

from localization import LocaleStr

# Monday=0 … Sunday=6, matching Python's ``date.weekday()``.
_WEEKDAYS = (
    LocaleStr(ru="понедельник", en="Monday"),
    LocaleStr(ru="вторник", en="Tuesday"),
    LocaleStr(ru="среда", en="Wednesday"),
    LocaleStr(ru="четверг", en="Thursday"),
    LocaleStr(ru="пятница", en="Friday"),
    LocaleStr(ru="суббота", en="Saturday"),
    LocaleStr(ru="воскресенье", en="Sunday"),
)

# January=0 … December=11, matching ``date.month - 1``.
_MONTHS = (
    LocaleStr(ru="января", en="January"),
    LocaleStr(ru="февраля", en="February"),
    LocaleStr(ru="марта", en="March"),
    LocaleStr(ru="апреля", en="April"),
    LocaleStr(ru="мая", en="May"),
    LocaleStr(ru="июня", en="June"),
    LocaleStr(ru="июля", en="July"),
    LocaleStr(ru="августа", en="August"),
    LocaleStr(ru="сентября", en="September"),
    LocaleStr(ru="октября", en="October"),
    LocaleStr(ru="ноября", en="November"),
    LocaleStr(ru="декабря", en="December"),
)

# Word order differs between languages (RU "31 июля" vs EN "July 31"), so each
# template arranges the placeholders itself.
_MSG_TIME = LocaleStr(
    ru="Сейчас {weekday}, {day} {month}, {time}.",
    en="It's {weekday}, {month} {day}, {time}.",
)


def get_time(language: str, tz: tzinfo) -> str:
    """Return a localised sentence with the current time, date and weekday.

    Always succeeds (there is no input to validate); the result is a plain
    string the LLM speaks to the user.
    """
    now = datetime.now(tz)
    return _MSG_TIME.render(
        language,
        weekday=_WEEKDAYS[now.weekday()].render(language),
        day=now.day,
        month=_MONTHS[now.month - 1].render(language),
        time=now.strftime("%H:%M"),
    )


# Public, JSON-schema-validated argument contract. No arguments: the tool just
# reports the current moment in the user's timezone.
TIME_PARAMS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
    "required": [],
}


__all__ = ["get_time", "TIME_PARAMS"]
