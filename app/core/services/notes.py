"""Async access to the note repository and its reminder scheduler."""

import asyncio

from typing import Any

from app.core.config import Settings
from app.core.note_reminders import NoteReminderScheduler
from app.core.notes import NotesStore, NotesUnavailableError
from app.utils.time import local_iso, utc_now


class NotesService:
    def __init__(self, settings: Settings, store: NotesStore | None, scheduler: NoteReminderScheduler | None) -> None:
        self.settings, self.store, self.scheduler = settings, store, scheduler

    def _require_store(self) -> NotesStore:
        if self.store is None:
            raise NotesUnavailableError("Notes are not configured: set NOTES_ROOT and ASSISTANT_STATE_DIR")
        return self.store

    async def get_note_context(self, query: str, kind: str | None = None, tag: str | None = None) -> str:
        return await asyncio.to_thread(self._require_store().get_context, query=query, kind=kind, tag=tag)

    async def search_notes(self, query: str = "", kind: str | None = None, status: str | None = None,
                           tag: str | None = None, include_archive: bool = False, limit: int = 5) -> dict:
        return await asyncio.to_thread(self._require_store().search, query=query, kind=kind, status=status,
                                       tag=tag, include_archive=include_archive, limit=limit)

    async def read_note(self, note_id: str, offset: int = 0, limit: int = 4000, section: str | None = None) -> dict:
        return await asyncio.to_thread(self._require_store().read, note_id, offset=offset, limit=limit, section=section)

    async def create_note(self, title: str, body: str, kind: str = "note", tags: list[str] | None = None,
                          due_at: str | None = None, remind_at: str | None = None, aliases: list[str] | None = None) -> dict:
        return await self._mutate(self._require_store().create, title=title, body=body, kind=kind,
                                  tags=tags or [], due_at=due_at, remind_at=remind_at, aliases=aliases)

    async def append_note(self, note_id: str, text: str, expected_version: str) -> dict:
        return await asyncio.to_thread(self._require_store().append, note_id, text, expected_version)

    async def update_note(self, note_id: str, expected_version: str, patch: dict[str, Any]) -> dict:
        return await self._mutate(self._require_store().update, note_id, expected_version, patch)

    async def archive_note(self, note_id: str, expected_version: str) -> dict:
        return await self._mutate(self._require_store().archive, note_id, expected_version)

    async def validate_people(self, person_ids: list[str]) -> list[str]:
        return await asyncio.to_thread(self._require_store().validate_people, person_ids)

    async def notes_status(self) -> dict:
        return {"now": local_iso(utc_now(), self.settings.tz), "timezone": self.settings.TIMEZONE,
                "store": await asyncio.to_thread(self._require_store().status),
                "reminders": self.scheduler.status()}

    async def _mutate(self, action, *args, **kwargs) -> dict:
        result = await asyncio.to_thread(action, *args, **kwargs)
        self.scheduler.request_check()
        return result
