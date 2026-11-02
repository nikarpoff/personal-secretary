"""Bounded local index shared by lookups, search and reminder scans."""

from __future__ import annotations

import os
import stat
import time

from loguru import logger

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.core.notes.codec import parse_note
from app.core.notes.filesystem import NoteFiles
from app.core.notes.models import ALLOWED_DIRECTORIES, MAX_DEPTH, MAX_FILES, NoteRecord, NotesConflictError, NotesUnavailableError, canonical_id
from app.utils.files import child_directory

INDEX_TTL_SECONDS = 3.0
MAX_INDEX_BYTES = 32 * 1024 * 1024


class NotesIndex:
    def __init__(self, files: NoteFiles) -> None:
        self.files = files
        self.clear()

    def clear(self) -> None:
        self.cache: dict[str, tuple[tuple, NoteRecord | None]] = {}
        self.by_id: dict[str, NoteRecord] = {}
        self.invalid_count = self.duplicate_count = self.index_bytes = 0
        self.truncated = self._walk_truncated = False
        self._last_scan = 0.0

    def invalidate(self) -> None:
        self._last_scan = 0.0

    def _walk(self, descriptor: int, prefix: str, budget: list[int], depth: int = 0):
        if depth > MAX_DEPTH:
            return
        with os.scandir(descriptor) as entries:
            for entry in entries:
                budget[0] += 1
                if budget[0] > MAX_FILES * 4:
                    self._walk_truncated = True
                    return
                if entry.name.startswith(".") or "\\" in entry.name:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    path = f"{prefix}/{entry.name}"
                    if stat.S_ISDIR(info.st_mode):
                        child = child_directory(descriptor, entry.name)
                        try:
                            yield from self._walk(child, path, budget, depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and entry.name.endswith(".md"):
                        yield path, info
                except OSError:
                    continue  # External synchronization can move entries during a scan.

    @staticmethod
    def _signature(info: os.stat_result) -> tuple:
        return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size

    def refresh(self, *, force: bool = False) -> None:
        root = self.files.root_fd
        if not force and time.monotonic() - self._last_scan < INDEX_TTL_SECONDS:
            return
        cache, by_id, duplicates = {}, {}, set()
        invalid = count = index_bytes = 0
        truncated = self._walk_truncated = False
        budget = [0]
        for directory in sorted(ALLOWED_DIRECTORIES):
            try:
                descriptor = child_directory(root, directory)
            except OSError:
                invalid += 1
                continue
            try:
                for path, info in self._walk(descriptor, directory, budget):
                    count += 1
                    if count > MAX_FILES:
                        truncated = True
                        break
                    signature = self._signature(info)
                    cached = self.cache.get(path)
                    if cached is not None and cached[0] == signature:
                        record = cached[1]
                    else:
                        try:
                            raw, actual = self.files.read(path)
                            record = parse_note(raw, path, self.files.tz)
                            self.files.source(record)
                            signature = self._signature(actual)
                        except (OSError, InvalidRequestError, NotesConflictError):
                            record = None
                    if record is not None:
                        index_bytes += len(record.raw)
                        if index_bytes > MAX_INDEX_BYTES:
                            truncated = True
                            break
                    cache[path] = signature, record
                    if record is None:
                        invalid += 1
                    elif record.id in by_id:
                        duplicates.add(record.id)
                    else:
                        by_id[record.id] = record
            finally:
                os.close(descriptor)
            truncated = truncated or self._walk_truncated
            if truncated:
                break
        for note_id in duplicates:
            by_id.pop(note_id, None)
        self.cache, self.by_id = cache, by_id
        self.invalid_count, self.duplicate_count = invalid, len(duplicates)
        self.index_bytes, self.truncated = min(index_bytes, MAX_INDEX_BYTES), truncated
        self._last_scan = time.monotonic()
        if invalid or duplicates or truncated:
            logger.warning("Notes index: {} invalid files, {} duplicated ids, limit reached={}", invalid, len(duplicates), truncated)

    def require_complete(self) -> None:
        if self.truncated:
            raise NotesUnavailableError("Notes index exceeds its safety limits; reduce the Secretary scope before reading or editing")

    def current(self, indexed: NoteRecord) -> NoteRecord:
        try:
            current = self.files.load(indexed.relative_path)
        except OSError as exc:
            raise NotFoundError("Note changed during synchronization; search again") from exc
        if current.id != indexed.id:
            raise NotesConflictError("Note identity changed; search again")
        return current

    def lookup(self, note_id: str) -> NoteRecord:
        canonical_id(note_id)
        self.refresh(force=True)
        self.require_complete()
        indexed = self.by_id.get(note_id)
        if indexed is None:
            raise NotFoundError("Note not found, invalid or its id is duplicated")
        return self.current(indexed)

    def check_capacity(self, raw: bytes, current: NoteRecord | None = None) -> None:
        self.require_complete()
        if current is None and len(self.cache) >= MAX_FILES:
            raise NotesUnavailableError("Notes index is at its safety limit; reduce the Secretary scope before creating notes")
        indexed = self.cache.get(current.relative_path, (None, current))[1] if current is not None else None
        if self.index_bytes + len(raw) - (len(indexed.raw) if indexed is not None else 0) > MAX_INDEX_BYTES:
            raise NotesUnavailableError("This change would exceed the notes index byte limit")

    def summary(self, record: NoteRecord) -> dict:
        return record.summary(self.files.source(record))

    def status(self) -> dict:
        self.refresh()
        return {"enabled": True, "indexed_notes": len(self.by_id), "invalid_files": self.invalid_count,
                "duplicate_ids": self.duplicate_count, "index_truncated": self.truncated,
                "index_raw_bytes": self.index_bytes, "conflicts": self.files.conflict_count()}
