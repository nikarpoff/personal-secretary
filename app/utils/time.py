"""Parsing and formatting dates in the owner's timezone and UTC."""

import re

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.exceptions import InvalidRequestError

DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
Moment = date | datetime


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def local_iso(moment: datetime | None, tz: ZoneInfo) -> str | None:
    return moment.astimezone(tz).isoformat(timespec="minutes") if moment else None


def local_day(moment: datetime, tz: ZoneInfo) -> date:
    return moment.astimezone(tz).date()


def local_timestamp(tz: ZoneInfo) -> str:
    return datetime.now(tz).isoformat(timespec="seconds")


def parse_moment(value: str, tz: ZoneInfo) -> Moment:
    """Date-only values stay dates; naive timestamps use the owner's timezone."""
    value = value.strip()
    if DATE_ONLY.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise InvalidRequestError(f"Bad date '{value}': expected YYYY-MM-DD") from exc
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidRequestError(f"Bad time '{value}': expected ISO 8601, e.g. 2026-11-03T10:00") from exc
    return moment.replace(tzinfo=moment.tzinfo or tz, microsecond=0)


def parse_bound(value: str | None, tz: ZoneInfo, default: datetime, end: bool = False) -> datetime:
    if not value:
        return default
    moment = parse_moment(value, tz)
    if isinstance(moment, datetime):
        return moment
    return local_midnight(moment + timedelta(days=int(end)), tz)


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def to_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def iso_utc(moment: datetime) -> str:
    return to_utc(moment).isoformat()


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def aware_utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return moment.astimezone(timezone.utc)


def parse_timestamp(value: str | None, tz: ZoneInfo, field: str) -> datetime | None:
    """An explicit date and time, rejecting ambiguous local times and UTC overflow."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"{field} must be an ISO timestamp or null")
    result = parse_moment(value, tz)
    if not isinstance(result, datetime):
        raise InvalidRequestError(f"{field} requires a date and time")
    try:
        naive = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if naive.tzinfo is None:
            choices = [naive.replace(tzinfo=tz, fold=fold) for fold in (0, 1)]
            valid = [choice for choice in choices
                     if choice.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == naive]
            if not valid or len({choice.utcoffset() for choice in valid}) > 1:
                raise InvalidRequestError(f"{field} is ambiguous or nonexistent in TIMEZONE; provide an explicit UTC offset")
        result.astimezone(timezone.utc)
    except (OverflowError, ValueError, OSError) as exc:
        raise InvalidRequestError(f"{field} is outside the supported timestamp range") from exc
    return result
