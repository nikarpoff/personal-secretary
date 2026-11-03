"""
MCP tools of the OpenClaw agent.

The same engine as the REST API, flattened into tool arguments: a model fills flat
fields more reliably than nested objects. A public domain error reaches the model as
its text, so it can correct the call; other failures stay "Error executing tool".
"""

from typing import Annotated, Any, Dict, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
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
    PEOPLE_HINT,
    People,
    ReminderMinutes,
)
from app.core.engine import AssistantEngine
from app.core.exceptions import AppError

INSTRUCTIONS = (
    "Personal secretary backend: the owner's Google Calendar. "
    "Times are the owner's local time; list_events returns the current local time in `now`. "
    "Telegram reminders before events are sent by the service itself: never promise to remind on your own, "
    "set remind_minutes or important instead."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True)


class AssistantMCP(MCPServer):
    """MCP server with one error handler for all the tools, like the REST one"""

    async def call_tool(self, name: str, arguments: Dict[str, Any], context: Any = None) -> Any:
        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as e:
            # The SDK hides a crash behind a generic text; a public domain error is safe to show
            cause = e.__cause__
            if isinstance(cause, AppError) and cause.public:
                raise ToolError(f"Error executing tool {name}: {cause}") from cause
            raise


def build_mcp_server(engine: AssistantEngine, version: str) -> MCPServer:
    """MCP server whose tools call the engine."""
    server = AssistantMCP(name="personal-assistant", instructions=INSTRUCTIONS, version=version)

    @server.tool(annotations=READ_ONLY)
    async def list_events(
        start: Annotated[Optional[str], Field(description=f"Range start: {TIME_HINT}; today by default")] = None,
        end: Annotated[Optional[str], Field(description="Range end; a date includes its whole day; the start day when start is a date, a week otherwise")] = None,
        query: Annotated[Optional[str], Field(description="Words in the title, description, place or attendees: finds an event to change or delete")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum events")] = 100,
    ) -> EventListResponse:
        """Events of the calendar in a range, plus the current local time."""
        return await engine.events.list(start, end, query, limit)

    @server.tool()
    async def create_event(
        title: Annotated[str, Field(min_length=1, description="Event title")],
        start: Annotated[str, Field(description=f"Start: {TIME_HINT}")],
        end: Annotated[Optional[str], Field(description="End in the same form; for an all-day event the last day, inclusive")] = None,
        duration_minutes: Annotated[Optional[int], Field(ge=1, description="Duration when end is omitted; 60 by default")] = None,
        description: Annotated[str, Field(description="Details, agenda, links")] = "",
        location: Annotated[str, Field(description="Place or a meeting link")] = "",
        important: Annotated[bool, Field(description="Important: extra early reminders (a day and two hours before by default)")] = False,
        remind_minutes: Annotated[Optional[ReminderMinutes], Field(description="Telegram reminders N minutes before the start, e.g. [60, 10]; [] for none; the defaults when omitted")] = None,
        recurrence: Annotated[Optional[str], Field(description="RRULE for a recurring event, e.g. 'RRULE:FREQ=WEEKLY;BYDAY=MO'")] = None,
        people: Annotated[Optional[People], Field(description=PEOPLE_HINT)] = None,
    ) -> EventInfo:
        """Creates an event in Google Calendar; the owner gets Telegram reminders before it."""
        request = EventCreateRequest(
            title=title, start=start, end=end, duration_minutes=duration_minutes, description=description,
            location=location, important=important, remind_minutes=remind_minutes, recurrence=recurrence,
            people=people or [],
        )
        return await engine.events.create(request)

    @server.tool()
    async def update_event(
        event_id: Annotated[str, Field(description="Event id; for a recurring event the occurrence id changes one occurrence, recurring_event_id the series")],
        title: Annotated[Optional[str], Field(min_length=1, description="New title")] = None,
        start: Annotated[Optional[str], Field(description=f"New start: {TIME_HINT}; the duration is kept when end is omitted")] = None,
        end: Annotated[Optional[str], Field(description="New end")] = None,
        description: Annotated[Optional[str], Field(description="New details; empty string clears them")] = None,
        location: Annotated[Optional[str], Field(description="New place; empty string clears it")] = None,
        important: Annotated[Optional[bool], Field(description="Mark or unmark as important")] = None,
        remind_minutes: Annotated[Optional[ReminderMinutes], Field(description="New reminders, minutes before the start; [] for none")] = None,
        people: Annotated[Optional[People], Field(description=f"{PEOPLE_HINT}; they replace the old ones, [] unlinks all")] = None,
    ) -> EventInfo:
        """Changes an event: only the given fields change."""
        request = EventUpdateRequest(
            title=title, start=start, end=end, description=description,
            location=location, important=important, remind_minutes=remind_minutes, people=people,
        )
        return await engine.events.update(event_id, request)

    @server.tool(annotations=DESTRUCTIVE)
    async def delete_event(
        event_id: Annotated[str, Field(description="Event id; recurring_event_id deletes the whole series")],
    ) -> str:
        """Deletes an event from Google Calendar. Confirm with the owner first."""
        await engine.events.delete(event_id)
        return f"Event {event_id} deleted"

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
