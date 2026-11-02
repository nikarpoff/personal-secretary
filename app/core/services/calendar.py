"""Calendar CRUD and its links to person cards."""

from typing import Any

from loguru import logger

from app.api_v1.schemas import EventCreateRequest, EventInfo, EventListResponse, EventUpdateRequest
from app.core.config import Settings
from app.core.entities import Event
from app.core.exceptions import CalendarNotConfiguredError, InvalidRequestError, NotFoundError
from app.core.people import set_event_person_ids
from app.core.reminders import reminder_offsets
from app.core.scheduler import ReminderScheduler
from app.core.services.event_times import EventTimes
from app.core.services.notes import NotesService
from app.core.upstream import GoogleCalendarClient
from app.core.upstream.calendar import reminders_body, time_body, to_event
from app.utils.time import local_iso, utc_now

MAX_REMINDER_MINUTES = 4 * 7 * 24 * 60


class CalendarService:
    def __init__(self, settings: Settings, calendar: GoogleCalendarClient | None,
                 scheduler: ReminderScheduler, notes: NotesService) -> None:
        self.settings, self.calendar, self.scheduler, self.notes = settings, calendar, scheduler, notes
        self.times = EventTimes(settings.tz)

    async def list_events(self, start: str | None, end: str | None, query: str | None, limit: int) -> EventListResponse:
        calendar = self._require_calendar()
        time_min, time_max = self.times.range(start, end)
        items = await calendar.list_events(time_min, time_max, query=query or None, limit=limit)
        events = [event for item in items if (event := self._to_event(item)) is not None]
        return EventListResponse(now=local_iso(utc_now(), self.settings.tz), timezone=self.settings.TIMEZONE,
                                 events=[self._event_info(event) for event in events])

    async def create_event(self, request: EventCreateRequest) -> EventInfo:
        calendar = self._require_calendar()
        start, end = self.times.create(request.start, request.end, request.duration_minutes)
        body: dict[str, Any] = {
            "summary": request.title.strip(), "description": request.description, "location": request.location,
            "start": time_body(start, self.settings.tz), "end": time_body(end, self.settings.tz),
            "reminders": reminders_body(self._check_minutes(request.remind_minutes)),
        }
        if request.important:
            body["colorId"] = self.settings.IMPORTANT_COLOR_ID
        if request.recurrence:
            rule = request.recurrence.strip()
            body["recurrence"] = [rule if rule.upper().startswith("RRULE:") else f"RRULE:{rule}"]
        await self._set_people(body, request.person_ids)
        event = self._changed(await calendar.insert_event(body))
        logger.info("Event created: '{}' at {}", event.id, event.start)
        return self._event_info(event)

    async def update_event(self, event_id: str, request: EventUpdateRequest) -> EventInfo:
        calendar = self._require_calendar()
        item = await calendar.get_event(event_id)
        current = self._to_event(item)
        if current is None:
            raise NotFoundError("Event not found")
        for attribute, field in (("title", "summary"), ("description", "description"), ("location", "location")):
            value = getattr(request, attribute)
            if value is not None:
                item[field] = value.strip() if attribute == "title" else value
        if request.start is not None or request.end is not None:
            start, end = self.times.update(current, request.start, request.end)
            item["start"], item["end"] = time_body(start, self.settings.tz), time_body(end, self.settings.tz)
        if request.important is True:
            item["colorId"] = self.settings.IMPORTANT_COLOR_ID
        elif request.important is False and item.get("colorId") == self.settings.IMPORTANT_COLOR_ID:
            item.pop("colorId")
        if request.remind_minutes is not None:
            item["reminders"] = reminders_body(self._check_minutes(request.remind_minutes))
        await self._set_people(item, request.person_ids)
        event = self._changed(await calendar.update_event(event_id, item))
        logger.info("Event updated: '{}'", event.id)
        return self._event_info(event)

    async def delete_event(self, event_id: str) -> None:
        await self._require_calendar().delete_event(event_id)
        self.scheduler.remove_event(event_id)
        logger.info("Event deleted: '{}'", event_id)

    async def _set_people(self, item: dict, person_ids: list[str] | None) -> None:
        if person_ids is not None:
            resolved = await self.notes.validate_people(person_ids) if person_ids else []
            set_event_person_ids(item, resolved)

    def _require_calendar(self) -> GoogleCalendarClient:
        if self.calendar is None:
            raise CalendarNotConfiguredError()
        return self.calendar

    def _to_event(self, item: dict) -> Event | None:
        return to_event(item, self.settings.tz, self.settings.IMPORTANT_COLOR_ID)

    def _changed(self, item: dict) -> Event:
        event = self._to_event(item)
        if event is None:
            raise NotFoundError("Event not found")
        self.scheduler.event_changed(event, recurring=bool(item.get("recurrence")))
        return event

    def _event_info(self, event: Event) -> EventInfo:
        minutes = reminder_offsets(event, self.settings.REMINDER_MINUTES, self.settings.IMPORTANT_REMINDER_MINUTES)
        return EventInfo.from_event(event, self.settings.tz, minutes)

    @staticmethod
    def _check_minutes(minutes: list[int] | None) -> list[int] | None:
        if minutes is not None and any(value < 0 or value > MAX_REMINDER_MINUTES for value in minutes):
            raise InvalidRequestError(f"Reminder minutes must be within 0..{MAX_REMINDER_MINUTES}")
        return minutes
