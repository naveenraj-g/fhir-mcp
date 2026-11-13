"""
FastMCP middleware (MCP-message level, not ASGI).

`RequestContextMiddleware` — outermost. Binds `request_id` and the caller's
identity (`actor_sub`, `actor_org_id` from the verified JWT) into the logging
ContextVars, so every line logged while handling the message — ours,
FastMCP's, httpx2's — correlates back to it.

`ToolCallMiddleware` — one line per tool call (`tool.called`) with duration
and outcome: INFO when it succeeded, WARNING when it failed with a ToolError
(backend error, missing token — see app.errors.upstream for how backend
failures become ToolErrors). Anything else that escapes a tool is a bug:
FastMCP logs it with a traceback and, with `mask_error_details=True`
(app.main), hides its text from the client.
"""

import logging
import time
import uuid
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from app.core.config import LogConfig
from app.core.logging import (
    actor_org_var,
    actor_sub_var,
    get_logger,
    redact,
    request_id_var,
)

logger = get_logger("app.tool_calls")


class RequestContextMiddleware(Middleware):
    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        token = get_access_token()
        claims = token.claims if token is not None else {}
        tokens = (
            request_id_var.set(uuid.uuid4().hex),
            actor_sub_var.set(claims.get("sub")),
            actor_org_var.set(claims.get("activeOrganizationId")),
        )
        try:
            return await call_next(context)
        finally:
            for var, tok in zip(
                (request_id_var, actor_sub_var, actor_org_var), tokens, strict=True
            ):
                var.reset(tok)


class ToolCallMiddleware(Middleware):
    def __init__(self, config: LogConfig) -> None:
        self._config = config

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        name = context.message.name
        started = time.perf_counter()
        extra: dict[str, Any] = {"event": "tool.called", "tool": name}
        if self._config.debug_payloads and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Tool arguments",
                extra={
                    "event": "tool.arguments",
                    "tool": name,
                    "arguments": redact(
                        context.message.arguments or {}, self._config.redact
                    ),
                },
            )
        try:
            result = await call_next(context)
        except ToolError as e:
            self._log(extra, started, "tool_error", error=str(e)[:300])
            raise
        self._log(extra, started, "ok")
        return result

    @staticmethod
    def _log(extra: dict[str, Any], started: float, outcome: str, **more: Any) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        level = logging.INFO if outcome == "ok" else logging.WARNING
        logger.log(
            level,
            f"{extra['tool']} -> {outcome}",
            extra={**extra, "outcome": outcome, "duration_ms": duration_ms, **more},
        )
