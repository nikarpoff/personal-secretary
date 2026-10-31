"""In-memory stand-ins of the upstream clients"""

import itertools

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core.clock import iso_utc, local_midnight
from app.core.exceptions import NotFoundError

from zoneinfo import ZoneInfo


class FakeCalendar:
    """Google Calendar of single events; every event counts as created a day ago"""

    def __init__(self, tz: ZoneInfo) -> None:
        self.tz = tz
        self.items: Dict[str, Dict[str, Any]] = {}
        self._ids = itertools.count(1)

    async def startup(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def list_events(self, time_min: datetime, time_max: datetime, query: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        found = [
            item for item in self.items.values()
            if self._start(item) < time_max and self._end(item) > time_min
            and (not query or query.lower() in item.get("summary", "").lower())
        ]
        return sorted(found, key=self._start)[:limit]

    async def get_event(self, event_id: str) -> Dict[str, Any]:
        if event_id not in self.items:
            raise NotFoundError("Event not found")
        return dict(self.items[event_id])

    async def insert_event(self, body: Dict[str, Any]) -> Dict[str, Any]:
        event_id = f"ev{next(self._ids)}"
        created = datetime.now(timezone.utc) - timedelta(days=1)
        self.items[event_id] = {**body, "id": event_id, "status": "confirmed", "created": iso_utc(created)}
        return dict(self.items[event_id])

    async def update_event(self, event_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        await self.get_event(event_id)
        self.items[event_id] = dict(body)
        return dict(body)

    async def delete_event(self, event_id: str) -> None:
        await self.get_event(event_id)
        del self.items[event_id]

    def _start(self, item: Dict[str, Any]) -> datetime:
        return self._moment(item["start"])

    def _end(self, item: Dict[str, Any]) -> datetime:
        return self._moment(item["end"])

    def _moment(self, value: Dict[str, str]) -> datetime:
        if "date" in value:
            return local_midnight(date.fromisoformat(value["date"]), self.tz)
        return datetime.fromisoformat(value["dateTime"])


class FakeTelegram:
    """Collects the messages instead of sending them"""

    def __init__(self) -> None:
        self.messages: List[str] = []

    async def send_message(self, text: str) -> None:
        self.messages.append(text)

    async def aclose(self) -> None:
        pass
