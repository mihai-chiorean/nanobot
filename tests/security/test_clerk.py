"""Contract tests for Clerk-backed transport bootstrap authentication."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from nanobot.security.clerk import (
    ClerkAuthenticationError,
    ClerkAuthorizationError,
    ClerkTokenVerifier,
)

ISSUER = "https://clerk.example.test"
AUDIENCE = "ziggy-control"
AUTHORIZED_PARTY = "https://ziggy-control.example.test"


@pytest.fixture()
def signed_identity() -> tuple[object, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    return private_key, jwk


def _verifier(jwk: dict[str, object]) -> ClerkTokenVerifier:
    verifier = ClerkTokenVerifier(
        issuer=ISSUER,
        jwks_url=f"{ISSUER}/.well-known/jwks.json",
        audience=AUDIENCE,
        allowed_emails=["tenant@example.com"],
        authorized_parties=[AUTHORIZED_PARTY],
        secret_key="sk_test_backend",
    )
    verifier._fetch_jwks = AsyncMock(return_value={"keys": [jwk]})
    return verifier


def _token(private_key: object, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": "user_test",
        "iat": now,
        "exp": now + 300,
        "aud": AUDIENCE,
        "azp": AUTHORIZED_PARTY,
        "email": "TENANT@example.com",
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


@pytest.mark.asyncio
async def test_signed_token_accepts_configured_tenant(signed_identity) -> None:
    private_key, jwk = signed_identity

    claims = await _verifier(jwk).verify(_token(private_key))

    assert claims["sub"] == "user_test"


@pytest.mark.asyncio
async def test_missing_email_resolves_primary_email(signed_identity) -> None:
    private_key, jwk = signed_identity
    verifier = _verifier(jwk)
    verifier._fetch_primary_email = AsyncMock(return_value="tenant@example.com")

    await verifier.verify(_token(private_key, email=None))

    verifier._fetch_primary_email.assert_awaited_once_with("user_test")


@pytest.mark.asyncio
async def test_primary_email_lookup_uses_clerk_backend_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_from_env")
    verifier = ClerkTokenVerifier(
        issuer=ISSUER,
        jwks_url=f"{ISSUER}/.well-known/jwks.json",
        allowed_emails=["tenant@example.com"],
    )
    request_seen: dict[str, object] = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]):
            request_seen.update(url=url, headers=headers)
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "primary_email_address_id": "email_primary",
                    "email_addresses": [
                        {
                            "id": "email_other",
                            "email_address": "other@example.com",
                            "verification": {"status": "verified"},
                        },
                        {
                            "id": "email_primary",
                            "email_address": "tenant@example.com",
                            "verification": {"status": "verified"},
                        },
                    ],
                },
            )

    monkeypatch.setattr(
        "nanobot.security.clerk.httpx.AsyncClient", lambda **_kwargs: FakeClient()
    )

    assert await verifier._fetch_primary_email("user/with/slash") == "tenant@example.com"
    assert str(request_seen["url"]).endswith("/user%2Fwith%2Fslash")
    assert request_seen["headers"] == {"Authorization": "Bearer sk_test_from_env"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "other@example.com"},
        {"azp": "https://attacker.example"},
        {"azp": None},
    ],
)
async def test_forbidden_email_or_authorized_party(
    signed_identity, overrides: dict[str, object]
) -> None:
    private_key, jwk = signed_identity

    with pytest.raises(ClerkAuthorizationError):
        await _verifier(jwk).verify(_token(private_key, **overrides))


@pytest.mark.asyncio
async def test_wrong_audience_is_rejected(signed_identity) -> None:
    private_key, jwk = signed_identity

    with pytest.raises(ClerkAuthenticationError):
        await _verifier(jwk).verify(_token(private_key, aud="other-service"))
