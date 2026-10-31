"""Domain-level entities"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Optional, Tuple


class ItemKind(StrEnum):
    """What a knowledge base item is"""
    NOTE = "note"
    EVENT = "event"


@dataclass(frozen=True)
class Event:
    """Calendar event as the mirror keeps it; times are timezone-aware"""
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
        """Id the notes of any occurrence are linked to."""
        return self.recurring_event_id or self.id


@dataclass(frozen=True)
class Note:
    """Free-form note of the owner, optionally linked to an event"""
    id: int
    title: str
    content: str
    tags: Tuple[str, ...] = ()
    event_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class SearchHit:
    """Knowledge base item found by a query"""
    kind: ItemKind
    ref: str                        # note id or event id
    title: str
    snippet: str
    when: Optional[datetime]        # event start or note update
    score: float


@dataclass(frozen=True)
class Reminder:
    """Reminder due now: one message, every passed offset marked as sent"""
    event: Event
    minutes: int                    # the closest to the start offset that fired
    marks: Tuple[Tuple[str, str, int], ...] = ()   # (event id, start in UTC, minutes) of every offset it covers
