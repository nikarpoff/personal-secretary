"""
SQLite knowledge base: notes, the calendar mirror, sent reminders, search indexes.

sqlite3 is blocking, so every call runs on one dedicated thread: the connection never
crosses threads and writes are serialized without any locking of our own. One table
`kb_fts` (FTS5) indexes notes and events together, `embeddings` keeps their vectors
with the model signature: a vector of another model is treated as missing.
"""

import asyncio
import json
import sqlite3

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, TypeVar

import numpy as np

from loguru import logger

from app.core.clock import from_iso, iso_utc
from app.core.entities import Event, ItemKind, Note
from app.core.exceptions import NotFoundError

T = TypeVar("T")

Mark = Tuple[str, str, int]         # event id, start in UTC, minutes before
ItemKey = Tuple[str, str]           # kind, ref

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    event_id    TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_event ON notes (event_id);

CREATE TABLE IF NOT EXISTS events (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    location            TEXT NOT NULL,
    start_at            TEXT NOT NULL,
    end_at              TEXT NOT NULL,
    all_day             INTEGER NOT NULL,
    important           INTEGER NOT NULL,
    own_reminders       TEXT,
    recurring_event_id  TEXT,
    html_link           TEXT,
    created             TEXT
);
CREATE INDEX IF NOT EXISTS events_start ON events (start_at);

CREATE TABLE IF NOT EXISTS sent_reminders (
    event_id    TEXT NOT NULL,
    start_at    TEXT NOT NULL,
    minutes     INTEGER NOT NULL,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (event_id, start_at, minutes)
);

CREATE TABLE IF NOT EXISTS embeddings (
    kind        TEXT NOT NULL,
    ref         TEXT NOT NULL,
    signature   TEXT NOT NULL,
    vector      BLOB NOT NULL,
    PRIMARY KEY (kind, ref)
);

CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
    kind UNINDEXED, ref UNINDEXED, title, body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

_EVENT_COLUMNS = (
    "id, title, description, location, start_at, end_at, all_day, important, "
    "own_reminders, recurring_event_id, html_link, created"
)
_NOTE_COLUMNS = "id, title, content, tags, event_id, created_at, updated_at"


