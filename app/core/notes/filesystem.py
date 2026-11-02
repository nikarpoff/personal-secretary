"""Secretary scope and recoverable note mutations; no indexing or search policy."""

from __future__ import annotations

import hashlib
import json
import os
import sys

from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.core.exceptions import InvalidRequestError
from app.core.notes.codec import parse_note
from app.core.notes.models import ALLOWED_DIRECTORIES, MAX_DEPTH, MAX_NOTE_BYTES, VERSION, NoteRecord, NotesConflictError, NotesUnavailableError
from app.utils.files import AtomicRenamer, AtomicRenameUnavailable, FileChangedError, child_directory, open_directory, parent_directory, read_regular, write_new
from app.utils.time import local_timestamp


class NoteFiles:
    def __init__(self, root: Path, state_dir: Path, tz: ZoneInfo, vault_name: str) -> None:
        self.root, self.state_dir, self.tz, self.vault_name = root, state_dir, tz, vault_name
        self._root = self._state = self._history = self._conflicts = None
        self._renamer: AtomicRenamer | None = None

    @property
    def root_fd(self) -> int:
        if self._root is None:
            raise NotesUnavailableError("Notes storage is not initialized")
        return self._root

    def initialize(self) -> None:
        if self._root is not None:
            return
        if sys.platform != "linux" or not hasattr(os, "O_NOFOLLOW"):
            raise NotesUnavailableError("Notes storage requires Linux with no-follow file operations")
        if not self.vault_name or len(self.vault_name) > 100 or any(c in self.vault_name for c in "/\\\r\n\x00"):
            raise InvalidRequestError("OBSIDIAN_VAULT_NAME must be a single vault name")
        try:
            self._renamer = AtomicRenamer()
            self._root = open_directory(self.root, create=True)
            self._state = open_directory(self.state_dir, create=True)
            for directory in ALLOWED_DIRECTORIES:
                os.close(child_directory(self.root_fd, directory, create=True))
            self._history = child_directory(self.root_fd, ".secretary-history", create=True)
            self._conflicts = child_directory(self._state, "conflicts", create=True)
        except (OSError, ValueError) as exc:
            self.close()
            raise NotesUnavailableError("Cannot securely open notes/state mounts or Linux atomic rename is unavailable") from exc

    def close(self) -> None:
        for attribute in ("_conflicts", "_history", "_state", "_root"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, attribute, None)
        self._renamer = None

    def parent(self, path: str):
        parts = PurePosixPath(path).parts
        if (len(parts) < 2 or parts[0] not in ALLOWED_DIRECTORIES or len(parts) > MAX_DEPTH + 2
                or PurePosixPath(path).is_absolute()
                or any(part in ("", ".", "..") or part.startswith(".") or "\\" in part or "\x00" in part for part in parts)
                or not parts[-1].endswith(".md") or len(path) > 256 or len(path.encode("utf-8")) > 512):
            raise InvalidRequestError("Unsupported note path")
        return parent_directory(self.root_fd, parts)

    @staticmethod
    def read_at(parent: int, name: str) -> tuple[bytes, os.stat_result]:
        try:
            return read_regular(parent, name, MAX_NOTE_BYTES)
        except FileChangedError as exc:
            raise NotesConflictError("Note is changing; retry after synchronization finishes") from exc
        except ValueError as exc:
            raise InvalidRequestError("Note must be a regular, non-linked file up to 128 KiB") from exc

    def read(self, path: str) -> tuple[bytes, os.stat_result]:
        with self.parent(path) as (parent, name):
            return self.read_at(parent, name)

    def load(self, path: str) -> NoteRecord:
        return parse_note(self.read(path)[0], path, self.tz)

    def source(self, record: NoteRecord) -> str:
        result = "obsidian://open?" + urlencode({"vault": self.vault_name, "file": f"Secretary/{record.relative_path}"}, quote_via=quote)
        if len(result) > 1500:
            raise InvalidRequestError("Encoded note source is too long")
        return result

    def _move(self, source: tuple[int, str], target: tuple[int, str], *, exchange: bool = False) -> None:
        if self._renamer is None:
            raise NotesUnavailableError("Notes storage is not initialized")
        try:
            self._renamer.move(source, target, exchange=exchange)
        except AtomicRenameUnavailable as exc:
            raise NotesUnavailableError("Notes filesystem must support atomic exchange/no-replace on one mount") from exc

    def _history_copy(self, raw: bytes, prefix: str) -> str:
        if self._history is None:
            raise NotesUnavailableError("Notes storage is not initialized")
        filename = f"{prefix}-{uuid4()}.md"
        write_new(self._history, filename, raw)
        return filename

    def _conflict(self, record: NoteRecord, expected: str, history: list[str]) -> str:
        conflict_id = str(uuid4())
        document = {"conflict_id": conflict_id, "note_id": record.id, "expected_version": expected,
                    "created_at": local_timestamp(self.tz), "history": history}
        write_new(self._conflicts, f"{conflict_id}.json", json.dumps(document, ensure_ascii=False).encode("utf-8"))
        return conflict_id

    def create(self, path: str, raw: bytes, note_id: str) -> None:
        staged = self._history_copy(raw, f"create-{note_id}")
        with self.parent(path) as target:
            self._move((self._history, staged), target)

    def replace(self, record: NoteRecord, raw: bytes, expected: str) -> None:
        if not isinstance(expected, str) or not VERSION.fullmatch(expected):
            raise InvalidRequestError("expected_version must be the SHA-256 returned by read_note")
        if record.version != expected:
            proposal = self._history_copy(raw, f"proposal-{record.id}")
            conflict = self._conflict(record, expected, [proposal])
            raise NotesConflictError(f"Note version changed; user file kept, proposed edit saved; conflict_id={conflict}. Read the note again.")
        staged = self._history_copy(raw, f"revision-{record.id}")
        with self.parent(record.relative_path) as target:
            try:
                self._move((self._history, staged), target, exchange=True)
            except FileNotFoundError as exc:
                conflict = self._conflict(record, expected, [staged])
                raise NotesConflictError(f"Note moved/deleted; proposed edit saved; conflict_id={conflict}") from exc
            try:
                matches = hashlib.sha256(self.read_at(self._history, staged)[0]).hexdigest() == expected
            except (OSError, InvalidRequestError, NotesConflictError):
                matches = False
            if not matches:
                proposal = self._history_copy(raw, f"proposal-{record.id}")
                try:
                    self._move((self._history, staged), target, exchange=True)
                except OSError:
                    pass  # Keep every displaced inode; no destructive fallback after an external move.
                conflict = self._conflict(record, expected, [staged, proposal])
                raise NotesConflictError(f"Concurrent edit detected; all versions preserved; conflict_id={conflict}. Read the note again.")

    def archive(self, record: NoteRecord, raw: bytes, expected: str) -> str:
        destination = f"Archive/{record.id}.md"
        with self.parent(record.relative_path) as source, self.parent(destination) as target:
            try:
                self._move(source, target)
            except FileExistsError as exc:
                raise NotesConflictError("Archive destination already exists; note marked archived, no file overwritten") from exc
            try:
                unchanged = self.read_at(*target)[0] == raw
            except (OSError, InvalidRequestError, NotesConflictError):
                unchanged = False
            if not unchanged:
                proposal = self._history_copy(raw, f"archive-proposal-{record.id}")
                conflict = self._conflict(record, expected, [proposal])
                raise NotesConflictError(f"Note changed during archive; current file retained in Archive, proposal saved; conflict_id={conflict}. Search again.")
        return destination

    def conflict_count(self) -> int:
        if self._conflicts is None:
            return 0
        with os.scandir(self._conflicts) as entries:
            return sum(entry.name.endswith(".json") and entry.is_file(follow_symlinks=False) for entry in entries)
