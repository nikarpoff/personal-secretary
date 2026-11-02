"""Select and deliver explicit reminders of open Markdown tasks without an LLM.

Delivery is at least once: a crash after Telegram accepts a message but before the
repository fsyncs its acknowledgement may repeat that message after its retry delay.
"""

from __future__ import annotations

import asyncio

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from loguru import logger

from app.core.task_reminders.message import format_task_reminder
from app.core.task_reminders.state import TaskReminderRepository
from app.core.upstream.telegram import TelegramClient
from app.utils.async_io import finish_before_cancel, shielded_io
from app.utils.time import aware_utc, utc_now

if TYPE_CHECKING:
    from app.core.notes import NotesStore

MAX_SENDS_PER_TICK = 50


def reminder_key(task: dict[str, Any]) -> str | None:
    if task.get("type") != "task" or task.get("status") != "open":
        return None
    moment = aware_utc(task.get("remind_at"))
    return f"{task['id']}|{moment.isoformat()}" if moment is not None else None


class NoteReminderScheduler:
    """One scheduler per assistant-state directory; durable marks survive restarts."""

    def __init__(
        self, store: NotesStore, state_dir: Path, telegram: TelegramClient | None,
        tz: ZoneInfo, poll_seconds: int = 30,
    ) -> None:
        self._store = store
        self._repository = TaskReminderRepository(state_dir)
        self._telegram = telegram
        self._tz = tz
        self._poll_seconds = max(10, poll_seconds)
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
            await shielded_io(self._repository.open)
        except BaseException:
            await shielded_io(self._repository.close)
            raise
        self._stop.clear()
        self._fatal = False
        self._error = None
        self._task = asyncio.create_task(self._run(), name="note-reminders")

    def request_check(self) -> None:
        """Wake after a note mutation without invoking the LLM."""
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        try:
            if self._task is not None:
                try:
                    # Keep the send and durable acknowledgement together during shutdown.
                    await finish_before_cancel(self._task)
                finally:
                    self._task = None
        finally:
            await shielded_io(self._repository.close)

    def status(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "telegram_enabled": self._telegram is not None,
            "last_scan_at": self._last_scan.isoformat() if self._last_scan else None,
            "last_delivery_at": self._last_delivery.isoformat() if self._last_delivery else None,
            "last_delivery_failure_at": self._last_failure.isoformat() if self._last_failure else None,
            "last_delivery_failure_type": self._last_failure_type,
            "delivery_failures_since_start": self._delivery_failures,
            **self._repository.status(),
            "blocked": self._fatal,
            "error": self._error,
        }

    async def _state_change(self, operation: Callable[..., Any], *args: Any) -> None:
        try:
            await shielded_io(operation, *args)
        except Exception:
            # An unwritable state must not become repeated unmarked deliveries.
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
        now = utc_now()
        self._last_scan = now
        candidates: list[tuple[datetime, str, dict[str, Any]]] = []
        active_keys: set[str] = set()
        for task in tasks:
            key = reminder_key(task)
            if key is not None:
                active_keys.add(key)
                remind_at = aware_utc(task["remind_at"])
                if remind_at is not None and remind_at <= now:
                    candidates.append((remind_at, key, task))
        await self._state_change(self._repository.prune, active_keys, now)
        if self._telegram is None:
            # Log-only installations remain eligible for later real delivery.
            return
        attempted = 0
        for _, key, task in sorted(candidates, key=lambda item: (item[0], item[1])):
            if self._stop.is_set():
                return
            if not self._repository.ready(key, utc_now()):
                continue
            current = await asyncio.to_thread(self._store.get_task, task["id"])
            if current is None or reminder_key(current) != key:
                continue
            if attempted >= MAX_SENDS_PER_TICK:
                break
            attempted += 1
            await self._state_change(self._repository.begin_attempt, key, utc_now())
            # A completion/archive/reschedule during fsync cancels this attempt.
            current = await asyncio.to_thread(self._store.get_task, task["id"])
            if current is None or reminder_key(current) != key or self._stop.is_set():
                continue
            try:
                await self._telegram.send_message(format_task_reminder(current, self._tz))
            except Exception as error:
                self._last_failure = utc_now()
                self._last_failure_type = type(error).__name__
                self._delivery_failures += 1
                logger.warning("Task reminder failed; retry scheduled ({})", type(error).__name__)
                continue
            delivered_at = utc_now()
            await self._state_change(self._repository.mark_sent, key, delivered_at)
            self._last_delivery = delivered_at
            logger.info("Task reminder delivered")
