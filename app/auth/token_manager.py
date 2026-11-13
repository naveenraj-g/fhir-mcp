"""
OAuth2 client_credentials token acquisition and caching — port of fhir-gql's
app/connectors/medplum/token_manager.py onto httpx2.

The token is reused for its lifetime (Medplum issues `expires_in: 3600`) and
refetched only when missing, about to expire, or invalidated after a 401. An
asyncio.Lock stops concurrent tool calls from each fetching a fresh token the
moment the cached one expires.
"""

import asyncio
import time

import httpx2

from app.errors import ConfigError, UpstreamAuthError

# Refetch this long before the real expiry so an in-flight call doesn't race it.
_EXPIRY_SAFETY_MARGIN_SECONDS = 30


class ClientCredentialsTokenManager:
    def __init__(
        self,
        token_url: str | None,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if not token_url:
            raise ConfigError("oauth_client_credentials needs token_url")
        if not client_id or not client_secret:
            raise ConfigError(
                "oauth_client_credentials needs client_id and client_secret — set "
                "CONNECTORS__<NAME>__CLIENT_ID / CONNECTORS__<NAME>__CLIENT_SECRET "
                "(env or .env, never the committed YAML)"
            )
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        # Own client: the token endpoint lives outside the API base_url.
        self._http = httpx2.AsyncClient(timeout=10.0, transport=transport)
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            if self._access_token is None or time.monotonic() >= self._expires_at:
                await self._fetch()
            assert self._access_token is not None
            return self._access_token

    async def invalidate(self) -> None:
        async with self._lock:
            self._access_token = None

    async def _fetch(self) -> None:
        try:
            r = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            r.raise_for_status()
            body = r.json()
        except httpx2.HTTPError as e:
            # Our own misconfiguration or an outage, not the caller's fault —
            # say so plainly instead of surfacing a raw transport error.
            raise UpstreamAuthError(f"Could not obtain a backend access token: {e}") from e
        self._access_token = body["access_token"]
        self._expires_at = time.monotonic() + body.get("expires_in", 3600) - _EXPIRY_SAFETY_MARGIN_SECONDS

    async def aclose(self) -> None:
        await self._http.aclose()
