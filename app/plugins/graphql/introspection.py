"""Loading a GraphQL schema — live introspection or a saved result file."""

import json
from pathlib import Path
from typing import Any

import httpx2

from app.core.config import ROOT_DIR, GraphQLPluginConfig
from app.errors import DiscoveryError

# The standard introspection query, trimmed to what tool generation reads.
# TypeRef nests ofType 7 deep — enough for [[T!]!]! and then some.
INTROSPECTION_QUERY = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    types {
      kind name description
      fields(includeDeprecated: false) {
        name description
        args { name description type { ...TypeRef } defaultValue }
        type { ...TypeRef }
      }
      inputFields { name description type { ...TypeRef } defaultValue }
      enumValues(includeDeprecated: false) { name description }
      possibleTypes { name }
    }
  }
}
fragment TypeRef on __Type {
  kind name
  ofType { kind name ofType { kind name ofType { kind name ofType { kind name
    ofType { kind name ofType { kind name ofType { kind name } } } } } } }
}
"""


def _unwrap(payload: dict[str, Any], source: str) -> dict[str, Any]:
    """Accept both `{"data": {"__schema": ...}}` and a bare `{"__schema": ...}`."""
    if payload.get("errors"):
        messages = "; ".join(e.get("message", "?") for e in payload["errors"])
        raise DiscoveryError(f"introspection of {source} failed: {messages}")
    schema = (payload.get("data") or payload).get("__schema")
    if not schema:
        raise DiscoveryError(f"{source} returned no __schema")
    return schema


async def load_schema(config: GraphQLPluginConfig, discovery: httpx2.AsyncClient) -> dict[str, Any]:
    if config.schema_file:
        path = Path(ROOT_DIR / config.schema_file)
        try:
            return _unwrap(json.loads(path.read_text(encoding="utf-8")), str(path))
        except (OSError, ValueError) as e:
            raise DiscoveryError(f"cannot read GraphQL schema file {path}: {e}") from e
    try:
        r = await discovery.post(config.endpoint, json={"query": INTROSPECTION_QUERY})
        r.raise_for_status()
        payload = r.json()
    except httpx2.HTTPStatusError as e:
        hint = (
            " — the endpoint needs auth for introspection; set the connector's "
            "discovery_token or graphql.schema_file"
            if e.response.status_code in (401, 403)
            else ""
        )
        raise DiscoveryError(f"introspection of {config.endpoint} failed: {e}{hint}") from e
    except (httpx2.HTTPError, ValueError) as e:
        raise DiscoveryError(f"introspection of {config.endpoint} failed: {e}") from e
    return _unwrap(payload, config.endpoint)