class KnowledgeRepository:
    """Notes, mirrored events and their indexes in one SQLite file"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")

    async def startup(self) -> None:
        await self._run(self._open)
        logger.info("Knowledge base ready: '{}'", self._path)

    async def shutdown(self) -> None:
        await self._run(self._close)
        self._executor.shutdown(wait=True)

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(fn, *args))

    def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Knowledge base is not open")
        return self._conn

    # ---- notes -------------------------------------------------------------

    async def add_note(self, title: str, content: str, tags: Sequence[str], event_id: Optional[str], now: datetime) -> Note:
        return await self._run(self._add_note, title, content, tags, event_id, now)

    async def update_note(self, note_id: int, changes: Dict[str, Any], now: datetime) -> Note:
        """Applies the given columns only: title, content, tags, event_id."""
        return await self._run(self._update_note, note_id, changes, now)

    async def delete_note(self, note_id: int) -> None:
        await self._run(self._delete_note, note_id)

    async def get_note(self, note_id: int) -> Note:
        return await self._run(self._get_note, note_id)

    async def list_notes(self, tag: Optional[str], event_ids: Sequence[str], limit: int) -> List[Note]:
        """The latest notes first; filters combine."""
        return await self._run(self._list_notes, tag, list(event_ids), limit)

    def _add_note(self, title: str, content: str, tags: Sequence[str], event_id: Optional[str], now: datetime) -> Note:
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO notes (title, content, tags, event_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (title, content, json.dumps(list(tags), ensure_ascii=False), event_id, iso_utc(now), iso_utc(now)),
            )
            note = self._get_note(cursor.lastrowid)
            self._index_note(note)
        return note

    def _update_note(self, note_id: int, changes: Dict[str, Any], now: datetime) -> Note:
        current = self._get_note(note_id)
        values = {
            "title": changes.get("title", current.title),
            "content": changes.get("content", current.content),
            "tags": json.dumps(list(changes.get("tags", current.tags)), ensure_ascii=False),
            "event_id": changes.get("event_id", current.event_id),
        }
        with self._db:
            self._db.execute(
                "UPDATE notes SET title = ?, content = ?, tags = ?, event_id = ?, updated_at = ? WHERE id = ?",
                (values["title"], values["content"], values["tags"], values["event_id"], iso_utc(now), note_id),
            )
            note = self._get_note(note_id)
            self._index_note(note)
        return note

    def _delete_note(self, note_id: int) -> None:
        with self._db:
            if self._db.execute("DELETE FROM notes WHERE id = ?", (note_id, )).rowcount == 0:
                raise NotFoundError("Note not found")
            self._unindex(ItemKind.NOTE, str(note_id))

    def _get_note(self, note_id: int) -> Note:
        row = self._db.execute(f"SELECT {_NOTE_COLUMNS} FROM notes WHERE id = ?", (note_id, )).fetchone()
        if row is None:
            raise NotFoundError("Note not found")
        return self._note(row)

    def _list_notes(self, tag: Optional[str], event_ids: List[str], limit: int) -> List[Note]:
        where, params = [], []
        if tag:
            where.append("EXISTS (SELECT 1 FROM json_each(notes.tags) WHERE value = ?)")
            params.append(tag)
        if event_ids:
            where.append(f"event_id IN ({', '.join('?' * len(event_ids))})")
            params.extend(event_ids)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._db.execute(
            f"SELECT {_NOTE_COLUMNS} FROM notes {clause} ORDER BY updated_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._note(row) for row in rows]

    def _index_note(self, note: Note) -> None:
        body = "\n".join([note.content, " ".join(f"#{tag}" for tag in note.tags)])
        self._reindex(ItemKind.NOTE, str(note.id), note.title, body)

    # ---- calendar mirror ---------------------------------------------------

    async def upsert_events(self, events: Sequence[Event]) -> int:
        """Number of events new or changed."""
        return await self._run(self._upsert_events, list(events))

    async def replace_window(self, events: Sequence[Event], time_min: datetime, time_max: datetime) -> Tuple[int, int]:
        """Mirror of a range: upserts the events, drops the ones starting in it that are gone."""
        return await self._run(self._replace_window, list(events), time_min, time_max)

    async def delete_event(self, event_id: str) -> None:
        """Drops the event, or the whole series when the id is a recurring one."""
        await self._run(self._delete_event, event_id)

    async def events_starting(self, start: datetime, end: datetime) -> List[Event]:
        return await self._run(self._events_starting, start, end)

    def _upsert_events(self, events: List[Event]) -> int:
        with self._db:
            return sum(self._upsert_event(event) for event in events)

    def _replace_window(self, events: List[Event], time_min: datetime, time_max: datetime) -> Tuple[int, int]:
        with self._db:
            changed = sum(self._upsert_event(event) for event in events)
            keep = {event.id for event in events}
            stale = [
                row["id"] for row in self._db.execute(
                    "SELECT id FROM events WHERE start_at >= ? AND start_at < ?",
                    (iso_utc(time_min), iso_utc(time_max)),
                )
                if row["id"] not in keep
            ]
            for event_id in stale:
                self._drop_event(event_id)
        return changed, len(stale)

    def _upsert_event(self, event: Event) -> bool:
        row = (
            event.id, event.title, event.description, event.location,
            iso_utc(event.start), iso_utc(event.end), int(event.all_day), int(event.important),
            None if event.own_reminders is None else json.dumps(list(event.own_reminders)),
            event.recurring_event_id, event.html_link,
            iso_utc(event.created) if event.created else None,
        )
        current = self._db.execute(f"SELECT {_EVENT_COLUMNS} FROM events WHERE id = ?", (event.id, )).fetchone()
        if current is not None and tuple(current) == row:
            return False

        self._db.execute(
            f"INSERT OR REPLACE INTO events ({_EVENT_COLUMNS}) VALUES ({', '.join('?' * len(row))})", row
        )
        text_changed = current is None or (current["title"], current["description"], current["location"]) != row[1:4]
        if text_changed:
            body = "\n".join(part for part in (event.description, event.location) if part)
            self._reindex(ItemKind.EVENT, event.id, event.title, body)
        return True

    def _delete_event(self, event_id: str) -> None:
        with self._db:
            rows = self._db.execute(
                "SELECT id FROM events WHERE id = ? OR recurring_event_id = ?", (event_id, event_id)
            ).fetchall()
            for row in rows:
                self._drop_event(row["id"])

    def _drop_event(self, event_id: str) -> None:
        self._db.execute("DELETE FROM events WHERE id = ?", (event_id, ))
        self._unindex(ItemKind.EVENT, event_id)

    def _events_starting(self, start: datetime, end: datetime) -> List[Event]:
        rows = self._db.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events WHERE start_at >= ? AND start_at < ? ORDER BY start_at",
            (iso_utc(start), iso_utc(end)),
        ).fetchall()
        return [self._event(row) for row in rows]

    # ---- sent reminders ----------------------------------------------------

    async def sent_marks(self, event_ids: Sequence[str]) -> Set[Mark]:
        return await self._run(self._sent_marks, list(event_ids))

    async def mark_sent(self, marks: Sequence[Mark], now: datetime) -> None:
        await self._run(self._mark_sent, list(marks), now)

    async def purge_marks(self, before: datetime) -> int:
        return await self._run(self._purge_marks, before)

    def _sent_marks(self, event_ids: List[str]) -> Set[Mark]:
        if not event_ids:
            return set()
        rows = self._db.execute(
            f"SELECT event_id, start_at, minutes FROM sent_reminders WHERE event_id IN ({', '.join('?' * len(event_ids))})",
            event_ids,
        ).fetchall()
        return {(row["event_id"], row["start_at"], row["minutes"]) for row in rows}

    def _mark_sent(self, marks: List[Mark], now: datetime) -> None:
        with self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO sent_reminders (event_id, start_at, minutes, sent_at) VALUES (?, ?, ?, ?)",
                [(*mark, iso_utc(now)) for mark in marks],
            )

    def _purge_marks(self, before: datetime) -> int:
        with self._db:
            return self._db.execute("DELETE FROM sent_reminders WHERE start_at < ?", (iso_utc(before), )).rowcount

    # ---- search indexes ----------------------------------------------------

    async def full_text(self, match: str, kinds: Sequence[ItemKind], limit: int) -> List[ItemKey]:
        """BM25 ranking of an FTS5 query, best first."""
        return await self._run(self._full_text, match, list(kinds), limit)

    async def vectors(self, kinds: Sequence[ItemKind], signature: str) -> Tuple[List[ItemKey], np.ndarray]:
        return await self._run(self._vectors, list(kinds), signature)

    async def pending_embeddings(self, signature: str, limit: int) -> List[Tuple[ItemKey, str]]:
        """Items without a vector of this signature and the text to embed."""
        return await self._run(self._pending_embeddings, signature, limit)

    async def store_vectors(self, keys: Sequence[ItemKey], vectors: np.ndarray, signature: str) -> None:
        await self._run(self._store_vectors, list(keys), vectors, signature)

    async def items(self, keys: Sequence[ItemKey]) -> Dict[ItemKey, Any]:
        """Notes and events by key; missing keys are skipped."""
        return await self._run(self._items, list(keys))

    def _full_text(self, match: str, kinds: List[ItemKind], limit: int) -> List[ItemKey]:
        # Title matches weigh twice the body ones; the kind and ref columns are not indexed
        rows = self._db.execute(
            f"SELECT kind, ref FROM kb_fts WHERE kb_fts MATCH ? AND kind IN ({', '.join('?' * len(kinds))}) "
            "ORDER BY bm25(kb_fts, 0.0, 0.0, 2.0, 1.0) LIMIT ?",
            (match, *[str(kind) for kind in kinds], limit),
        ).fetchall()
        return [(row["kind"], row["ref"]) for row in rows]

    def _vectors(self, kinds: List[ItemKind], signature: str) -> Tuple[List[ItemKey], np.ndarray]:
        rows = self._db.execute(
            f"SELECT kind, ref, vector FROM embeddings WHERE signature = ? AND kind IN ({', '.join('?' * len(kinds))})",
            (signature, *[str(kind) for kind in kinds]),
        ).fetchall()
        if not rows:
            return [], np.empty((0, 0), dtype=np.float32)
        keys = [(row["kind"], row["ref"]) for row in rows]
        return keys, np.vstack([np.frombuffer(row["vector"], dtype=np.float32) for row in rows])

    def _pending_embeddings(self, signature: str, limit: int) -> List[Tuple[ItemKey, str]]:
        rows = self._db.execute(
            "SELECT f.kind, f.ref, f.title, f.body FROM kb_fts f "
            "LEFT JOIN embeddings e ON e.kind = f.kind AND e.ref = f.ref AND e.signature = ? "
            "WHERE e.ref IS NULL LIMIT ?",
            (signature, limit),
        ).fetchall()
        return [((row["kind"], row["ref"]), f"{row['title']}\n{row['body']}".strip()) for row in rows]

    def _store_vectors(self, keys: List[ItemKey], vectors: np.ndarray, signature: str) -> None:
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO embeddings (kind, ref, signature, vector) VALUES (?, ?, ?, ?)",
                [(kind, ref, signature, vector.astype(np.float32).tobytes()) for (kind, ref), vector in zip(keys, vectors, strict=True)],
            )

    def _items(self, keys: List[ItemKey]) -> Dict[ItemKey, Any]:
        found: Dict[ItemKey, Any] = {}
        note_ids = [ref for kind, ref in keys if kind == ItemKind.NOTE]
        event_ids = [ref for kind, ref in keys if kind == ItemKind.EVENT]
        if note_ids:
            rows = self._db.execute(
                f"SELECT {_NOTE_COLUMNS} FROM notes WHERE id IN ({', '.join('?' * len(note_ids))})",
                [int(ref) for ref in note_ids],
            )
            found.update(((str(ItemKind.NOTE), str(row["id"])), self._note(row)) for row in rows)
        if event_ids:
            rows = self._db.execute(
                f"SELECT {_EVENT_COLUMNS} FROM events WHERE id IN ({', '.join('?' * len(event_ids))})", event_ids
            )
            found.update(((str(ItemKind.EVENT), row["id"]), self._event(row)) for row in rows)
        return found

    def _reindex(self, kind: ItemKind, ref: str, title: str, body: str) -> None:
        """Fresh full-text row; the vector is dropped and gets rebuilt in the background."""
        self._unindex(kind, ref)
        self._db.execute("INSERT INTO kb_fts (kind, ref, title, body) VALUES (?, ?, ?, ?)", (str(kind), ref, title, body))

    def _unindex(self, kind: ItemKind, ref: str) -> None:
        self._db.execute("DELETE FROM kb_fts WHERE kind = ? AND ref = ?", (str(kind), ref))
        self._db.execute("DELETE FROM embeddings WHERE kind = ? AND ref = ?", (str(kind), ref))

    # ---- rows --------------------------------------------------------------

    @staticmethod
    def _note(row: sqlite3.Row) -> Note:
        return Note(
            id=row["id"],
            title=row["title"],
            content=row["content"],
            tags=tuple(json.loads(row["tags"])),
            event_id=row["event_id"],
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> Event:
        own = row["own_reminders"]
        return Event(
            id=row["id"],
            title=row["title"],
            start=from_iso(row["start_at"]),
            end=from_iso(row["end_at"]),
            all_day=bool(row["all_day"]),
            description=row["description"],
            location=row["location"],
            important=bool(row["important"]),
            own_reminders=None if own is None else tuple(json.loads(own)),
            recurring_event_id=row["recurring_event_id"],
            html_link=row["html_link"],
            created=from_iso(row["created"]),
        )

