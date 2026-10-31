from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)

from app.api_v1.schemas import (
    EventCreateRequest,
    EventInfo,
    EventListResponse,
    EventUpdateRequest,
    NoteCreateRequest,
    NoteInfo,
    NoteListResponse,
    NoteUpdateRequest,
    SearchResponse,
)
from app.core.auth import verify_token
from app.core.engine import AssistantEngine
from app.core.entities import ItemKind

router = APIRouter(prefix="/v1", dependencies=[Depends(verify_token)])

UPSTREAM_ERRORS = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Google Calendar rejected the event"},
    status.HTTP_502_BAD_GATEWAY: {"description": "Google Calendar failed or denied access"},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Google Calendar is not configured or unreachable"},
    status.HTTP_504_GATEWAY_TIMEOUT: {"description": "Google Calendar did not answer in time"},
}


def get_engine(request: Request) -> AssistantEngine:
    # Prefer app.state singleton created on startup
    svc = getattr(request.app.state, "engine", None)
    if isinstance(svc, AssistantEngine):
        return svc
    raise HTTPException(500, "service is not initialized")


# ---- events ----------------------------------------------------------------

@router.get("/events",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Bad range bounds"},
        **UPSTREAM_ERRORS,
    },
)
async def list_events(
    start: Optional[str] = Query(None, description="Начало периода: дата или локальное время; по умолчанию сегодня"),
    end: Optional[str] = Query(None, description="Конец периода; по умолчанию неделя от начала"),
    q: Optional[str] = Query(None, description="Слова в названии, описании или месте"),
    limit: int = Query(100, ge=1, le=500, description="Сколько событий вернуть"),
    engine: AssistantEngine = Depends(get_engine),
) -> EventListResponse:
    """Events of a range with their notes"""
    return await engine.list_events(start, end, q, limit)


@router.post("/events", status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Bad time or reminder minutes"},
        **UPSTREAM_ERRORS,
    },
)
async def create_event(
    body: EventCreateRequest,
    engine: AssistantEngine = Depends(get_engine),
) -> EventInfo:
    """Creating a calendar event"""
    return await engine.create_event(body)


@router.patch("/events/{event_id}",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Bad time or reminder minutes"},
        status.HTTP_404_NOT_FOUND: {"description": "No event with this id"},
        **UPSTREAM_ERRORS,
    },
)
async def update_event(
    event_id: str,
    body: EventUpdateRequest,
    engine: AssistantEngine = Depends(get_engine),
) -> EventInfo:
    """Changing a calendar event"""
    return await engine.update_event(event_id, body)


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No event with this id"},
        **UPSTREAM_ERRORS,
    },
)
async def delete_event(
    event_id: str,
    engine: AssistantEngine = Depends(get_engine),
) -> Response:
    """Deleting an event; the series id deletes the whole series"""
    await engine.delete_event(event_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- notes -----------------------------------------------------------------

@router.get("/notes")
async def list_notes(
    tag: Optional[str] = Query(None, description="Только заметки с этим тегом"),
    event_id: Optional[str] = Query(None, description="Только заметки к этому событию"),
    limit: int = Query(50, ge=1, le=500, description="Сколько заметок вернуть"),
    engine: AssistantEngine = Depends(get_engine),
) -> NoteListResponse:
    """The latest notes"""
    return await engine.list_notes(tag, event_id, limit)


@router.post("/notes", status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Empty note"},
        status.HTTP_404_NOT_FOUND: {"description": "No event with the linked id"},
    },
)
async def add_note(
    body: NoteCreateRequest,
    engine: AssistantEngine = Depends(get_engine),
) -> NoteInfo:
    """Adding a note"""
    return await engine.add_note(body)


@router.get("/notes/{note_id}",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No note with this id"}},
)
async def get_note(
    note_id: int,
    engine: AssistantEngine = Depends(get_engine),
) -> NoteInfo:
    """A note by id"""
    return await engine.get_note(note_id)


@router.patch("/notes/{note_id}",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Empty note"},
        status.HTTP_404_NOT_FOUND: {"description": "No note or linked event with this id"},
    },
)
async def update_note(
    note_id: int,
    body: NoteUpdateRequest,
    engine: AssistantEngine = Depends(get_engine),
) -> NoteInfo:
    """Changing a note"""
    return await engine.update_note(note_id, body)


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT,
    responses={status.HTTP_404_NOT_FOUND: {"description": "No note with this id"}},
)
async def delete_note(
    note_id: int,
    engine: AssistantEngine = Depends(get_engine),
) -> Response:
    """Deleting a note"""
    await engine.delete_note(note_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- knowledge base --------------------------------------------------------

@router.get("/search",
    responses={status.HTTP_400_BAD_REQUEST: {"description": "Empty query"}},
)
async def search(
    q: str = Query(..., description="Что искать: слова или смысл"),
    kind: Optional[List[ItemKind]] = Query(None, description="Где искать: note, event; по умолчанию везде"),
    limit: int = Query(10, ge=1, le=50, description="Сколько результатов вернуть"),
    engine: AssistantEngine = Depends(get_engine),
) -> SearchResponse:
    """Search over notes and events by words and by meaning"""
    return await engine.search(q, kind, limit)
