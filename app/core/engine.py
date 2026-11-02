"""Application composition and resource lifecycle."""

from contextlib import AsyncExitStack

from loguru import logger

from app.core.config import Settings
from app.core.note_reminders import NoteReminderScheduler
from app.core.notes import NotesStore
from app.core.scheduler import ReminderScheduler
from app.core.services.calendar import CalendarService
from app.core.services.notes import NotesService
from app.core.upstream import GoogleCalendarClient, TelegramClient
from app.utils.async_io import shielded_io


class AssistantEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._calendar = (GoogleCalendarClient(settings.GOOGLE_CREDENTIALS_FILE, settings.GOOGLE_CALENDAR_ID)
                          if settings.calendar_enabled else None)
        self._telegram = (TelegramClient(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
                          if settings.TELEGRAM_BOT_TOKEN else None)
        self._scheduler = ReminderScheduler(settings, self._calendar, self._telegram)
        self._notes = (NotesStore(settings.NOTES_ROOT, settings.ASSISTANT_STATE_DIR, settings.tz,
                                  settings.OBSIDIAN_VAULT_NAME) if settings.notes_enabled else None)
        self._note_scheduler = (NoteReminderScheduler(self._notes, settings.ASSISTANT_STATE_DIR,
                                                     self._telegram, settings.tz,
                                                     poll_seconds=settings.NOTE_REMINDER_POLL_SECONDS)
                                if self._notes is not None else None)
        self.notes = NotesService(settings, self._notes, self._note_scheduler)
        self.events = CalendarService(settings, self._calendar, self._scheduler, self.notes)
        self.list_events = self.events.list_events
        self.create_event = self.events.create_event
        self.update_event = self.events.update_event
        self.delete_event = self.events.delete_event
        self.get_note_context = self.notes.get_note_context
        self.search_notes = self.notes.search_notes
        self.read_note = self.notes.read_note
        self.create_note = self.notes.create_note
        self.append_note = self.notes.append_note
        self.update_note = self.notes.update_note
        self.archive_note = self.notes.archive_note
        self.notes_status = self.notes.notes_status

    async def startup(self) -> None:
        try:
            if self._calendar is not None:
                await self._calendar.startup()
                self._scheduler.start()
            else:
                logger.warning("GOOGLE_CREDENTIALS_FILE is unset: calendar tools are off")
            if self._notes is not None:
                await shielded_io(self._notes.initialize)
                await self._note_scheduler.start()
        except BaseException:
            await self.shutdown()
            raise
        if self._telegram is None:
            logger.warning("TELEGRAM_BOT_TOKEN is unset: reminders go to the log only")

    async def shutdown(self) -> None:
        async with AsyncExitStack() as cleanup:
            for client in (self._calendar, self._telegram):
                if client is not None:
                    cleanup.push_async_callback(client.aclose)
            cleanup.push_async_callback(self._scheduler.stop)
            if self._notes is not None:
                cleanup.push_async_callback(shielded_io, self._notes.close)
            if self._note_scheduler is not None:
                cleanup.push_async_callback(self._note_scheduler.stop)
