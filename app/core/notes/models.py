"""Small, bounded Markdown/frontmatter contract shared by notes and reminders."""

from __future__ import annotations

import hashlib
import re

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import yaml
from yaml.events import AliasEvent

from app.core.clock import parse_moment
from app.core.exceptions import AppError, InvalidRequestError

KINDS = frozenset({"note", "task", "idea", "knowledge"})
STATUSES = frozenset({"open", "done", "cancelled", "archived"})
DIRECTORIES = {"note": "Inbox", "task": "Tasks", "idea": "Ideas", "knowledge": "Knowledge"}
ALLOWED_DIRECTORIES = frozenset({*DIRECTORIES.values(), "Archive"})
MAX_NOTE_BYTES = 128 * 1024
MAX_BODY_CHARS = 32_000
MAX_FRONTMATTER_BYTES = 8 * 1024
MAX_RESULTS = 5
MAX_READ_CHARS = 4_000
MAX_SNIPPET_CHARS = 240
MAX_FILES = 5_000
MAX_DEPTH = 8
TAG = re.compile(r"^[\w/-]{1,40}$", re.UNICODE)
VERSION = re.compile(r"^[0-9a-f]{64}$")


class NotesUnavailableError(AppError):
    status_code = 503
    public = True


class NotesConflictError(AppError):
    status_code = 409
    public = True


class FrontmatterLoader(yaml.SafeLoader):
    """No aliases, duplicate keys, timestamp coercion or unbounded YAML nesting."""

    yaml_implicit_resolvers = {
        key: [(tag, regexp) for tag, regexp in values if tag != "tag:yaml.org,2002:timestamp"]
        for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0
        self._nodes = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise ValueError("YAML aliases are not supported")
        self._depth += 1
        self._nodes += 1
        try:
            if self._depth > 6 or self._nodes > 200:
                raise ValueError("Frontmatter is too complex")
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node: Any, deep: bool = False) -> dict:
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                raise ValueError("Frontmatter keys must be unique strings")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def canonical_id(value: str) -> str:
    try:
        result = str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidRequestError("note_id must be a UUID, not a file path") from exc
    if value != result:
        raise InvalidRequestError("note_id must be a canonical lowercase UUID")
    return result


def timestamp(value: Any, tz: ZoneInfo, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"{field} must be an ISO timestamp or null")
    try:
        result = parse_moment(value, tz)
        if not isinstance(result, datetime):
            raise InvalidRequestError(f"{field} requires a time, e.g. 2030-05-01T10:00")
        naive = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if naive.tzinfo is None:
            choices = [naive.replace(tzinfo=tz, fold=fold) for fold in (0, 1)]
            valid = [choice for choice in choices
                     if choice.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == naive]
            if not valid or len({choice.utcoffset() for choice in valid}) > 1:
                raise InvalidRequestError(f"{field} is ambiguous or nonexistent in TIMEZONE; provide an explicit UTC offset")
        # Even explicit offsets near datetime.min/max can overflow in UTC. Reject
        # them here so one malformed hand-edited note never stops the whole scan.
        result.astimezone(timezone.utc)
        return result
    except (OverflowError, ValueError, OSError) as exc:
        raise InvalidRequestError(f"{field} is outside the supported timestamp range") from exc


def validate_metadata(meta: dict, tz: ZoneInfo) -> dict:
    if not isinstance(meta, dict):
        raise InvalidRequestError("YAML frontmatter must be a mapping")
    result = dict(meta)
    result["id"] = canonical_id(meta.get("id"))
    if not isinstance(meta.get("type"), str) or meta["type"] not in KINDS:
        raise InvalidRequestError("type must be note, task, idea or knowledge")
    if not isinstance(meta.get("status"), str) or meta["status"] not in STATUSES:
        raise InvalidRequestError("status must be open, done, cancelled or archived")
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or "\n" in title or "\r" in title:
        raise InvalidRequestError("title must contain 1..200 characters on one line")
    result["title"] = title.strip()
    tags = meta.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 16 or any(not isinstance(t, str) or not TAG.fullmatch(t) for t in tags):
        raise InvalidRequestError("tags must be at most 16 names of 1..40 letters, numbers, _, / or -")
    result["tags"] = list(dict.fromkeys(tags))
    for field in ("created_at", "updated_at"):
        value = timestamp(meta.get(field), tz, field)
        if value is None:
            raise InvalidRequestError(f"{field} is required")
        result[field] = value.isoformat()
    for field in ("due_at", "remind_at"):
        value = timestamp(meta.get(field), tz, field)
        if value is not None and meta["type"] != "task":
            raise InvalidRequestError("Only tasks may have due_at or remind_at")
        if value is not None:
            result[field] = value.isoformat()
        else:
            result.pop(field, None)
    return result


@dataclass(frozen=True)
class NoteRecord:
    relative_path: str
    metadata: dict
    body: str
    raw: bytes
    version: str

    @property
    def id(self) -> str:
        return self.metadata["id"]

    @property
    def archived(self) -> bool:
        return self.metadata["status"] == "archived" or self.relative_path.split("/", 1)[0] == "Archive"


def parse_note(raw: bytes, relative_path: str, tz: ZoneInfo) -> NoteRecord:
    if len(raw) > MAX_NOTE_BYTES:
        raise InvalidRequestError("Note is larger than 128 KiB")
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise InvalidRequestError("Note must use UTF-8") from exc
    if not text.startswith("---\n"):
        raise InvalidRequestError("Note has no YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0 or len(text[:end].encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise InvalidRequestError("Note has invalid or oversized YAML frontmatter")
    try:
        meta = yaml.load(text[4:end], Loader=FrontmatterLoader)
        metadata = validate_metadata(meta, tz)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise InvalidRequestError("Note has invalid YAML frontmatter") from exc
    body = text[end + 5:]
    return NoteRecord(relative_path, metadata, body, raw, hashlib.sha256(raw).hexdigest())


def render_note(metadata: dict, body: str, tz: ZoneInfo) -> bytes:
    if not isinstance(body, str) or len(body) > MAX_BODY_CHARS or "\x00" in body:
        raise InvalidRequestError("Note body must be text up to 32000 characters without NUL")
    metadata = validate_metadata(metadata, tz)
    try:
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False, default_flow_style=False)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise InvalidRequestError("Frontmatter contains unsupported values") from exc
    if len(frontmatter.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise InvalidRequestError("Frontmatter is larger than 8 KiB")
    raw = f"---\n{frontmatter}---\n{body.rstrip()}\n".encode("utf-8")
    if len(raw) > MAX_NOTE_BYTES:
        raise InvalidRequestError("Note is larger than 128 KiB")
    return raw
