"""
Background loop of the assistant.

Every tick it sends the reminders that came due; every CALENDAR_SYNC_INTERVAL it first
mirrors the calendar window into the knowledge base, and embeds what is new. A failed
step is logged and retried on the next tick: an unsent reminder is not marked as sent.
"""

import asyncio

from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from app.core.clock import utc_now
from app.core.config import CALENDAR_SETTINGS, REMINDER_SETTINGS as CFG, Settings
from app.core.exceptions import AppError
from app.core.knowledge import KnowledgeRepository, KnowledgeSearch
from app.core.reminders import due_reminders, format_reminder
from app.core.upstream import GoogleCalendarClient, TelegramClient
from app.core.upstream.calendar import to_event


class ReminderScheduler:
    """Mirrors the calendar, sends due reminders to Telegram, indexes new items"""

    def __init__(
        self,
        settings: Settings,
        repository: KnowledgeRepository,
        knowledge: KnowledgeSearch,
        calendar: Optional[GoogleCalendarClient],
        telegram: Optional[TelegramClient],
    ) -> None:
        self.settings = settings
        self._repository = repository
        self._knowledge = knowledge
        self._calendar = calendar
        self._telegram = telegram

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
        """Mirror the calendar on the next tick, without waiting for the interval."""
        self._sync_requested = True
        self._wake.set()

    async def _run(self) -> None:
        logger.info(
            "Scheduler started: sync every {}s, reminders {} min, important +{} min",
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

        # 1. Firstly, refresh the mirror: reminders are computed from it
        if self._calendar is not None and (self._sync_requested or now - self._last_sync >= interval):
            # Cleared before the sync: a request made while it runs gets its own sync
            self._sync_requested = False
            try:
                await self.sync_calendar()
                self._last_sync = now
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

        # 3. Embed new and changed items, search works on keywords meanwhile
        try:
            await self._knowledge.index_pending()
        except AppError as e:
            logger.warning("Indexing postponed: {}", e)
        except Exception as e:
            logger.opt(exception=e).error("Indexing crashed")

    async def sync_calendar(self) -> None:
        """Mirror of the window [now - PAST_DAYS, now + CALENDAR_SYNC_DAYS]."""
        async with self._sync_lock:
            now = utc_now()
            time_min = now - timedelta(days=CALENDAR_SETTINGS.PAST_DAYS)
            time_max = now + timedelta(days=self.settings.CALENDAR_SYNC_DAYS)

            items = await self._calendar.list_events(time_min, time_max)
            events = [
                event for item in items
                if (event := to_event(item, self.settings.tz, self.settings.IMPORTANT_COLOR_ID)) is not None
            ]
            changed, removed = await self._repository.replace_window(events, time_min, time_max)
            if changed or removed:
                logger.info("Calendar synced: {} events, {} changed, {} removed", len(events), changed, removed)

    async def send_due_reminders(self) -> int:
        """Sends the due reminders; returns how many went out."""
        async with self._reminder_lock:
            return await self._send_due_reminders()

    async def _send_due_reminders(self) -> int:
        now = utc_now()
        offsets = self.settings.REMINDER_MINUTES + self.settings.IMPORTANT_REMINDER_MINUTES
        if not offsets:
            return 0

        horizon = now + timedelta(minutes=max(offsets) + 1)
        events = await self._repository.events_starting(now, horizon)
        sent = await self._repository.sent_marks([event.id for event in events])
        reminders = due_reminders(
            events, sent, now, self.settings.REMINDER_MINUTES, self.settings.IMPORTANT_REMINDER_MINUTES
        )

        delivered = 0
        for reminder in reminders:
            event = reminder.event
            notes = await self._repository.list_notes(None, [event.id, event.series_id], CFG.MAX_NOTES_SHOWN + 1)
            text = format_reminder(reminder, notes, now, self.settings.tz, CFG.MAX_NOTES_SHOWN)

            if self._telegram is None:
                logger.info("Reminder (Telegram is off) for event '{}': {} min", event.id, reminder.minutes)
            else:
                try:
                    await self._telegram.send_message(text)
                except AppError as e:
                    logger.warning("Reminder for event '{}' not sent, retry on the next tick: {}", event.id, e)
                    continue
                logger.info("Reminder sent for event '{}': {} min before", event.id, reminder.minutes)

            await self._repository.mark_sent(reminder.marks, now)
            delivered += 1

        # Marks of long past events are of no use
        await self._repository.purge_marks(now - timedelta(days=CFG.SENT_RETENTION_DAYS))
        return delivered
