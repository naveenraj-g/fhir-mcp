"""
Server assembly.

    settings ──► discover_connector(active)  ──► Provider (rest | graphql)
             └─► build_token_verifier(auth)  ──► FastMCP(auth=...)
                                                   + middleware
                                                   + /health route

`create_server()` is async because tool discovery reads the backend's
OpenAPI spec or GraphQL schema over HTTP. It runs once, before the server
starts accepting connections (see app/__main__.py), so a misconfigured or
unreachable backend fails the process at startup instead of producing a
server with no tools.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx2
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth import build_token_verifier
from app.connectors import Connector, discover_connector
from app.core.config import Settings
from app.core.logging import get_logger
from app.middleware import RequestContextMiddleware, ToolCallMiddleware

logger = get_logger(__name__)


async def create_server(
    settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
) -> FastMCP:
    """`transport` swaps the network out for every backend call — tests use
    it to run the full server against a fake backend."""
    name = settings.connectors.active
    connector = await discover_connector(
        name, settings.connectors.active_config(), transport=transport
    )

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncGenerator[None]:
        logger.info(
            "MCP server started",
            extra={
                "event": "startup.ready",
                "connector": name,
                "plugin": connector.config.plugin,
            },
        )
        try:
            yield
        finally:
            await connector.aclose()
            logger.info(
                "Connector closed",
                extra={"event": "shutdown.connector_closed", "connector": name},
            )

    mcp = FastMCP(
        settings.app.name,
        instructions=settings.app.instructions,
        version=settings.app.version,
        auth=build_token_verifier(settings.auth),
        providers=[connector.provider],
        middleware=[RequestContextMiddleware(), ToolCallMiddleware(settings.logging)],
        lifespan=lifespan,
        # Unexpected exceptions are bugs — don't leak their text to clients.
        # Useful failures (backend errors, missing token) are ToolErrors,
        # which are never masked; see app.middleware.
        mask_error_details=True,
    )
    _add_health_route(mcp, connector)
    return mcp


def _add_health_route(mcp: FastMCP, connector: Connector) -> None:
    """Liveness + which backend this instance serves. Custom routes bypass
    MCP auth, so it reveals nothing beyond the connector and plugin names."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "connector": connector.name,
                "plugin": connector.config.plugin,
            }
        )
