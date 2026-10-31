import asyncio

from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.core.exceptions import NotFoundError, UpstreamRejectedError, UpstreamUnavailableError
from app.core.upstream.calendar import GoogleCalendarClient, reminders_body, to_event

TZ = ZoneInfo("Europe/Moscow")


def test_to_event_reads_timed_all_day_and_cancelled():
    timed = to_event({
        "id": "a", "summary": "Стендап", "colorId": "11",
        "start": {"dateTime": "2026-11-03T10:00:00+03:00"}, "end": {"dateTime": "2026-11-03T10:15:00+03:00"},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}, {"method": "email", "minutes": 60}]},
        "recurringEventId": "series", "created": "2026-10-01T08:00:00.000Z",
    }, TZ, "11")
    assert (timed.title, timed.important, timed.own_reminders, timed.series_id) == ("Стендап", True, (60, 10), "series")
    assert timed.start.isoformat() == "2026-11-03T10:00:00+03:00"

    all_day = to_event({"id": "b", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}}, TZ, "11")
    assert all_day.all_day and all_day.title == "(без названия)" and all_day.own_reminders is None
    assert all_day.start.isoformat() == "2026-11-05T00:00:00+03:00"

    assert to_event({"id": "c", "status": "cancelled"}, TZ, "11") is None


def test_reminders_body():
    assert reminders_body(None) == {"useDefault": True}
    assert reminders_body([]) == {"useDefault": False, "overrides": []}
    assert [o["minutes"] for o in reminders_body([1, 2, 3, 4, 5, 6, 6])["overrides"]] == [6, 5, 4, 3, 2]


@pytest.mark.parametrize(
    "status, body, error",
    [
        (404, {"error": {"message": "Not Found"}}, NotFoundError),
        (400, {"error": {"message": "Invalid recurrence rule"}}, UpstreamRejectedError),
        (503, {"error": {"message": "Backend Error"}}, UpstreamUnavailableError),
    ],
    ids=["not-found", "rejected", "unavailable"],
)
def test_client_maps_google_errors(status, body, error):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    async def scenario():
        client = GoogleCalendarClient(Path("unused.json"), "me@example.com")
        client._credentials = SimpleNamespace(valid=True, token="access-token")
        client._http = httpx.AsyncClient(base_url="https://calendar.test/v3", transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(error) as exc_info:
                await client.get_event("id/with slash")
        finally:
            await client.aclose()
        return exc_info.value

    exc = asyncio.run(scenario())
    assert seen[0].headers["Authorization"] == "Bearer access-token"
    assert seen[0].url.raw_path == b"/v3/calendars/me%40example.com/events/id%2Fwith%20slash"
    if error is UpstreamRejectedError:
        assert "Invalid recurrence rule" in str(exc)


def test_client_pages_through_the_list():
    pages = {None: {"items": [{"id": "1"}], "nextPageToken": "p2"}, "p2": {"items": [{"id": "2"}]}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["singleEvents"] == "true"
        return httpx.Response(200, json=pages[request.url.params.get("pageToken")])

    async def scenario():
        client = GoogleCalendarClient(Path("unused.json"), "primary")
        client._credentials = SimpleNamespace(valid=True, token="t")
        client._http = httpx.AsyncClient(base_url="https://calendar.test/v3", transport=httpx.MockTransport(handler))
        try:
            from datetime import datetime, timezone
            moment = datetime(2026, 11, 1, tzinfo=timezone.utc)
            return await client.list_events(moment, moment)
        finally:
            await client.aclose()

    assert [item["id"] for item in asyncio.run(scenario())] == ["1", "2"]
