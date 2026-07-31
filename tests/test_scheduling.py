"""Tests for the shared scheduling core (no threads required for the logic)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import scheduling
from tools.scheduling import (
    Recurrence,
    ScheduledEvent,
    Scheduler,
    SchedulerStore,
)

# +05:00 mirrors the deployment timezone; lets us assert UTC conversions.
_TZ = timezone(timedelta(hours=5))
_UTC = UTC

# 2026-07-30 is a Thursday; Fri=07-31, Sat=08-01, Sun=08-02, Mon=08-03, Wed=08-05.
_THU_MORNING = datetime(2026, 7, 30, 2, 0, tzinfo=_UTC)  # 07:00 +05 Thu
_THU_AFTER = datetime(2026, 7, 30, 4, 0, tzinfo=_UTC)  # 09:00 +05 Thu
_FRI_AFTER = datetime(2026, 7, 31, 4, 0, tzinfo=_UTC)  # 09:00 +05 Fri


# ── next_occurrence ──────────────────────────────────────────────────────────


def test_next_occurrence_daily_today_when_future() -> None:
    nxt = scheduling.next_occurrence(8, 0, Recurrence.daily(), tz=_TZ, now_utc=_THU_MORNING)
    assert nxt == datetime(2026, 7, 30, 3, 0, tzinfo=_UTC)  # 08:00 +05 today


def test_next_occurrence_daily_rolls_to_tomorrow_when_past() -> None:
    nxt = scheduling.next_occurrence(8, 0, Recurrence.daily(), tz=_TZ, now_utc=_THU_AFTER)
    assert nxt == datetime(2026, 7, 31, 3, 0, tzinfo=_UTC)


def test_next_occurrence_weekdays_skips_weekend() -> None:
    nxt = scheduling.next_occurrence(8, 0, Recurrence.weekdays(), tz=_TZ, now_utc=_FRI_AFTER)
    assert nxt == datetime(2026, 8, 3, 3, 0, tzinfo=_UTC)  # Mon 08:00 +05


def test_next_occurrence_weekdays_today_when_future() -> None:
    nxt = scheduling.next_occurrence(8, 0, Recurrence.weekdays(), tz=_TZ, now_utc=_THU_MORNING)
    assert nxt == datetime(2026, 7, 30, 3, 0, tzinfo=_UTC)  # Thu 08:00


def test_next_occurrence_weekends_picks_saturday() -> None:
    nxt = scheduling.next_occurrence(8, 0, Recurrence.weekends(), tz=_TZ, now_utc=_THU_MORNING)
    assert nxt == datetime(2026, 8, 1, 3, 0, tzinfo=_UTC)  # Sat 08:00


def test_next_occurrence_weekly_picks_next_matching_weekday() -> None:
    # Wed only (weekday 2), now Thursday morning → next Wed.
    nxt = scheduling.next_occurrence(8, 0, Recurrence.weekly([2]), tz=_TZ, now_utc=_THU_MORNING)
    assert nxt == datetime(2026, 8, 5, 3, 0, tzinfo=_UTC)


def test_next_occurrence_once_today_future_else_tomorrow() -> None:
    today = scheduling.next_occurrence(8, 0, Recurrence.once(), tz=_TZ, now_utc=_THU_MORNING)
    assert today == datetime(2026, 7, 30, 3, 0, tzinfo=_UTC)
    tomorrow = scheduling.next_occurrence(8, 0, Recurrence.once(), tz=_TZ, now_utc=_THU_AFTER)
    assert tomorrow == datetime(2026, 7, 31, 3, 0, tzinfo=_UTC)


def test_next_occurrence_rejects_empty_weekly() -> None:
    with pytest.raises(ValueError):
        Recurrence.weekly([])
    with pytest.raises(ValueError):
        Recurrence.weekly([7])


# ── formatting helpers ───────────────────────────────────────────────────────


def test_format_local_is_24_hour() -> None:
    # 03:00 UTC == 08:00 +05 → "08:00", never "8 AM".
    assert scheduling.format_local(datetime(2026, 7, 30, 3, 0, tzinfo=_UTC), _TZ) == "08:00"
    assert scheduling.format_local(datetime(2026, 7, 30, 14, 30, tzinfo=_UTC), _TZ) == "19:30"


def test_describe_recurrence() -> None:
    assert scheduling.describe_recurrence(Recurrence.once(), "ru") == ""
    assert scheduling.describe_recurrence(Recurrence.daily(), "ru") == "каждый день"
    assert scheduling.describe_recurrence(Recurrence.weekdays(), "ru") == "по будням"
    assert scheduling.describe_recurrence(Recurrence.weekends(), "en") == "on weekends"
    assert scheduling.describe_recurrence(Recurrence.weekly([0, 2]), "ru") == "по пн, ср"
    assert scheduling.describe_recurrence(Recurrence.weekly([0, 2]), "en") == "on Mon, Wed"


def test_validate_clock() -> None:
    assert scheduling.validate_clock(8, 0, "ru") == (8, 0)
    assert scheduling.validate_clock("13", "30", "ru") == (13, 30)
    assert isinstance(scheduling.validate_clock(25, 0, "ru"), str)
    assert isinstance(scheduling.validate_clock(8, 60, "en"), str)
    assert isinstance(scheduling.validate_clock(None, 0, "ru"), str)


# ── parse_clock_text (spoken number words → hour/minute) ─────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Russian — the case the LLM mis-reads ("двадцать тридцать" ≠ 23:30).
        ("двадцать тридцать", (20, 30)),
        ("Двадцать тридцать", (20, 30)),
        ("восемь ноль ноль", (8, 0)),
        ("восемь ноль пять", (8, 5)),
        ("девятнадцать тридцать", (19, 30)),
        ("восемь тридцать", (8, 30)),
        ("двадцать один", (21, 0)),
        ("двадцать один тридцать", (21, 30)),
        ("ноль пять", (0, 5)),
        ("в восемь часов тридцать минут", (8, 30)),
        # English.
        ("eight thirty", (8, 30)),
        ("twenty thirty", (20, 30)),
        ("nineteen thirty", (19, 30)),
        ("eight oh five", (8, 5)),
        ("eight hundred", (8, 0)),
        ("twenty one", (21, 0)),
        ("two thirty pm", (2, 30)),
        # Digits.
        ("20:30", (20, 30)),
        ("8:00", (8, 0)),
        ("20 30", (20, 30)),
    ],
)
def test_parse_clock_text(text: str, expected: tuple[int, int]) -> None:
    assert scheduling.parse_clock_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "когда-нибудь",
        "sometime",
        "тридцать тридцать",  # hour 30 out of range
    ],
)
def test_parse_clock_text_invalid(text: str) -> None:
    assert scheduling.parse_clock_text(text) is None


# ── fire_message ─────────────────────────────────────────────────────────────


def _ev(kind: str, **kw: object) -> ScheduledEvent:
    base = dict(
        id=1,
        kind=kind,
        label=None,
        fire_at_utc=datetime(2026, 7, 30, 3, 0, tzinfo=_UTC),
        recurrence=Recurrence.once(),
        enabled=True,
        created_at=datetime(2026, 7, 30, 2, 0, tzinfo=_UTC),
    )
    base.update(kw)
    return ScheduledEvent(**base)  # type: ignore[arg-type]


def test_fire_message_reminder_includes_text() -> None:
    msg = scheduling.fire_message(_ev("reminder", label="выпить таблетку"), "ru", _TZ)
    assert "Напоминание" in msg and "выпить таблетку" in msg
    assert "[СИСТЕМНОЕ СОБЫТИЕ" in msg


def test_fire_message_alarm_includes_time() -> None:
    msg = scheduling.fire_message(_ev("alarm"), "ru", _TZ)
    assert "08:00" in msg and "будильник" in msg.lower()
    assert "[СИСТЕМНОЕ СОБЫТИЕ" in msg


def test_fire_message_timer_includes_label() -> None:
    msg = scheduling.fire_message(_ev("timer", label="чай"), "ru", _TZ)
    assert "таймер" in msg and "«чай»" in msg and "[СИСТЕМНОЕ СОБЫТИЕ" in msg


# ── SchedulerStore (SQLite, in-memory) ───────────────────────────────────────


def test_store_add_round_trip_preserves_recurrence() -> None:
    store = SchedulerStore(":memory:")
    event = store.add(_ev("alarm", label="work", recurrence=Recurrence.weekdays()))
    assert event.id == 1
    listed = store.list()
    assert len(listed) == 1
    got = listed[0]
    assert got.kind == "alarm"
    assert got.label == "work"
    assert got.recurrence == Recurrence.weekdays()
    assert got.enabled is True
    store.close()


def test_store_due_and_count() -> None:
    store = SchedulerStore(":memory:")
    now = datetime(2026, 7, 30, 12, 0, tzinfo=_UTC)
    past = store.add(_ev("timer", fire_at_utc=now - timedelta(minutes=5)))
    store.add(_ev("timer", fire_at_utc=now + timedelta(minutes=5)))
    due = store.due(now)
    assert [e.id for e in due] == [past.id]
    assert store.count() == 2
    assert store.count("timer") == 2
    assert store.count("alarm") == 0
    store.close()


def test_store_set_fire_at_delete_delete_kind() -> None:
    store = SchedulerStore(":memory:")
    now = datetime(2026, 7, 30, 12, 0, tzinfo=_UTC)
    e = store.add(_ev("alarm"))
    store.set_fire_at(e.id, now + timedelta(days=1))  # type: ignore[arg-type]
    assert store.list()[0].fire_at_utc == now + timedelta(days=1)
    assert store.delete(e.id) is True  # type: ignore[arg-type]
    assert store.delete(999) is False
    store.add(_ev("timer"))
    store.add(_ev("timer"))
    assert store.delete_kind("timer") == 2
    store.close()


def test_store_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    a = SchedulerStore(path)
    a.add(_ev("reminder", label="pill"))
    a.close()
    b = SchedulerStore(path)
    rows = b.list()
    assert len(rows) == 1
    assert rows[0].kind == "reminder"
    assert rows[0].label == "pill"
    b.close()


# ── Scheduler.tick ───────────────────────────────────────────────────────────


def _scheduler(now: datetime) -> Scheduler:
    return Scheduler(SchedulerStore(":memory:"), tz=_TZ, now_utc=lambda: now)


def test_add_timer_and_add_alarm_store_correctly() -> None:
    sched = _scheduler(_THU_MORNING)
    timer = sched.add_timer(30, "tea")
    assert timer.kind == "timer"
    assert timer.recurrence.is_repeating is False
    assert timer.fire_at_utc == _THU_MORNING + timedelta(seconds=30)
    assert timer.label == "tea"

    alarm = sched.add_alarm(8, 0, Recurrence.weekdays(), None)
    assert alarm.kind == "alarm"
    assert alarm.fire_at_utc == datetime(2026, 7, 30, 3, 0, tzinfo=_UTC)
    assert alarm.recurrence == Recurrence.weekdays()
    assert sched.store.count() == 2
    sched.close()


def test_tick_fires_oneshot_then_deletes() -> None:
    now = _THU_MORNING
    sched = _scheduler(now)
    fired: list[ScheduledEvent] = []
    sched.on_fire = fired.append
    # A timer that already elapsed 5 min ago.
    sched.store.add(_ev("timer", fire_at_utc=now - timedelta(minutes=5)))

    result = sched.tick(now)
    assert len(result) == 1
    assert fired == result
    assert sched.store.count() == 0  # one-shot deleted after firing
    sched.close()


def test_tick_advances_repeating_without_deleting() -> None:
    now = _THU_AFTER  # 09:00 Thu
    sched = _scheduler(now)
    fired: list[ScheduledEvent] = []
    sched.on_fire = fired.append
    # Weekday alarm that elapsed earlier today (08:00 has passed).
    sched.store.add(
        _ev(
            "alarm",
            recurrence=Recurrence.weekdays(),
            fire_at_utc=datetime(2026, 7, 30, 3, 0, tzinfo=_UTC),  # 08:00 +05
        )
    )

    result = sched.tick(now)
    assert len(result) == 1
    assert sched.store.count() == 1  # still present, advanced to next weekday
    advanced = sched.store.list()[0]
    # Fri 08:00 == 2026-07-31T03:00Z, strictly after now (09:00 Thu).
    assert advanced.fire_at_utc == datetime(2026, 7, 31, 3, 0, tzinfo=_UTC)
    assert advanced.fire_at_utc > now
    sched.close()


def test_tick_ignores_future_events() -> None:
    now = _THU_MORNING
    sched = _scheduler(now)
    fired: list[ScheduledEvent] = []
    sched.on_fire = fired.append
    sched.add_timer(30, None)  # 30 s in the future
    assert sched.tick(now) == []
    assert fired == []
    sched.close()


def test_scheduler_thread_fires_due_event() -> None:
    now = _THU_MORNING
    sched = _scheduler(now)
    fired: list[ScheduledEvent] = []
    sched.on_fire = fired.append
    sched.store.add(_ev("timer", fire_at_utc=now - timedelta(seconds=1)))

    sched.start()
    try:
        for _ in range(100):
            if fired:
                break
            time.sleep(0.01)
    finally:
        sched.stop()
    assert len(fired) == 1
    assert sched.store.count() == 0
