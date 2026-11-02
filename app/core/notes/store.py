"""Thread-safe public facade for Secretary notes.

The repository owns mutations, the index owns bounded discovery, queries own
retrieval, and NoteFiles owns recoverable filesystem changes.
"""

import threading

from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.notes.filesystem import NoteFiles
from app.core.notes.index import INDEX_TTL_SECONDS, MAX_INDEX_BYTES, NotesIndex
from app.core.notes.models import MAX_READ_CHARS, MAX_RESULTS
from app.core.notes.queries import NoteQueries
from app.core.notes.repository import NoteRepository


class NotesStore:
    def __init__(self, root: Path, state_dir: Path, tz: ZoneInfo, vault_name: str) -> None:
        self.root, self.state_dir, self.tz, self.vault_name = Path(root), Path(state_dir), tz, vault_name
        self._lock = threading.RLock()
        self._files = NoteFiles(self.root, self.state_dir, tz, vault_name)
        self._index = NotesIndex(self._files)
        self._queries = NoteQueries(self._index)
        self._repository = NoteRepository(self._index)

    def _run(self, operation, *args, **kwargs):
        with self._lock:
            return operation(*args, **kwargs)

    def initialize(self) -> None:
        self._run(self._files.initialize)

    def close(self) -> None:
        with self._lock:
            self._files.close()
            self._index.clear()
            self._queries.clear()

    def get_context(self, query: str, kind: str | None = None, tag: str | None = None) -> str:
        return self._run(self._queries.get_context, query, kind, tag)

    def search(self, query: str = "", kind: str | None = None, status: str | None = None,
               tag: str | None = None, include_archive: bool = False, limit: int = MAX_RESULTS) -> dict:
        return self._run(self._queries.search, query, kind, status, tag, include_archive, limit)

    def read(self, note_id: str, offset: int = 0, limit: int = MAX_READ_CHARS, section: str | None = None) -> dict:
        return self._run(self._queries.read, note_id, offset, limit, section)

    def create(self, title: str, body: str, kind: str = "note", tags: list[str] | None = None,
               due_at: str | None = None, remind_at: str | None = None, aliases: list[str] | None = None) -> dict:
        return self._run(self._repository.create, title, body, kind, tags, due_at, remind_at, aliases)

    def append(self, note_id: str, text: str, expected_version: str) -> dict:
        return self._run(self._repository.append, note_id, text, expected_version)

    def update(self, note_id: str, expected_version: str, patch: dict) -> dict:
        return self._run(self._repository.update, note_id, expected_version, patch)

    def archive(self, note_id: str, expected_version: str) -> dict:
        return self._run(self._repository.archive, note_id, expected_version)

    def validate_people(self, person_ids: list[str]) -> list[str]:
        return self._run(self._repository.validate_people, person_ids)

    def scan_tasks(self) -> list[dict]:
        return self._run(self._repository.scan_tasks)

    def get_task(self, note_id: str) -> dict | None:
        return self._run(self._repository.get_task, note_id)

    def status(self) -> dict:
        return self._run(self._index.status)
