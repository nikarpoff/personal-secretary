from typing import Any, Dict, List, Optional

from loguru import logger

from app.api_v1.schemas import EventCreateRequest, EventInfo, EventListResponse, EventUpdateRequest
from app.core.config import Settings
from app.core.entities import Event
from app.core.event_times import changed_span, listing_range, new_span
from app.core.exceptions import CalendarNotConfiguredError, NotFoundError
from app.core.reminders import reminder_offsets
from app.core.scheduler import ReminderScheduler
from app.core.upstream import GoogleCalendarClient
from app.core.upstream.calendar import reminders_body, time_body, to_event
from app.utils.time import local_iso, utc_now


class EventService:
    """Events of the owner's calendar: list, create, change, delete"""

    def __init__(self, settings: Settings, calendar: Optional[GoogleCalendarClient], scheduler: ReminderScheduler) -> None:
        self.settings = settings
        self._tz = settings.tz
        self._calendar = calendar
        self._scheduler = scheduler

    async def list(self, start: Optional[str], end: Optional[str], query: Optional[str], limit: int) -> EventListResponse:
        """Events of a range straight from Google."""
        time_min, time_max = listing_range(start, end, self._tz)
        logger.info("Listing events: {} - {}", time_min, time_max)

        items = await self._require_calendar().list_events(time_min, time_max, query=query or None, limit=limit)
        events = [event for item in items if (event := self._to_event(item)) is not None]
        return EventListResponse(
            now=local_iso(utc_now(), self._tz),
            timezone=self.settings.TIMEZONE,
            events=[self._info(event) for event in events],
        )

    async def create(self, request: EventCreateRequest) -> EventInfo:
        calendar = self._require_calendar()
        start, end = new_span(request.start, request.end, request.duration_minutes, self._tz)

        body: Dict[str, Any] = {
            "summary": request.title.strip(),
            "description": request.description,
            "location": request.location,
            "start": time_body(start, self._tz),
            "end": time_body(end, self._tz),
        }
        if request.recurrence:
            rule = request.recurrence.strip()
            body["recurrence"] = [rule if rule.upper().startswith("RRULE:") else f"RRULE:{rule}"]
        self._set_alerts(body, request.important, request.remind_minutes)

        event = self._changed(await calendar.insert_event(body))
        logger.info("Event created: '{}' at {}", event.id, event.start)
        return self._info(event)

    async def update(self, event_id: str, request: EventUpdateRequest) -> EventInfo:
        """Only the given fields change; the rest of the Google event is sent back as it was."""
        calendar = self._require_calendar()
        body = await calendar.get_event(event_id)
        current = self._to_event(body)
        if current is None:
            raise NotFoundError("Event not found")

        if request.title is not None:
            body["summary"] = request.title.strip()
        if request.description is not None:
            body["description"] = request.description
        if request.location is not None:
            body["location"] = request.location
        if request.start is not None or request.end is not None:
            start, end = changed_span(current, request.start, request.end, self._tz)
            body["start"], body["end"] = time_body(start, self._tz), time_body(end, self._tz)
        self._set_alerts(body, request.important, request.remind_minutes)

        event = self._changed(await calendar.update_event(event_id, body))
        logger.info("Event updated: '{}'", event.id)
        return self._info(event)

    async def delete(self, event_id: str) -> None:
        """Deletes an event, an occurrence or, by the series id, the whole series."""
        await self._require_calendar().delete_event(event_id)
        self._scheduler.request_sync()
        logger.info("Event deleted: '{}'", event_id)

    def _set_alerts(self, body: Dict[str, Any], important: Optional[bool], remind_minutes: Optional[List[int]]) -> None:
        """Importance (the event color) and reminders; None keeps what the event has."""
        color = self.settings.IMPORTANT_COLOR_ID
        if important:
            body["colorId"] = color
        elif important is False and body.get("colorId") == color:
            body.pop("colorId")
        if remind_minutes is not None:
            body["reminders"] = reminders_body(remind_minutes)

    def _changed(self, item: Dict[str, Any]) -> Event:
        """A created or changed event; the reminders re-read the calendar right away."""
        event = self._to_event(item)
        if event is None:
            raise NotFoundError("Event not found")
        # A recurring series comes back as one event: only the calendar listing expands its occurrences
        self._scheduler.request_sync()
        return event

    def _require_calendar(self) -> GoogleCalendarClient:
        if self._calendar is None:
            raise CalendarNotConfiguredError()
        return self._calendar

    def _to_event(self, item: Dict[str, Any]) -> Optional[Event]:
        return to_event(item, self._tz, self.settings.IMPORTANT_COLOR_ID)

    def _info(self, event: Event) -> EventInfo:
        reminders = reminder_offsets(event, self.settings.REMINDER_MINUTES, self.settings.IMPORTANT_REMINDER_MINUTES)
        return EventInfo.from_event(event, self._tz, reminders)
