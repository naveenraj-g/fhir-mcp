"""
The graphql plugin — one tool per included Query/Mutation root field.

FastMCP has no GraphQL integration, so this is a custom `Provider` (the
FastMCP 3+/4 extension point for "components from somewhere else"). Each
tool's operation document is generated once at startup from the schema:

    query GetFhirOrganization($id: String!) {
      getFhirOrganization(id: $id) { id name active telecom { system value } ... }
    }

A call sends that document plus the tool arguments as `variables` through
the connector's shared httpx2 client — so outbound auth (token passthrough
or client credentials) is applied exactly as for the rest plugin.
"""

import json
import re
from collections.abc import Sequence
from typing import Any

import httpx2
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool, ToolResult
from mcp.types import ToolAnnotations

from app.core.config import GraphQLPluginConfig
from app.core.logging import get_logger
from app.errors import GraphQLToolError
from app.plugins.graphql.introspection import load_schema
from app.plugins.graphql.schema import SchemaIndex, default_value, type_ref

logger = get_logger(__name__)

_MAX_ERROR_BODY = 2000


def snake_case(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


class GraphQLTool(Tool):
    """Executes one fixed GraphQL operation."""

    def __init__(
        self,
        *,
        client: httpx2.AsyncClient,
        endpoint: str,
        document: str,
        operation_name: str,
        field_name: str,
        variable_names: set[str],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._client = client
        self._endpoint = endpoint
        self._document = document
        self._operation_name = operation_name
        self._field_name = field_name
        self._variable_names = variable_names

    @property
    def document(self) -> str:
        return self._document

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        variables = {k: v for k, v in arguments.items() if k in self._variable_names}
        response = await self._client.post(
            self._endpoint,
            json={"query": self._document, "variables": variables, "operationName": self._operation_name},
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or (response.status_code >= 400 and not payload.get("errors")):
            raise GraphQLToolError(
                f"HTTP {response.status_code} from GraphQL backend: {response.text[:_MAX_ERROR_BODY]}"
            )
        if payload.get("errors"):
            raise GraphQLToolError("; ".join(_error_message(e) for e in payload["errors"]))

        result = prune_nulls((payload.get("data") or {}).get(self._field_name))
        structured = result if isinstance(result, dict) else {"result": result}
        return ToolResult(
            content=json.dumps(result, ensure_ascii=False),
            structured_content=structured,
        )


def prune_nulls(value: Any) -> Any:
    """Drop nulls and the empty objects/arrays they leave behind.

    GraphQL returns `null` for every selected field the resource doesn't
    have, and a generated selection selects every element — so a raw
    Medplum Organization is mostly nulls, which costs the model context and
    isn't valid FHIR JSON anyway (R4 forbids null and empty values). The
    pruned result is exactly the resource as the backend stores it."""
    if isinstance(value, dict):
        pruned = {k: prune_nulls(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v is not None and v != {} and v != []}
    if isinstance(value, list):
        pruned = [prune_nulls(v) for v in value]
        return [v for v in pruned if v is not None and v != {} and v != []]
    return value


def _error_message(error: dict[str, Any]) -> str:
    """GraphQL error message plus fhir-gql's/Medplum's structured detail
    (an OperationOutcome, a status code) when present — that's what tells an
    LLM how to fix its call."""
    message = error.get("message", "unknown error")
    extensions = error.get("extensions")
    if extensions:
        message += f" {json.dumps(extensions, ensure_ascii=False)[:_MAX_ERROR_BODY]}"
    return message


class GraphQLProvider(Provider):
    """Serves the tools built at startup. The HTTP client they share is
    owned (and closed) by app.connectors, not by this provider."""

    def __init__(self, tools: Sequence[GraphQLTool]) -> None:
        super().__init__()
        self._tools = list(tools)

    async def _list_tools(self) -> Sequence[Tool]:
        return self._tools


def _declaration(arg: dict[str, Any]) -> str:
    """`$name: Type` — plus the field's own default, so an omitted optional
    argument still satisfies a non-null variable (`$count: Int! = 50`).
    Introspection gives the default as GraphQL source text, valid as-is."""
    decl = f"${arg['name']}: {type_ref(arg['type'])}"
    default = default_value(arg)
    return f"{decl} = {default}" if default is not None else decl


def _selected(name: str, config: GraphQLPluginConfig) -> bool:
    if any(re.search(p, name) for p in config.exclude):
        return False
    return not config.include or any(re.search(p, name) for p in config.include)


def build_tools(
    schema: dict[str, Any], config: GraphQLPluginConfig, client: httpx2.AsyncClient
) -> list[GraphQLTool]:
    index = SchemaIndex(schema)
    roots = [("query", index.query_type)]
    if config.include_mutations:
        roots.append(("mutation", index.mutation_type))

    tools: list[GraphQLTool] = []
    for operation, root in roots:
        for field in index.root_fields(root):
            name = field["name"]
            if not _selected(name, config):
                continue
            args = field.get("args") or []
            operation_name = name[:1].upper() + name[1:]
            declarations = ", ".join(_declaration(a) for a in args)
            call_args = ", ".join(f"{a['name']}: ${a['name']}" for a in args)
            selection = index.selection_set(field["type"], config.selection_depth)
            document = (
                f"{operation} {operation_name}{f'({declarations})' if declarations else ''} "
                f"{{ {name}{f'({call_args})' if call_args else ''} {selection} }}"
            )
            read_only = operation == "query"
            tools.append(
                GraphQLTool(
                    client=client,
                    endpoint=config.endpoint,
                    document=document,
                    operation_name=operation_name,
                    field_name=name,
                    variable_names={a["name"] for a in args},
                    name=snake_case(name),
                    description=field.get("description")
                    or f"GraphQL {operation} `{name}` returning `{type_ref(field['type'])}`.",
                    parameters=index.input_schema(args),
                    annotations=ToolAnnotations(
                        read_only_hint=read_only,
                        destructive_hint=not read_only and name.lower().startswith("delete"),
                        idempotent_hint=read_only,
                        open_world_hint=False,
                    ),
                    tags={"graphql", operation},
                )
            )
    return tools


async def build_graphql_provider(
    config: GraphQLPluginConfig,
    *,
    client: httpx2.AsyncClient,
    discovery: httpx2.AsyncClient,
) -> GraphQLProvider:
    schema = await load_schema(config, discovery)
    tools = build_tools(schema, config, client)
    if not tools:
        logger.warning(
            "No GraphQL fields matched the graphql plugin's include rules",
            extra={"event": "discovery.no_tools", "endpoint": config.endpoint},
        )
    logger.info(
        "Loaded GraphQL tools",
        extra={"event": "discovery.loaded", "plugin": "graphql", "tools": sorted(t.name for t in tools)},
    )
    return GraphQLProvider(tools)
