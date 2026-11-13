"""Entry point: `uv run python -m app`."""

import asyncio
import sys

from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.errors import StartupError
from app.main import create_server

logger = get_logger("app")


def main() -> None:
    setup_logging(settings.logging)
    try:
        mcp = asyncio.run(create_server(settings))
    except StartupError as e:
        logger.error(f"Startup failed: {e}", extra={"event": "startup.failed"})
        sys.exit(1)

    if settings.server.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return
    mcp.run(
        transport="http",
        host=settings.server.host,
        port=settings.server.port,
        path=settings.server.path,
        show_banner=False,
        # Route Uvicorn's own loggers through app.core.logging's handler.
        uvicorn_config={"log_config": None},
    )


if __name__ == "__main__":
    main()
