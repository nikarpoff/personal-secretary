"""
Google Calendar API v3 over httpx.

google-api-python-client is sync and its httplib2 transport is not thread-safe, so the REST
calls go through one shared httpx.AsyncClient. google-auth only loads the credentials and
refreshes the access token: the refresh is blocking, so it runs in a thread under a lock.
"""

import asyncio
import json

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import google.auth
import httpx

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request as GoogleRequest
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from app.core.clock import from_iso, iso_utc, local_midnight
from app.core.config import CALENDAR_SETTINGS as CFG
from app.core.entities import Event
from app.core.exceptions import (
    NotFoundError,
    UpstreamError,
    UpstreamRejectedError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)

SERVICE = "google-calendar"


class GoogleTime(BaseModel):
    """Event start or end: `date` for all-day events, `dateTime` otherwise"""
    date: Optional[str] = None
    dateTime: Optional[str] = None
    timeZone: Optional[str] = None


class GoogleReminderOverride(BaseModel):
    method: str = "popup"
    minutes: int


class GoogleReminders(BaseModel):
    useDefault: bool = True
    overrides: List[GoogleReminderOverride] = []


class GoogleEvent(BaseModel):
    """The fields of a Google event the assistant reads"""
    model_config = ConfigDict(extra="ignore")

    id: str
    status: str = "confirmed"
    summary: str = ""
    description: str = ""
    location: str = ""
    start: GoogleTime = Field(default_factory=GoogleTime)
    end: GoogleTime = Field(default_factory=GoogleTime)
    colorId: Optional[str] = None
    reminders: Optional[GoogleReminders] = None
    recurringEventId: Optional[str] = None
    htmlLink: Optional[str] = None
    created: Optional[str] = None


def to_event(item: Dict[str, Any], tz: ZoneInfo, important_color: str) -> Optional[Event]:
    """Mirror entity of a Google event; None for a cancelled one."""
    google = GoogleEvent.model_validate(item)
    if google.status == "cancelled":
        return None

    all_day = google.start.date is not None
    if all_day:
        start = local_midnight(date.fromisoformat(google.start.date), tz)
        end = local_midnight(date.fromisoformat(google.end.date or google.start.date), tz)
    else:
        start = from_iso(google.start.dateTime)
        end = from_iso(google.end.dateTime) or start
    if start is None:
        return None

    own_reminders = None
    if google.reminders is not None and not google.reminders.useDefault:
        own_reminders = tuple(sorted({o.minutes for o in google.reminders.overrides}, reverse=True))

    return Event(
        id=google.id,
        title=google.summary or "(без названия)",
        start=start,
        end=end,
        all_day=all_day,
        description=google.description,
        location=google.location,
        important=google.colorId == important_color,
        own_reminders=own_reminders,
        recurring_event_id=google.recurringEventId,
        html_link=google.htmlLink,
        created=from_iso(google.created),
    )


def time_body(moment: Any, tz: ZoneInfo) -> Dict[str, str]:
    """Google start/end object of a date or an aware datetime."""
    if isinstance(moment, datetime):
        return {"dateTime": moment.astimezone(tz).isoformat(), "timeZone": tz.key}
    return {"date": moment.isoformat()}


def reminders_body(minutes: Optional[List[int]]) -> Dict[str, Any]:
    """None keeps the calendar defaults, an empty list turns notifications off."""
    if minutes is None:
        return {"useDefault": True}
    overrides = sorted(set(minutes), reverse=True)[:CFG.MAX_OVERRIDES]
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": m} for m in overrides]}


