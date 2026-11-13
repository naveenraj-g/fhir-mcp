"""
Connectors — which backend system the tools talk to. Same idea as fhir-gql's
app/connectors: `connectors.active` in configs/config.yaml picks one, and
everything about it (URLs, plugin, auth mode, filters) lives in
configs/connectors/<name>.yaml.

Here a connector is pure configuration plus two runtime objects built from
it, so there's no per-connector class: the backend differences that matter
for MCP — where the API is, how it describes itself, how it authenticates —
are all settings. (fhir-gql needs per-connector translator classes because
it maps between wire formats by hand; here the backend's own OpenAPI spec or
GraphQL schema *is* the mapping.) If a backend ever needs code-level quirks,
that is the place to add a package under app/connectors/<name>/.

Lifecycle:
  1. `discover_connector()` — before the server starts: build the runtime
     client, read the spec/schema with a short-lived discovery client, and
     return a `Connector` holding the plugin's Provider.
  2. The Provider is added to the FastMCP server.
  3. `Connector.aclose()` — at shutdown, from the server lifespan.
"""

from dataclasses import dataclass

import httpx2
from fastmcp.server.providers import Provider
from fastmcp.server.transforms import Namespace

from app.auth.outbound import (
    ClientCredentialsAuth,
    build_discovery_auth,
    build_runtime_auth,
)
from app.core.config import ConnectorConfig
from app.errors.upstream import UpstreamErrorTransform
from app.plugins import build_plugin_provider


@dataclass
class Connector:
    name: str
    config: ConnectorConfig
    provider: Provider
    client: httpx2.AsyncClient

    async def aclose(self) -> None:
        await self.client.aclose()
        if isinstance(self.client.auth, ClientCredentialsAuth):
            await self.client.auth.tokens.aclose()


async def discover_connector(
    name: str,
    config: ConnectorConfig,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Connector:
    """`transport` replaces the network for every backend call (tests plug a
    fake backend in here); None = real HTTP."""
    timeout = httpx2.Timeout(config.timeout_seconds)
    # Used by every tool call for the server's whole lifetime. Created here
    # but not used until the server's own event loop is running — httpx2
    # opens connections lazily, so it isn't tied to this discovery loop.
    # The rest plugin resolves spec paths against base_url; the graphql
    # plugin posts to an absolute endpoint.
    base_url = config.rest.base_url if config.plugin == "rest" and config.rest else ""
    client = httpx2.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        auth=build_runtime_auth(config, transport=transport),
        transport=transport,
    )
    discovery_auth = build_discovery_auth(config, transport=transport)
    async with httpx2.AsyncClient(
        timeout=timeout, auth=discovery_auth, transport=transport
    ) as discovery:
        try:
            provider = await build_plugin_provider(
                config, client=client, discovery=discovery
            )
        except BaseException:
            await client.aclose()
            raise
        finally:
            if isinstance(discovery_auth, ClientCredentialsAuth):
                await discovery_auth.tokens.aclose()
    # Backend 4xx/5xx and transport failures → ToolError inside the tool, so
    # the backend's own explanation reaches the client (app.errors.upstream).
    provider.add_transform(UpstreamErrorTransform())
    if config.tool_prefix:
        provider.add_transform(Namespace(config.tool_prefix))
    return Connector(name=name, config=config, provider=provider, client=client)
