"""Persistent reminders of Markdown tasks, independent of Calendar and the LLM.

Delivery is at least once: a crash after Telegram accepts a message but before its
JSON mark is fsynced can repeat that message. ``due_at`` alone never schedules a
message; only an explicit ``remind_at`` of an open task does.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import stat
import uuid

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable
from zoneinfo import ZoneInfo

from loguru import logger

from app.core.upstream.telegram import TelegramClient

if TYPE_CHECKING:
    from app.core.notes import NotesStore

STATE_FILE = "task-reminders.json"
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_MARKS = 20_000
MAX_SENDS_PER_TICK = 50
RETAIN_DAYS = 90


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("Reminder state timestamps must include a timezone")
    return moment.astimezone(timezone.utc)


def _key(task: dict[str, Any]) -> str | None:
    if task.get("type") != "task" or task.get("status") != "open":
        return None
    moment = _timestamp(task.get("remind_at"))
    return f"{task['id']}|{moment.isoformat()}" if moment is not None else None


async def _state_io(operation: Callable[..., Any], *args: Any) -> Any:
    """Finish descriptor-using I/O before cancellation permits closing its files."""
    pending = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        try:
            await pending
        finally:
            raise


class _StateFiles:
    """Private state directory, one process lock and atomic JSON snapshots.

    On the deployment platform (Linux), directory-relative operations pin the
    checked directory and O_NOFOLLOW refuses substituted symbolic links.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory.absolute()
        self._dir_fd: int | None = None
        self._lock: BinaryIO | None = None

    def open(self) -> dict[str, dict[str, Any]]:
        try:
            self._open_directory()
            fd = self._open("task-reminders.lock", os.O_RDWR | os.O_CREAT)
            self._lock = os.fdopen(fd, "r+b")
            if os.name == "posix":
                import fcntl

                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif os.name == "nt":
                import msvcrt

                # msvcrt locks from the current position, including on an empty file.
                self._lock.seek(0)
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                raise RuntimeError("No task-reminder process lock on this platform")
            try:
                fd = self._open(STATE_FILE, os.O_RDONLY)
            except FileNotFoundError:
                self.save({})
                return {}
            with os.fdopen(fd, "rb") as stream:
                if os.fstat(stream.fileno()).st_size > MAX_STATE_BYTES:
                    raise ValueError("Task-reminder state is too large")
                raw = stream.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError("Task-reminder state is too large")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("schema") != 1:
                raise ValueError("Unknown task-reminder state schema")
            marks = data.get("entries")
            if not isinstance(marks, dict) or len(marks) > MAX_MARKS:
                raise ValueError("Invalid task-reminder state entries")
            for key, entry in marks.items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    raise ValueError("Invalid task-reminder state entry")
                note_id, separator, moment = key.partition("|")
                if not separator:
                    raise ValueError("Invalid task-reminder state key")
                uuid.UUID(note_id)
                _timestamp(moment)
                attempts = entry.get("attempts")
                if type(attempts) is not int or attempts < 1:
                    raise ValueError("Invalid task-reminder attempt count")
                if entry.get("status") not in {"pending", "sent"}:
                    raise ValueError("Invalid task-reminder delivery status")
                if _timestamp(entry.get("updated_at")) is None:
                    raise ValueError("Missing task-reminder state timestamp")
                if _timestamp(entry.get("next_attempt_at")) is None:
                    raise ValueError("Missing task-reminder retry timestamp")
                if entry["status"] == "sent" and _timestamp(entry.get("sent_at")) is None:
                    raise ValueError("Missing task-reminder delivery timestamp")
            return marks
        except BaseException:
            self.close()
            raise

    def _open_directory(self) -> None:
        if os.name == "posix":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open(self.directory.anchor, flags)
            try:
                for part in self.directory.parts[1:]:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                self._dir_fd = descriptor
            except BaseException:
                os.close(descriptor)
                raise
            return
        # Windows is a development platform; reject reparse points before use.
        for ancestor in reversed((self.directory, *self.directory.parents)):
            if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
                raise ValueError("Task-reminder state directory cannot contain links")
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.directory.is_dir():
            raise ValueError("Task-reminder state must be a directory")

    def _open(self, name: str, flags: int) -> int:
        if self._dir_fd is not None:
            fd = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=self._dir_fd)
        else:
            path = self.directory / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Task-reminder state file cannot be a link")
            fd = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise ValueError("Task-reminder state must be a regular file with one link")
        return fd

    def save(self, marks: dict[str, dict[str, Any]]) -> None:
        if len(marks) > MAX_MARKS:
            raise ValueError("Task-reminder state capacity exceeded; reminders stopped")
        payload = json.dumps({"schema": 1, "entries": marks}, ensure_ascii=True, separators=(",", ":")).encode()
        if len(payload) > MAX_STATE_BYTES:
            raise ValueError("Task-reminder state capacity exceeded; reminders stopped")
        temporary = f".task-reminders-{uuid.uuid4().hex}.tmp"
        created = False
        try:
            fd = self._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            created = True
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if self._dir_fd is not None:
                os.replace(temporary, STATE_FILE, src_dir_fd=self._dir_fd, dst_dir_fd=self._dir_fd)
                created = False
                os.fsync(self._dir_fd)
            else:
                os.replace(self.directory / temporary, self.directory / STATE_FILE)
                created = False
        finally:
            if created:
                if self._dir_fd is not None:
                    os.unlink(temporary, dir_fd=self._dir_fd)
                else:
                    (self.directory / temporary).unlink(missing_ok=True)

    def close(self) -> None:
        if self._lock is not None:
            # Closing the descriptor releases flock / the Windows byte-range lock.
            self._lock.close()
            self._lock = None
        if self._dir_fd is not None:
            os.close(self._dir_fd)
            self._dir_fd = None


