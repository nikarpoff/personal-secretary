"""
Time parsing for the agent: it speaks in the owner's local time, Google and the mirror keep offsets
"""

import re

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

from app.core.exceptions import InvalidRequestError

DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Moment = Union[date, datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_moment(value: str, tz: ZoneInfo) -> Moment:
    """A date for an all-day value, otherwise an aware datetime; naive input is local time."""
    value = value.strip()
    if DATE_ONLY.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError as e:
            raise InvalidRequestError(f"Bad date '{value}': expected YYYY-MM-DD") from e

    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise InvalidRequestError(f"Bad time '{value}': expected ISO 8601, e.g. 2026-11-03T10:00") from e

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.replace(microsecond=0)


def parse_bound(value: Optional[str], tz: ZoneInfo, default: datetime, end: bool = False) -> datetime:
    """Range bound as an aware datetime: a date covers the whole day."""
    if not value:
        return default
    moment = parse_moment(value, tz)
    if isinstance(moment, datetime):
        return moment
    day = moment + timedelta(days=1) if end else moment
    return local_midnight(day, tz)


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def to_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def iso_utc(moment: datetime) -> str:
    """Sortable text form the mirror stores."""
    return to_utc(moment).isoformat()


def from_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
