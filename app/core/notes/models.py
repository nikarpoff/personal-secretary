"""Validated note metadata; Markdown remains the source of truth."""

from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter,
    ValidationError, ValidationInfo, field_validator, model_validator,
)

from app.core.exceptions import AppError, InvalidRequestError
from app.utils.time import parse_timestamp as timestamp

KINDS = frozenset({"note", "task", "idea", "knowledge", "person"})
STATUSES = frozenset({"open", "done", "cancelled", "archived"})
DIRECTORIES = {"note": "Inbox", "task": "Tasks", "idea": "Ideas", "knowledge": "Knowledge", "person": "People"}
ALLOWED_DIRECTORIES = frozenset({*DIRECTORIES.values(), "Archive"})
MAX_ALIASES, MAX_ALIAS_CHARS, MAX_PEOPLE = 8, 80, 10
MAX_NOTE_BYTES, MAX_BODY_CHARS, MAX_FRONTMATTER_BYTES = 128 * 1024, 32_000, 8 * 1024
MAX_RESULTS, MAX_READ_CHARS, MAX_SNIPPET_CHARS = 5, 4_000, 240
MAX_FILES, MAX_DEPTH = 5_000, 8
VERSION = re.compile(r"^[0-9a-f]{64}$")


class NotesUnavailableError(AppError):
    status_code, public = 503, True


class NotesConflictError(AppError):
    status_code, public = 409, True


def canonical_id(value: str) -> str:
    try:
        result = str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidRequestError("note_id must be a UUID, not a file path") from exc
    if value != result:
        raise InvalidRequestError("note_id must be a canonical lowercase UUID")
    return result


def normalized_name(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Name must not be blank")
    value.encode("utf-8")
    return value


Alias = Annotated[str, StringConstraints(min_length=1, max_length=MAX_ALIAS_CHARS, pattern=r"^[^\r\n\x00]+$"), AfterValidator(_name)]
Tag = Annotated[str, StringConstraints(pattern=r"^[\w/-]{1,40}$")]
ALIASES = TypeAdapter(Annotated[list[Alias], Field(max_length=MAX_ALIASES)])
BODY = TypeAdapter(Annotated[str, StringConstraints(max_length=MAX_BODY_CHARS, pattern=r"^[^\x00]*$")])


def validate_aliases(value: Any) -> list[str]:
    try:
        aliases = ALIASES.validate_python(value, strict=True)
    except ValidationError as exc:
        raise InvalidRequestError("Person aliases must contain at most eight nonempty single-line names of up to 80 characters") from exc
    unique = {}
    for alias in aliases:
        unique.setdefault(normalized_name(alias), alias)
    return list(unique.values())


class NoteMetadata(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")

    id: str
    type: Literal["note", "task", "idea", "knowledge", "person"]
    status: Literal["open", "done", "cancelled", "archived"]
    title: Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^\r\n\x00]+$"), AfterValidator(_name)]
    tags: Annotated[list[Tag], Field(max_length=16)] = Field(default_factory=list)
    created_at: str
    updated_at: str
    due_at: str | None = None
    remind_at: str | None = None
    aliases: Any = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return canonical_id(value)

    @field_validator("created_at", "updated_at", "due_at", "remind_at", mode="before")
    @classmethod
    def _time(cls, value: Any, info: ValidationInfo) -> str | None:
        moment = timestamp(value, info.context["tz"], info.field_name)
        return moment.isoformat() if moment is not None else None

    @model_validator(mode="after")
    def _task_and_person_fields(self) -> NoteMetadata:
        if self.type != "task" and (self.due_at is not None or self.remind_at is not None):
            raise ValueError("Only tasks may have due_at or remind_at")
        if self.type == "person":
            self.aliases = validate_aliases(self.aliases)
        self.tags = list(dict.fromkeys(self.tags))
        return self


def validate_metadata(meta: dict, tz: ZoneInfo) -> dict:
    try:
        validated = NoteMetadata.model_validate(meta, context={"tz": tz})
    except ValidationError as exc:
        error = exc.errors(include_input=False, include_url=False)[0]
        raise InvalidRequestError(f"Invalid note metadata {'.'.join(map(str, error['loc']))}: {error['msg']}") from exc
    result = validated.model_dump(exclude={"aliases"})
    for field in ("due_at", "remind_at"):
        if result[field] is None:
            result.pop(field)
    if validated.type == "person" or "aliases" in meta:
        result["aliases"] = validated.aliases
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

    def summary(self, source: str) -> dict:
        result = {"id": self.id, "title": self.metadata["title"], "type": self.metadata["type"],
                  "status": "archived" if self.archived else self.metadata["status"],
                  "tags": self.metadata["tags"], "version": self.version, "source": source}
        if self.metadata["type"] == "person":
            result["aliases"] = self.metadata["aliases"]
        return result
