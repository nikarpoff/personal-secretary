"""
Google Calendar polling and Telegram reminders.

Events and sent-reminder marks live only in memory. Restarting the process clears
these marks, so reminders for upcoming events may be sent again after a restart.
"""

import asyncio

from datetime import datetime, timedelta
from typing import Dict, Optional, Set

from loguru import logger

from app.core.clock import iso_utc, utc_now
from app.core.config import CALENDAR_SETTINGS, REMINDER_SETTINGS as CFG, Settings
from app.core.exceptions import AppError
from app.core.entities import Event
from app.core.reminders import Mark, due_reminders, format_reminder
from app.core.upstream import GoogleCalendarClient, TelegramClient
from app.core.upstream.calendar import to_event


class ReminderScheduler:
    """Fetches Google Calendar events and sends reminders without local storage."""

    def __init__(
        self,
        settings: Settings,
        calendar: Optional[GoogleCalendarClient],
        telegram: Optional[TelegramClient],
    ) -> None:
        self.settings = settings
        self._calendar = calendar
        self._telegram = telegram
        self._events: Dict[str, Event] = {}
        self._sent: Set[Mark] = set()
        self._revision = 0

        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._last_sync: Optional[datetime] = None
        self._sync_requested = True
        self._sync_lock = asyncio.Lock()
        # Two overlapping checks would both see a reminder as unsent
        self._reminder_lock = asyncio.Lock()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def request_sync(self) -> None:
        """Read Google Calendar on the next tick, without waiting for the interval."""
        self._sync_requested = True
        self._wake.set()

    def event_changed(self, event: Event, *, recurring: bool = False) -> None:
        """Apply a local change immediately; Google expands recurring series on refresh."""
        if recurring:
            self.remove_event(event.id)
        else:
            self._events[event.id] = event
            self._revision += 1
            self.request_sync()

    def remove_event(self, event_id: str) -> None:
        """Forget an event or all cached occurrences of a deleted/changed series."""
        self._events = {
            key: event for key, event in self._events.items()
            if event.id != event_id and event.recurring_event_id != event_id
        }
        self._revision += 1
        self.request_sync()

    async def _run(self) -> None:
        logger.info(
            "Scheduler started: calendar polling every {}s, reminders {} min, important +{} min",
            self.settings.CALENDAR_SYNC_INTERVAL,
            self.settings.REMINDER_MINUTES,
            self.settings.IMPORTANT_REMINDER_MINUTES,
        )
        while True:
            await self._tick()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=CFG.TICK_SECONDS)
            except TimeoutError:
                pass
            self._wake.clear()

    async def _tick(self) -> None:
        now = utc_now()
        interval = timedelta(seconds=self.settings.CALENDAR_SYNC_INTERVAL)

        # Refresh the in-memory cache before checking reminders.
        if self._calendar is not None and (
            self._sync_requested or self._last_sync is None or now - self._last_sync >= interval
        ):
            # Cleared before the sync: a request made while it runs gets its own sync
            self._sync_requested = False
            try:
                await self.sync_calendar()
            except AppError as e:
                self._sync_requested = True
                logger.warning("Calendar sync failed: {}", e)
            except Exception as e:
                self._sync_requested = True
                logger.opt(exception=e).error("Calendar sync crashed")

        # 2. Send what came due
        try:
            await self.send_due_reminders()
        except Exception as e:
            logger.opt(exception=e).error("Reminders check crashed")

    async def sync_calendar(self) -> None:
        """Replace the memory cache only after a complete, successful Google read."""
        if self._calendar is None:
            return
        async with self._sync_lock:
            revision = self._revision
            now = utc_now()
            time_min = now - timedelta(days=CALENDAR_SETTINGS.PAST_DAYS)
            time_max = now + timedelta(days=self.settings.CALENDAR_SYNC_DAYS)

            items = await self._calendar.list_events(time_min, time_max)
            events = [
                event for item in items
                if (event := to_event(item, self.settings.tz, self.settings.IMPORTANT_COLOR_ID)) is not None
            ]
            # An older response must not undo an event changed/deleted during the read.
            if revision != self._revision:
                self.request_sync()
                return
            self._events = {event.id: event for event in events}
            self._last_sync = utc_now()
            logger.debug("Calendar refreshed: {} events in memory", len(events))

    async def send_due_reminders(self) -> int:
        """Sends the due reminders; returns how many went out."""
        async with self._reminder_lock:
            return await self._send_due_reminders()

    async def _send_due_reminders(self) -> int:
        now = utc_now()
        events = list(self._events.values())
        # Keep future marks through cache refreshes and temporary removal of a series.
        cutoff = iso_utc(now)
        self._sent = {mark for mark in self._sent if mark[1] > cutoff}
        reminders = due_reminders(
            events, self._sent, now, self.settings.REMINDER_MINUTES, self.settings.IMPORTANT_REMINDER_MINUTES
        )

        delivered = 0
        for reminder in reminders:
            event = reminder.event
            text = format_reminder(reminder, now, self.settings.tz)

            if self._telegram is None:
                logger.info("Reminder (Telegram is off) for event '{}': {} min", event.id, reminder.minutes)
            else:
                try:
                    await self._telegram.send_message(text)
                except AppError as e:
                    logger.warning("Reminder for event '{}' not sent, retry on the next tick: {}", event.id, e)
                    continue
                logger.info("Reminder sent for event '{}': {} min before", event.id, reminder.minutes)

            self._sent.update(reminder.marks)
            delivered += 1

        return delivered
