"""Ziggy-local (MIT-1405): OAuth 2.0 client-credentials auth for HTTP MCP servers.

Ported from the production fork (``feat/shared-rooms``). The tenant connector
(ziggy-connectors) mints a short-lived bearer token at its token endpoint for a
runtime client that authenticates with HTTP Basic (``client_secret_basic``) and
names the MCP URL as the RFC 8707 ``resource``.

The client secret is read from a file at token-fetch time so a rotated systemd
credential is picked up without a restart, and it is never logged or included
in an exception message.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from loguru import logger

from nanobot.config.loader import resolve_env_refs

if TYPE_CHECKING:
    from nanobot.config.schema import OAuthClientCredentialsConfig

# Refresh this long before the server-declared expiry so an in-flight request
# never carries a token that expires on the wire.
REFRESH_MARGIN_SECONDS = 60.0
_DEFAULT_EXPIRES_IN_SECONDS = 300.0
_TOKEN_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _read_client_secret(configured_path: str) -> str:
    """Resolve ``${VAR}`` in the path and read the secret; never echo the secret."""
    path = resolve_env_refs(configured_path).strip()
    if not path:
        raise ValueError(
            "oauthClientCredentials.clientSecretFile is empty or references an unset "
            "environment variable"
        )
    try:
        secret = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        # The error names the path only (it is config, not a secret).
        raise ValueError(
            f"cannot read oauthClientCredentials.clientSecretFile {path!r}: "
            f"{exc.strerror or type(exc).__name__}"
        ) from None
    if not secret:
        raise ValueError(f"oauthClientCredentials.clientSecretFile {path!r} is empty")
    return secret


class OAuthClientCredentialsError(RuntimeError):
    """The token endpoint did not return a usable access token."""


class OAuthClientCredentialsAuth(httpx.Auth):
    """``httpx.Auth`` that attaches a cached client-credentials bearer token.

    One token fetch is serialized behind a lock; the token is reused until
    ``expires_in`` minus :data:`REFRESH_MARGIN_SECONDS`. A 401 from the MCP
    server discards the rejected token and retries the request exactly once.
    """

    requires_request_body = True

    def __init__(
        self,
        config: OAuthClientCredentialsConfig,
        server_url: str,
        *,
        token_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._token_url = config.token_url
        self._client_id = config.client_id
        self._client_secret_file = config.client_secret_file
        self._scope = " ".join(scope for scope in config.scopes if scope)
        self._resource = config.resource or server_url
        self._token_client_factory = token_client_factory or (
            lambda: httpx.AsyncClient(timeout=_TOKEN_TIMEOUT)
        )
        self._access_token: str | None = None
        self._valid_until = 0.0
        self._lock = asyncio.Lock()

    async def _token(self, *, rejected: str | None = None) -> str:
        async with self._lock:
            # A concurrent request may already have replaced a rejected token.
            if (
                self._access_token is not None
                and self._access_token != rejected
                and time.monotonic() < self._valid_until
            ):
                return self._access_token
            self._access_token = None
            self._valid_until = 0.0
            token, expires_in = await self._fetch_token()
            self._access_token = token
            self._valid_until = time.monotonic() + max(0.0, expires_in - REFRESH_MARGIN_SECONDS)
            logger.debug(
                "MCP client-credentials token issued for client {} (expires in {}s)",
                self._client_id,
                int(expires_in),
            )
            return token

    async def _fetch_token(self) -> tuple[str, float]:
        secret = _read_client_secret(self._client_secret_file)
        form = {"grant_type": "client_credentials", "resource": self._resource}
        if self._scope:
            form["scope"] = self._scope
        async with self._token_client_factory() as client:
            response = await client.post(
                self._token_url,
                data=form,
                auth=httpx.BasicAuth(self._client_id, secret),
                headers={"Accept": "application/json"},
            )
        if response.status_code != 200:
            # Do not surface the body: a misbehaving endpoint could echo input.
            raise OAuthClientCredentialsError(
                f"MCP token endpoint returned HTTP {response.status_code} "
                f"for client {self._client_id}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise OAuthClientCredentialsError("MCP token response was not JSON") from None
        if not isinstance(payload, dict):
            raise OAuthClientCredentialsError("MCP token response was not a JSON object")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise OAuthClientCredentialsError("MCP token response had no access_token")
        token_type = payload.get("token_type", "Bearer")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise OAuthClientCredentialsError("MCP token response was not a bearer token")
        try:
            expires_in = float(payload.get("expires_in", _DEFAULT_EXPIRES_IN_SECONDS))
        except (TypeError, ValueError):
            raise OAuthClientCredentialsError(
                "MCP token response had an invalid expires_in"
            ) from None
        return token, expires_in

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self._token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code != 401:
            return
        logger.info("MCP server rejected the client-credentials token; refreshing once")
        token = await self._token(rejected=token)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request
