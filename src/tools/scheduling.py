"""Shared scheduling core for the ``timer``, ``alarm_clock`` and ``reminder`` tools.

All three tools revolve around the same idea: an event that fires at a future
instant, optionally repeating on chosen weekdays. This module owns that logic:

  * :class:`Recurrence` — one-shot vs. a set of weekdays (Mon=0 … Sun=6).
  * :class:`ScheduledEvent` — a single scheduled item (kind + label + time).
  * :class:`SchedulerStore` — SQLite persistence (CRUD, thread-safe).
  * :class:`Scheduler` — background thread that fires due events via a callback
    and advances repeating ones to their next occurrence.

Times are stored as UTC ISO-8601 strings; the user-facing clock time is
rendered in a configurable local timezone, 24-hour. ``next_occurrence`` is the
single place that maps "08:00 on weekdays" to a concrete UTC datetime.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from localization import LocaleStr

log = logging.getLogger("voice-agent.scheduling")

# ── kinds: stable identifiers shared with the model and the chime builder ───

KIND_TIMER = "timer"
KIND_ALARM = "alarm"
KIND_REMINDER = "reminder"

_WEEKDAY_ABBR_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_WEEKDAY_ABBR_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ── recurrence ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Recurrence:
    """Repetition rule.

    ``days is None`` → one-shot (fire once, then delete).
    A non-empty set → repeat on those weekdays (full set == daily).
    """

    days: frozenset[int] | None

    @property
    def is_repeating(self) -> bool:
        return self.days is not None

    @classmethod
    def once(cls) -> Recurrence:
        return cls(days=None)

    @classmethod
    def daily(cls) -> Recurrence:
        return cls(days=frozenset(range(7)))

    @classmethod
    def weekdays(cls) -> Recurrence:
        return cls(days=frozenset(range(5)))  # Mon–Fri

    @classmethod
    def weekends(cls) -> Recurrence:
        return cls(days=frozenset({5, 6}))  # Sat, Sun

    @classmethod
    def weekly(cls, days: list[int]) -> Recurrence:
        selected = frozenset(days)
        if not selected:
            raise ValueError("weekdays must not be empty")
        if any(not 0 <= d <= 6 for d in selected):
            raise ValueError("weekday must be in 0..6 (Mon=0)")
        return cls(days=selected)

    @classmethod
    def from_params(cls, recurrence: str | None, weekdays: list[int] | None) -> Recurrence:
        """Build a recurrence from the tool-call argument pair."""
        mode = (recurrence or "once").lower()
        if mode == "once":
            return cls.once()
        if mode == "daily":
            return cls.daily()
        if mode == "weekdays":
            return cls.weekdays()
        if mode == "weekends":
            return cls.weekends()
        if mode == "weekly":
            if not weekdays:
                raise ValueError("weekly recurrence requires weekdays")
            return cls.weekly([int(d) for d in weekdays])
        raise ValueError(f"unknown recurrence {recurrence!r}")


# ── event ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScheduledEvent:
    """A persisted scheduled item."""

    id: int | None
    kind: str
    label: str | None
    fire_at_utc: datetime
    recurrence: Recurrence
    enabled: bool
    created_at: datetime


# ── pure helpers ─────────────────────────────────────────────────────────────


def next_occurrence(
    hour: int, minute: int, recurrence: Recurrence, *, tz: tzinfo, now_utc: datetime
) -> datetime:
    """Earliest UTC datetime strictly after ``now`` matching the rule.

    For a one-shot rule every weekday is allowed (it fires once); for a
    repeating rule only ``recurrence.days`` count.
    """
    allowed = recurrence.days if recurrence.days is not None else frozenset(range(7))
    now_local = now_utc.astimezone(tz)
    base = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    for delta in range(0, 8):  # at most a full week ahead
        day = base + timedelta(days=delta)
        if day.weekday() not in allowed:
            continue
        candidate = day.replace(hour=hour, minute=minute)
        candidate_utc = candidate.astimezone(UTC)
        if candidate_utc > now_utc:
            return candidate_utc
    raise ValueError("no valid future occurrence within a week")


def format_local(dt_utc: datetime, tz: tzinfo) -> str:
    """Render an instant as a 24-hour ``HH:MM`` in the local timezone."""
    return dt_utc.astimezone(tz).strftime("%H:%M")


def describe_recurrence(recurrence: Recurrence, language: str) -> str:
    """Short localised phrase; empty for a one-shot rule."""
    days = recurrence.days
    if days is None:
        return ""
    if days == frozenset(range(7)):
        return LocaleStr(ru="каждый день", en="every day").render(language)
    if days == frozenset(range(5)):
        return LocaleStr(ru="по будням", en="on weekdays").render(language)
    if days == frozenset({5, 6}):
        return LocaleStr(ru="по выходным", en="on weekends").render(language)
    names = _WEEKDAY_ABBR_RU if language == "ru" else _WEEKDAY_ABBR_EN
    ordered = ", ".join(names[d] for d in sorted(days))
    return LocaleStr(ru="по {days}", en="on {days}").render(language, days=ordered)


def validate_clock(hour: object, minute: object, language: str) -> tuple[int, int] | str:
    """Coerce ``hour``/``minute`` to ints in range or return a localised error."""
    try:
        h = int(hour)  # type: ignore[arg-type]
        m = int(minute)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _MSG_BAD_CLOCK.render(language)
    if not 0 <= h <= 23 or not 0 <= m <= 59:
        return _MSG_BAD_CLOCK.render(language)
    return h, m


# ── spoken clock-time parsing ────────────────────────────────────────────────
#
# STT delivers times as number words. The LLM mis-converts them — e.g. it reads
# Russian "двадцать тридцать" as 23:30, grabbing the "двадцать три" substring.
# Parsing the raw words here is deterministic and language-agnostic: whitespace
# tokens "двадцать"/"тридцать" (or "twenty"/"thirty") are distinct → 20 and 30.
# Russian and English words share one table (different alphabets never collide);
# unknown tokens (fillers like "часов", "hundred", "at", "o'clock") are ignored.

_ONES: dict[str, int] = {
    # Russian
    "ноль": 0,
    "нуль": 0,
    "один": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    # English
    "zero": 0,
    "oh": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
_TEENS: dict[str, int] = {
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS: dict[str, int] = {
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
}


def parse_clock_text(text: str) -> tuple[int, int] | None:
    """Parse a spoken/digit clock time, e.g. ``"двадцать тридцать"`` → ``(20, 30)``.

    Language-agnostic. Russian: "восемь ноль ноль" → 8:00, "девятнадцать
    тридцать" → 19:30. English: "eight thirty" → 8:30, "twenty thirty" → 20:30,
    "eight oh five" → 8:05, "eight hundred" → 8:00. Digit forms ("20:30",
    "8 00") also work. Returns ``(hour, minute)`` or ``None`` when nothing is
    parseable or the result is out of range. Relative forms ("без четверти
    девять", "quarter to nine") are not supported.
    """
    tokens = re.findall(r"[a-zа-яё0-9]+", text.lower())
    values: list[int] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.isdigit():
            values.append(int(tok))
            i += 1
            continue
        if tok in _TENS:
            value = _TENS[tok]
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is not None and _ONES.get(nxt, 0) > 0:  # "двадцать один" → 21
                value += _ONES[nxt]
                i += 1
            values.append(value)
        elif tok in _TEENS:
            values.append(_TEENS[tok])
        elif tok in _ONES:
            values.append(_ONES[tok])
        # any other token is a filler and is ignored
        i += 1

    if not values:
        return None
    hour = values[0]
    # Remaining values are the minute's digits: [30] → 30, [0, 5] → 05, [0, 0] → 00.
    minute = int("".join(str(v) for v in values[1:])) if len(values) > 1 else 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def resolve_clock(args: dict[str, Any], language: str) -> tuple[int, int] | str:
    """Resolve the clock time from tool args: prefer the raw spoken ``time``.

    The model passes the time exactly as the user said it (number words) in
    ``time``; we parse that deterministically. ``hour``/``minute`` ints are a
    fallback when ``time`` is absent or unparseable.
    """
    time_text = args.get("time")
    if isinstance(time_text, str) and time_text.strip():
        parsed = parse_clock_text(time_text)
        if parsed is not None:
            return parsed
    return validate_clock(args.get("hour"), args.get("minute"), language)


# ── fire-message text injected into the conversation when an event fires ─────


_FIRE_TIMER = LocaleStr(
    ru="Таймер{label} истёк. Коротко сообщи пользователю.",
    en="Timer{label} elapsed. Tell the user briefly.",
)
_FIRE_ALARM = LocaleStr(
    ru="Звонит будильник на {time}{label}. Коротко разбуди пользователя.",
    en="Alarm ringing for {time}{label}. Briefly wake the user.",
)
_FIRE_REMINDER = LocaleStr(
    ru="Напоминание: {message}. Коротко сообщи пользователю.",
    en="Reminder: {message}. Tell the user briefly.",
)

_MSG_BAD_CLOCK = LocaleStr(
    ru="Некорректное время: час 0–23, минуты 0–59 (24-часовой формат).",
    en="Invalid time: hour 0–23, minutes 0–59 (24-hour format).",
)


def fire_message(event: ScheduledEvent, language: str, tz: tzinfo) -> str:
    """Build the user-role text the assistant relays when an event fires."""
    label_part = f" «{event.label}»" if event.label else ""
    if event.kind == KIND_ALARM:
        return _FIRE_ALARM.render(
            language, time=format_local(event.fire_at_utc, tz), label=label_part
        )
    if event.kind == KIND_REMINDER:
        return _FIRE_REMINDER.render(language, message=event.label or "")
    return _FIRE_TIMER.render(language, label=label_part)


# ── SQLite store ────────────────────────────────────────────────────────────


_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,
    label      TEXT,
    fire_at    TEXT    NOT NULL,
    weekdays   TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL
)
"""


class SchedulerStore:
    """Thread-safe SQLite persistence for scheduled events.

    The connection is opened lazily (on first use) so constructing a store —
    e.g. while wiring the app in tests that never start the scheduler — does
    not touch the filesystem.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def ensure(self) -> sqlite3.Connection:
        """Open the connection lazily; safe to call from any thread."""
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    # ── write ──

    def add(self, event: ScheduledEvent) -> ScheduledEvent:
        weekdays = (
            json.dumps(sorted(event.recurrence.days)) if event.recurrence.days is not None else None
        )
        with self._lock:
            conn = self.ensure()
            cur = conn.execute(
                "INSERT INTO scheduled_events "
                "(kind, label, fire_at, weekdays, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.kind,
                    event.label,
                    event.fire_at_utc.isoformat(),
                    weekdays,
                    1 if event.enabled else 0,
                    event.created_at.isoformat(),
                ),
            )
            rid = cur.lastrowid
        assert rid is not None
        return dataclasses.replace(event, id=rid)

    def set_fire_at(self, event_id: int, fire_at: datetime) -> None:
        with self._lock:
            self.ensure().execute(
                "UPDATE scheduled_events SET fire_at=? WHERE id=?",
                (fire_at.isoformat(), event_id),
            )

    def delete(self, event_id: int) -> bool:
        with self._lock:
            cur = self.ensure().execute("DELETE FROM scheduled_events WHERE id=?", (event_id,))
            return cur.rowcount > 0

    def delete_kind(self, kind: str) -> int:
        with self._lock:
            cur = self.ensure().execute("DELETE FROM scheduled_events WHERE kind=?", (kind,))
            return cur.rowcount

    # ── read ──

    def due(self, now_utc: datetime) -> list[ScheduledEvent]:
        with self._lock:
            rows = (
                self.ensure()
                .execute(
                    "SELECT * FROM scheduled_events WHERE enabled=1 AND fire_at<=? "
                    "ORDER BY fire_at",
                    (now_utc.isoformat(),),
                )
                .fetchall()
            )
        return [self._row_to_event(r) for r in rows]

    def list(self, kind: str | None = None) -> list[ScheduledEvent]:
        with self._lock:
            if kind is None:
                rows = (
                    self.ensure()
                    .execute("SELECT * FROM scheduled_events ORDER BY fire_at")
                    .fetchall()
                )
            else:
                rows = (
                    self.ensure()
                    .execute(
                        "SELECT * FROM scheduled_events WHERE kind=? ORDER BY fire_at",
                        (kind,),
                    )
                    .fetchall()
                )
        return [self._row_to_event(r) for r in rows]

    def count(self, kind: str | None = None) -> int:
        with self._lock:
            if kind is None:
                row = self.ensure().execute("SELECT COUNT(*) AS n FROM scheduled_events").fetchone()
            else:
                row = (
                    self.ensure()
                    .execute(
                        "SELECT COUNT(*) AS n FROM scheduled_events WHERE kind=?",
                        (kind,),
                    )
                    .fetchone()
                )
        return int(row["n"]) if row is not None else 0

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> ScheduledEvent:
        weekdays_raw = row["weekdays"]
        weekdays: frozenset[int] | None = (
            frozenset(json.loads(weekdays_raw)) if weekdays_raw else None
        )
        return ScheduledEvent(
            id=row["id"],
            kind=row["kind"],
            label=row["label"],
            fire_at_utc=datetime.fromisoformat(row["fire_at"]),
            recurrence=Recurrence(days=weekdays),
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# ── scheduler ───────────────────────────────────────────────────────────────

NowFn = Callable[[], datetime]


class Scheduler:
    """Owns the background poll loop; the tools add events, the loop fires them.

    ``on_fire`` is set by the realtime client (so a fired event can be injected
    into the live conversation). ``tick`` is extracted for deterministic tests.
    """

    def __init__(
        self,
        store: SchedulerStore,
        *,
        tz: tzinfo,
        poll_interval: float = 1.0,
        now_utc: NowFn | None = None,
    ) -> None:
        self._store = store
        self._tz = tz
        self._poll_interval = poll_interval
        self._now_utc = now_utc or (lambda: datetime.now(UTC))
        self.on_fire: Callable[[ScheduledEvent], None] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def tz(self) -> tzinfo:
        return self._tz

    @property
    def store(self) -> SchedulerStore:
        return self._store

    # ── lifecycle ──

    def start(self) -> None:
        if self._thread is not None:
            return
        self._store.ensure()  # connect before the worker thread touches it
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._thread = None

    def close(self) -> None:
        self.stop()
        self._store.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - never kill the loop
                log.exception("scheduler tick failed")
            self._stop.wait(self._poll_interval)

    # ── the actual work ──

    def tick(self, now_utc: datetime | None = None) -> list[ScheduledEvent]:
        """Fire every due event; advance repeating ones, delete one-shots.

        Returns the list of events that fired this tick (for tests).
        """
        now = now_utc or self._now_utc()
        fired: list[ScheduledEvent] = []
        for event in self._store.due(now):
            fired.append(event)
            if self.on_fire is not None:
                try:
                    self.on_fire(event)
                except Exception:  # pragma: no cover - one bad fire must not kill
                    log.exception("on_fire failed for %s", event.kind)
            self._advance(event, now)
        return fired

    def _advance(self, event: ScheduledEvent, now_utc: datetime) -> None:
        assert event.id is not None
        if not event.recurrence.is_repeating:
            self._store.delete(event.id)
            return
        local = event.fire_at_utc.astimezone(self._tz)
        nxt = next_occurrence(
            local.hour, local.minute, event.recurrence, tz=self._tz, now_utc=now_utc
        )
        self._store.set_fire_at(event.id, nxt)

    # ── tool-facing factory helpers ──

    def add_timer(self, seconds: float, label: str | None) -> ScheduledEvent:
        now = self._now_utc()
        return self._store.add(
            ScheduledEvent(
                id=None,
                kind=KIND_TIMER,
                label=label,
                fire_at_utc=now + timedelta(seconds=seconds),
                recurrence=Recurrence.once(),
                enabled=True,
                created_at=now,
            )
        )

    def add_alarm(
        self, hour: int, minute: int, recurrence: Recurrence, label: str | None
    ) -> ScheduledEvent:
        now = self._now_utc()
        return self._store.add(
            ScheduledEvent(
                id=None,
                kind=KIND_ALARM,
                label=label,
                fire_at_utc=next_occurrence(hour, minute, recurrence, tz=self._tz, now_utc=now),
                recurrence=recurrence,
                enabled=True,
                created_at=now,
            )
        )

    def add_reminder(
        self, hour: int, minute: int, message: str, recurrence: Recurrence
    ) -> ScheduledEvent:
        now = self._now_utc()
        return self._store.add(
            ScheduledEvent(
                id=None,
                kind=KIND_REMINDER,
                label=message,
                fire_at_utc=next_occurrence(hour, minute, recurrence, tz=self._tz, now_utc=now),
                recurrence=recurrence,
                enabled=True,
                created_at=now,
            )
        )

    def cancel(self, event_id: int) -> bool:
        return self._store.delete(event_id)

    def cancel_kind(self, kind: str) -> int:
        return self._store.delete_kind(kind)

    def list(self, kind: str | None = None) -> list[ScheduledEvent]:
        return self._store.list(kind)


__all__ = [
    "KIND_TIMER",
    "KIND_ALARM",
    "KIND_REMINDER",
    "Recurrence",
    "ScheduledEvent",
    "SchedulerStore",
    "Scheduler",
    "next_occurrence",
    "format_local",
    "describe_recurrence",
    "validate_clock",
    "parse_clock_text",
    "resolve_clock",
    "fire_message",
]
