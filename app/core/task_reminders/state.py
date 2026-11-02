"""Schema-1 JSON delivery marks and the single-process ownership of their directory."""

from __future__ import annotations

import stat

from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from filelock import FileLock
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, field_serializer, model_validator

from app.utils.persistence import atomic_write_bytes
from app.utils.time import aware_utc

STATE_FILE = "task-reminders.json"
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_MARKS = 20_000
RETAIN_DAYS = 90

UtcTimestamp = Annotated[datetime, BeforeValidator(aware_utc)]


def _delivery_key(value: str) -> str:
    note_id, separator, moment = value.partition("|")
    if not separator:
        raise ValueError("Invalid task-reminder state key")
    UUID(note_id)
    if aware_utc(moment) is None:
        raise ValueError("Missing task-reminder key timestamp")
    return value


DeliveryKey = Annotated[str, AfterValidator(_delivery_key)]


class DeliveryMark(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["pending", "sent"]
    attempts: Annotated[int, Field(strict=True, ge=1)]
    updated_at: UtcTimestamp
    next_attempt_at: UtcTimestamp
    sent_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def _sent_has_timestamp(self) -> DeliveryMark:
        if self.status == "sent" and self.sent_at is None:
            raise ValueError("Missing task-reminder delivery timestamp")
        return self

    @field_serializer("updated_at", "next_attempt_at", "sent_at")
    def _iso_timestamp(self, value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None


class ReminderState(BaseModel):
    schema_version: Literal[1] = Field(alias="schema")
    entries: Annotated[dict[DeliveryKey, DeliveryMark], Field(max_length=MAX_MARKS)]


class TaskReminderRepository:
    """Own the process lock, retention, retry policy and atomic state snapshots."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.absolute()
        self._path = self.directory / STATE_FILE
        self._lock_path = self.directory / "task-reminders.lock"
        # Startup and shutdown may run on different asyncio.to_thread workers.
        self._lock = FileLock(
            self._lock_path, timeout=0, mode=0o600, thread_local=False,
            fallback_to_soft=False, preserve_lock_file=True,
        )
        self._state = ReminderState(schema=1, entries={})
        self._opened = False

    @staticmethod
    def _regular_file(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Task-reminder state must be a regular file with one link")

    def open(self) -> None:
        if self._opened:
            raise RuntimeError("Task-reminder repository is already open")
        try:
            if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
                raise ValueError("Task-reminder state directory cannot contain links")
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._regular_file(self._lock_path)
            self._lock.acquire()
            self._opened = True
            self._regular_file(self._path)
            try:
                with self._path.open("rb") as stream:
                    raw = stream.read(MAX_STATE_BYTES + 1)
            except FileNotFoundError:
                self._commit({})
                return
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError("Task-reminder state is too large")
            # Existing keys and schema-1 entries are loaded rather than migrated/reset.
            self._state = ReminderState.model_validate_json(raw)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._lock.release()
        self._opened = False

    def status(self) -> dict[str, int]:
        entries = self._state.entries
        return {"delivery_marks": len(entries), "pending_marks": sum(mark.status == "pending" for mark in entries.values())}

    def ready(self, key: str, now: datetime) -> bool:
        mark = self._state.entries.get(key)
        return mark is None or (mark.status == "pending" and mark.next_attempt_at <= now)

    def prune(self, active_keys: set[str], now: datetime) -> None:
        cutoff = now - timedelta(days=RETAIN_DAYS)
        entries = {key: mark for key, mark in self._state.entries.items() if key in active_keys or mark.updated_at >= cutoff}
        if len(entries) != len(self._state.entries):
            self._commit(entries)

    def begin_attempt(self, key: str, now: datetime) -> None:
        previous = self._state.entries.get(key)
        attempts = (previous.attempts if previous is not None else 0) + 1
        delay = min(900, 30 * 2 ** min(attempts - 1, 5))
        mark = DeliveryMark(
            status="pending", attempts=attempts, updated_at=now,
            next_attempt_at=now + timedelta(seconds=delay),
        )
        self._commit({**self._state.entries, key: mark})

    def mark_sent(self, key: str, now: datetime) -> None:
        mark = self._state.entries[key].model_copy(update={"status": "sent", "sent_at": now, "updated_at": now})
        self._commit({**self._state.entries, key: mark})

    def _commit(self, entries: dict[str, DeliveryMark]) -> None:
        if not self._opened:
            raise RuntimeError("Task-reminder repository is not open")
        state = ReminderState(schema=1, entries=entries)
        payload = state.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        if len(payload) > MAX_STATE_BYTES:
            raise ValueError("Task-reminder state capacity exceeded; reminders stopped")
        atomic_write_bytes(self._path, payload)
        # Publish only complete snapshots; status reads never race an in-place mutation.
        self._state = state
