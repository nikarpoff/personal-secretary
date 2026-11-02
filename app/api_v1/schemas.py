from datetime import date, datetime, timedelta
from typing import Annotated, List, Optional, Sequence
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.core.entities import Event

TIME_HINT = "local time 'YYYY-MM-DDTHH:MM' or a date 'YYYY-MM-DD' for an all-day event; an explicit offset is honored"
PersonId = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]


def local_iso(moment: Optional[datetime], tz: ZoneInfo) -> Optional[str]:
    return moment.astimezone(tz).isoformat(timespec="minutes") if moment else None


def local_day(moment: datetime, tz: ZoneInfo) -> date:
    return moment.astimezone(tz).date()


# ---- requests --------------------------------------------------------------

class EventCreateRequest(BaseModel):
    """A new calendar event"""
    title: str = Field(min_length=1, description="Event title")
    start: str = Field(description=f"Start: {TIME_HINT}")
    end: Optional[str] = Field(None, description="End in the same form as start; for an all-day event the last day, inclusive")
    duration_minutes: Optional[int] = Field(None, ge=1, le=7 * 24 * 60, description="Duration when end is omitted; 60 by default")
    description: str = Field("", description="Details, agenda, links")
    location: str = Field("", description="Place or a meeting link")
    important: bool = Field(False, description="Important events get extra early reminders and the red color in Google Calendar")
    remind_minutes: Optional[List[int]] = Field(None, description="Reminders N minutes before the start, e.g. [60, 10]; [] for none; the defaults when omitted")
    recurrence: Optional[str] = Field(None, description="RRULE of a recurring event, e.g. 'RRULE:FREQ=WEEKLY;BYDAY=MO'")
    person_ids: Optional[List[PersonId]] = Field(None, max_length=10, description="Selected Obsidian person-card UUIDs; does not invite attendees")


class EventUpdateRequest(BaseModel):
    """Changes of an event: only the given fields change"""
    title: Optional[str] = Field(None, min_length=1, description="New title")
    start: Optional[str] = Field(None, description=f"New start: {TIME_HINT}; the duration is kept when end is omitted")
    end: Optional[str] = Field(None, description="New end in the same form as start")
    description: Optional[str] = Field(None, description="New details; empty string clears them")
    location: Optional[str] = Field(None, description="New place; empty string clears it")
    important: Optional[bool] = Field(None, description="Mark or unmark the event as important")
    remind_minutes: Optional[List[int]] = Field(None, description="New reminders N minutes before the start; [] for none")
    person_ids: Optional[List[PersonId]] = Field(None, max_length=10, description="Replace person-card references; [] clears them; omitted keeps existing references")


# ---- responses -------------------------------------------------------------

class EventInfo(BaseModel):
    """A calendar event"""
    id: str = Field(description="Event id; an occurrence of a recurring event has its own")
    title: str = Field(description="Event title")
    start: str = Field(description="Local start; a date for an all-day event")
    end: str = Field(description="Local end; the last day, inclusive, for an all-day event")
    all_day: bool = Field(description="All-day event")
    description: str = Field("", description="Details")
    location: str = Field("", description="Place")
    important: bool = Field(False, description="Important event")
    reminders: List[int] = Field([], description="Telegram reminders, minutes before the start")
    recurring_event_id: Optional[str] = Field(None, description="Id of the whole series for an occurrence of a recurring event")
    link: Optional[str] = Field(None, description="Event page in Google Calendar")
    person_ids: List[str] = Field(default_factory=list, max_length=10, description="Person-card UUIDs; read only selected cards for meeting context")

    @classmethod
    def from_event(cls, event: Event, tz: ZoneInfo, reminders: Sequence[int]) -> "EventInfo":
        if event.all_day:
            start = local_day(event.start, tz).isoformat()
            end = (local_day(event.end, tz) - timedelta(days=1)).isoformat()
        else:
            start, end = local_iso(event.start, tz), local_iso(event.end, tz)
        return cls(
            id=event.id,
            title=event.title,
            start=start,
            end=end,
            all_day=event.all_day,
            description=event.description,
            location=event.location,
            important=event.important,
            reminders=[] if event.all_day else list(reminders),
            recurring_event_id=event.recurring_event_id,
            link=event.html_link,
            person_ids=list(event.person_ids),
        )


class EventListResponse(BaseModel):
    """Events of a range"""
    now: str = Field(description="Current local time: the reference for 'today' and 'tomorrow'")
    timezone: str = Field(description="Owner's timezone")
    events: List[EventInfo] = Field(description="Events sorted by start")
