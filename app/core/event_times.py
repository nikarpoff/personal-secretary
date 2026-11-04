"""
Start and end of events from the agent's words: local times, dates for all-day events.

Google keeps the end of an all-day event exclusive, while the agent names its last day:
the conversion happens here and nowhere else.
"""

from datetime import date, datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from app.core.config import CALENDAR_SETTINGS as CFG
from app.core.entities import Event
from app.core.exceptions import InvalidRequestError
from app.utils.time import Moment, local_day, local_midnight, parse_moment, utc_now

DEFAULT_DURATION = timedelta(minutes=CFG.DEFAULT_DURATION_MIN)
ONE_DAY = timedelta(days=1)


def listing_range(start: Optional[str], end: Optional[str], tz: ZoneInfo) -> Tuple[datetime, datetime]:
    """Range of a listing: today and a week ahead by default, a date covers its whole day."""
    first = parse_moment(start, tz) if start else local_day(utc_now(), tz)
    time_min = first if isinstance(first, datetime) else local_midnight(first, tz)

    if end:
        last = parse_moment(end, tz)
        time_max = last if isinstance(last, datetime) else local_midnight(last + ONE_DAY, tz)
    elif not isinstance(first, datetime):
        time_max = local_midnight(first + (ONE_DAY if start else 7 * ONE_DAY), tz)
    else:
        time_max = time_min + 7 * ONE_DAY

    if time_max <= time_min:
        raise InvalidRequestError("Range end must be after its start")
    return time_min, time_max


def new_span(start: str, end: Optional[str], duration_minutes: Optional[int], tz: ZoneInfo) -> Tuple[Moment, Moment]:
    """Start and Google end of a new event: an hour by default, one day for an all-day event."""
    first = parse_moment(start, tz)
    last = parse_moment(end, tz) if end else None

    if isinstance(first, datetime):
        duration = timedelta(minutes=duration_minutes) if duration_minutes else DEFAULT_DURATION
        return _checked(first, last or first + duration)
    return _checked(first, _all_day_end(first, last))


def changed_span(current: Event, start: Optional[str], end: Optional[str], tz: ZoneInfo) -> Tuple[Moment, Moment]:
    """Start and Google end of a moved event: the duration is kept when only the start moves."""
    if current.all_day:
        old_start: Moment = local_day(current.start, tz)
        old_length = local_day(current.end, tz) - old_start
    else:
        old_start, old_length = current.start, current.end - current.start

    first = parse_moment(start, tz) if start else old_start
    last = parse_moment(end, tz) if end else None
    # A timed event moved to a date, or back, gets the default length of its new kind
    switched = isinstance(first, datetime) != isinstance(old_start, datetime)

    if isinstance(first, datetime):
        return _checked(first, last or first + (DEFAULT_DURATION if switched else old_length))
    if last is None:
        return _checked(first, first + ONE_DAY if switched else first + old_length)
    return _checked(first, _all_day_end(first, last))


def _all_day_end(first: date, last: Optional[Moment]) -> date:
    if isinstance(last, datetime):
        raise InvalidRequestError("All-day event ends on a date, not a time")
    return (last or first) + ONE_DAY


def _checked(start: Moment, end: Moment) -> Tuple[Moment, Moment]:
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise InvalidRequestError("Start and end must both be times or both be dates")
    if end <= start:
        raise InvalidRequestError("Event must end after it starts")
    return start, end
