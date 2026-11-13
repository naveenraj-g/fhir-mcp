"""
Plugins — how a connector's tools are generated. Each turns a backend's
self-description into a FastMCP `Provider`:

    rest     OpenAPI spec      → FastMCP OpenAPIProvider (app.plugins.rest)
    graphql  GraphQL schema    → GraphQLProvider          (app.plugins.graphql)
             with `graphql.engine: graphql_mcp`, the graphql-mcp library
             generates the tools instead               (app.plugins.graphql_mcp)

Plugins know nothing about which backend they're pointed at or how it
authenticates; they get two ready httpx2 clients — `discovery` (startup only,
to read the spec/schema) and `client` (shared by every tool call, carrying
the connector's outbound auth) — and their own config block. Adding a plugin:
write `build_<name>_provider(config, *, client, discovery)`, register it
below, and extend ConnectorConfig.plugin's Literal in app/core/config.py.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
from fastmcp.server.providers import Provider

from app.core.config import ConnectorConfig
from app.plugins.graphql import build_graphql_provider
from app.plugins.graphql_mcp import build_graphql_mcp_provider
from app.plugins.rest import build_rest_provider

PluginBuilder = Callable[..., Awaitable[Provider]]

PLUGINS: dict[str, PluginBuilder] = {
    "rest": build_rest_provider,
    "graphql": build_graphql_provider,
}


async def build_plugin_provider(
    connector: ConnectorConfig, *, client: httpx2.AsyncClient, discovery: httpx2.AsyncClient
) -> Provider:
    plugin_config: Any = getattr(connector, connector.plugin)
    builder = PLUGINS[connector.plugin]
    if connector.plugin == "graphql" and plugin_config.engine == "graphql_mcp":
        builder = build_graphql_mcp_provider
    return await builder(plugin_config, client=client, discovery=discovery)
