# fhir-mcp

A config-driven MCP server (FastMCP 4) that exposes FHIR backends as MCP tools.
Tools are never hand-written: they are generated at startup from the backend's
own self-description, either its OpenAPI spec or its GraphQL schema. Adding an
endpoint to the backend adds a tool here, with no code change.

```
MCP client ──Bearer JWT──► fhir-mcp ──► connector (internal | medplum)
                              │            └─ plugin (rest | graphql) → tools
                              └─ verifies the JWT against the IAM (JWKS)
```

## Connectors and plugins

The vocabulary matches fhir-gql. A **connector** is a backend system: where
calls go and how they authenticate. A **plugin** is how that connector's
tools are generated. The plugin code is shared by every connector.

| connector | plugin `rest` | plugin `graphql` | outbound auth |
|---|---|---|---|
| **`universal`** (default) | — (if fhir-gql gains a REST API) | fhir-gql `/graphql` | `token_passthrough` |
| `internal` | fhir-server `/openapi.json` | fhir-gql `/graphql` (introspection) | `token_passthrough`: the caller's own JWT |
| `medplum` | Medplum `/openapi.json` | Medplum `$graphql` | `oauth_client_credentials`: this server's own Medplum client |

**`universal` is the intended path:** fhir-mcp talks only to fhir-gql, our
universal FHIR gateway, and fhir-gql reaches whichever EMR/EHR its own
`connectors.active` names (fhir-server, Medplum, ...). Changing EMR is a
fhir-gql config change; the MCP tools stay the same. `internal` and `medplum`
talk to a backend directly and stay available.

```
MCP client ──► fhir-mcp (universal) ──► fhir-gql ──► fhir-server | Medplum | ...
```

Pick the connector with `connectors.active` in `configs/config.yaml`. Each
connector's own settings live in `configs/connectors/<name>.yaml`: its
plugin, URLs, and the include/exclude rules that decide which operations
become tools. Today the rules allow Organization and Location only.

```bash
CONNECTORS__INTERNAL__PLUGIN=graphql uv run python -m app   # switch plugin without editing YAML
```

## Tokens

- **Inbound.** The MCP client sends `Authorization: Bearer <jwt>`. FastMCP's
  `JWTVerifier` checks it against the same IAM that fhir-server and fhir-gql
  trust (signature via JWKS, plus `iss`, `aud == iss` and `exp`). A bad or
  missing token gets a 401 before any MCP message is handled.
- **Outbound, `token_passthrough`.** The verified token is forwarded to the
  backend, so fhir-server and fhir-gql apply their own RBAC and org scoping to
  the real user. FastMCP never forwards `Authorization` on its own;
  `app/auth/outbound.py` does it explicitly, through an `httpx2.Auth` on the
  connector's client.
- **Outbound, `oauth_client_credentials`.** For a backend with its own OAuth
  server (Medplum), this server authenticates as itself. The token is cached,
  refreshed before it expires, and fetched again once after a 401.
- **Discovery.** At startup there is no caller yet. fhir-server's
  `/openapi.json` is public. fhir-gql's `/graphql` requires a token even for
  introspection, so set `CONNECTORS__UNIVERSAL__DISCOVERY_TOKEN` (or `__INTERNAL__` for the
  internal connector's graphql plugin) or
  `graphql.schema_file`.

## Tool shapes

- **rest.** One tool per operation, named by `operationId`
  (`search_organizations`, `create_location`, ...). FHIR search modifiers live
  in parameter names (`name:exact`), which LLM tool APIs reject. Clients
  therefore see `name__exact`, and it is mapped back before the request is
  sent (`app/plugins/rest/arguments.py`).
- **rest, generic specs.** Medplum's spec has no per-resource paths, only
  `/fhir/R4/{resourceType}/...`. `rest.resource_types` expands it into one
  set of operations per type (`read_organization`, `search_location`, ...),
  with request bodies typed as the concrete resource
  (`app/plugins/rest/expand.py`).
- **graphql.** One tool per Query/Mutation root field, in snake_case
  (`get_fhir_organization`, Medplum's `organization_list`). The selection set
  is generated from the schema, down to `selection_depth`. It selects the
  resource's own elements but skips reverse-reference searches (Medplum's
  `AccountList(...)` on every resource) and expands large unions only to
  `__typename`. Nulls are stripped from results, as FHIR JSON requires.
- **graphql, `engine: graphql_mcp`.** The same plugin can hand tool
  generation to the [graphql-mcp](https://graphql-mcp.com) library instead
  (`CONNECTORS__<NAME>__GRAPHQL__ENGINE=graphql_mcp`). Same tool names, same
  include/exclude, same outbound auth (calls still go through the connector's
  client). Its limits: results are selected only 2 levels deep, so
  `identifier.type`, `type.coding` and `contact` are not returned; on Medplum
  it uses ~1.9 GB vs ~0.3 GB for `native`. `native` stays the default
  (`app/plugins/graphql_mcp/provider.py` explains the workarounds).
- **Annotations.** Reads are marked `read_only`. Deletes are marked
  `destructive`.
- **Errors.** A backend 4xx/5xx reaches the client as a tool error carrying
  the backend's own body, such as a FHIR OperationOutcome, so the model can
  fix its call. Unexpected exceptions are masked (`app/errors/upstream.py`).

## Layout

```
app/
  __main__.py        entry point: uv run python -m app
  main.py            create_server(): connector + auth + middleware
  core/config.py     Settings: env > .env > configs/*.yaml > defaults
  core/logging.py    JSON/console logging, request_id + actor_* correlation
  errors/            startup vs tool-call errors; upstream error mapping
  auth/              inbound JWT verifier; outbound passthrough / client credentials
  connectors/        builds the active connector (client + plugin provider)
  plugins/rest/      OpenAPI → FastMCP OpenAPIProvider (+ filters, renames, annotations)
  plugins/graphql/   introspection → custom Provider of GraphQLTools
  plugins/graphql_mcp/  graphql engine `graphql_mcp`: the graphql-mcp library generates the tools
  middleware/        request context, one log line per tool call
configs/config.yaml, configs/connectors/{universal,internal,medplum}.yaml
```

## Run / test

```bash
uv sync
cp .env.example .env    # fill in what your connector needs
just dev                # http://127.0.0.1:8010/mcp, health at /health
just test               # in-process Streamable HTTP against a fake backend
just fixtures           # re-dump the real fhir-server/fhir-gql schemas the tests use

# Live: create → read → search → patch → delete one Organization through MCP
FHIR_MCP_TOKEN=<IAM JWT> uv run python scripts/live_smoke.py internal-rest
#   flavors: universal | internal-rest | internal-graphql | medplum-rest | medplum-graphql
```

The Medplum fixtures (`tests/fixtures/medplum_*.json`) were captured from a
live Medplum instance. Re-capture them after a Medplum upgrade.

`server.transport: stdio` is available for local desktop clients. stdio
carries no caller token, so a `token_passthrough` connector can't
authenticate over it.
