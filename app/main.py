from fastapi import FastAPI, APIRouter

from app.api_v1 import router
from app.api_v1.errors import register_error_handlers
from app.api_v1.tools import McpEndpoint
from app.core.auth import BearerTokenMiddleware
from app.core.config import load_settings_or_exit
from app.utils.lifespan import lifespan
from app.utils.logging import configure_logging


# Get config; a bad one stops the service with the reasons in the log
settings = load_settings_or_exit()

# Configure app and uvicorn loggers
configure_logging(settings.LOG_LEVEL, settings.LOG_DIAGNOSE)

# Create FastAPI app with lifespan
app = FastAPI(lifespan=lifespan, title="Personal Assistant", version="1.0.0")

# Health check router
health_router = APIRouter()

@health_router.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok"}

register_error_handlers(app)
app.include_router(router)
app.include_router(health_router)

# MCP endpoint of the OpenClaw agent: http://<host>:8000/mcp/
app.mount("/mcp", BearerTokenMiddleware(McpEndpoint(), settings.AUTH_TOKEN))
