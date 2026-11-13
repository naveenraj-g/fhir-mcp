"""graphql plugin with `engine: graphql_mcp` (the graphql-mcp library), end to
end over Streamable HTTP against fhir-gql's real schema.

graphql-mcp's documents carry no operationName, so the fake backend here
routes on the root field named in the query instead."""

import json

import httpx2
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.utilities.tests import asgi_client

from app.errors import DiscoveryError
from app.main import create_server
from tests.conftest import FIXTURES, ORG, FakeBackend, make_settings

GRAPHQL = {
    "engine": "graphql_mcp",
    "endpoint": "http://backend.test/graphql",
    "include": ["Organization"],
    "exclude": ["^getFhirOrganizations$"],
}


class LibraryBackend(FakeBackend):
    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/graphql":
            query = json.loads(request.content)["query"]
            if "IntrospectionQuery" not in query:
                self.requests.append(request)
                if self.reject_next_bearer and request.headers.get("authorization") == f"Bearer {self.reject_next_bearer}":
                    self.reject_next_bearer = None
                    return httpx2.Response(401, json={"error": "invalid_token"})
                if "getFhirOrganization(" in query:
                    variables = json.loads(request.content).get("variables", {})
                    if variables.get("id") == "404":
                        return httpx2.Response(200, json={"data": {"getFhirOrganization": None}, "errors": [
                            {"message": "Organization/404 not found", "path": ["getFhirOrganization"]}]})
                    return httpx2.Response(200, json={"data": {"getFhirOrganization": {**ORG, "alias": None}}})
                if "createFhirOrganization(" in query:
                    return httpx2.Response(422, json={"resourceType": "OperationOutcome", "issue": [
                        {"severity": "error", "code": "invalid", "diagnostics": "GSTIN required"}]})
        return super().handler(request)


@pytest.fixture
def backend() -> LibraryBackend:
    return LibraryBackend()


def settings(keypair, **graphql):
    return make_settings(keypair, plugin="graphql", graphql={**GRAPHQL, **graphql})


def last_call(backend):
    return [r for r in backend.calls("/graphql") if b"IntrospectionQuery" not in r.content][-1]


async def test_one_tool_per_selected_root_field_and_no_nested_tools(keypair, token, transport):
    server = await create_server(settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {
        "get_fhir_organization", "search_fhir_organizations",
        "create_fhir_organization", "update_fhir_organization", "delete_fhir_organization",
    }
    assert tools["get_fhir_organization"].annotations.read_only_hint is True
    assert tools["delete_fhir_organization"].annotations.read_only_hint is False
    assert tools["get_fhir_organization"].input_schema["required"] == ["id"]


async def test_mutations_can_be_turned_off(keypair, token, transport):
    server = await create_server(settings(keypair, include_mutations=False), transport=transport)
    async with asgi_client(server, auth=token) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"get_fhir_organization", "search_fhir_organizations"}


async def test_call_goes_through_connector_client_with_caller_token(keypair, token, transport, backend):
    server = await create_server(settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        result = await client.call_tool("get_fhir_organization", {"id": "190001"})
    assert json.loads(result.content[0].text) == ORG  # nulls pruned
    call = last_call(backend)
    body = json.loads(call.content)
    assert body["variables"] == {"id": "190001"}
    assert "getFhirOrganization(id: $id)" in body["query"]
    assert call.headers["authorization"] == f"Bearer {token}"


async def test_graphql_errors_and_backend_bodies_reach_the_client(keypair, token, transport):
    server = await create_server(settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        with pytest.raises(ToolError, match="Organization/404 not found"):
            await client.call_tool("get_fhir_organization", {"id": "404"})
        with pytest.raises(ToolError, match="GSTIN required"):
            await client.call_tool("create_fhir_organization", {"input": {"name": "x"}})


async def test_client_credentials_and_refetch_after_401(keypair, token, transport, backend):
    s = make_settings(
        keypair, active="medplum", plugin="graphql", auth_mode="oauth_client_credentials",
        token_url="http://backend.test/oauth2/token", client_id="svc", client_secret="s3cret",
        graphql=GRAPHQL,
    )
    server = await create_server(s, transport=transport)
    async with asgi_client(server, auth=token) as client:
        await client.call_tool("get_fhir_organization", {"id": "190001"})
        assert last_call(backend).headers["authorization"] == "Bearer svc-token-2"
        backend.reject_next_bearer = "svc-token-2"
        await client.call_tool("get_fhir_organization", {"id": "190001"})
    assert last_call(backend).headers["authorization"] == "Bearer svc-token-3"


async def test_trimmed_introspection_file_is_a_clear_startup_error(keypair, transport, tmp_path):
    """The native engine's trimmed introspection (our Medplum fixture) lacks
    `interfaces`; graphql-core can't build a schema from it."""
    with pytest.raises(DiscoveryError, match="full introspection"):
        await create_server(
            settings(keypair, schema_file=str(FIXTURES / "medplum_introspection.json"),
                     include=["^Organization$"], exclude=[]),
            transport=transport,
        )
