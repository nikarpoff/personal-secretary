"""The assistant's bounded, filesystem-backed Obsidian interface."""

from app.core.notes.models import NotesConflictError, NotesUnavailableError
from app.core.notes.store import NotesStore

__all__ = ["NotesStore", "NotesConflictError", "NotesUnavailableError"]
