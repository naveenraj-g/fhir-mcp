"""
Inbound auth — verifying the bearer token an MCP client sends.

FastMCP's `JWTVerifier` checks signature (against the IAM's JWKS, with key
rotation and caching), `iss`, `aud` and `exp` on every HTTP request, before
any MCP message is handled; a bad or missing token is a 401 at the transport
layer. The verified token is then available to tool code through
`fastmcp.server.dependencies.get_access_token()` — app.auth.outbound reads
it there to forward it.
"""

from fastmcp.server.auth.providers.jwt import JWTVerifier

from app.core.config import AuthConfig


def build_token_verifier(config: AuthConfig) -> JWTVerifier | None:
    if not config.enabled:
        return None
    key = {"public_key": config.public_key} if config.public_key else {"jwks_uri": config.jwks_url}
    return JWTVerifier(
        **key,
        issuer=config.issuer,
        audience=config.audience or config.issuer,
        algorithm=config.algorithm,
    )
