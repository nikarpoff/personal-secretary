"""Domain-level entities"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple


@dataclass(frozen=True)
class Event:
    """Google Calendar event; times are timezone-aware."""
    id: str
    title: str
    start: datetime
    end: datetime                   # exclusive, as Google keeps it
    all_day: bool = False
    description: str = ""
    location: str = ""
    important: bool = False
    own_reminders: Optional[Tuple[int, ...]] = None     # None: the event follows the default reminders
    recurring_event_id: Optional[str] = None
    html_link: Optional[str] = None
    created: Optional[datetime] = None

    @property
    def series_id(self) -> str:
        """Series identifier for an occurrence, or the event's own identifier."""
        return self.recurring_event_id or self.id


@dataclass(frozen=True)
class Reminder:
    """Reminder due now: one message, every passed offset marked as sent"""
    event: Event
    minutes: int                    # the closest to the start offset that fired
    marks: Tuple[Tuple[str, str, int], ...] = ()   # (event id, start in UTC, minutes) of every offset it covers
