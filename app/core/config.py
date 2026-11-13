"""
Application configuration — same layered model as fhir-gql's and fhir-server's
app/core/config.py. Precedence, highest first:

  1. Real env var (e.g. exported in the shell, or set by an orchestrator).
  2. `.env` — per-environment values and secrets only: where the IAM lives,
     a connector's client_id/client_secret, a discovery token.
  3. `configs/config.yaml` + `configs/connectors/<name>.yaml` — checked-in,
     non-secret application behavior: which connector is active, which
     plugin it uses, which backend operations become tools, logging.
  4. Field defaults (lowest) — defined on the models below.

Any nested field can be overridden by an env var using `__` as the nesting
delimiter, without touching the committed YAML — e.g.
`CONNECTORS__ACTIVE=medplum`, `CONNECTORS__INTERNAL__PLUGIN=graphql`,
`CONNECTORS__MEDPLUM__CLIENT_SECRET=...`.

The vocabulary mirrors fhir-gql on purpose:

- A **connector** is a backend system (`internal` = our own fhir-server
  stack, `medplum` = a Medplum instance). It decides *where* calls go and
  *how they authenticate* (`auth_mode`).
- A **plugin** is how a connector's tools are generated: `rest` reads the
  backend's OpenAPI spec, `graphql` introspects a GraphQL schema. The same
  plugin code serves every connector — the connector only supplies URLs,
  filters and auth.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# Repo root, so configs/ resolves the same no matter which directory the
# process was started from.
ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT_DIR / "configs"


class AppConfig(BaseModel):
    """Identity the MCP server reports to clients during initialization."""

    name: str = "FHIR MCP"
    version: str = "0.1.0"
    # Sent to MCP clients as the server's `instructions`; LLM hosts typically
    # put it in the model's context, so it's the place to explain conventions
    # shared by every tool (FHIR R4 shapes, search syntax).
    instructions: str | None = None


class ServerConfig(BaseModel):
    """How the MCP endpoint itself is served."""

    # "http" = Streamable HTTP (the transport remote MCP clients use, and the
    # only one where an inbound bearer token exists). "stdio" is for local
    # desktop clients; there is no caller token on it, so a token_passthrough
    # connector can't authenticate — see app.auth.outbound.
    transport: Literal["http", "stdio"] = "http"
    host: str = "127.0.0.1"
    port: int = 8010
    path: str = "/mcp"


class AuthConfig(BaseModel):
    """Inbound auth: how this MCP server verifies the bearer token a client
    sends. Same IAM (BetterAuth) as fhir-server and fhir-gql, so a token
    accepted here is also accepted by those backends — which is what makes
    token_passthrough possible.

    The IAM sets `aud == iss`, so `audience` defaults to the issuer (same
    rule fhir-server's app/auth/dependencies.py applies)."""

    enabled: bool = True
    # Single algorithm — FastMCP's JWTVerifier accepts one. BetterAuth signs
    # with EdDSA by default.
    algorithm: str = "EdDSA"
    jwks_url: str = "http://localhost:5001/api/auth/jwks"
    issuer: str = "http://localhost:5001"
    audience: str | None = None
    # PEM public key to verify against instead of the JWKS — for tests and
    # offline development against a locally minted token. Never needed in a
    # real deployment (JWKS handles key rotation).
    public_key: str | None = None


class RouteFilter(BaseModel):
    """One include/exclude rule for the rest plugin — maps directly onto a
    FastMCP `RouteMap`. Every criterion that is set must match."""

    methods: list[str] | Literal["*"] = "*"
    pattern: str = r".*"
    tags: list[str] = Field(default_factory=list)


class RestPluginConfig(BaseModel):
    """Tools generated from the backend's OpenAPI spec (FastMCP's
    OpenAPIProvider). One tool per included operation; its name is the
    operation's operationId, its arguments are the operation's parameters
    and request body."""

    # Base URL the generated tools call. Spec paths are appended to it, so it
    # is the server root when the spec's paths are absolute
    # (/api/fhir/v1/organizations, /fhir/R4/Organization).
    base_url: str
    # Where to load the spec from — `openapi_file` (a path relative to the
    # repo root) wins when set, for backends whose live spec is unreachable
    # or needs curating.
    openapi_url: str | None = None
    openapi_file: str | None = None
    # For a generic spec that describes every resource through one
    # `{resourceType}` path (Medplum's), the resource types to expand it
    # into — one concrete set of operations per type. Empty = use the spec's
    # paths as they are (fhir-server's spec is already per-resource). See
    # app.plugins.rest.expand.
    resource_types: list[str] = Field(default_factory=list)
    # Allow-list: only operations matching at least one rule become tools.
    # Empty = every operation in the spec.
    include: list[RouteFilter] = Field(default_factory=list)
    # Checked before `include`; a match is never exposed.
    exclude: list[RouteFilter] = Field(default_factory=list)
    # Validate responses against the spec's declared response schema. Off by
    # default: FHIR resources carry elements a spec's response model may not
    # enumerate, and a stricter-than-reality output schema fails good calls.
    validate_output: bool = False

    @model_validator(mode="after")
    def _one_source(self) -> RestPluginConfig:
        if not (self.openapi_url or self.openapi_file):
            raise ValueError("rest plugin needs openapi_url or openapi_file")
        return self


class GraphQLPluginConfig(BaseModel):
    """Tools generated by introspecting a GraphQL schema — one tool per
    included Query/Mutation field."""

    endpoint: str
    # Which code turns the schema into tools:
    #   native       our own generator (app.plugins.graphql)
    #   graphql_mcp  the graphql-mcp library (app.plugins.graphql_mcp);
    #                selects results only 2 levels deep, ignores selection_depth
    engine: Literal["native", "graphql_mcp"] = "native"
    # Load the schema from an introspection-result JSON file instead of
    # querying the endpoint at startup (relative to the repo root).
    schema_file: str | None = None
    # Regexes matched against the root field name (e.g. getFhirOrganization).
    # Empty include = every field.
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    include_mutations: bool = True
    # How deep the generated selection set walks object fields. FHIR
    # resources nest about four levels (Organization.contact.telecom.period),
    # so 5 returns whole resources without unbounded recursion.
    selection_depth: int = Field(default=5, ge=1, le=10)


class ConnectorConfig(BaseModel):
    """One backend system. See the module docstring for connector vs. plugin."""

    plugin: Literal["rest", "graphql"] = "rest"
    # Outbound auth — how this server authenticates to the backend:
    #   token_passthrough        forward the MCP caller's own bearer token
    #                            (backend trusts the same IAM).
    #   oauth_client_credentials this server authenticates as itself with
    #                            client_id/client_secret (backend has its own
    #                            authorization server — Medplum, Epic, ...).
    #   none                     no Authorization header at all.
    auth_mode: Literal["token_passthrough", "oauth_client_credentials", "none"] = (
        "token_passthrough"
    )
    # oauth_client_credentials only. Secrets — env vars, never YAML.
    token_url: str | None = None
    client_id: str = ""
    client_secret: str = ""
    # Bearer token used only to *discover* tools at startup (fetch the
    # OpenAPI spec / run GraphQL introspection) when there is no caller yet.
    # Needed for token_passthrough backends whose schema endpoint requires
    # auth (fhir-gql's /graphql does). Secret — env var only.
    discovery_token: str = ""
    timeout_seconds: float = 30.0
    # Prefix added to every tool name from this connector ("" = none).
    tool_prefix: str = ""

    rest: RestPluginConfig | None = None
    graphql: GraphQLPluginConfig | None = None

    @model_validator(mode="after")
    def _plugin_configured(self) -> ConnectorConfig:
        if getattr(self, self.plugin) is None:
            raise ValueError(f"plugin is {self.plugin!r} but no `{self.plugin}:` block is configured")
        return self


class ConnectorsConfig(BaseModel):
    """Which connector is live, plus one sub-config per known connector
    (each from its own configs/connectors/<name>.yaml). Adding a connector:
    add a field here, its YAML file to _CONNECTOR_FILES below, and extend the
    `active` Literal."""

    active: Literal["universal", "internal", "medplum"] = "universal"
    universal: ConnectorConfig | None = None
    internal: ConnectorConfig | None = None
    medplum: ConnectorConfig | None = None

    def active_config(self) -> ConnectorConfig:
        cfg = getattr(self, self.active)
        if cfg is None:
            raise ValueError(
                f"connectors.active is {self.active!r} but configs/connectors/{self.active}.yaml "
                "defines no settings for it"
            )
        return cfg


class LogConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # "json" (one object per line, for aggregators) or "console" (local dev).
    format: Literal["json", "console"] = "json"
    # ⚠️ PHI — local development only. Logs tool-call arguments at DEBUG
    # (patient names, identifiers). `redact` masks obvious secrets only.
    debug_payloads: bool = False
    redact: list[str] = Field(
        default_factory=lambda: ["authorization", "token", "password", "secret"]
    )

    # Hand-edited values like `level: debug` / `format: JSON` shouldn't crash
    # the app at import time.
    @field_validator("level", "format", mode="before")
    @classmethod
    def _normalise_case(cls, v):
        if isinstance(v, str):
            return v.upper() if v.upper() in _LOG_LEVELS else v.lower()
        return v


class Settings(BaseSettings):
    app: AppConfig = Field(default_factory=AppConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    connectors: ConnectorsConfig = Field(default_factory=ConnectorsConfig)
    logging: LogConfig = Field(default_factory=LogConfig)

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """real env > .env > configs/config.yaml + configs/connectors/*.yaml
        > defaults. The YAML files share one precedence tier and never define
        the same leaf key; pydantic-settings deep-merges their nested
        `connectors.*` dicts rather than letting one clobber another."""
        yaml_sources = [
            YamlConfigSettingsSource(settings_cls, yaml_file=path, yaml_file_encoding="utf-8")
            for path in [CONFIG_DIR / "config.yaml", *_CONNECTOR_FILES]
        ]
        return (init_settings, env_settings, dotenv_settings, *yaml_sources, file_secret_settings)


_CONNECTOR_FILES = [CONFIG_DIR / "connectors" / f"{name}.yaml" for name in ("universal", "internal", "medplum")]

settings = Settings()
