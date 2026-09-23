"""Clerk JWT verification for the private Nanobot bootstrap endpoint."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from urllib.parse import quote

import httpx
import jwt
from jwt import InvalidTokenError, PyJWK


class ClerkAuthenticationError(Exception):
    """The supplied credential is missing or invalid."""


class ClerkAuthorizationError(Exception):
    """The credential is valid but belongs to another tenant."""


class ClerkUnavailableError(Exception):
    """The configured Clerk verifier cannot currently validate credentials."""


class ClerkTokenVerifier:
    """Validate Clerk session JWTs against a configured issuer and JWKS."""

    _CACHE_TTL_S = 300.0
    _MAX_JWKS_KEYS = 32
    _USERS_URL = "https://api.clerk.com/v1/users"

    def __init__(
        self,
        *,
        issuer: str,
        jwks_url: str,
        audience: str = "",
        allowed_emails: list[str] | None = None,
        authorized_parties: list[str] | None = None,
        secret_key: str | None = None,
    ) -> None:
        self.issuer = issuer.strip()
        self.jwks_url = jwks_url.strip()
        self.audience = audience.strip()
        self.allowed_emails = {
            email.strip().casefold()
            for email in (allowed_emails or [])
            if email.strip()
        }
        self.authorized_parties = {
            party.strip() for party in (authorized_parties or []) if party.strip()
        }
        self.secret_key = (
            secret_key if secret_key is not None else os.environ.get("CLERK_SECRET_KEY", "")
        ).strip()
        self._keys: dict[str, PyJWK] = {}
        self._keys_expires_at = 0.0
        self._refresh_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """Whether any external identity-auth setting is present."""
        return bool(
            self.issuer
            or self.jwks_url
            or self.audience
            or self.allowed_emails
            or self.authorized_parties
        )

    @property
    def configured(self) -> bool:
        return bool(
            self.issuer
            and self.jwks_url
            and self.allowed_emails
            and self.authorized_parties
        )

    async def verify(self, token: str) -> dict[str, Any]:
        if not token:
            raise ClerkAuthenticationError("missing bearer token")
        if not self.configured:
            raise ClerkUnavailableError("Clerk verification is not configured")

        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            raise ClerkAuthenticationError("invalid bearer token") from exc
        if header.get("alg") != "RS256":
            raise ClerkAuthenticationError("unsupported JWT algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise ClerkAuthenticationError("JWT key id is missing")

        key = await self._signing_key(kid)
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience or None,
                leeway=5,
                options={
                    "require": ["exp", "iat", "iss", "sub"],
                    "verify_aud": bool(self.audience),
                },
            )
        except InvalidTokenError as exc:
            raise ClerkAuthenticationError("invalid bearer token") from exc

        if "azp" in claims:
            authorized_party = claims["azp"]
            if (
                not isinstance(authorized_party, str)
                or authorized_party not in self.authorized_parties
            ):
                raise ClerkAuthorizationError("authorized party is not allowed")

        email = self._claim_email(claims)
        if email is None:
            email = await self._fetch_primary_email(str(claims["sub"]))
        if email.casefold() not in self.allowed_emails:
            raise ClerkAuthorizationError("email is not allowed")
        return claims

    async def _signing_key(self, kid: str) -> PyJWK:
        now = time.monotonic()
        key = self._keys.get(kid)
        if key is not None and now < self._keys_expires_at:
            return key
        await self._refresh_keys(force=key is None)
        key = self._keys.get(kid)
        if key is None:
            raise ClerkAuthenticationError("JWT signing key is unknown")
        return key

    async def _refresh_keys(self, *, force: bool) -> None:
        async with self._refresh_lock:
            now = time.monotonic()
            if not force and self._keys and now < self._keys_expires_at:
                return
            data = await self._fetch_jwks()
            rows = data.get("keys") if isinstance(data, dict) else None
            if not isinstance(rows, list) or not rows:
                raise ClerkUnavailableError("Clerk JWKS did not contain keys")
            keys: dict[str, PyJWK] = {}
            try:
                for row in rows[: self._MAX_JWKS_KEYS]:
                    if not isinstance(row, dict):
                        continue
                    kid = row.get("kid")
                    if isinstance(kid, str) and kid:
                        keys[kid] = PyJWK.from_dict(row, algorithm="RS256")
            except (InvalidTokenError, ValueError, TypeError) as exc:
                raise ClerkUnavailableError("Clerk JWKS contained an invalid key") from exc
            if not keys:
                raise ClerkUnavailableError("Clerk JWKS did not contain usable keys")
            self._keys = keys
            self._keys_expires_at = now + self._CACHE_TTL_S

    async def _fetch_jwks(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self.jwks_url)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ClerkUnavailableError("Clerk JWKS is unavailable") from exc
        if not isinstance(data, dict):
            raise ClerkUnavailableError("Clerk JWKS response is invalid")
        return data

    async def _fetch_primary_email(self, user_id: str) -> str:
        if not self.secret_key:
            raise ClerkUnavailableError(
                "CLERK_SECRET_KEY is required when the session token omits email"
            )
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{self._USERS_URL}/{quote(user_id, safe='')}",
                    headers={"Authorization": f"Bearer {self.secret_key}"},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ClerkAuthenticationError("Clerk user no longer exists") from exc
            raise ClerkUnavailableError("Clerk user lookup is unavailable") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ClerkUnavailableError("Clerk user lookup is unavailable") from exc
        if not isinstance(data, dict):
            raise ClerkUnavailableError("Clerk user response is invalid")
        primary_id = data.get("primary_email_address_id")
        addresses = data.get("email_addresses")
        if not isinstance(primary_id, str) or not isinstance(addresses, list):
            raise ClerkAuthorizationError("verified primary email is unavailable")
        for address in addresses:
            if not isinstance(address, dict) or address.get("id") != primary_id:
                continue
            email = address.get("email_address")
            verification = address.get("verification")
            if (
                isinstance(email, str)
                and email.strip()
                and isinstance(verification, dict)
                and verification.get("status") == "verified"
            ):
                return email.strip()
            break
        raise ClerkAuthorizationError("verified primary email is unavailable")

    @staticmethod
    def _claim_email(claims: dict[str, Any]) -> str | None:
        for name in ("email", "primary_email", "primaryEmail"):
            value = claims.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
