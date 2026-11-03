"""
Sent reminders, kept in SQLite: after a restart the same reminder must not go out again.

sqlite3 is blocking, so every call runs on one dedicated thread: the connection never
crosses threads and writes are serialized without any locking of our own.
"""

import asyncio
import sqlite3

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Set, Tuple, TypeVar

from loguru import logger

from app.utils.time import iso_utc

T = TypeVar("T")

Mark = Tuple[str, str, int]         # event id, start in UTC, minutes before

SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_reminders (
    event_id    TEXT NOT NULL,
    start_at    TEXT NOT NULL,
    minutes     INTEGER NOT NULL,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (event_id, start_at, minutes)
);
"""


class ReminderStore:
    """Marks of the reminders already sent"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")

    async def startup(self) -> None:
        await self._run(self._open)
        logger.info("Reminder store ready: '{}'", self._path)

    async def shutdown(self) -> None:
        await self._run(self._close)
        self._executor.shutdown(wait=True)

    async def sent_marks(self, event_ids: Sequence[str]) -> Set[Mark]:
        return await self._run(self._sent_marks, list(event_ids))

    async def mark_sent(self, marks: Sequence[Mark], now: datetime) -> None:
        await self._run(self._mark_sent, list(marks), now)

    async def purge(self, before: datetime) -> int:
        """Drops the marks of events that started before the moment."""
        return await self._run(self._purge, before)

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(fn, *args))

    def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Reminder store is not open")
        return self._conn

    def _sent_marks(self, event_ids: List[str]) -> Set[Mark]:
        if not event_ids:
            return set()
        rows = self._db.execute(
            f"SELECT event_id, start_at, minutes FROM sent_reminders WHERE event_id IN ({', '.join('?' * len(event_ids))})",
            event_ids,
        ).fetchall()
        return {tuple(row) for row in rows}

    def _mark_sent(self, marks: List[Mark], now: datetime) -> None:
        with self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO sent_reminders (event_id, start_at, minutes, sent_at) VALUES (?, ?, ?, ?)",
                [(*mark, iso_utc(now)) for mark in marks],
            )

    def _purge(self, before: datetime) -> int:
        with self._db:
            return self._db.execute("DELETE FROM sent_reminders WHERE start_at < ?", (iso_utc(before), )).rowcount
