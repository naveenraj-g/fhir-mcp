"""rest plugin + token passthrough, end to end over Streamable HTTP."""

import httpx2
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.utilities.tests import asgi_client, asgi_server

from app.main import create_server
from tests.conftest import ORG, make_settings


async def test_tools_are_generated_from_openapi_and_filtered(keypair, token, transport):
    server = await create_server(make_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        names = {t.name for t in await client.list_tools()}
    # Only Organization operations (include rule), no POST /_search (exclude rule).
    assert names == {
        "search_organizations", "create_organization", "get_organization_by_id",
        "patch_organization", "delete_organization",
    }


async def test_annotations_follow_http_method(keypair, token, transport):
    server = await create_server(make_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools["get_organization_by_id"].annotations.read_only_hint is True
    assert tools["delete_organization"].annotations.destructive_hint is True
    assert tools["create_organization"].annotations.read_only_hint is False


async def test_caller_token_is_forwarded_to_backend(keypair, token, transport, backend):
    server = await create_server(make_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        result = await client.call_tool("get_organization_by_id", {"organization_id": 190001})
    assert result.structured_content["name"] == ORG["name"]
    (call,) = backend.calls("/organizations/190001")
    assert call.headers["authorization"] == f"Bearer {token}"


async def test_search_modifier_arguments_are_renamed_and_mapped_back(keypair, token, transport, backend):
    server = await create_server(make_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        tools = {t.name: t for t in await client.list_tools()}
        props = tools["search_organizations"].input_schema["properties"]
        # LLM APIs reject ':' in property names — clients see `__` instead.
        assert "name__exact" in props and "name:exact" not in props
        assert all(":" not in p for p in props)
        await client.call_tool("search_organizations", {"name__exact": "Meridian Health", "_count": 5})
    (call,) = backend.calls("/api/fhir/v1/organizations/")
    # The backend still receives the real FHIR parameter name.
    assert call.url.params["name:exact"] == "Meridian Health"
    assert call.url.params["_count"] == "5"


async def test_backend_error_reaches_the_client_with_operation_outcome(keypair, token, transport):
    server = await create_server(make_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        with pytest.raises(ToolError) as e:
            await client.call_tool("create_organization", {"name": "x"})
    assert "422" in str(e.value)
    assert "Organization.name: bad" in str(e.value)


async def test_missing_caller_token_is_a_clear_tool_error(transport):
    # Inbound auth off (local dev) and the client sends no token: the tool
    # can't forward one, and says so instead of calling the backend.
    server = await create_server(make_settings(None), transport=transport)
    async with asgi_client(server) as client:
        with pytest.raises(ToolError, match="Authorization: Bearer"):
            await client.call_tool("get_organization_by_id", {"organization_id": 190001})


async def test_raw_token_is_forwarded_when_inbound_auth_is_off(transport, backend):
    server = await create_server(make_settings(None), transport=transport)
    async with asgi_client(server, auth="dev-token") as client:
        await client.call_tool("get_organization_by_id", {"organization_id": 190001})
    (call,) = backend.calls("/organizations/190001")
    assert call.headers["authorization"] == "Bearer dev-token"


@pytest.mark.parametrize("bad_token", [None, "not-a-jwt", "foreign-issuer"])
async def test_inbound_auth_rejects_bad_tokens(keypair, transport, bad_token):
    if bad_token == "foreign-issuer":
        bad_token = keypair.create_token(issuer="http://evil.test", audience="http://evil.test")
    server = await create_server(make_settings(keypair), transport=transport)
    async with asgi_server(server) as running:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(running.app)) as http:
            headers = {"accept": "application/json, text/event-stream"}
            if bad_token:
                headers["authorization"] = f"Bearer {bad_token}"
            r = await http.post(running.url, headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
            })
    assert r.status_code == 401


async def test_generic_medplum_spec_expands_into_per_resource_tools():
    """Medplum's real spec only has /fhir/R4/{resourceType}/... paths."""
    import json

    from app.core.config import RestPluginConfig
    from app.plugins.rest import build_rest_provider
    from tests.conftest import FIXTURES

    config = RestPluginConfig(
        base_url="http://medplum.test",
        openapi_file="tests/fixtures/medplum_openapi.json",
        resource_types=["Organization", "Location"],
        include=[{"pattern": "^/fhir/R4/Organization(/|$)"}, {"pattern": "^/fhir/R4/Location(/|$)"}],
        exclude=[{"pattern": "/_history"}, {"pattern": r"/\$validate$"},
                 {"pattern": "^/fhir/R4/[A-Za-z]+$", "methods": ["PUT"]}],
    )
    async with httpx2.AsyncClient(base_url=config.base_url) as client:
        provider = await build_rest_provider(config, client=client, discovery=client)
        tools = {t.name: t for t in await provider.list_tools()}
    assert set(tools) == {
        f"{op}_{rt}" for op in ("search", "create", "read", "update", "delete", "patch")
        for rt in ("organization", "location")
    }
    # Results are the resource itself, not wrapped in {"result": ...}.
    assert not tools["read_organization"].output_schema.get("x-fastmcp-wrap-result")
    # No free-text resourceType argument, and the body is the concrete type.
    assert "resourceType" not in tools["read_organization"].parameters["properties"]
    spec = json.loads((FIXTURES / "medplum_openapi.json").read_text())
    org_props = set(spec["components"]["schemas"]["Organization"]["properties"])
    assert {"name", "alias", "partOf"} <= set(tools["create_organization"].parameters["properties"]) & org_props
