import asyncio

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.api_v1.schemas import EventCreateRequest, EventUpdateRequest
from app.core import engine as engine_module
from app.core.config import Settings
from app.core.engine import AssistantEngine
from app.core.exceptions import InvalidRequestError, NotFoundError
from tests.fakes import FakeCalendar, FakeTelegram


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        DATA_DIR=tmp_path,
        TIMEZONE="Europe/Moscow",
        GOOGLE_CREDENTIALS_FILE=tmp_path / "google.json",
        TELEGRAM_BOT_TOKEN="123:abc",
        TELEGRAM_CHAT_ID=42,
    )


def _run(settings: Settings, monkeypatch: pytest.MonkeyPatch, scenario) -> None:
    calendar = FakeCalendar(settings.tz)
    telegram = FakeTelegram()
    monkeypatch.setattr(engine_module, "GoogleCalendarClient", lambda *args: calendar)
    monkeypatch.setattr(engine_module, "TelegramClient", lambda *args: telegram)

    async def wrapper():
        engine = AssistantEngine(settings)
        await engine.startup()
        try:
            await scenario(engine, calendar, telegram)
        finally:
            await engine.shutdown()

    asyncio.run(wrapper())


def _local(settings: Settings, minutes: int) -> str:
    """Naive local time string, as the agent passes it."""
    return (datetime.now(settings.tz) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def test_event_reminder_goes_out_once(settings, monkeypatch):
    async def scenario(engine: AssistantEngine, calendar: FakeCalendar, telegram: FakeTelegram):
        # The background loop would race the checks below
        await engine._scheduler.stop()

        await engine.events.create(EventCreateRequest(
            title="Созвон с Петром", start=_local(settings, 11), location="Zoom", description="Спросить про бюджет",
        ))
        await engine._scheduler.sync_calendar()

        assert await engine._scheduler.send_due_reminders() == 1
        assert await engine._scheduler.send_due_reminders() == 0

        assert len(telegram.messages) == 1
        message = telegram.messages[0]
        assert "<b>Созвон с Петром</b>" in message
        assert "через 1" in message
        assert "📍 Zoom" in message
        assert "📝 Спросить про бюджет" in message

    _run(settings, monkeypatch, scenario)


def test_event_update_keeps_duration_and_important_adds_reminders(settings, monkeypatch):
    async def scenario(engine: AssistantEngine, calendar: FakeCalendar, telegram: FakeTelegram):
        event = await engine.events.create(
            EventCreateRequest(title="Ревью", start="2030-03-01T10:00", duration_minutes=30, remind_minutes=[60, 10])
        )
        assert (event.start, event.end) == ("2030-03-01T10:00+03:00", "2030-03-01T10:30+03:00")
        assert event.reminders == [60, 10]

        moved = await engine.events.update(event.id, EventUpdateRequest(start="2030-03-02T15:00", important=True))
        assert (moved.start, moved.end) == ("2030-03-02T15:00+03:00", "2030-03-02T15:30+03:00")
        assert moved.important and moved.reminders == [1440, 120, 60, 10]
        assert calendar.items[event.id]["colorId"] == settings.IMPORTANT_COLOR_ID

        plain = await engine.events.update(event.id, EventUpdateRequest(important=False, remind_minutes=[]))
        assert not plain.important and plain.reminders == []
        assert "colorId" not in calendar.items[event.id]

        listed = await engine.events.list("2030-03-02", None, None, 100)
        assert [e.id for e in listed.events] == [event.id]

        await engine.events.delete(event.id)
        assert (await engine.events.list("2030-03-02", None, None, 100)).events == []
        with pytest.raises(NotFoundError):
            await engine.events.delete(event.id)

    _run(settings, monkeypatch, scenario)


def test_all_day_event_uses_inclusive_last_day(settings, monkeypatch):
    async def scenario(engine: AssistantEngine, calendar: FakeCalendar, telegram: FakeTelegram):
        event = await engine.events.create(EventCreateRequest(title="Отпуск", start="2030-07-01", end="2030-07-14"))

        assert calendar.items[event.id]["end"] == {"date": "2030-07-15"}
        assert (event.start, event.end, event.all_day, event.reminders) == ("2030-07-01", "2030-07-14", True, [])

        # Moving the start keeps the length of the vacation
        moved = await engine.events.update(event.id, EventUpdateRequest(start="2030-07-05"))
        assert (moved.start, moved.end) == ("2030-07-05", "2030-07-18")

    _run(settings, monkeypatch, scenario)


@pytest.mark.parametrize(
    "start, end",
    [("2030-03-01T10:00", "2030-03-01T09:00"), ("2030-03-01", "2030-03-01T12:00"), ("завтра", None)],
    ids=["end-before-start", "date-and-time", "not-iso"],
)
def test_create_event_rejects_bad_times(settings, monkeypatch, start, end):
    async def scenario(engine: AssistantEngine, calendar: FakeCalendar, telegram: FakeTelegram):
        with pytest.raises(InvalidRequestError):
            await engine.events.create(EventCreateRequest(title="x", start=start, end=end))
        assert calendar.items == {}

    _run(settings, monkeypatch, scenario)


def test_deleted_event_gets_no_reminder(settings, monkeypatch):
    async def scenario(engine: AssistantEngine, calendar: FakeCalendar, telegram: FakeTelegram):
        await engine._scheduler.stop()

        event = await engine.events.create(EventCreateRequest(title="Отменится", start=_local(settings, 11)))
        await engine.events.delete(event.id)
        await engine._scheduler.sync_calendar()

        assert await engine._scheduler.send_due_reminders() == 0
        assert telegram.messages == []

    _run(settings, monkeypatch, scenario)


def test_event_people_are_kept_private_and_replaced(settings, monkeypatch):
    async def scenario(engine: AssistantEngine, calendar: FakeCalendar, telegram: FakeTelegram):
        event = await engine.events.create(
            EventCreateRequest(title="Созвон", start="2030-03-01T10:00", people=["main/people/piotr", "main/people/piotr"])
        )
        assert event.people == ["main/people/piotr"]
        assert "attendees" not in calendar.items[event.id]

        renamed = await engine.events.update(event.id, EventUpdateRequest(title="Созвон с Петей"))
        assert renamed.people == ["main/people/piotr"]

        cleared = await engine.events.update(event.id, EventUpdateRequest(people=[]))
        assert cleared.people == []

    _run(settings, monkeypatch, scenario)
