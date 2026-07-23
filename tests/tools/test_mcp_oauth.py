from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

import nanobot.agent.tools.mcp as mcp_mod
from nanobot.agent.tools.mcp import OAuthClientCredentialsAuth, connect_mcp_servers
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import MCPServerConfig


def _oauth_config(secret_file: str) -> MCPServerConfig:
    return MCPServerConfig(
        type="streamableHttp",
        url="http://127.0.0.1:8790/mcp",
        oauthClientCredentials={
            "tokenUrl": "http://127.0.0.1:8790/oauth/token",
            "clientId": "connector-client",
            "clientSecretFile": secret_file,
            "scopes": ["mcp:read", "mcp:write"],
        },
    )


def test_oauth_config_serializes_with_provisioned_shape(tmp_path) -> None:
    config = _oauth_config(str(tmp_path / "client-secret"))

    assert config.model_dump(by_alias=True)["oauthClientCredentials"] == {
        "tokenUrl": "http://127.0.0.1:8790/oauth/token",
        "clientId": "connector-client",
        "clientSecretFile": str(tmp_path / "client-secret"),
        "scopes": ["mcp:read", "mcp:write"],
    }


def test_oauth_config_rejects_static_authorization_header(tmp_path) -> None:
    with pytest.raises(ValueError, match="Authorization"):
        MCPServerConfig(
            headers={"aUtHoRiZaTiOn": "Bearer static"},
            oauthClientCredentials={
                "tokenUrl": "http://127.0.0.1:8790/oauth/token",
                "clientId": "connector-client",
                "clientSecretFile": str(tmp_path / "client-secret"),
            },
        )


def test_official_provider_does_not_mask_secret_file_errors(tmp_path) -> None:
    config = _oauth_config(str(tmp_path / "missing-secret"))

    with pytest.raises(FileNotFoundError):
        mcp_mod._build_official_oauth_provider(
            config.oauth_client_credentials,
            config.url,
        )


@pytest.mark.asyncio
async def test_oauth_auth_posts_basic_client_credentials_and_caches_token(tmp_path) -> None:
    secret_file = tmp_path / "client-secret"
    secret_file.write_text("super-secret\n", encoding="utf-8")
    config = _oauth_config(str(secret_file))
    token_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path == "/oauth/token":
            assert request.headers["authorization"] == "Basic " + base64.b64encode(
                b"connector-client:super-secret"
            ).decode()
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["client_credentials"],
                "resource": ["http://127.0.0.1:8790/mcp"],
                "scope": ["mcp:read mcp:write"],
            }
            token_requests += 1
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 120})
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    auth = OAuthClientCredentialsAuth(
        config.oauth_client_credentials,
        config.url,
        token_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    async with httpx.AsyncClient(transport=transport, auth=auth) as client:
        assert (await client.get("http://127.0.0.1:8790/mcp")).status_code == 200
        assert (await client.get("http://127.0.0.1:8790/mcp")).status_code == 200

    assert token_requests == 1


@pytest.mark.asyncio
async def test_oauth_auth_refreshes_once_after_401(tmp_path) -> None:
    secret_file = tmp_path / "client-secret"
    secret_file.write_text("super-secret", encoding="utf-8")
    config = _oauth_config(str(secret_file))
    token_requests = 0
    resource_authorizations: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path == "/oauth/token":
            token_requests += 1
            return httpx.Response(
                200,
                json={"access_token": f"token-{token_requests}", "expires_in": 120},
            )
        resource_authorizations.append(request.headers["authorization"])
        if len(resource_authorizations) == 1:
            return httpx.Response(401)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    auth = OAuthClientCredentialsAuth(
        config.oauth_client_credentials,
        config.url,
        token_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    async with httpx.AsyncClient(transport=transport, auth=auth) as client:
        response = await client.get("http://127.0.0.1:8790/mcp")

    assert response.status_code == 200
    assert resource_authorizations == ["Bearer token-1", "Bearer token-2"]
    assert token_requests == 2


@pytest.mark.asyncio
async def test_oauth_auth_serializes_concurrent_refreshes(tmp_path) -> None:
    secret_file = tmp_path / "client-secret"
    secret_file.write_text("super-secret", encoding="utf-8")
    config = _oauth_config(str(secret_file))
    started = asyncio.Event()
    release = asyncio.Event()
    token_requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        token_requests += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json={"access_token": "token-1", "expires_in": 120})

    transport = httpx.MockTransport(handler)
    auth = OAuthClientCredentialsAuth(
        config.oauth_client_credentials,
        config.url,
        token_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    first = asyncio.create_task(auth._get_access_token())
    await started.wait()
    second = asyncio.create_task(auth._get_access_token())
    release.set()

    assert await asyncio.gather(first, second) == ["token-1", "token-1"]
    assert token_requests == 1


@pytest.mark.asyncio
async def test_oauth_auth_refreshes_before_token_expiry(tmp_path, monkeypatch) -> None:
    secret_file = tmp_path / "client-secret"
    secret_file.write_text("super-secret", encoding="utf-8")
    config = _oauth_config(str(secret_file))
    token_requests = 0
    clock = iter([100.0, 100.0, 159.0, 160.0, 160.0])
    monkeypatch.setattr(mcp_mod, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        token_requests += 1
        return httpx.Response(
            200,
            json={"access_token": f"token-{token_requests}", "expires_in": 120},
        )

    transport = httpx.MockTransport(handler)
    auth = OAuthClientCredentialsAuth(
        config.oauth_client_credentials,
        config.url,
        token_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    assert await auth._get_access_token() == "token-1"
    assert await auth._get_access_token() == "token-1"
    assert await auth._get_access_token() == "token-2"
    assert token_requests == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_type", ["sse", "streamableHttp"])
async def test_current_http_transports_receive_official_oauth_provider(
    tmp_path, monkeypatch, transport_type: str
) -> None:
    import mcp
    from mcp.client import sse, streamable_http
    from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self) -> None:
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[])

        async def list_resources(self):
            raise RuntimeError("not supported")

        async def list_prompts(self):
            raise RuntimeError("not supported")

    secret_file = tmp_path / "client-secret"
    secret_file.write_text("super-secret", encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.setattr(mcp, "ClientSession", FakeSession)
    if transport_type == "sse":

        @asynccontextmanager
        async def _capturing_sse(_url: str, httpx_client_factory=None, auth=None):
            captured["auth"] = auth
            yield object(), object()

        monkeypatch.setattr(sse, "sse_client", _capturing_sse)
    else:

        @asynccontextmanager
        async def _capturing_streamable(_url: str, http_client=None):
            captured["auth"] = http_client._auth
            yield object(), object(), object()

        monkeypatch.setattr(streamable_http, "streamable_http_client", _capturing_streamable)

    stacks = await connect_mcp_servers(
        {
            "test": MCPServerConfig(
                type=transport_type,
                url="http://127.0.0.1:8790/mcp",
                enabled_tools=[],
                oauthClientCredentials={
                    "tokenUrl": "http://127.0.0.1:8790/oauth/token",
                    "clientId": "connector-client",
                    "clientSecretFile": str(secret_file),
                },
            )
        },
        ToolRegistry(),
    )
    for stack in stacks.values():
        await stack.aclose()

    assert isinstance(captured["auth"], ClientCredentialsOAuthProvider)
