"""Bounded Markdown storage, anchored to the Secretary mount.

Mutations require Linux renameat2. RENAME_EXCHANGE retains the actual displaced
inode before verifying its version, including changes made by an uncooperative
WebDAV writer in the read/rename window. History is intentionally retained: a
writer holding an old file descriptor can still finish writing to that inode.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from urllib.parse import quote, urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

from loguru import logger

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.core.notes.context import (
    MAX_CONTEXT_RESULTS,
    ContextText,
    excerpt,
    pack_context,
    topic_pattern,
    topic_words,
)
from app.core.notes.models import (
    ALLOWED_DIRECTORIES,
    DIRECTORIES,
    KINDS,
    MAX_DEPTH,
    MAX_FILES,
    MAX_NOTE_BYTES,
    MAX_PEOPLE,
    MAX_READ_CHARS,
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    STATUSES,
    VERSION,
    NoteRecord,
    NotesConflictError,
    NotesUnavailableError,
    canonical_id,
    normalized_name,
    parse_note,
    render_note,
    timestamp,
)

INDEX_TTL_SECONDS = 3.0
MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_RELATIVE_PATH_CHARS = 256
MAX_RELATIVE_PATH_BYTES = 512
MAX_SOURCE_CHARS = 1500
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2


class NotesStore:
    def __init__(self, root: Path, state_dir: Path, tz: ZoneInfo, vault_name: str) -> None:
        self.root = Path(root)
        self.state_dir = Path(state_dir)
        self.tz = tz
        self.vault_name = vault_name
        self._lock = threading.RLock()
        self._root_fd: int | None = None
        self._state_fd: int | None = None
        self._history_fd: int | None = None
        self._conflicts_fd: int | None = None
        self._renameat2: Any = None
        self._last_scan = 0.0
        self._cache: dict[str, tuple[tuple, NoteRecord | None]] = {}
        self._by_id: dict[str, NoteRecord] = {}
        self._context_cache: dict[str, ContextText] = {}
        self._invalid_count = 0
        self._duplicate_count = 0
        self._truncated = False
        self._index_bytes = 0
        self._walk_truncated = False

    def initialize(self) -> None:
        """Open trusted mount roots and create only the service's own directories."""
        with self._lock:
            if self._root_fd is not None:
                return
            if sys.platform != "linux" or not hasattr(os, "O_NOFOLLOW"):
                raise NotesUnavailableError("Notes storage requires Linux with no-follow file operations")
            if not self.vault_name or len(self.vault_name) > 100 or any(c in self.vault_name for c in "/\\\r\n\x00"):
                raise InvalidRequestError("OBSIDIAN_VAULT_NAME must be a single vault name")
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                self._renameat2 = libc.renameat2
                self._renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
                self._renameat2.restype = ctypes.c_int
                self._root_fd = self._open_absolute(self.root, create=True)
                self._state_fd = self._open_absolute(self.state_dir, create=True)
                for directory in sorted(ALLOWED_DIRECTORIES):
                    fd = self._child_directory(self._root_fd, directory, create=True)
                    os.close(fd)
                self._history_fd = self._child_directory(self._root_fd, ".secretary-history", create=True)
                self._conflicts_fd = self._child_directory(self._state_fd, "conflicts", create=True)
            except (OSError, AttributeError) as exc:
                self.close()
                raise NotesUnavailableError("Cannot securely open notes/state mounts or renameat2 is unavailable") from exc

    def close(self) -> None:
        with self._lock:
            for name in ("_conflicts_fd", "_history_fd", "_state_fd", "_root_fd"):
                fd = getattr(self, name)
                if fd is not None:
                    os.close(fd)
                    setattr(self, name, None)
            self._context_cache.clear()

    @staticmethod
    def _open_absolute(path: Path, *, create: bool) -> int:
        if not path.is_absolute() or ".." in path.parts:
            raise InvalidRequestError("Notes and state mounts must use absolute paths without '..'")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.parts[1:]:
                if part in ("", "."):
                    continue
                child = NotesStore._child_directory(fd, part, create=create)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _child_directory(parent_fd: int, name: str, *, create: bool = False) -> int:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)

    def _require_ready(self) -> int:
        if self._root_fd is None:
            raise NotesUnavailableError("Notes storage is not initialized")
        return self._root_fd

    @contextmanager
    def _parent(self, relative_path: str) -> Iterator[tuple[int, str]]:
        parts = PurePosixPath(relative_path).parts
        if (
            len(parts) < 2 or parts[0] not in ALLOWED_DIRECTORIES or len(parts) > MAX_DEPTH + 2
            or PurePosixPath(relative_path).is_absolute()
            or any(p in ("", ".", "..") or p.startswith(".") or "\\" in p or "\x00" in p for p in parts)
            or not parts[-1].endswith(".md")
            or len(relative_path) > MAX_RELATIVE_PATH_CHARS or len(relative_path.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES
        ):
            raise InvalidRequestError("Unsupported note path")
        fd = os.dup(self._require_ready())
        try:
            for part in parts[:-1]:
                child = self._child_directory(fd, part)
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        finally:
            os.close(fd)

    @staticmethod
    def _read_file(parent_fd: int, filename: str) -> tuple[bytes, os.stat_result]:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_NOTE_BYTES:
                raise InvalidRequestError("Note must be a regular, non-linked file up to 128 KiB")
            chunks = []
            remaining = MAX_NOTE_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 16_384))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
            if (info.st_mtime_ns, info.st_ctime_ns, info.st_size) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size):
                raise NotesConflictError("Note is changing; retry after synchronization finishes")
            if len(raw) > MAX_NOTE_BYTES:
                raise InvalidRequestError("Note is larger than 128 KiB")
            return raw, after
        finally:
            os.close(fd)

    def _walk(self, fd: int, prefix: str, budget: list[int], depth: int = 0) -> Iterator[tuple[str, os.stat_result]]:
        if depth > MAX_DEPTH:
            return
        with os.scandir(fd) as entries:
            for entry in entries:
                budget[0] += 1
                if budget[0] > MAX_FILES * 4:
                    self._walk_truncated = True
                    return
                if entry.name.startswith(".") or "\\" in entry.name:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    relative = f"{prefix}/{entry.name}"
                    if stat.S_ISLNK(info.st_mode):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        child = self._child_directory(fd, entry.name)
                        try:
                            yield from self._walk(child, relative, budget, depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and entry.name.endswith(".md"):
                        yield relative, info
                except OSError:
                    # A sync may rename/remove one entry while scanning the others.
                    continue

    def _refresh(self, *, force: bool = False) -> None:
        root_fd = self._require_ready()
        if not force and time.monotonic() - self._last_scan < INDEX_TTL_SECONDS:
            return
        fresh = {}
        by_id: dict[str, NoteRecord] = {}
        duplicates = set()
        invalid = 0
        count = 0
        index_bytes = 0
        truncated = False
        self._walk_truncated = False
        walk_budget = [0]
        for directory in sorted(ALLOWED_DIRECTORIES):
            try:
                fd = self._child_directory(root_fd, directory)
            except OSError:
                invalid += 1
                continue
            try:
                for path, info in self._walk(fd, directory, walk_budget):
                    count += 1
                    if count > MAX_FILES:
                        truncated = True
                        break
                    signature = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
                    cached = self._cache.get(path)
                    if cached is not None and cached[0] == signature:
                        record = cached[1]
                    else:
                        try:
                            with self._parent(path) as (parent, name):
                                raw, actual = self._read_file(parent, name)
                            record = parse_note(raw, path, self.tz)
                            self._source(record)
                            signature = (actual.st_dev, actual.st_ino, actual.st_mtime_ns, actual.st_ctime_ns, actual.st_size)
                        except (OSError, InvalidRequestError, NotesConflictError):
                            record = None
                    if record is not None:
                        index_bytes += len(record.raw)
                        if index_bytes > MAX_INDEX_BYTES:
                            truncated = True
                            break
                    fresh[path] = (signature, record)
                    if record is None:
                        invalid += 1
                    elif record.id in by_id:
                        duplicates.add(record.id)
                    else:
                        by_id[record.id] = record
            finally:
                os.close(fd)
            if self._walk_truncated:
                truncated = True
            if truncated:
                break
        for note_id in duplicates:
            by_id.pop(note_id, None)
        self._cache, self._by_id = fresh, by_id
        self._invalid_count, self._duplicate_count = invalid, len(duplicates)
        self._truncated = truncated
        self._index_bytes = min(index_bytes, MAX_INDEX_BYTES)
        self._last_scan = time.monotonic()
        if invalid or duplicates or truncated:
            logger.warning("Notes index: {} invalid files, {} duplicated ids, limit reached={}", invalid, len(duplicates), truncated)

    def _lookup(self, note_id: str) -> NoteRecord:
        canonical_id(note_id)
        self._refresh(force=True)
        if self._truncated:
            raise NotesUnavailableError("Notes index exceeds its safety limits; reduce the Secretary scope before editing or reading by id")
        record = self._by_id.get(note_id)
        if record is None:
            raise NotFoundError("Note not found, invalid or its id is duplicated")
        try:
            with self._parent(record.relative_path) as (parent, name):
                raw, _ = self._read_file(parent, name)
            current = parse_note(raw, record.relative_path, self.tz)
        except OSError as exc:
            raise NotFoundError("Note changed during synchronization; search again") from exc
        if current.id != note_id:
            raise NotesConflictError("Note identity changed; search again")
        return current

    def _source(self, record: NoteRecord) -> str:
        source = "obsidian://open?" + urlencode({"vault": self.vault_name, "file": f"Secretary/{record.relative_path}"}, quote_via=quote)
        if len(source) > MAX_SOURCE_CHARS:
            raise InvalidRequestError("Encoded note source is too long")
        return source

    def _summary(self, record: NoteRecord) -> dict:
        result = {"id": record.id, "title": record.metadata["title"], "type": record.metadata["type"],
                  "status": "archived" if record.archived else record.metadata["status"],
                  "tags": record.metadata["tags"], "version": record.version, "source": self._source(record)}
        if record.metadata["type"] == "person":
            result["aliases"] = record.metadata["aliases"]
        return result

    @staticmethod
    def _context_visible(record: NoteRecord, kind: str | None, tag: str | None) -> bool:
        return (
            not record.archived
            and (kind is None or record.metadata["type"] == kind)
            and (tag is None or tag in record.metadata["tags"])
            and (record.metadata["type"] != "task" or record.metadata["status"] == "open")
        )

    def get_context(self, query: str, kind: str | None = None, tag: str | None = None) -> str:
        """Select up to three current excerpts without an LLM or network request."""
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise InvalidRequestError("Context query must contain 1..200 characters of topic words")
        if kind is not None and kind not in KINDS:
            raise InvalidRequestError("Unsupported note type")
        if tag is not None and (not isinstance(tag, str) or not 1 <= len(tag) <= 40):
            raise InvalidRequestError("Tag must contain 1..40 characters")
        words = topic_words(query)
        phrase = normalized_name(query)
        with self._lock:
            self._refresh()
            # Keep the same fail-closed scope as read_note when the index is incomplete.
            if self._truncated or not words:
                return pack_context([], index_truncated=self._truncated)
            pattern = topic_pattern(words)
            self._context_cache = {key: text for key, text in self._context_cache.items() if key in self._by_id}
            candidates = []
            for record in self._by_id.values():
                if not self._context_visible(record, kind, tag):
                    continue
                text = self._context_cache.get(record.id)
                if text is None or text.version != record.version:
                    text = self._context_cache[record.id] = ContextText.from_record(record)
                score = text.score(pattern, phrase)
                if score[1]:
                    updated = datetime.fromisoformat(record.metadata["updated_at"]).astimezone(timezone.utc)
                    candidates.append((score, updated, record.id, record))
            candidates.sort(key=lambda value: value[:3], reverse=True)
            items = []
            # Re-read only the selected files; do not return stale bodies after sync.
            for _, _, note_id, indexed in candidates[:MAX_CONTEXT_RESULTS]:
                try:
                    with self._parent(indexed.relative_path) as (parent, name):
                        raw, _ = self._read_file(parent, name)
                    current = parse_note(raw, indexed.relative_path, self.tz)
                    if current.id != note_id or not self._context_visible(current, kind, tag):
                        continue
                    text = ContextText.from_record(current)
                    self._context_cache[note_id] = text
                    if not text.score(pattern, phrase)[1]:
                        continue
                    content, offset, truncated = excerpt(current.body, words)
                    item = {"id": current.id, "title": current.metadata["title"],
                            "type": current.metadata["type"], "status": current.metadata["status"],
                            "source": self._source(current), "version": current.version,
                            "content": content, "offset": offset, "truncated": truncated}
                    if current.metadata["type"] == "task":
                        item.update({key: current.metadata[key] for key in ("due_at", "remind_at") if key in current.metadata})
                    items.append(item)
                except (OSError, InvalidRequestError, NotesConflictError):
                    # An external sync may move, replace or temporarily invalidate a file.
                    continue
            return pack_context(items, words=words, has_more=len(candidates) > len(items))

    def search(self, query: str = "", kind: str | None = None, status: str | None = None,
               tag: str | None = None, include_archive: bool = False, limit: int = MAX_RESULTS) -> dict:
        if not isinstance(query, str) or len(query) > 200:
            raise InvalidRequestError("Search query must be at most 200 characters")
        if kind is not None and kind not in KINDS:
            raise InvalidRequestError("Unsupported note type")
        if status is not None and status not in STATUSES:
            raise InvalidRequestError("Unsupported note status")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
            raise InvalidRequestError("Search limit must be within 1..5")
        if tag is not None and (not isinstance(tag, str) or len(tag) > 40):
            raise InvalidRequestError("Tag must be at most 40 characters")
        with self._lock:
            self._refresh()
            normalized_query = normalized_name(query)
            words = normalized_query.split()
            matches = []
            for record in self._by_id.values():
                if record.archived and not include_archive:
                    continue
                if kind is not None and record.metadata["type"] != kind:
                    continue
                effective_status = "archived" if record.archived else record.metadata["status"]
                if status is not None and effective_status != status:
                    continue
                if tag is not None and tag not in record.metadata["tags"]:
                    continue
                aliases = record.metadata["aliases"] if record.metadata["type"] == "person" else []
                names = [normalized_name(record.metadata["title"]), *(normalized_name(alias) for alias in aliases)]
                haystack = normalized_name(f"{' '.join(names)}\n{' '.join(record.metadata['tags'])}\n{record.body}")
                if not all(word in haystack for word in words):
                    continue
                score = sum(any(word in name for name in names) for word in words)
                if record.metadata["type"] == "person" and normalized_query and normalized_query in names:
                    # Rank actual names above incidental mentions in another biography;
                    # a matching name is still not proof of unique real-world identity.
                    score += 100
                updated = datetime.fromisoformat(record.metadata["updated_at"]).astimezone(timezone.utc)
                matches.append((score, updated, record))
            matches.sort(key=lambda value: (value[0], value[1], value[2].id), reverse=True)
            results = []
            for _, _, record in matches[:limit]:
                snippet = " ".join(record.body.split())
                if words:
                    position = normalized_name(snippet).find(words[0])
                    if position > 60:
                        snippet = "…" + snippet[position - 60:]
                result = self._summary(record)
                result["snippet"] = snippet[:MAX_SNIPPET_CHARS]
                results.append(result)
            return {"results": results, "has_more": len(matches) > limit, "index_truncated": self._truncated}

    def read(self, note_id: str, offset: int = 0, limit: int = MAX_READ_CHARS, section: str | None = None) -> dict:
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 128_000:
            raise InvalidRequestError("Read offset must be within 0..128000")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_READ_CHARS:
            raise InvalidRequestError("Read limit must be within 1..4000 characters")
        with self._lock:
            record = self._lookup(note_id)
            body = record.body
            if section is not None:
                if not isinstance(section, str) or not section.strip() or len(section) > 200:
                    raise InvalidRequestError("Section must be a heading up to 200 characters")
                headings = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*#*\s*$", body, re.MULTILINE))
                chosen = next((h for h in headings if h.group(2).casefold() == section.strip().casefold()), None)
                if chosen is None:
                    raise NotFoundError("Section heading not found")
                following = next((h.start() for h in headings if h.start() > chosen.start() and len(h.group(1)) <= len(chosen.group(1))), len(body))
                body = body[chosen.start():following]
            result = self._summary(record)
            result.update({"created_at": record.metadata["created_at"], "updated_at": record.metadata["updated_at"],
                           "due_at": record.metadata.get("due_at"), "remind_at": record.metadata.get("remind_at"),
                           "content": body[offset:offset + limit], "offset": offset,
                           "next_offset": offset + limit if offset + limit < len(body) else None})
            return result

    def _write_history(self, raw: bytes, prefix: str) -> str:
        if self._history_fd is None:
            raise NotesUnavailableError("Notes storage is not initialized")
        filename = f"{prefix}-{uuid4()}.md"
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._history_fd)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self._history_fd)
        return filename

    def _rename(self, source_fd: int, source: str, target_fd: int, target: str, flags: int) -> None:
        if self._renameat2 is None:
            raise NotesUnavailableError("Linux atomic renameat2 is required for note mutations")
        result = self._renameat2(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flags)
        if result != 0:
            number = ctypes.get_errno()
            if number in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV):
                raise NotesUnavailableError("Notes filesystem must support renameat2 exchange/no-replace on one mount")
            raise OSError(number, os.strerror(number))

    def _journal_conflict(self, note_id: str, expected_version: str, history: list[str]) -> str:
        conflict_id = str(uuid4())
        document = {"conflict_id": conflict_id, "note_id": note_id, "expected_version": expected_version,
                    "created_at": datetime.now(self.tz).isoformat(), "history": history}
        if self._conflicts_fd is None:
            raise NotesUnavailableError("Conflict state is not initialized")
        fd = os.open(f"{conflict_id}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._conflicts_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
                json.dump(document, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self._conflicts_fd)
        return conflict_id

    def _replace(self, record: NoteRecord, raw: bytes, expected_version: str) -> NoteRecord:
        if not isinstance(expected_version, str) or not VERSION.fullmatch(expected_version):
            raise InvalidRequestError("expected_version must be the SHA-256 returned by read_note")
        if record.version != expected_version:
            proposal = self._write_history(raw, f"proposal-{record.id}")
            conflict = self._journal_conflict(record.id, expected_version, [proposal])
            raise NotesConflictError(f"Note version changed; user file kept, proposed edit saved; conflict_id={conflict}. Read the note again.")
        staged = self._write_history(raw, f"revision-{record.id}")
        with self._parent(record.relative_path) as (parent, name):
            # No check/replace gap: the displaced user inode remains in history.
            try:
                self._rename(self._history_fd, staged, parent, name, RENAME_EXCHANGE)
            except FileNotFoundError as exc:
                conflict = self._journal_conflict(record.id, expected_version, [staged])
                raise NotesConflictError(f"Note moved/deleted; proposed edit saved; conflict_id={conflict}") from exc
            os.fsync(parent)
            os.fsync(self._history_fd)
            try:
                displaced, _ = self._read_file(self._history_fd, staged)
                matches = hashlib.sha256(displaced).hexdigest() == expected_version
            except (OSError, InvalidRequestError, NotesConflictError):
                matches = False
            if not matches:
                proposal = self._write_history(raw, f"proposal-{record.id}")
                preserved = [staged, proposal]
                try:
                    # Restore the user's displaced inode; keep everything displaced
                    # by this exchange too, including a third concurrent version.
                    self._rename(self._history_fd, staged, parent, name, RENAME_EXCHANGE)
                    os.fsync(parent)
                    os.fsync(self._history_fd)
                except OSError:
                    # No destructive fallback if an external client moved the file.
                    pass
                conflict = self._journal_conflict(record.id, expected_version, preserved)
                self._last_scan = 0.0
                raise NotesConflictError(f"Concurrent edit detected; all versions preserved; conflict_id={conflict}. Read the note again.")
        self._last_scan = 0.0
        return parse_note(raw, record.relative_path, self.tz)

    def create(self, title: str, body: str, kind: str = "note", tags: list[str] | None = None,
               due_at: str | None = None, remind_at: str | None = None,
               aliases: list[str] | None = None) -> dict:
        with self._lock:
            self._require_ready()
            self._refresh(force=True)
            if self._truncated or len(self._cache) >= MAX_FILES:
                raise NotesUnavailableError("Notes index is at its safety limit; reduce the Secretary scope before creating notes")
            if kind not in KINDS:
                raise InvalidRequestError("type must be note, task, idea, knowledge or person")
            if aliases is not None and kind != "person":
                raise InvalidRequestError("aliases can only be set on a person card")
            note_id = str(uuid4())
            now = datetime.now(self.tz).isoformat(timespec="seconds")
            meta = {"id": note_id, "title": title, "type": kind, "status": "open", "created_at": now,
                    "updated_at": now, "tags": tags or []}
            if kind == "person":
                meta["aliases"] = [] if aliases is None else aliases
            if due_at is not None:
                meta["due_at"] = due_at
            if remind_at is not None:
                meta["remind_at"] = remind_at
            raw = render_note(meta, body, self.tz)
            if self._index_bytes + len(raw) > MAX_INDEX_BYTES:
                raise NotesUnavailableError("Creating this note would exceed the notes index byte limit")
            path = f"{DIRECTORIES[kind]}/{note_id}.md"
            staged = self._write_history(raw, f"create-{note_id}")
            with self._parent(path) as (parent, name):
                self._rename(self._history_fd, staged, parent, name, RENAME_NOREPLACE)
                os.fsync(parent)
                os.fsync(self._history_fd)
            self._last_scan = 0.0
            return self._summary(parse_note(raw, path, self.tz))

    def append(self, note_id: str, text: str, expected_version: str) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise InvalidRequestError("Append text must be nonempty")
        with self._lock:
            current = self._lookup(note_id)
            return self._update_record(current, expected_version, {"body": current.body.rstrip() + "\n\n" + text})

    def update(self, note_id: str, expected_version: str, patch: dict) -> dict:
        with self._lock:
            return self._update_record(self._lookup(note_id), expected_version, patch)

    def _update_record(self, current: NoteRecord, expected_version: str, patch: dict) -> dict:
        if not isinstance(patch, dict) or not patch or set(patch) - {"title", "body", "status", "tags", "due_at", "remind_at", "aliases"}:
            raise InvalidRequestError("Unsupported or empty note update")
        if "aliases" in patch and current.metadata["type"] != "person":
            raise InvalidRequestError("aliases can only be changed on a person card")
        if current.archived:
            raise InvalidRequestError("Archived notes are read-only in version 1")
        if patch.get("status") == "archived":
            raise InvalidRequestError("Use archive_note to archive a note")
        meta = dict(current.metadata)
        body = patch.get("body", current.body)
        meta.update({key: value for key, value in patch.items() if key != "body"})
        meta["updated_at"] = datetime.now(self.tz).isoformat(timespec="seconds")
        raw = render_note(meta, body, self.tz)
        return self._summary(self._replace(current, raw, expected_version))

    def archive(self, note_id: str, expected_version: str) -> dict:
        with self._lock:
            current = self._lookup(note_id)
            if current.archived:
                if current.version != expected_version:
                    raise NotesConflictError("Note version changed; read the note again")
                return self._summary(current)
            meta = {**current.metadata, "status": "archived", "updated_at": datetime.now(self.tz).isoformat(timespec="seconds")}
            raw = render_note(meta, current.body, self.tz)
            updated = self._replace(current, raw, expected_version)
            destination = f"Archive/{current.id}.md"
            with self._parent(updated.relative_path) as (source_fd, name), self._parent(destination) as (target_fd, target):
                try:
                    self._rename(source_fd, name, target_fd, target, RENAME_NOREPLACE)
                except FileExistsError as exc:
                    # Status is already archived; retain both files for manual review.
                    self._last_scan = 0.0
                    raise NotesConflictError("Archive destination already exists; note marked archived, no file overwritten") from exc
                os.fsync(source_fd)
                os.fsync(target_fd)
                try:
                    actual_raw, _ = self._read_file(target_fd, target)
                    actual = parse_note(actual_raw, destination, self.tz)
                    unchanged = actual.id == current.id and actual.version == updated.version
                except (OSError, InvalidRequestError, NotesConflictError):
                    unchanged = False
                if not unchanged:
                    proposal = self._write_history(raw, f"archive-proposal-{current.id}")
                    conflict = self._journal_conflict(current.id, expected_version, [proposal])
                    self._last_scan = 0.0
                    raise NotesConflictError(
                        f"Note changed during archive; current file retained in Archive, proposal saved; conflict_id={conflict}. Search again."
                    )
            self._last_scan = 0.0
            return self._summary(actual)

    def validate_people(self, person_ids: list[str]) -> list[str]:
        """Validate a small explicit set of meeting references without returning biographies."""
        if not isinstance(person_ids, list) or len(person_ids) > MAX_PEOPLE:
            raise InvalidRequestError("person_ids must be a list of at most 10 person UUIDs")
        identifiers = sorted({canonical_id(value) for value in person_ids})
        with self._lock:
            self._require_ready()
            if not identifiers:
                return []
            self._refresh(force=True)
            if self._truncated:
                raise NotesUnavailableError("People cannot be linked while the notes index exceeds its safety limits")
            for person_id in identifiers:
                indexed = self._by_id.get(person_id)
                if indexed is None:
                    raise NotFoundError("Person not found, invalid or its id is duplicated")
                try:
                    with self._parent(indexed.relative_path) as (parent, name):
                        raw, _ = self._read_file(parent, name)
                    person = parse_note(raw, indexed.relative_path, self.tz)
                except OSError as exc:
                    raise NotFoundError("Person changed during synchronization; search again") from exc
                if person.id != person_id:
                    raise NotesConflictError("Person identity changed; search again")
                if person.metadata["type"] != "person" or person.archived:
                    raise InvalidRequestError("Meeting references must point to non-archived person cards")
            return identifiers

    def _task_record(self, record: NoteRecord) -> dict:
        result = self._summary(record)
        result.update({"due_at": timestamp(record.metadata.get("due_at"), self.tz, "due_at"),
                       "remind_at": timestamp(record.metadata.get("remind_at"), self.tz, "remind_at")})
        return result

    def scan_tasks(self) -> list[dict]:
        with self._lock:
            self._refresh(force=True)
            if self._truncated:
                raise NotesUnavailableError("Task reminders are paused because the notes index exceeds its safety limits")
            return [self._task_record(record) for record in self._by_id.values()
                    if record.metadata["type"] == "task" and not record.archived]

    def get_task(self, note_id: str) -> dict | None:
        with self._lock:
            try:
                record = self._lookup(note_id)
            except (NotFoundError, InvalidRequestError, NotesConflictError):
                return None
            if record.metadata["type"] != "task" or record.archived:
                return None
            return self._task_record(record)

    def status(self) -> dict:
        with self._lock:
            self._refresh()
            conflicts = 0
            if self._conflicts_fd is not None:
                with os.scandir(self._conflicts_fd) as entries:
                    conflicts = sum(1 for entry in entries if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False))
            return {"enabled": True, "indexed_notes": len(self._by_id), "invalid_files": self._invalid_count,
                    "duplicate_ids": self._duplicate_count, "index_truncated": self._truncated,
                    "index_raw_bytes": self._index_bytes, "conflicts": conflicts}
