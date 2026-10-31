from secrets import compare_digest
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.core.config import get_settings, Settings


security = HTTPBearer(auto_error=False)


def get_auth_config(settings: Settings = Depends(get_settings)) -> Optional[str]:
    """Auth token from the config; None when auth is off."""
    return settings.AUTH_TOKEN


async def verify_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    auth_token: Optional[str] = Depends(get_auth_config),
) -> str:
    """Bearer token check: 401 without a token, 403 with a wrong one."""
    # If auth is disabled, allow access
    if auth_token is None:
        return "anonymous"

    # If auth is enabled, require credentials
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time comparison: no timing hints about the token
    if not compare_digest(credentials.credentials, auth_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return "user"