class NoteReminderScheduler:
    """One scheduler per assistant-state directory; durable marks survive restarts."""

    def __init__(
        self,
        store: NotesStore,
        state_dir: Path,
        telegram: TelegramClient | None,
        tz: ZoneInfo,
        poll_seconds: int = 30,
    ) -> None:
        self._store = store
        self._files = _StateFiles(state_dir)
        self._telegram = telegram
        self._tz = tz
        self._poll_seconds = max(10, poll_seconds)
        self._marks: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._last_scan: datetime | None = None
        self._last_delivery: datetime | None = None
        self._last_failure: datetime | None = None
        self._last_failure_type: str | None = None
        self._delivery_failures = 0
        self._error: str | None = None
        self._fatal = False

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Task-reminder scheduler is already started")
        try:
            self._marks = await _state_io(self._files.open)
        except BaseException:
            # _state_io drains an in-flight open before releasing its process lock.
            await _state_io(self._files.close)
            raise
        self._stop.clear()
        self._fatal = False
        self._error = None
        self._task = asyncio.create_task(self._run(), name="note-reminders")

    def request_check(self) -> None:
        """Wake the scheduler after a note mutation without invoking the LLM."""
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        try:
            if self._task is not None:
                # Do not cancel a send between its network acknowledgement and fsync.
                try:
                    await asyncio.shield(self._task)
                except asyncio.CancelledError:
                    await self._task
                    raise
                finally:
                    self._task = None
        finally:
            await _state_io(self._files.close)

    def status(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "telegram_enabled": self._telegram is not None,
            "last_scan_at": self._last_scan.isoformat() if self._last_scan else None,
            "last_delivery_at": self._last_delivery.isoformat() if self._last_delivery else None,
            "last_delivery_failure_at": self._last_failure.isoformat() if self._last_failure else None,
            "last_delivery_failure_type": self._last_failure_type,
            "delivery_failures_since_start": self._delivery_failures,
            "delivery_marks": len(self._marks),
            "pending_marks": sum(entry["status"] == "pending" for entry in self._marks.values()),
            "blocked": self._fatal,
            "error": self._error,
        }

    async def _persist(self) -> None:
        try:
            await _state_io(self._files.save, self._marks)
        except Exception:
            # A corrupt/unwritable state must never turn into repeated unmarked sends.
            self._fatal = True
            self._error = "Task-reminder state cannot be saved; delivery stopped"
            logger.error(self._error)
            raise

    async def _run(self) -> None:
        logger.info("Task reminders started: polling every {}s", self._poll_seconds)
        while not self._stop.is_set() and not self._fatal:
            self._wake.clear()
            try:
                await self._tick()
                if not self._fatal:
                    self._error = None
            except Exception as error:
                if not self._fatal:
                    self._error = f"Task scan failed: {type(error).__name__}"
                    logger.warning(self._error)
            if self._stop.is_set() or self._fatal:
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    async def _tick(self) -> None:
        tasks = await asyncio.to_thread(self._store.scan_tasks)
        now = _now()
        self._last_scan = now
        candidates: list[tuple[datetime, str, dict[str, Any]]] = []
        active_keys: set[str] = set()
        for task in tasks:
            key = _key(task)
            if key is not None:
                active_keys.add(key)
                remind_at = _timestamp(task["remind_at"])
                if remind_at is not None and remind_at <= now:
                    candidates.append((remind_at, key, task))
        cutoff = now - timedelta(days=RETAIN_DAYS)
        old_count = len(self._marks)
        self._marks = {
            key: entry for key, entry in self._marks.items()
            if key in active_keys or _timestamp(entry["updated_at"]) >= cutoff
        }
        if len(self._marks) != old_count:
            await self._persist()
        if self._telegram is None:
            # A log-only installation must remain eligible for later real delivery.
            return
        attempted = 0
        for _, key, task in sorted(candidates, key=lambda item: (item[0], item[1])):
            if self._stop.is_set():
                return
            mark = self._marks.get(key)
            if mark is not None and (
                mark["status"] == "sent" or _timestamp(mark["next_attempt_at"]) > _now()
            ):
                continue
            current = await asyncio.to_thread(self._store.get_task, task["id"])
            if current is None or _key(current) != key:
                continue
            if attempted >= MAX_SENDS_PER_TICK:
                break
            attempted += 1
            # Persist an attempt before sending. A restart respects its retry delay.
            attempt_at = _now()
            attempts = (mark["attempts"] if mark else 0) + 1
            delay = min(900, 30 * (2 ** min(attempts - 1, 5)))
            self._marks[key] = {
                "status": "pending",
                "attempts": attempts,
                "updated_at": attempt_at.isoformat(),
                "next_attempt_at": (attempt_at + timedelta(seconds=delay)).isoformat(),
            }
            await self._persist()
            # A completion/archive/reschedule during the fsync cancels this attempt.
            current = await asyncio.to_thread(self._store.get_task, task["id"])
            if current is None or _key(current) != key or self._stop.is_set():
                continue
            try:
                await self._telegram.send_message(self._message(current))
            except Exception as error:
                # No note body, token or upstream response is written to these logs.
                self._last_failure = _now()
                self._last_failure_type = type(error).__name__
                self._delivery_failures += 1
                logger.warning("Task reminder failed; retry scheduled ({})", type(error).__name__)
                continue
            delivered_at = _now()
            self._marks[key].update(
                status="sent", sent_at=delivered_at.isoformat(), updated_at=delivered_at.isoformat()
            )
            await self._persist()
            self._last_delivery = delivered_at
            logger.info("Task reminder delivered")

    def _message(self, task: dict[str, Any]) -> str:
        # Clip raw text before escaping: never truncate an HTML tag or entity.
        title = html.escape(str(task.get("title", "Задача"))[:200])
        lines = [f"🔔 <b>{title}</b>"]
        due_at = _timestamp(task.get("due_at"))
        if due_at is not None:
            lines.append(f"Срок: {due_at.astimezone(self._tz):%d.%m.%Y %H:%M %Z}")
        source = str(task.get("source", ""))
        escaped_source = html.escape(source)
        if source.startswith("obsidian://open?") and len(escaped_source) <= 2200:
            # Bot API does not guarantee arbitrary URI schemes in HTML hrefs.
            # Preserve a complete URI for copy/open without invalidating the message.
            lines.append(f"Obsidian (скопировать ссылку): <code>{escaped_source}</code>")
        return "\n".join(lines)