class GoogleCalendarClient:
    """Events of one Google calendar"""

    def __init__(self, credentials_file: Path, calendar_id: str) -> None:
        self._credentials_file = credentials_file
        self._calendar_id = calendar_id
        self._credentials = None
        self._token_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(base_url=CFG.API_URL, timeout=CFG.TIMEOUT)

    async def startup(self) -> None:
        try:
            self._credentials, _ = await asyncio.to_thread(self._load_credentials)
        except (OSError, GoogleAuthError, ValueError) as e:
            logger.error("Bad configuration: GOOGLE_CREDENTIALS_FILE '{}': {}", self._credentials_file, e)
            raise UpstreamError(f"Can not load credentials '{self._credentials_file}': {e}", service=SERVICE) from e
        logger.info("Google Calendar client ready: calendar '{}'", self._calendar_id)

    def _load_credentials(self) -> Tuple[Any, Optional[str]]:
        """Blocking: reads the credentials file."""
        info = json.loads(self._credentials_file.read_text())
        # The OAuth client file is the input of the consent, not a token: an easy one to mix up
        if "installed" in info or "web" in info:
            raise ValueError("this is an OAuth client file, not a token: pass it to scripts/authorize_google.py --client")
        return google.auth.load_credentials_from_dict(info, scopes=list(CFG.SCOPES))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_events(
        self,
        time_min: datetime,
        time_max: datetime,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Event occurrences overlapping the range, recurring series expanded, sorted by start."""
        params: Dict[str, Any] = {
            "timeMin": iso_utc(time_min),
            "timeMax": iso_utc(time_max),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": CFG.PAGE_SIZE,
        }
        if query:
            params["q"] = query

        items: List[Dict[str, Any]] = []
        while True:
            page = (await self._request("GET", self._events_path(), params=params)).json()
            items.extend(page.get("items", []))
            if limit is not None and len(items) >= limit:
                return items[:limit]
            if not (token := page.get("nextPageToken")):
                return items
            params["pageToken"] = token

    async def get_event(self, event_id: str) -> Dict[str, Any]:
        return (await self._request("GET", self._events_path(event_id))).json()

    async def insert_event(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return (await self._request("POST", self._events_path(), json=body)).json()

    async def update_event(self, event_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Full replace: a field missing from the body is cleared."""
        return (await self._request("PUT", self._events_path(event_id), json=body)).json()

    async def delete_event(self, event_id: str) -> None:
        await self._request("DELETE", self._events_path(event_id))

    def _events_path(self, event_id: Optional[str] = None) -> str:
        path = f"/calendars/{quote(self._calendar_id, safe='')}/events"
        return f"{path}/{quote(event_id, safe='')}" if event_id else path

    async def _token(self) -> str:
        async with self._token_lock:
            if self._credentials is None:
                raise UpstreamError("Client is not started", service=SERVICE)
            if not self._credentials.valid:
                try:
                    await asyncio.to_thread(self._credentials.refresh, GoogleRequest())
                except RefreshError as e:
                    # invalid_grant here means a revoked token or an OAuth app left in Testing mode
                    raise UpstreamError(f"Token refresh failed: {e}", service=SERVICE) from e
                except GoogleAuthError as e:
                    raise UpstreamUnavailableError(f"Token refresh failed: {e}", service=SERVICE) from e
                logger.debug("Google access token refreshed")
            return self._credentials.token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        try:
            response = await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError(f"No answer in {CFG.TIMEOUT}s", service=SERVICE) from e
        except httpx.TransportError as e:
            raise UpstreamUnavailableError(f"Unreachable: {e}", service=SERVICE) from e

        if response.is_success:
            return response

        reason = self._reason(response)
        if response.status_code in (404, 410):
            raise NotFoundError("Event not found")
        if response.status_code in (401, 403):
            raise UpstreamError(f"Access denied ({reason}): check the credentials and calendar sharing", service=SERVICE)
        if response.status_code < 500 and response.status_code != 429:
            raise UpstreamRejectedError(reason, service=SERVICE)
        raise UpstreamUnavailableError(f"HTTP {response.status_code}: {reason}", service=SERVICE)

    @staticmethod
    def _reason(response: httpx.Response) -> str:
        try:
            return response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            return response.text[:200] or f"HTTP {response.status_code}"


def all_day_end(start: date, last_day: Optional[date]) -> date:
    """Google keeps the end of an all-day event exclusive, the agent names the last day."""
    return (last_day or start) + timedelta(days=1)
