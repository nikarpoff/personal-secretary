"""Date ranges and durations for calendar operations."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import CALENDAR_SETTINGS as CFG
from app.core.entities import Event
from app.core.exceptions import InvalidRequestError
from app.core.upstream.calendar import all_day_end
from app.utils.time import Moment, local_day, local_midnight, parse_moment, utc_now


class EventTimes:
    def __init__(self, tz: ZoneInfo) -> None:
        self.tz = tz

    def range(self, start: str | None, end: str | None) -> tuple[datetime, datetime]:
        first = parse_moment(start, self.tz) if start else local_day(utc_now(), self.tz)
        time_min = first if isinstance(first, datetime) else local_midnight(first, self.tz)
        if end:
            last = parse_moment(end, self.tz)
            time_max = last if isinstance(last, datetime) else local_midnight(last + timedelta(days=1), self.tz)
        else:
            days = 1 if start and not isinstance(first, datetime) else 7
            time_max = time_min + timedelta(days=days)
        if time_max <= time_min:
            raise InvalidRequestError("Range end must be after its start")
        return time_min, time_max

    def create(self, start: str, end: str | None, duration_minutes: int | None) -> tuple[Moment, Moment]:
        first = parse_moment(start, self.tz)
        last = parse_moment(end, self.tz) if end else None
        if isinstance(first, datetime):
            last = last or first + timedelta(minutes=duration_minutes or CFG.DEFAULT_DURATION_MIN)
        else:
            last = all_day_end(first, self._last_day(last))
        self._check_span(first, last)
        return first, last

    def update(self, current: Event, start: str | None, end: str | None) -> tuple[Moment, Moment]:
        if current.all_day:
            old_start = local_day(current.start, self.tz)
            old_length = local_day(current.end, self.tz) - old_start
        else:
            old_start, old_length = current.start, current.end - current.start
        first = parse_moment(start, self.tz) if start else old_start
        last = parse_moment(end, self.tz) if end else None
        switched = isinstance(first, datetime) != isinstance(old_start, datetime)
        if isinstance(first, datetime):
            if last is None:
                length = timedelta(minutes=CFG.DEFAULT_DURATION_MIN) if switched else old_length
                last = first + length
        elif last is None:
            last = all_day_end(first, None) if switched else first + old_length
        else:
            last = all_day_end(first, self._last_day(last))
        self._check_span(first, last)
        return first, last

    @staticmethod
    def _last_day(moment: Moment | None) -> date | None:
        if isinstance(moment, datetime):
            raise InvalidRequestError("All-day event ends on a date, not a time")
        return moment

    @staticmethod
    def _check_span(start: Moment, end: Moment) -> None:
        if isinstance(start, datetime) != isinstance(end, datetime):
            raise InvalidRequestError("Start and end must both be times or both be dates")
        if end <= start:
            raise InvalidRequestError("Event must end after it starts")
