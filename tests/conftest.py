"""
Test harness: the real server (app.main.create_server) talking to a fake
backend through an httpx2.MockTransport, driven by a real FastMCP Client
over in-process Streamable HTTP (fastmcp.utilities.tests.asgi_client) — so
inbound auth, MCP sessions, headers and middleware all run as in production.

Fixtures are the *real* schemas, dumped from the sibling repos:
  tests/fixtures/fhir_server_openapi.json     fhir-server app.openapi()
  tests/fixtures/fhir_gql_introspection.json  fhir-gql schema.introspect()
Re-dump them when either API changes (see README).
"""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastmcp.server.auth.providers.jwt import RSAKeyPair

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from app.core.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
ISSUER = "http://iam.test"
ORG = {"resourceType": "Organization", "id": "190001", "name": "Meridian Health", "active": True}


class FakeBackend:
    """Stands in for fhir-server, fhir-gql and Medplum at once. Records every
    request; routes are deliberately small — just what the tests exercise."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.openapi = json.loads((FIXTURES / "fhir_server_openapi.json").read_text())
        self.introspection = json.loads((FIXTURES / "fhir_gql_introspection.json").read_text())
        self.token_calls = 0
        self.reject_next_bearer: str | None = None  # simulate a revoked token once

    def calls(self, path_suffix: str) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path.endswith(path_suffix)]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        auth = request.headers.get("authorization")

        if path == "/openapi.json":
            return httpx2.Response(200, json=self.openapi)
        if path == "/oauth2/token":
            self.token_calls += 1
            return httpx2.Response(200, json={"access_token": f"svc-token-{self.token_calls}", "expires_in": 3600})
        if self.reject_next_bearer and auth == f"Bearer {self.reject_next_bearer}":
            self.reject_next_bearer = None
            return httpx2.Response(401, json={"error": "invalid_token"})

        if path == "/graphql":
            body = json.loads(request.content)
            if "IntrospectionQuery" in body["query"]:
                return httpx2.Response(200, json=self.introspection)
            if body.get("operationName") == "GetFhirOrganization":
                if body["variables"]["id"] == "404":
                    return httpx2.Response(
                        200,
                        json={"data": None, "errors": [{"message": "Organization/404 not found",
                                                         "extensions": {"status": 404}}]},
                    )
                return httpx2.Response(200, json={"data": {"getFhirOrganization": ORG}})
            if body.get("operationName") == "SearchFhirOrganizations":
                return httpx2.Response(200, json={"data": {"searchFhirOrganizations": {"total": 0, "entry": []}}})
            if body.get("operationName") == "DeleteFhirOrganization":
                return httpx2.Response(200, json={"data": {"deleteFhirOrganization": True}})
            return httpx2.Response(400, json={"errors": [{"message": "unexpected operation"}]})

        if path == "/api/fhir/v1/organizations/190001" and method == "GET":
            return httpx2.Response(200, json=ORG, headers={"content-type": "application/fhir+json"})
        if path == "/api/fhir/v1/organizations/" and method == "GET":
            bundle = {"resourceType": "Bundle", "type": "searchset", "total": 1, "entry": [{"resource": ORG}]}
            return httpx2.Response(200, json=bundle)
        if path == "/api/fhir/v1/organizations/" and method == "POST":
            outcome = {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "invalid", "diagnostics": "Organization.name: bad"}],
            }
            return httpx2.Response(422, json=outcome)
        return httpx2.Response(404, json={"detail": f"no fake route for {method} {path}"})


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def transport(backend: FakeBackend) -> httpx2.MockTransport:
    return httpx2.MockTransport(backend.handler)


@pytest.fixture(scope="session")
def keypair() -> RSAKeyPair:
    return RSAKeyPair.generate()


@pytest.fixture
def token(keypair: RSAKeyPair) -> str:
    return keypair.create_token(
        subject="user-1",
        issuer=ISSUER,
        audience=ISSUER,
        additional_claims={"activeOrganizationId": "org-1"},
    )


class IsolatedSettings(Settings):
    """Settings from init kwargs only — no env, .env or committed YAML, which
    pydantic-settings would otherwise deep-merge into what a test passes."""

    @classmethod
    def settings_customise_sources(
        cls, settings_cls: type[BaseSettings], init_settings: PydanticBaseSettingsSource, **_: Any
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def make_settings(
    keypair: RSAKeyPair | None = None,
    *,
    active: str = "internal",
    plugin: str = "rest",
    auth_mode: str = "token_passthrough",
    **connector: Any,
) -> Settings:
    """Settings for one connector, isolated from the committed YAML/env."""
    auth = (
        {"enabled": True, "algorithm": "RS256", "issuer": ISSUER,
         "public_key": keypair.public_key}
        if keypair
        else {"enabled": False}
    )
    connector_cfg = {
        "plugin": plugin,
        "auth_mode": auth_mode,
        "rest": {
            "base_url": "http://backend.test",
            "openapi_url": "http://backend.test/openapi.json",
            "include": [{"pattern": "^/api/fhir/v1/organizations/"}],
            "exclude": [{"pattern": "/_search$"}],
        },
        "graphql": {
            "endpoint": "http://backend.test/graphql",
            "include": ["Organization"],
            "exclude": ["^getFhirOrganizations$"],
        },
        **connector,
    }
    return IsolatedSettings(
        auth=auth,
        connectors={"active": active, "internal": None, "medplum": None, active: connector_cfg},
    )
