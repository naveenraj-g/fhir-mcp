"""
The rest plugin — tools generated from a backend's OpenAPI spec.

FastMCP's `OpenAPIProvider` does the heavy lifting: one tool per operation,
arguments from the operation's parameters + request body, the HTTP request
built and sent through our `httpx2.AsyncClient` (which carries the
connector's outbound auth). This plugin adds what a FHIR backend needs on top:

- config-driven include/exclude rules (`RouteMap`s), so only the configured
  resources become tools;
- argument names that LLM tool APIs accept — see app.plugins.rest.arguments;
- MCP tool annotations from the HTTP method (GET = read-only, DELETE =
  destructive), so clients can tell safe calls from dangerous ones.
"""

from app.plugins.rest.provider import build_rest_provider

__all__ = ["build_rest_provider"]
