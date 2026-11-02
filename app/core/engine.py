from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.api_v1.schemas import (
    EventCreateRequest,
    EventInfo,
    EventListResponse,
    EventUpdateRequest,
    local_day,
    local_iso,
)
from app.core.clock import Moment, local_midnight, parse_moment, utc_now
from app.core.config import CALENDAR_SETTINGS as CFG, Settings
from app.core.entities import Event
from app.core.exceptions import CalendarNotConfiguredError, InvalidRequestError, NotFoundError
from app.core.reminders import reminder_offsets
from app.core.scheduler import ReminderScheduler
from app.core.store import ReminderStore
from app.core.upstream import GoogleCalendarClient, TelegramClient
from app.core.upstream.calendar import all_day_end, reminders_body, time_body, to_event

MAX_REMINDER_MINUTES = 4 * 7 * 24 * 60      # Google limit: four weeks


class AssistantEngine:
    """Facade of the assistant: calendar operations and the reminder loop"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tz = settings.tz

        self._store = ReminderStore(settings.database_path)
        self._calendar = (
            GoogleCalendarClient(settings.GOOGLE_CREDENTIALS_FILE, settings.GOOGLE_CALENDAR_ID)
            if settings.calendar_enabled else None
        )
        self._telegram = (
            TelegramClient(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
            if settings.TELEGRAM_BOT_TOKEN else None
        )
        self._scheduler = ReminderScheduler(settings, self._store, self._calendar, self._telegram)

    async def startup(self) -> None:
        await self._store.startup()

        if self._calendar is not None:
            await self._calendar.startup()
        else:
            logger.warning("GOOGLE_CREDENTIALS_FILE is unset: calendar tools are off")
        if self._telegram is None:
            logger.warning("TELEGRAM_BOT_TOKEN is unset: reminders go to the log only")

        self._scheduler.start()

    async def shutdown(self) -> None:
        await self._scheduler.stop()
        for client in (self._calendar, self._telegram):
            if client is not None:
                await client.aclose()
        await self._store.shutdown()

    # ---- calendar ----------------------------------------------------------

    async def list_events(self, start: Optional[str], end: Optional[str], query: Optional[str], limit: int) -> EventListResponse:
        """Events of a range straight from Google."""
        calendar = self._require_calendar()
        time_min, time_max = self._range(start, end)
        logger.info("Listing events: {} - {}", time_min, time_max)

        items = await calendar.list_events(time_min, time_max, query=query or None, limit=limit)
        events = [event for item in items if (event := self._to_event(item)) is not None]
        return EventListResponse(
            now=local_iso(utc_now(), self._tz),
            timezone=self.settings.TIMEZONE,
            events=[self._event_info(event) for event in events],
        )

    async def create_event(self, request: EventCreateRequest) -> EventInfo:
        calendar = self._require_calendar()
        start = parse_moment(request.start, self._tz)
        end = parse_moment(request.end, self._tz) if request.end else None

        if isinstance(start, datetime):
            end = end or start + timedelta(minutes=request.duration_minutes or CFG.DEFAULT_DURATION_MIN)
        else:
            end = all_day_end(start, self._last_day(end))
        self._check_span(start, end)

        body: Dict[str, Any] = {
            "summary": request.title.strip(),
            "description": request.description,
            "location": request.location,
            "start": time_body(start, self._tz),
            "end": time_body(end, self._tz),
            "reminders": reminders_body(self._check_minutes(request.remind_minutes)),
        }
        if request.important:
            body["colorId"] = self.settings.IMPORTANT_COLOR_ID
        if request.recurrence:
            rule = request.recurrence.strip()
            body["recurrence"] = [rule if rule.upper().startswith("RRULE:") else f"RRULE:{rule}"]

        event = self._changed(await calendar.insert_event(body))
        logger.info("Event created: '{}' at {}", event.id, event.start)
        return self._event_info(event)

    async def update_event(self, event_id: str, request: EventUpdateRequest) -> EventInfo:
        calendar = self._require_calendar()
        item = await calendar.get_event(event_id)
        current = self._to_event(item)
        if current is None:
            raise NotFoundError("Event not found")

        if request.title is not None:
            item["summary"] = request.title.strip()
        if request.description is not None:
            item["description"] = request.description
        if request.location is not None:
            item["location"] = request.location
        if request.start is not None or request.end is not None:
            start, end = self._new_span(current, request.start, request.end)
            item["start"], item["end"] = time_body(start, self._tz), time_body(end, self._tz)
        if request.important is True:
            item["colorId"] = self.settings.IMPORTANT_COLOR_ID
        elif request.important is False and item.get("colorId") == self.settings.IMPORTANT_COLOR_ID:
            item.pop("colorId")
        if request.remind_minutes is not None:
            item["reminders"] = reminders_body(self._check_minutes(request.remind_minutes))

        event = self._changed(await calendar.update_event(event_id, item))
        logger.info("Event updated: '{}'", event.id)
        return self._event_info(event)

    async def delete_event(self, event_id: str) -> None:
        """Deletes an event, an occurrence or, by the series id, the whole series."""
        calendar = self._require_calendar()
        await calendar.delete_event(event_id)
        self._scheduler.request_sync()
        logger.info("Event deleted: '{}'", event_id)

    # ---- helpers -----------------------------------------------------------

    def _require_calendar(self) -> GoogleCalendarClient:
        if self._calendar is None:
            raise CalendarNotConfiguredError()
        return self._calendar

    def _to_event(self, item: Dict[str, Any]) -> Optional[Event]:
        return to_event(item, self._tz, self.settings.IMPORTANT_COLOR_ID)

    def _changed(self, item: Dict[str, Any]) -> Event:
        """A created or changed event; the reminders re-read the calendar right away."""
        event = self._to_event(item)
        if event is None:
            raise NotFoundError("Event not found")
        # A recurring series comes back as one event: its occurrences only the listing expands
        self._scheduler.request_sync()
        return event

    def _event_info(self, event: Event) -> EventInfo:
        reminders = reminder_offsets(event, self.settings.REMINDER_MINUTES, self.settings.IMPORTANT_REMINDER_MINUTES)
        return EventInfo.from_event(event, self._tz, reminders)

    def _range(self, start: Optional[str], end: Optional[str]) -> Tuple[datetime, datetime]:
        """Range of a listing: today and a week ahead by default, a date covers its whole day."""
        today = local_day(utc_now(), self._tz)
        first = parse_moment(start, self._tz) if start else today
        time_min = first if isinstance(first, datetime) else local_midnight(first, self._tz)

        if end:
            last = parse_moment(end, self._tz)
            time_max = last if isinstance(last, datetime) else local_midnight(last + timedelta(days=1), self._tz)
        elif start and not isinstance(first, datetime):
            time_max = local_midnight(first + timedelta(days=1), self._tz)
        else:
            time_max = time_min + timedelta(days=7)

        if time_max <= time_min:
            raise InvalidRequestError("Range end must be after its start")
        return time_min, time_max

    def _new_span(self, current: Event, start: Optional[str], end: Optional[str]) -> Tuple[Moment, Moment]:
        """New start and end of a changed event; Google end form (exclusive for all-day)."""
        if current.all_day:
            old_start: Moment = local_day(current.start, self._tz)
            old_length = local_day(current.end, self._tz) - old_start
        else:
            old_start, old_length = current.start, current.end - current.start

        new_start = parse_moment(start, self._tz) if start else old_start
        new_end = parse_moment(end, self._tz) if end else None
        switched = isinstance(new_start, datetime) != isinstance(old_start, datetime)

        if isinstance(new_start, datetime):
            if new_end is None:
                length = timedelta(minutes=CFG.DEFAULT_DURATION_MIN) if switched else old_length
                new_end = new_start + length
        elif new_end is None:
            new_end = all_day_end(new_start, None) if switched else new_start + old_length
        else:
            new_end = all_day_end(new_start, self._last_day(new_end))

        self._check_span(new_start, new_end)
        return new_start, new_end

    @staticmethod
    def _last_day(moment: Optional[Moment]) -> Optional[date]:
        if moment is not None and isinstance(moment, datetime):
            raise InvalidRequestError("All-day event ends on a date, not a time")
        return moment

    @staticmethod
    def _check_span(start: Moment, end: Moment) -> None:
        if isinstance(start, datetime) != isinstance(end, datetime):
            raise InvalidRequestError("Start and end must both be times or both be dates")
        if end <= start:
            raise InvalidRequestError("Event must end after it starts")

    @staticmethod
    def _check_minutes(minutes: Optional[List[int]]) -> Optional[List[int]]:
        if minutes is not None and any(m < 0 or m > MAX_REMINDER_MINUTES for m in minutes):
            raise InvalidRequestError(f"Reminder minutes must be within 0..{MAX_REMINDER_MINUTES}")
        return minutes
