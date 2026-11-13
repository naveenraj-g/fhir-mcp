import json
from pathlib import Path
from typing import Any

import httpx2
from fastmcp.server.providers.openapi import (
    MCPType,
    OpenAPIProvider,
    OpenAPIResource,
    OpenAPIResourceTemplate,
    OpenAPITool,
    RouteMap,
)
from fastmcp.utilities.openapi import HTTPRoute
from mcp.types import ToolAnnotations

from app.core.config import ROOT_DIR, RestPluginConfig, RouteFilter
from app.core.logging import get_logger
from app.errors import DiscoveryError
from app.plugins.rest.arguments import argument_rename_transform
from app.plugins.rest.expand import expand_resource_types

logger = get_logger(__name__)


async def load_openapi_spec(config: RestPluginConfig, discovery: httpx2.AsyncClient) -> dict[str, Any]:
    if config.openapi_file:
        path = ROOT_DIR / config.openapi_file
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise DiscoveryError(f"cannot read OpenAPI spec file {path}: {e}") from e
    assert config.openapi_url
    try:
        r = await discovery.get(config.openapi_url)
        r.raise_for_status()
        return r.json()
    except (httpx2.HTTPError, ValueError) as e:
        raise DiscoveryError(f"cannot load OpenAPI spec from {config.openapi_url}: {e}") from e


def route_maps(config: RestPluginConfig) -> list[RouteMap]:
    """exclude rules first (first match wins), then include rules, then — if
    there is an allow-list at all — a catch-all EXCLUDE so nothing else
    falls through to FastMCP's default "every route is a tool"."""

    def to_map(f: RouteFilter, mcp_type: MCPType) -> RouteMap:
        methods = f.methods if f.methods == "*" else [m.upper() for m in f.methods]
        return RouteMap(methods=methods, pattern=f.pattern, tags=set(f.tags), mcp_type=mcp_type)

    maps = [to_map(f, MCPType.EXCLUDE) for f in config.exclude]
    maps += [to_map(f, MCPType.TOOL) for f in config.include]
    if config.include:
        maps.append(RouteMap(mcp_type=MCPType.EXCLUDE))
    return maps


def _annotate(route: HTTPRoute, component: OpenAPITool | OpenAPIResource | OpenAPIResourceTemplate) -> None:
    """MCP tool hints from the HTTP method. `_search` is a POST that only
    reads, so it counts as read-only too."""
    if not isinstance(component, OpenAPITool):
        return
    method = route.method.upper()
    read_only = method in {"GET", "HEAD"} or route.path.endswith("/_search")
    component.annotations = ToolAnnotations(
        title=route.summary or None,
        read_only_hint=read_only,
        destructive_hint=method == "DELETE",
        idempotent_hint=read_only or method in {"PUT", "DELETE"},
        open_world_hint=False,
    )


async def build_rest_provider(
    config: RestPluginConfig,
    *,
    client: httpx2.AsyncClient,
    discovery: httpx2.AsyncClient,
) -> OpenAPIProvider:
    spec = await load_openapi_spec(config, discovery)
    try:
        spec = expand_resource_types(spec, config.resource_types)
        provider = OpenAPIProvider(
            spec,
            client=client,
            route_maps=route_maps(config),
            mcp_component_fn=_annotate,
            validate_output=config.validate_output,
        )
    except ValueError as e:
        raise DiscoveryError(f"invalid OpenAPI spec: {e}") from e

    tools = await provider.list_tools()
    if rename := argument_rename_transform(tools):
        provider.add_transform(rename)
    if not tools:
        logger.warning(
            "No operations matched the rest plugin's include rules",
            extra={"event": "discovery.no_tools", "openapi": config.openapi_url or config.openapi_file},
        )
    logger.info(
        "Loaded OpenAPI tools",
        extra={"event": "discovery.loaded", "plugin": "rest", "tools": sorted(t.name for t in tools)},
    )
    return provider
