"""Tests for the ``get_time`` tool.

``datetime.datetime`` is a built-in immutable type, so its ``now`` cannot be
monkeypatched directly (unlike a module-level ``time``). Instead we swap the
``datetime`` name inside ``time_tool`` for a small stub whose ``now()`` always
returns a fixed instant converted to the requested timezone. This keeps the
assertions deterministic regardless of the real clock or the server locale.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tools import time_tool

_MOSCOW = ZoneInfo("Europe/Moscow")
# 2026-07-31 is a Friday.
_FROZEN = datetime(2026, 7, 31, 14, 25, tzinfo=UTC)


class _FrozenDatetime:
    """Stand-in for ``datetime`` exposing only the ``now`` used by get_time."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    @staticmethod
    def now(tz: Any = UTC) -> datetime:  # noqa: D401 - stub signature
        # Same absolute instant, expressed in the caller's timezone.
        return _FrozenDatetime._instant.astimezone(tz)

    _instant = _FROZEN  # set per-test below


def _freeze(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    """Patch ``time_tool`` so its ``datetime.now(tz)`` returns ``instant``."""
    stub = _FrozenDatetime(instant)
    monkeypatch.setattr(time_tool, "datetime", stub)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    _freeze(monkeypatch, _FROZEN)
    return _FROZEN


def test_get_time_ru_includes_weekday_date_and_time(frozen_now: datetime) -> None:
    # 14:25 UTC == 17:25 Moscow.
    out = time_tool.get_time("ru", _MOSCOW)
    assert "Сейчас" in out
    assert "пятница" in out
    assert "31 июля" in out
    assert "17:25" in out


def test_time_ru_uses_month_in_genitive_case(frozen_now: datetime) -> None:
    # Russian dates use the genitive form: «31 июля», not nominative «июль».
    out = time_tool.get_time("ru", _MOSCOW)
    assert "июля" in out
    assert "июль" not in out  # nominative must not leak through


def test_get_time_en_localized_with_different_word_order(frozen_now: datetime) -> None:
    # EN uses "July 31" (month before day); RU uses "31 июля".
    out = time_tool.get_time("en", _MOSCOW)
    assert "It's" in out
    assert "Friday" in out
    assert "July 31" in out
    assert "17:25" in out


def test_get_time_respects_timezone_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same frozen instant rendered in UTC vs Moscow must differ by the offset.
    _freeze(monkeypatch, _FROZEN)
    ru_utc = time_tool.get_time("ru", UTC)
    ru_msk = time_tool.get_time("ru", _MOSCOW)
    assert "14:25" in ru_utc
    assert "17:25" in ru_msk


@pytest.mark.parametrize("offset_hours", [0, 5, 23])
def test_get_time_always_24_hour_two_digit(
    monkeypatch: pytest.MonkeyPatch, offset_hours: int
) -> None:
    _freeze(monkeypatch, _FROZEN + timedelta(hours=offset_hours))
    out = time_tool.get_time("ru", UTC)
    assert re.search(r"\b\d{2}:\d{2}\b", out)


def test_time_params_is_empty_object() -> None:
    # The tool takes no arguments: properties empty, nothing required.
    assert time_tool.TIME_PARAMS["type"] == "object"
    assert time_tool.TIME_PARAMS["additionalProperties"] is False
    assert time_tool.TIME_PARAMS["properties"] == {}
    assert time_tool.TIME_PARAMS["required"] == []
