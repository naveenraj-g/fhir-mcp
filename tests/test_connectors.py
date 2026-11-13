"""Connector selection, client-credentials auth (Medplum-style), config validation."""

import pytest
from fastmcp.utilities.tests import asgi_client
from pydantic import ValidationError

from app.errors import ConfigError, DiscoveryError
from app.main import create_server
from tests.conftest import make_settings


def medplum_settings(keypair, **overrides):
    connector = {
        "token_url": "http://backend.test/oauth2/token",
        "client_id": "svc",
        "client_secret": "s3cret",
        **overrides,
    }
    return make_settings(keypair, active="medplum", auth_mode="oauth_client_credentials", **connector)


async def test_client_credentials_replace_the_caller_token(keypair, token, transport, backend):
    server = await create_server(medplum_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        await client.call_tool("get_organization_by_id", {"organization_id": 190001})
        await client.call_tool("get_organization_by_id", {"organization_id": 190001})
    calls = backend.calls("/organizations/190001")
    # The backend sees this server's own token, never the caller's...
    assert all(c.headers["authorization"].startswith("Bearer svc-token-") for c in calls)
    # ...and the runtime token is cached across calls (one fetch for discovery
    # with its own short-lived manager, one for runtime).
    assert {c.headers["authorization"] for c in calls} == {"Bearer svc-token-2"}
    assert backend.token_calls == 2


async def test_client_credentials_refetch_once_after_401(keypair, token, transport, backend):
    server = await create_server(medplum_settings(keypair), transport=transport)
    async with asgi_client(server, auth=token) as client:
        await client.call_tool("get_organization_by_id", {"organization_id": 190001})
        backend.reject_next_bearer = "svc-token-2"  # revoke the cached token
        result = await client.call_tool("get_organization_by_id", {"organization_id": 190001})
    assert result.structured_content["id"] == "190001"
    assert backend.calls("/organizations/190001")[-1].headers["authorization"] == "Bearer svc-token-3"


async def test_client_credentials_without_secret_fails_at_startup(keypair, transport):
    with pytest.raises(ConfigError, match="client_secret"):
        await create_server(medplum_settings(keypair, client_secret=""), transport=transport)


async def test_discovery_failure_is_a_startup_error(keypair, transport):
    settings = make_settings(keypair, rest={"base_url": "http://backend.test",
                                            "openapi_url": "http://backend.test/missing.json"})
    with pytest.raises(DiscoveryError, match="missing.json"):
        await create_server(settings, transport=transport)


async def test_tool_prefix_namespaces_every_tool(keypair, token, transport):
    server = await create_server(make_settings(keypair, tool_prefix="emr"), transport=transport)
    async with asgi_client(server, auth=token) as client:
        names = {t.name for t in await client.list_tools()}
        await client.call_tool("emr_get_organization_by_id", {"organization_id": 190001})
    assert names and all(n.startswith("emr_") for n in names)


def test_plugin_without_its_config_block_is_rejected():
    with pytest.raises(ValidationError, match="no `graphql:` block"):
        make_settings(None, plugin="graphql", graphql=None)


def test_active_connector_must_be_configured():
    settings = make_settings(None)
    settings.connectors.active = "medplum"
    with pytest.raises(ValueError, match="medplum"):
        settings.connectors.active_config()


def test_committed_yaml_loads():
    """configs/*.yaml must always parse into valid Settings."""
    from app.core.config import Settings

    s = Settings()
    assert s.connectors.active in ("universal", "internal", "medplum")
    assert s.connectors.universal and s.connectors.universal.plugin == "graphql"
    assert s.connectors.universal.auth_mode == "token_passthrough"
    assert s.connectors.internal and s.connectors.internal.rest and s.connectors.internal.graphql
    assert s.connectors.medplum and s.connectors.medplum.auth_mode == "oauth_client_credentials"
