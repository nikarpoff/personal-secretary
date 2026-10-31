"""
Which reminders are due and how they read.

An event follows its own Google notifications when it has them, the REMINDER_MINUTES
otherwise; an important one adds IMPORTANT_REMINDER_MINUTES. A reminder whose time came
before the event was created is skipped: nobody wants "tomorrow" for an event made tonight.
After downtime only the reminder closest to the start goes out, the earlier ones are
marked as sent along with it.
"""

import html
import math

from datetime import datetime, timedelta
from typing import List, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from app.core.clock import iso_utc
from app.core.entities import Event, Reminder

Mark = Tuple[str, str, int]

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
DESCRIPTION_CHARS = 300             # the rest of the description is one tap away in the calendar


def reminder_offsets(event: Event, default_minutes: Sequence[int], important_minutes: Sequence[int]) -> Tuple[int, ...]:
    """Minutes before the start, the earliest first."""
    offsets = set(default_minutes if event.own_reminders is None else event.own_reminders)
    if event.important:
        offsets.update(important_minutes)
    return tuple(sorted(offsets, reverse=True))


def due_reminders(
    events: Sequence[Event],
    sent: Set[Mark],
    now: datetime,
    default_minutes: Sequence[int],
    important_minutes: Sequence[int],
) -> List[Reminder]:
    """Reminders to send now, one per event at most."""
    due: List[Reminder] = []
    for event in events:
        # All-day events have no hour to count from: the morning briefing covers them
        if event.all_day or event.start <= now:
            continue

        start_key = iso_utc(event.start)
        fired = []
        for minutes in reminder_offsets(event, default_minutes, important_minutes):
            fire_at = event.start - timedelta(minutes=minutes)
            if fire_at > now or (event.id, start_key, minutes) in sent:
                continue
            if event.created is not None and fire_at < event.created:
                continue
            fired.append(minutes)

        if fired:
            marks = tuple((event.id, start_key, minutes) for minutes in fired)
            due.append(Reminder(event=event, minutes=min(fired), marks=marks))
    return due


def format_reminder(reminder: Reminder, now: datetime, tz: ZoneInfo) -> str:
    """Telegram HTML text of a reminder."""
    event = reminder.event
    start = event.start.astimezone(tz)
    end = event.end.astimezone(tz)

    mark = "❗️" if event.important else "⏰"
    lines = [
        f"{mark} <b>{html.escape(event.title)}</b>",
        f"{_time_left(event.start - now)} · {_day_label(start, now.astimezone(tz))} {start:%H:%M}–{end:%H:%M}",
    ]
    if event.location:
        lines.append(f"📍 {html.escape(event.location)}")
    if description := " ".join(event.description.split()):
        if len(description) > DESCRIPTION_CHARS:
            description = description[:DESCRIPTION_CHARS - 1] + "…"
        lines.append(f"📝 {html.escape(description)}")
    if event.html_link:
        lines.append(f'<a href="{html.escape(event.html_link, quote=True)}">Открыть в календаре</a>')
    return "\n".join(lines)


def _time_left(delta: timedelta) -> str:
    minutes = max(1, math.ceil(delta.total_seconds() / 60))
    if minutes < 60:
        return f"через {minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"через {hours} ч {minutes} мин" if minutes else f"через {hours} ч"
    days, hours = divmod(hours, 24)
    return f"через {days} д {hours} ч" if hours else f"через {days} д"


def _day_label(moment: datetime, today: datetime) -> str:
    days = (moment.date() - today.date()).days
    if days == 0:
        return "сегодня"
    if days == 1:
        return "завтра"
    return f"{moment:%d.%m} ({WEEKDAYS[moment.weekday()]})"

