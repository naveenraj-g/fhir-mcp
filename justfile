dev:
    uv run python -m app

test:
    uv run pytest -q

# Re-dump the real schemas the tests run against, from the sibling repos.
fixtures:
    cd ../fhir-server && uv run python -c "import json; from fastapi.testclient import TestClient; from app.main import app; c = TestClient(app).__enter__(); json.dump(c.get('/openapi.json').json(), open('../fhir-mcp/tests/fixtures/fhir_server_openapi.json', 'w'))"
    cd ../fhir-gql && uv run python -c "import json; from app.gql.schema import schema; json.dump({'data': schema.introspect()}, open('../fhir-mcp/tests/fixtures/fhir_gql_introspection.json', 'w'))"
