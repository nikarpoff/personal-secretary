"""
MCP tools for the OpenClaw agent.

The same engine as the REST API, flattened into tool arguments: a model fills flat
fields more reliably than nested objects. A public domain error reaches the model as
its text, so it can correct the call; other failures stay "Error executing tool".
"""

from typing import Annotated, Awaitable, List, Literal, Optional, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.api_v1.schemas import (
    TIME_HINT,
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
from app.core.engine import AssistantEngine
from app.core.entities import ItemKind
from app.core.exceptions import AppError

T = TypeVar("T")

INSTRUCTIONS = (
    "Personal secretary backend: the owner's Google Calendar, notes and one knowledge base over both. "
    "Times are the owner's local time; list_events returns the current local time in `now`. "
    "Telegram reminders before events are sent by the service itself: never promise to remind on your own, "
    "set remind_minutes or important instead. Search the knowledge base before answering questions about "
    "past meetings, plans or notes."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True)


async def _call(action: Awaitable[T]) -> T:
    try:
        return await action
    except AppError as e:
        if e.public:
            raise ToolError(str(e)) from e
        raise


def build_mcp_server(engine: AssistantEngine, version: str) -> MCPServer:
    """MCP server whose tools call the engine."""
    server = MCPServer(name="personal-assistant", instructions=INSTRUCTIONS, version=version)

    # ---- calendar ----------------------------------------------------------

    @server.tool(annotations=READ_ONLY)
    async def list_events(
        start: Annotated[Optional[str], Field(description=f"Range start: {TIME_HINT}; today by default")] = None,
        end: Annotated[Optional[str], Field(description="Range end; a date includes its whole day; the start day when start is a date, a week otherwise")] = None,
        query: Annotated[Optional[str], Field(description="Words in the title, description or place")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum events")] = 100,
    ) -> EventListResponse:
        """Events of the calendar in a range with their notes, plus the current local time."""
        return await _call(engine.list_events(start, end, query, limit))

    @server.tool()
    async def create_event(
        title: Annotated[str, Field(min_length=1, description="Event title")],
        start: Annotated[str, Field(description=f"Start: {TIME_HINT}")],
        end: Annotated[Optional[str], Field(description="End in the same form; for an all-day event the last day, inclusive")] = None,
        duration_minutes: Annotated[Optional[int], Field(ge=1, description="Duration when end is omitted; 60 by default")] = None,
        description: Annotated[str, Field(description="Details, agenda, links")] = "",
        location: Annotated[str, Field(description="Place or a meeting link")] = "",
        important: Annotated[bool, Field(description="Important: extra early reminders (a day and two hours before by default)")] = False,
        remind_minutes: Annotated[Optional[List[int]], Field(description="Telegram reminders N minutes before the start, e.g. [60, 10]; [] for none; the defaults when omitted")] = None,
        recurrence: Annotated[Optional[str], Field(description="RRULE for a recurring event, e.g. 'RRULE:FREQ=WEEKLY;BYDAY=MO'")] = None,
    ) -> EventInfo:
        """Creates an event in Google Calendar; the owner gets Telegram reminders before it."""
        request = EventCreateRequest(
            title=title, start=start, end=end, duration_minutes=duration_minutes, description=description,
            location=location, important=important, remind_minutes=remind_minutes, recurrence=recurrence,
        )
        return await _call(engine.create_event(request))

    @server.tool()
    async def update_event(
        event_id: Annotated[str, Field(description="Event id; for a recurring event the occurrence id changes one occurrence, recurring_event_id the series")],
        title: Annotated[Optional[str], Field(min_length=1, description="New title")] = None,
        start: Annotated[Optional[str], Field(description=f"New start: {TIME_HINT}; the duration is kept when end is omitted")] = None,
        end: Annotated[Optional[str], Field(description="New end")] = None,
        description: Annotated[Optional[str], Field(description="New details; empty string clears them")] = None,
        location: Annotated[Optional[str], Field(description="New place; empty string clears it")] = None,
        important: Annotated[Optional[bool], Field(description="Mark or unmark as important")] = None,
        remind_minutes: Annotated[Optional[List[int]], Field(description="New reminders, minutes before the start; [] for none")] = None,
    ) -> EventInfo:
        """Changes an event: only the given fields change."""
        request = EventUpdateRequest(
            title=title, start=start, end=end, description=description,
            location=location, important=important, remind_minutes=remind_minutes,
        )
        return await _call(engine.update_event(event_id, request))

    @server.tool(annotations=DESTRUCTIVE)
    async def delete_event(
        event_id: Annotated[str, Field(description="Event id; recurring_event_id deletes the whole series")],
    ) -> str:
        """Deletes an event from Google Calendar. Confirm with the owner first."""
        await _call(engine.delete_event(event_id))
        return f"Event {event_id} deleted"

    # ---- notes -------------------------------------------------------------

    @server.tool()
    async def add_note(
        content: Annotated[str, Field(min_length=1, description="Note text as the owner said it, without retelling")],
        title: Annotated[Optional[str], Field(description="Short title; the first line when omitted")] = None,
        tags: Annotated[Optional[List[str]], Field(description="Tags without '#', e.g. ['work', 'ideas']")] = None,
        event_id: Annotated[Optional[str], Field(description="Id of the calendar event the note belongs to")] = None,
    ) -> NoteInfo:
        """Saves a note into the knowledge base, optionally linked to an event."""
        request = NoteCreateRequest(content=content, title=title, tags=tags or [], event_id=event_id)
        return await _call(engine.add_note(request))

    @server.tool()
    async def update_note(
        note_id: Annotated[int, Field(description="Note id")],
        content: Annotated[Optional[str], Field(min_length=1, description="New text, it replaces the old one")] = None,
        title: Annotated[Optional[str], Field(min_length=1, description="New title")] = None,
        tags: Annotated[Optional[List[str]], Field(description="New tags, they replace the old ones")] = None,
        event_id: Annotated[Optional[str], Field(description="Event to link to; empty string unlinks")] = None,
    ) -> NoteInfo:
        """Changes a note: only the given fields change."""
        request = NoteUpdateRequest(content=content, title=title, tags=tags, event_id=event_id)
        return await _call(engine.update_note(note_id, request))

    @server.tool(annotations=DESTRUCTIVE)
    async def delete_note(
        note_id: Annotated[int, Field(description="Note id")],
    ) -> str:
        """Deletes a note. Confirm with the owner first."""
        await _call(engine.delete_note(note_id))
        return f"Note {note_id} deleted"

    @server.tool(annotations=READ_ONLY)
    async def list_notes(
        tag: Annotated[Optional[str], Field(description="Only notes with this tag")] = None,
        event_id: Annotated[Optional[str], Field(description="Only notes of this event")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum notes")] = 20,
    ) -> NoteListResponse:
        """The latest notes, full text."""
        return await _call(engine.list_notes(tag, event_id, limit))

    # ---- knowledge base ----------------------------------------------------

    @server.tool(annotations=READ_ONLY)
    async def search(
        query: Annotated[str, Field(description="What to find: names, words or a question in plain language")],
        kind: Annotated[Optional[Literal["note", "event"]], Field(description="Only notes or only events; both by default")] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum hits")] = 10,
    ) -> SearchResponse:
        """Searches notes and calendar events by words and by meaning, past ones included."""
        kinds = [ItemKind(kind)] if kind else None
        return await _call(engine.search(query, kinds, limit))

    return server


class McpEndpoint:
    """ASGI app at /mcp forwarding to the MCP app the lifespan builds around the engine"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Inside a Mount the scope still carries the FastAPI app
        mcp_app = getattr(scope["app"].state, "mcp_app", None)
        if mcp_app is None:
            response = JSONResponse({"detail": "service is not initialized"}, status_code=500)
            await response(scope, receive, send)
            return
        await mcp_app(scope, receive, send)
