"""
Domain errors
"""

from typing import Optional


class AppError(Exception):
    """Base app error; its message is hidden from the client."""
    status_code = 500
    public = False

class InvalidRequestError(AppError):
    """The request itself is wrong: bad time, empty text, end before start."""
    status_code = 400
    public = True

class NotFoundError(AppError):
    """No note or event with this id."""
    status_code = 404
    public = True

class CalendarNotConfiguredError(AppError):
    """Calendar tools are called while GOOGLE_CREDENTIALS_FILE is unset."""
    status_code = 503
    public = True

    def __init__(self, message: str = "Google Calendar is not configured: set GOOGLE_CREDENTIALS_FILE") -> None:
        super().__init__(message)

class UpstreamError(AppError):
    """Upstream service produced an error"""
    status_code = 502
    public = True

    def __init__(self, message: str = "", service: Optional[str] = None) -> None:
        self.service = service
        super().__init__(f"{service}: {message}" if service else message)

class UpstreamTimeoutError(UpstreamError):
    """Request to upstream service timeouted"""
    status_code = 504

class UpstreamUnavailableError(UpstreamError):
    """Upstream service is unreachable: the request is worth another attempt"""
    status_code = 503

class UpstreamRejectedError(UpstreamError):
    """Upstream service refused the content itself (4xx); retrying will not help"""
    status_code = 422
