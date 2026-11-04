from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger
from mcp.server.transport_security import TransportSecuritySettings

from app.api_v1.tools import build_mcp_server
from app.core.config import get_settings
from app.core.engine import AssistantEngine


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    engine = AssistantEngine(get_settings())
    await engine.startup()

    # The MCP tools need the engine, so their app is built here and served by McpEndpoint
    mcp_server = build_mcp_server(engine, app.version)
    app.state.mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        # OpenClaw calls by the compose service name; the bearer token guards the endpoint instead
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.state.engine = engine

    try:
        async with mcp_server.session_manager.run():
            yield
    finally:
        logger.info("Shutting down...")
        await engine.shutdown()
