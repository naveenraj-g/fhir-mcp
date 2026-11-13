"""graphql plugin, end to end over Streamable HTTP against fhir-gql's real schema."""

import json

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.utilities.tests import asgi_client

from app.main import create_server
from app.plugins.graphql.schema import SchemaIndex
from tests.conftest import FIXTURES, ORG, make_settings


def graphql_settings(keypair, **graphql):
    base = {
        "endpoint": "http://backend.test/graphql",
        "include": ["Organization"],
        "exclude": ["^getFhirOrganizations$"],
    }
    return make_settings(keypair, plugin="graphql", graphql={**base, **graphql})


async def test_one_tool_per_selected_root_field(keypair, token, transport):
    server = await create_server(graphql_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {
        "get_fhir_organization", "search_fhir_organizations",
        "create_fhir_organization", "update_fhir_organization", "delete_fhir_organization",
    }
    assert tools["get_fhir_organization"].annotations.read_only_hint is True
    assert tools["delete_fhir_organization"].annotations.destructive_hint is True
    assert tools["get_fhir_organization"].input_schema["required"] == ["id"]
    # count/offset are Int! with defaults — optional for the caller.
    assert not tools["search_fhir_organizations"].input_schema.get("required")


async def test_mutations_can_be_turned_off(keypair, token, transport):
    server = await create_server(graphql_settings(keypair, include_mutations=False), transport=transport)
    async with asgi_client(server, auth=token) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"get_fhir_organization", "search_fhir_organizations"}


async def test_call_sends_document_variables_and_caller_token(keypair, token, transport, backend):
    server = await create_server(graphql_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        result = await client.call_tool("get_fhir_organization", {"id": "190001"})
    assert result.structured_content == ORG

    call = [r for r in backend.calls("/graphql") if b"IntrospectionQuery" not in r.content][-1]
    body = json.loads(call.content)
    assert body["operationName"] == "GetFhirOrganization"
    assert body["variables"] == {"id": "190001"}
    assert body["query"].startswith("query GetFhirOrganization($id: String!) { getFhirOrganization(id: $id) {")
    assert call.headers["authorization"] == f"Bearer {token}"


async def test_defaulted_arguments_keep_their_default_in_the_document(keypair, token, transport, backend):
    """count/offset are `Int!` with defaults on the field: the operation's
    variables must carry the same defaults, or omitting them is an error."""
    server = await create_server(graphql_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        calls_before = len(backend.requests)
        await client.call_tool("search_fhir_organizations", {"name": "x"})
    body = json.loads(backend.requests[calls_before].content)
    assert "$count: Int! = " in body["query"] and "$offset: Int! = " in body["query"]
    assert "count" not in body["variables"]


async def test_scalar_result_is_wrapped(keypair, token, transport):
    server = await create_server(graphql_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        result = await client.call_tool("delete_fhir_organization", {"id": "190001"})
    assert result.structured_content == {"result": True}


async def test_graphql_errors_become_tool_errors_with_extensions(keypair, token, transport):
    server = await create_server(graphql_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        with pytest.raises(ToolError, match=r"Organization/404 not found.*\"status\": 404"):
            await client.call_tool("get_fhir_organization", {"id": "404"})


async def test_schema_file_skips_introspection(keypair, token, transport, backend):
    settings = graphql_settings(keypair, schema_file="tests/fixtures/fhir_gql_introspection.json")
    await create_server(settings, transport=transport)
    assert backend.calls("/graphql") == []


# ── schema translation units ──────────────────────────────────────────────


def _index() -> SchemaIndex:
    payload = json.loads((FIXTURES / "fhir_gql_introspection.json").read_text())
    return SchemaIndex(payload["data"]["__schema"])


def test_input_objects_become_defs():
    index = _index()
    (field,) = [f for f in index.root_fields(index.mutation_type) if f["name"] == "createFhirOrganization"]
    schema = index.input_schema(field["args"])
    assert schema["properties"]["input"]["$ref"] == "#/$defs/FhirOrganizationInput"
    org = schema["$defs"]["FhirOrganizationInput"]
    assert org["properties"]["identifier"]["items"] == {"$ref": "#/$defs/FhirIdentifierInput"}
    assert "Default: null" not in json.dumps(schema)


def test_selection_set_respects_depth():
    index = _index()
    (field,) = [f for f in index.root_fields(index.query_type) if f["name"] == "getFhirOrganization"]
    shallow = index.selection_set(field["type"], depth=1)
    deep = index.selection_set(field["type"], depth=5)
    assert "telecom" not in shallow and "name" in shallow
    assert "telecom { system value" in deep
    assert deep.count("{") == deep.count("}")


def test_medplum_schema_builds_bounded_tools():
    """Medplum's schema (~30k types) gives every resource ~300 argument-taking
    reverse-reference fields and an all-resources `contained` union. Walking
    them once took >1.3 GB and never finished; selections must stay small."""
    import time

    import httpx2

    from app.core.config import GraphQLPluginConfig
    from app.plugins.graphql.provider import build_tools

    schema = json.loads((FIXTURES / "medplum_introspection.json").read_text())["data"]["__schema"]
    config = GraphQLPluginConfig(
        endpoint="http://medplum.test/fhir/R4/$graphql",
        include=["^(Organization|Location)(List|Create|Update|Patch|Delete)?$"],
        selection_depth=4,
    )
    started = time.perf_counter()
    tools = {t.name: t for t in build_tools(schema, config, httpx2.AsyncClient())}
    assert time.perf_counter() - started < 2
    assert set(tools) == {
        f"{rt}{op}" for rt in ("organization", "location")
        for op in ("", "_list", "_create", "_update", "_patch", "_delete")
    }
    doc = tools["organization"].document
    # Every element of the resource, none of the reverse-reference searches.
    assert all(f in doc for f in ("identifier {", "telecom {", "address {", "contact {", "partOf {", " alias "))
    assert "AccountList" not in doc and "Connection" not in doc
    assert "contained { __typename }" in doc  # all-resources union: type name only
    # Bounded: ~33 KB at depth 4 (Extension's value[x] variants dominate);
    # unbounded, this never finished.
    assert len(doc) < 60_000


def test_null_fields_are_pruned_from_results():
    from app.plugins.graphql.provider import prune_nulls

    raw = {"id": "1", "meta": {"versionId": None, "source": None}, "name": "x",
           "identifier": [{"system": None, "value": "v"}], "telecom": [], "contact": [{"name": None}]}
    assert prune_nulls(raw) == {"id": "1", "name": "x", "identifier": [{"value": "v"}]}
    assert prune_nulls(None) is None and prune_nulls(False) is False
