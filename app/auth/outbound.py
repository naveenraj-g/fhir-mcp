"""
Outbound auth — how a connector's HTTP client authenticates to its backend.

Each strategy is an `httpx2.Auth`, attached to the connector's one shared
`httpx2.AsyncClient`. That keeps auth out of both plugins: FastMCP's
OpenAPITool and our GraphQLTool just `send()` through the client, and the
client stamps `Authorization` per request. Note FastMCP deliberately never
forwards the inbound `Authorization` header on its own (see its OpenAPI docs,
"Request Parameter Handling") — forwarding it is this module's explicit job.

`auth_flow` runs in the same task as the tool call, so FastMCP's
request-scoped ContextVars (the verified token, the HTTP request) are
visible here.
"""

from collections.abc import AsyncGenerator, Generator

import httpx2
from fastmcp.server.dependencies import get_access_token, get_http_headers

from app.auth.token_manager import ClientCredentialsTokenManager
from app.core.config import ConnectorConfig
from app.errors import ConfigError, MissingCallerTokenError


def caller_bearer_token() -> str | None:
    """The MCP caller's bearer token for the current request, or None.

    Prefers the token JWTVerifier already verified (auth.enabled). With
    inbound auth off, falls back to the raw Authorization header so a
    token_passthrough backend can still do the verification itself — local
    development only."""
    token = get_access_token()
    if token is not None:
        return token.token
    header = get_http_headers(include={"authorization"}).get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


class TokenPassthroughAuth(httpx2.Auth):
    """Forward the caller's own token. Used when the backend trusts the same
    IAM this server does (fhir-server, fhir-gql)."""

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        token = caller_bearer_token()
        if token is None:
            raise MissingCallerTokenError()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class StaticBearerAuth(httpx2.Auth):
    """A fixed token — the startup-only `discovery_token`."""

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


class ClientCredentialsAuth(httpx2.Auth):
    """Authenticate as this server, via OAuth2 client_credentials. On a 401
    the cached token is discarded and the request retried once — covers a
    token revoked early or clock skew without looping on a bad token."""

    def __init__(self, tokens: ClientCredentialsTokenManager) -> None:
        self.tokens = tokens

    def sync_auth_flow(self, request):  # pragma: no cover - async clients only
        raise RuntimeError("ClientCredentialsAuth requires an async client")

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = f"Bearer {await self.tokens.get_token()}"
        response = yield request
        if response.status_code == 401:
            await self.tokens.invalidate()
            request.headers["Authorization"] = f"Bearer {await self.tokens.get_token()}"
            yield request


def build_runtime_auth(
    config: ConnectorConfig, *, transport: httpx2.AsyncBaseTransport | None = None
) -> httpx2.Auth | None:
    """Auth for tool calls."""
    match config.auth_mode:
        case "token_passthrough":
            return TokenPassthroughAuth()
        case "oauth_client_credentials":
            return ClientCredentialsAuth(
                ClientCredentialsTokenManager(
                    config.token_url, config.client_id, config.client_secret, transport=transport
                )
            )
        case "none":
            return None
    raise ConfigError(f"unknown auth_mode {config.auth_mode!r}")


def build_discovery_auth(
    config: ConnectorConfig, *, transport: httpx2.AsyncBaseTransport | None = None
) -> httpx2.Auth | None:
    """Auth for startup discovery (spec fetch / introspection), when there is
    no caller. A connector that authenticates as itself uses its own
    credentials; a passthrough connector uses `discovery_token` if one is
    set, else goes unauthenticated (fine for fhir-server's public
    /openapi.json)."""
    if config.auth_mode == "oauth_client_credentials":
        return build_runtime_auth(config, transport=transport)
    if config.discovery_token:
        return StaticBearerAuth(config.discovery_token)
    return None
