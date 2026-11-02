"""Note mutations and small typed projections for Calendar/task integrations."""

from uuid import uuid4

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.core.notes.codec import parse_note, render_note
from app.core.notes.index import NotesIndex
from app.core.notes.models import DIRECTORIES, KINDS, MAX_PEOPLE, NoteRecord, NotesConflictError, canonical_id, timestamp
from app.utils.time import local_timestamp

EDITABLE_FIELDS = frozenset({"title", "body", "status", "tags", "due_at", "remind_at", "aliases"})


class NoteRepository:
    def __init__(self, index: NotesIndex) -> None:
        self.index, self.files = index, index.files

    def create(self, title: str, body: str, kind: str = "note", tags: list[str] | None = None,
               due_at: str | None = None, remind_at: str | None = None, aliases: list[str] | None = None) -> dict:
        self.index.refresh(force=True)
        if kind not in KINDS:
            raise InvalidRequestError("type must be note, task, idea, knowledge or person")
        if aliases is not None and kind != "person":
            raise InvalidRequestError("aliases can only be set on a person card")
        note_id, now = str(uuid4()), local_timestamp(self.files.tz)
        metadata = {"id": note_id, "title": title, "type": kind, "status": "open", "created_at": now,
                    "updated_at": now, "tags": tags or []}
        if kind == "person":
            metadata["aliases"] = [] if aliases is None else aliases
        metadata.update({key: value for key, value in (("due_at", due_at), ("remind_at", remind_at)) if value is not None})
        raw = render_note(metadata, body, self.files.tz)
        self.index.check_capacity(raw)
        path = f"{DIRECTORIES[kind]}/{note_id}.md"
        try:
            self.files.create(path, raw, note_id)
        finally:
            self.index.invalidate()
        return self.index.summary(parse_note(raw, path, self.files.tz))

    def _replace(self, current: NoteRecord, raw: bytes, expected_version: str) -> NoteRecord:
        self.index.check_capacity(raw, current)
        try:
            self.files.replace(current, raw, expected_version)
        finally:
            self.index.invalidate()
        return parse_note(raw, current.relative_path, self.files.tz)

    def _update(self, current: NoteRecord, expected_version: str, patch: dict) -> dict:
        if not isinstance(patch, dict) or not patch or set(patch) - EDITABLE_FIELDS:
            raise InvalidRequestError("Unsupported or empty note update")
        if "aliases" in patch and current.metadata["type"] != "person":
            raise InvalidRequestError("aliases can only be changed on a person card")
        if current.archived:
            raise InvalidRequestError("Archived notes are read-only in version 1")
        if patch.get("status") == "archived":
            raise InvalidRequestError("Use archive_note to archive a note")
        metadata = {**current.metadata, **{key: value for key, value in patch.items() if key != "body"}, "updated_at": local_timestamp(self.files.tz)}
        raw = render_note(metadata, patch.get("body", current.body), self.files.tz)
        return self.index.summary(self._replace(current, raw, expected_version))

    def append(self, note_id: str, text: str, expected_version: str) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise InvalidRequestError("Append text must be nonempty")
        current = self.index.lookup(note_id)
        return self._update(current, expected_version, {"body": current.body.rstrip() + "\n\n" + text})

    def update(self, note_id: str, expected_version: str, patch: dict) -> dict:
        return self._update(self.index.lookup(note_id), expected_version, patch)

    def archive(self, note_id: str, expected_version: str) -> dict:
        current = self.index.lookup(note_id)
        if current.archived:
            if current.version != expected_version:
                raise NotesConflictError("Note version changed; read the note again")
            return self.index.summary(current)
        metadata = {**current.metadata, "status": "archived", "updated_at": local_timestamp(self.files.tz)}
        raw = render_note(metadata, current.body, self.files.tz)
        self._replace(current, raw, expected_version)
        try:
            destination = self.files.archive(current, raw, expected_version)
        finally:
            self.index.invalidate()
        return self.index.summary(parse_note(raw, destination, self.files.tz))

    def validate_people(self, person_ids: list[str]) -> list[str]:
        if not isinstance(person_ids, list) or len(person_ids) > MAX_PEOPLE:
            raise InvalidRequestError("person_ids must be a list of at most 10 person UUIDs")
        identifiers = sorted({canonical_id(value) for value in person_ids})
        self.files.root_fd
        if not identifiers:
            return []
        self.index.refresh(force=True)
        self.index.require_complete()
        for person_id in identifiers:
            indexed = self.index.by_id.get(person_id)
            if indexed is None:
                raise NotFoundError("Person not found, invalid or its id is duplicated")
            person = self.index.current(indexed)
            if person.metadata["type"] != "person" or person.archived:
                raise InvalidRequestError("Meeting references must point to non-archived person cards")
        return identifiers

    def _task(self, record: NoteRecord) -> dict:
        result = self.index.summary(record)
        result.update({key: timestamp(record.metadata.get(key), self.files.tz, key) for key in ("due_at", "remind_at")})
        return result

    def scan_tasks(self) -> list[dict]:
        self.index.refresh(force=True)
        self.index.require_complete()
        return [self._task(record) for record in self.index.by_id.values() if record.metadata["type"] == "task" and not record.archived]

    def get_task(self, note_id: str) -> dict | None:
        try:
            record = self.index.lookup(note_id)
        except (NotFoundError, InvalidRequestError, NotesConflictError):
            return None
        return self._task(record) if record.metadata["type"] == "task" and not record.archived else None
