"""
Error hierarchy.

Two kinds of failure, kept apart on purpose:

- **Startup errors** (`StartupError` and subclasses) — misconfiguration or an
  unreachable backend while discovering tools. Raised before the server
  accepts connections; the process exits with the message. Never shown to an
  MCP client.
- **Tool-call errors** — raised while a tool runs. These subclass FastMCP's
  `ToolError`, whose message FastMCP always returns to the client as an
  `isError` tool result (even with `mask_error_details` on), so an LLM sees
  *why* the call failed — a missing token, the backend's OperationOutcome —
  and can correct itself. Anything else raised inside a tool is a bug and is
  masked/logged by app.middleware.
"""

from fastmcp.exceptions import ToolError


class StartupError(Exception):
    """Base for anything that stops the server from starting."""


class ConfigError(StartupError):
    """configs/*.yaml or env vars describe something impossible."""


class DiscoveryError(StartupError):
    """The OpenAPI spec or GraphQL schema couldn't be loaded or parsed."""


class MissingCallerTokenError(ToolError):
    """A token_passthrough connector was called with no caller bearer token
    to forward (no Authorization header on the MCP request, or stdio)."""

    def __init__(self) -> None:
        super().__init__(
            "This tool forwards the caller's bearer token to the backend, but the MCP "
            "request carried none. Connect with an `Authorization: Bearer <token>` header."
        )


class UpstreamAuthError(ToolError):
    """This server couldn't authenticate itself to the backend
    (oauth_client_credentials grant rejected or token endpoint unreachable)."""


class GraphQLToolError(ToolError):
    """The GraphQL backend answered with `errors` (or a non-2xx status)."""
