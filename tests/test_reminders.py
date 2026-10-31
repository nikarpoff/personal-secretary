from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.clock import iso_utc
from app.core.entities import Event, Note
from app.core.reminders import due_reminders, format_reminder, reminder_offsets

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def _event(minutes_to_start: int, **kwargs) -> Event:
    start = NOW + timedelta(minutes=minutes_to_start)
    values = dict(
        id="ev1",
        title="Созвон <команды>",
        start=start,
        end=start + timedelta(hours=1),
        created=NOW - timedelta(days=7),
    )
    values.update(kwargs)
    return Event(**values)


def _due(events, sent=frozenset(), default=(15, ), important=(1440, 120)):
    return due_reminders(events, set(sent), NOW, list(default), list(important))


def test_reminder_offsets_default_own_and_important():
    assert reminder_offsets(_event(60), [15], [1440, 120]) == (15, )
    assert reminder_offsets(_event(60, own_reminders=(10, 60)), [15], [1440]) == (60, 10)
    assert reminder_offsets(_event(60, own_reminders=()), [15], [1440]) == ()
    assert reminder_offsets(_event(60, important=True, own_reminders=(30, )), [15], [1440, 120]) == (1440, 120, 30)


@pytest.mark.parametrize(
    "minutes_to_start, expected",
    [(20, []), (15, [15]), (5, [15])],
    ids=["too-early", "on-time", "late-after-downtime"],
)
def test_due_reminders_default_offset(minutes_to_start, expected):
    assert [r.minutes for r in _due([_event(minutes_to_start)])] == expected


def test_due_reminders_collapses_passed_offsets_into_the_closest():
    event = _event(10, important=True)

    reminders = _due([event])

    assert len(reminders) == 1
    assert reminders[0].minutes == 15
    assert {mark[2] for mark in reminders[0].marks} == {1440, 120, 15}


def test_due_reminders_skips_sent_started_and_all_day():
    event = _event(10)
    sent = {(event.id, iso_utc(event.start), 15)}

    assert _due([event], sent) == []
    assert _due([_event(-5)]) == []
    assert _due([_event(10, all_day=True)]) == []


def test_due_reminders_skips_offsets_before_creation():
    # Created 3 hours before the start: "a day before" makes no sense, "2 hours before" does
    event = _event(110, important=True, created=NOW - timedelta(minutes=70))

    reminders = _due([event])

    assert [r.minutes for r in reminders] == [120]
    assert [mark[2] for mark in reminders[0].marks] == [120]


def test_due_reminders_new_start_fires_again():
    event = _event(10)
    moved_from = event.start - timedelta(days=1)
    sent = {(event.id, iso_utc(moved_from), 15)}

    assert [r.minutes for r in _due([event], sent)] == [15]


def test_format_reminder_escapes_and_lists_notes():
    event = _event(10, important=True, location="Zoom", html_link="https://calendar.google.com/e?a=1&b=2")
    reminder = _due([event])[0]
    notes = [Note(id=i, title=f"Заметка {i}", content="...") for i in range(5)]

    text = format_reminder(reminder, notes, NOW, TZ, max_notes=3)

    assert text.startswith("❗️ <b>Созвон &lt;команды&gt;</b>")
    assert "через 10 мин · сегодня 12:10–13:10" in text
    assert "📍 Zoom" in text
    assert "«Заметка 2» и ещё 2" in text
    assert "a=1&amp;b=2" in text
