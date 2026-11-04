from loguru import logger

from app.core.config import Settings
from app.core.events import EventService
from app.core.scheduler import ReminderScheduler
from app.core.store import ReminderStore
from app.core.upstream import GoogleCalendarClient, TelegramClient


class AssistantEngine:
    """Owns the upstream clients and the reminder loop; the API reaches the calendar through `events`"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        self._store = ReminderStore(settings.database_path)
        self._calendar = (
            GoogleCalendarClient(settings.GOOGLE_CREDENTIALS_FILE, settings.GOOGLE_CALENDAR_ID)
            if settings.calendar_enabled else None
        )
        self._telegram = (
            TelegramClient(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
            if settings.telegram_enabled else None
        )
        self._scheduler = ReminderScheduler(settings, self._store, self._calendar, self._telegram)
        self.events = EventService(settings, self._calendar, self._scheduler)

    async def startup(self) -> None:
        await self._store.startup()

        if self._calendar is not None:
            await self._calendar.startup()
        else:
            logger.warning("GOOGLE_CREDENTIALS_FILE is unset: calendar tools are off")
        if self._telegram is None:
            logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is unset: reminders go to the log only")

        self._scheduler.start()

    async def shutdown(self) -> None:
        await self._scheduler.stop()
        for client in (self._calendar, self._telegram):
            if client is not None:
                await client.aclose()
        await self._store.shutdown()
