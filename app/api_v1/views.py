from typing import Optional

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
)
from app.core.auth import verify_token
from app.core.engine import AssistantEngine

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
    """Events of a range"""
    return await engine.events.list(start, end, q, limit)


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
    return await engine.events.create(body)


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
    return await engine.events.update(event_id, body)


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
    await engine.events.delete(event_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
