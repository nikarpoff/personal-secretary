from secrets import compare_digest
from typing import Optional

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class BearerTokenMiddleware:
    """Bearer check for a mounted ASGI app that FastAPI dependencies do not reach"""

    def __init__(self, app: ASGIApp, token: Optional[str]) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._token is None:
            await self.app(scope, receive, send)
            return

        header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() != "bearer" or not credentials:
            response = JSONResponse({"detail": "Authentication required"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        elif not compare_digest(credentials.strip(), self._token):
            response = JSONResponse({"detail": "Invalid authentication token"}, status_code=403, headers={"WWW-Authenticate": "Bearer"})
        else:
            await self.app(scope, receive, send)
            return

        await response(scope, receive, send)
