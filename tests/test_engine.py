import asyncio

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


def test_event_update_keeps_duration_and_important_adds_reminders(settings, monkeypatch):
    async def scenario(engine: AssistantEngine, calendar: FakeCalendar, telegram: FakeTelegram):
        event = await engine.create_event(
            EventCreateRequest(title="Ревью", start="2030-03-01T10:00", duration_minutes=30, remind_minutes=[60, 10])
        )
        assert (event.start, event.end) == ("2030-03-01T10:00+03:00", "2030-03-01T10:30+03:00")
        assert event.reminders == [60, 10]

        moved = await engine.update_event(event.id, EventUpdateRequest(start="2030-03-02T15:00", important=True))
        assert (moved.start, moved.end) == ("2030-03-02T15:00+03:00", "2030-03-02T15:30+03:00")
        assert moved.important and moved.reminders == [1440, 120, 60, 10]
        assert calendar.items[event.id]["colorId"] == settings.IMPORTANT_COLOR_ID

        plain = await engine.update_event(event.id, EventUpdateRequest(important=False, remind_minutes=[]))
        assert not plain.important and plain.reminders == []
        assert "colorId" not in calendar.items[event.id]

        listed = await engine.list_events("2030-03-02", None, None, 100)
        assert [e.id for e in listed.events] == [event.id]

        await engine.delete_event(event.id)
        assert (await engine.list_events("2030-03-02", None, None, 100)).events == []
        with pytest.raises(NotFoundError):
            await engine.delete_event(event.id)

    _run(settings, monkeypatch, scenario)


def test_all_day_event_uses_inclusive_last_day(settings, monkeypatch):
    async def scenario(engine: AssistantEngine, calendar: FakeCalendar, telegram: FakeTelegram):
        event = await engine.create_event(EventCreateRequest(title="Отпуск", start="2030-07-01", end="2030-07-14"))

        assert calendar.items[event.id]["end"] == {"date": "2030-07-15"}
        assert (event.start, event.end, event.all_day, event.reminders) == ("2030-07-01", "2030-07-14", True, [])

        # Moving the start keeps the length of the vacation
        moved = await engine.update_event(event.id, EventUpdateRequest(start="2030-07-05"))
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
            await engine.create_event(EventCreateRequest(title="x", start=start, end=end))
        assert calendar.items == {}

    _run(settings, monkeypatch, scenario)
