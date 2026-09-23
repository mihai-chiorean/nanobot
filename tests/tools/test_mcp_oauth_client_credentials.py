"""OAuth 2.0 client-credentials auth for HTTP MCP servers (MIT-1405).

The tenant Gmail connector (ziggy-connectors on https://127.0.0.1:8790) only
accepts a bearer token minted by its ``/oauth/token`` endpoint, using HTTP
Basic client authentication and an RFC 8707 ``resource`` that names the MCP
URL. ``provision_tenant.py`` writes the config as ``oauthClientCredentials``.
"""

from __future__ import annotations

import base64
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import mcp.client.sse  # noqa: F401  (patched below)
import mcp.client.streamable_http  # noqa: F401
import pytest
from loguru import logger

import nanobot.agent.tools.mcp as mcp_mod
from nanobot.agent.tools.mcp import connect_mcp_servers
from nanobot.agent.tools.mcp_client_credentials import OAuthClientCredentialsAuth
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import Config, MCPServerConfig

MCP_URL = "https://127.0.0.1:8790/mcp"
TOKEN_URL = "https://127.0.0.1:8790/oauth/token"
CLIENT_ID = "rt_0123456789abcdef"
SECRET = "s3cr3t-value-that-must-never-be-logged"
SCOPES = ["gmail.read", "gmail.search"]


def _server_dict(secret_file: str) -> dict:
    """The shape provision_tenant.gmail_mcp_server() writes."""
    return {
        "type": "streamableHttp",
        "url": MCP_URL,
        "oauthClientCredentials": {
            "tokenUrl": TOKEN_URL,
            "clientId": CLIENT_ID,
            "clientSecretFile": secret_file,
            "scopes": SCOPES,
        },
        "enabledTools": ["gmail_search"],
        "toolTimeout": 240,
    }


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "mcp-client-secret"
    path.write_text(SECRET + "\n", encoding="utf-8")
    return path


class FakeConnector:
    """Token endpoint plus an MCP endpoint that checks the bearer token."""

    def __init__(self, *, expires_in: int = 3600) -> None:
        self.expires_in = expires_in
        self.token_requests: list[httpx.Request] = []
        self.mcp_auth_headers: list[str | None] = []
        self.revoked: set[str] = set()

    @property
    def issued(self) -> int:
        return len(self.token_requests)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            self.token_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{self.issued}",
                    "token_type": "Bearer",
                    "expires_in": self.expires_in,
                },
            )
        header = request.headers.get("Authorization")
        self.mcp_auth_headers.append(header)
        token = (header or "").removeprefix("Bearer ")
        if not token.startswith("tok-") or token in self.revoked:
            return httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})


def _auth(connector: FakeConnector, secret_file: str) -> OAuthClientCredentialsAuth:
    config = MCPServerConfig.model_validate(_server_dict(secret_file))
    assert config.oauth_client_credentials is not None
    return OAuthClientCredentialsAuth(
        config.oauth_client_credentials,
        MCP_URL,
        token_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(connector.handler)
        ),
    )


def _mcp_client(connector: FakeConnector, auth: httpx.Auth) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(connector.handler), auth=auth)


# -- (1) config ----------------------------------------------------------------


def test_camelcase_oauth_client_credentials_parses(secret_file: Path) -> None:
    config = Config.model_validate(
        {"tools": {"mcpServers": {"gmail": _server_dict(str(secret_file))}}}
    )

    creds = config.tools.mcp_servers["gmail"].oauth_client_credentials
    assert creds is not None
    assert creds.token_url == TOKEN_URL
    assert creds.client_id == CLIENT_ID
    assert creds.client_secret_file == str(secret_file)
    assert creds.scopes == SCOPES
    assert creds.resource is None
    dumped = config.tools.mcp_servers["gmail"].model_dump(by_alias=True)
    assert dumped["oauthClientCredentials"]["tokenUrl"] == TOKEN_URL


def test_oauth_client_credentials_rejects_static_authorization_header(
    secret_file: Path,
) -> None:
    raw = _server_dict(str(secret_file))
    raw["headers"] = {"authorization": "Bearer static"}
    with pytest.raises(ValueError, match="oauthClientCredentials"):
        MCPServerConfig.model_validate(raw)


# -- (2)-(4) token flow ---------------------------------------------------------


@pytest.mark.asyncio
async def test_first_request_fetches_token_with_resource_and_bearer(
    secret_file: Path,
) -> None:
    connector = FakeConnector()
    async with _mcp_client(connector, _auth(connector, str(secret_file))) as client:
        response = await client.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1})

    assert response.status_code == 200
    assert connector.issued == 1
    token_request = connector.token_requests[0]
    assert token_request.method == "POST"
    form = parse_qs(token_request.content.decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "resource": [MCP_URL],
        "scope": [" ".join(SCOPES)],
    }
    # ziggy-connectors only supports client_secret_basic.
    expected = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    assert token_request.headers["Authorization"] == f"Basic {expected}"
    assert connector.mcp_auth_headers == ["Bearer tok-1"]


@pytest.mark.asyncio
async def test_explicit_resource_overrides_server_url(secret_file: Path) -> None:
    connector = FakeConnector()
    raw = _server_dict(str(secret_file))
    raw["oauthClientCredentials"]["resource"] = "https://connectors.example/mcp"
    creds = MCPServerConfig.model_validate(raw).oauth_client_credentials
    auth = OAuthClientCredentialsAuth(
        creds,
        MCP_URL,
        token_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(connector.handler)
        ),
    )
    async with _mcp_client(connector, auth) as client:
        await client.post(MCP_URL, json={})

    form = parse_qs(connector.token_requests[0].content.decode())
    assert form["resource"] == ["https://connectors.example/mcp"]


@pytest.mark.asyncio
async def test_second_request_reuses_cached_token(secret_file: Path) -> None:
    connector = FakeConnector()
    async with _mcp_client(connector, _auth(connector, str(secret_file))) as client:
        await client.post(MCP_URL, json={})
        await client.post(MCP_URL, json={})

    assert connector.issued == 1
    assert connector.mcp_auth_headers == ["Bearer tok-1", "Bearer tok-1"]


@pytest.mark.asyncio
async def test_token_inside_expiry_margin_is_refreshed(secret_file: Path) -> None:
    connector = FakeConnector(expires_in=30)  # shorter than the refresh margin
    async with _mcp_client(connector, _auth(connector, str(secret_file))) as client:
        await client.post(MCP_URL, json={})
        await client.post(MCP_URL, json={})

    assert connector.issued == 2
    assert connector.mcp_auth_headers == ["Bearer tok-1", "Bearer tok-2"]


@pytest.mark.asyncio
async def test_401_triggers_exactly_one_refresh_and_retry(secret_file: Path) -> None:
    connector = FakeConnector()
    async with _mcp_client(connector, _auth(connector, str(secret_file))) as client:
        await client.post(MCP_URL, json={})
        connector.revoked.add("tok-1")
        response = await client.post(MCP_URL, json={"jsonrpc": "2.0", "id": 2})

    assert response.status_code == 200
    assert connector.issued == 2
    assert connector.mcp_auth_headers == ["Bearer tok-1", "Bearer tok-1", "Bearer tok-2"]


@pytest.mark.asyncio
async def test_persistent_401_is_not_retried_forever(secret_file: Path) -> None:
    connector = FakeConnector()
    connector.revoked.update({"tok-1", "tok-2", "tok-3"})
    async with _mcp_client(connector, _auth(connector, str(secret_file))) as client:
        response = await client.post(MCP_URL, json={})

    assert response.status_code == 401
    assert connector.issued == 2
    assert len(connector.mcp_auth_headers) == 2


# -- (5) env expansion and secret hygiene ----------------------------------------


@pytest.mark.asyncio
async def test_env_var_in_secret_file_is_expanded_and_secret_never_logged(
    secret_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZIGGY_MCP_CLIENT_SECRET_FILE", str(secret_file))
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="TRACE")
    try:
        connector = FakeConnector()
        auth = _auth(connector, "${ZIGGY_MCP_CLIENT_SECRET_FILE}")
        async with _mcp_client(connector, auth) as client:
            await client.post(MCP_URL, json={})
            connector.revoked.add("tok-1")
            await client.post(MCP_URL, json={})

        # A failing token endpoint must not echo the secret either.
        def failing(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_client", "echo": SECRET})

        broken = OAuthClientCredentialsAuth(
            MCPServerConfig.model_validate(
                _server_dict("${ZIGGY_MCP_CLIENT_SECRET_FILE}")
            ).oauth_client_credentials,
            MCP_URL,
            token_client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(failing)
            ),
        )
        async with _mcp_client(connector, broken) as client:
            with pytest.raises(Exception) as excinfo:
                await client.post(MCP_URL, json={})
        assert SECRET not in str(excinfo.value)
        assert SECRET not in repr(excinfo.value)
    finally:
        logger.remove(sink)

    assert connector.issued == 2
    expected = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    assert connector.token_requests[0].headers["Authorization"] == f"Basic {expected}"
    assert lines, "expected the auth flow to log something"
    assert not any(SECRET in line for line in lines)
    assert not any(expected in line for line in lines)


@pytest.mark.asyncio
async def test_unset_env_var_fails_without_reading_a_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZIGGY_MCP_CLIENT_SECRET_FILE", raising=False)
    connector = FakeConnector()
    auth = _auth(connector, "${ZIGGY_MCP_CLIENT_SECRET_FILE}")
    async with _mcp_client(connector, auth) as client:
        with pytest.raises(ValueError, match="clientSecretFile"):
            await client.post(MCP_URL, json={})
    assert connector.issued == 0


# -- wiring into connect_mcp_servers ----------------------------------------------


@pytest.mark.asyncio
async def test_streamable_http_server_sends_bearer_from_client_credentials(
    secret_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = FakeConnector()
    seen: list[httpx.Response] = []

    @asynccontextmanager
    async def fake_streamable_http_client(url: str, http_client=None):
        assert http_client is not None
        seen.append(await http_client.post(url, json={"jsonrpc": "2.0", "id": 1}))
        raise RuntimeError("stop after the first request")
        yield  # pragma: no cover

    async def reachable(_url: str, timeout: float = 3.0) -> bool:
        return True

    monkeypatch.setattr(mcp_mod, "validate_url_target", lambda _url: (True, ""))
    monkeypatch.setattr(mcp_mod, "_probe_http_url", reachable)
    monkeypatch.setattr(
        mcp_mod, "PinnedDNSAsyncTransport", lambda: httpx.MockTransport(connector.handler)
    )
    monkeypatch.setattr(
        sys.modules["mcp.client.streamable_http"],
        "streamable_http_client",
        fake_streamable_http_client,
    )

    config = MCPServerConfig.model_validate(_server_dict(str(secret_file)))
    connections = await connect_mcp_servers({"gmail": config}, ToolRegistry())
    for connection in connections.values():
        await connection.aclose()

    assert [r.status_code for r in seen] == [200]
    assert connector.issued == 1
    assert connector.mcp_auth_headers == ["Bearer tok-1"]


@pytest.mark.asyncio
async def test_sse_server_gets_client_credentials_auth(
    secret_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_sse_client(url: str, httpx_client_factory=None, auth=None, **_kw):
        captured["auth"] = auth
        raise RuntimeError("stop")
        yield  # pragma: no cover

    async def reachable(_url: str, timeout: float = 3.0) -> bool:
        return True

    monkeypatch.setattr(mcp_mod, "validate_url_target", lambda _url: (True, ""))
    monkeypatch.setattr(mcp_mod, "_probe_http_url", reachable)
    monkeypatch.setattr(sys.modules["mcp.client.sse"], "sse_client", fake_sse_client)

    raw = _server_dict(str(secret_file))
    raw["type"] = "sse"
    connections = await connect_mcp_servers(
        {"gmail": MCPServerConfig.model_validate(raw)}, ToolRegistry()
    )
    for connection in connections.values():
        await connection.aclose()

    assert isinstance(captured["auth"], OAuthClientCredentialsAuth)


# -- operator-configured loopback connector (real sockets, no transport mocks) ----


class _LoopbackServer:
    """A plain-HTTP server on 127.0.0.1 that records every request it sees."""

    def __init__(self, redirect_to: str | None = None) -> None:
        import http.server
        import threading

        self.requests: list[tuple[str, str, str | None]] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                owner.requests.append(
                    ("POST", self.path, self.headers.get("Authorization"))
                )
                if self.path == "/oauth/token":
                    body = b'{"access_token":"tok-live","token_type":"Bearer","expires_in":3600}'
                    self.send_response(200)
                elif redirect_to is not None:
                    self.send_response(307)
                    self.send_header("Location", redirect_to)
                    body = b""
                else:
                    body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                owner.requests.append(("GET", self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def loopback_servers():
    started: list[_LoopbackServer] = []

    def start(**kwargs: object) -> _LoopbackServer:
        server = _LoopbackServer(**kwargs)  # type: ignore[arg-type]
        started.append(server)
        return server

    from nanobot.security.network import configure_loopback_exception, configure_ssrf_whitelist

    # The tenant runtime has neither a loopback exception nor a whitelist.
    configure_ssrf_whitelist([])
    configure_loopback_exception(False)
    yield start
    for server in started:
        server.close()


def _loopback_config(server: _LoopbackServer, secret_file: Path) -> MCPServerConfig:
    return MCPServerConfig.model_validate(
        {
            "type": "streamableHttp",
            "url": f"{server.base}/mcp",
            "oauthClientCredentials": {
                "tokenUrl": f"{server.base}/oauth/token",
                "clientId": CLIENT_ID,
                "clientSecretFile": str(secret_file),
                "scopes": SCOPES,
            },
        }
    )


def _first_post_transport(outcomes: list[object]):
    @asynccontextmanager
    async def fake_streamable_http_client(url: str, http_client=None):
        assert http_client is not None
        try:
            outcomes.append(await http_client.post(url, json={"jsonrpc": "2.0", "id": 1}))
        except Exception as exc:  # the guard raises inside the transport
            outcomes.append(exc)
        raise RuntimeError("stop after the first request")
        yield  # pragma: no cover

    return fake_streamable_http_client


async def _connect(servers: dict[str, MCPServerConfig], monkeypatch, outcomes) -> None:
    monkeypatch.setattr(
        sys.modules["mcp.client.streamable_http"],
        "streamable_http_client",
        _first_post_transport(outcomes),
    )
    connections = await connect_mcp_servers(servers, ToolRegistry())
    for connection in connections.values():
        await connection.aclose()


@pytest.mark.asyncio
async def test_operator_loopback_mcp_server_and_token_url_connect(
    loopback_servers, secret_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = loopback_servers()
    outcomes: list[object] = []

    servers = mcp_mod._mark_operator_configured({"gmail": _loopback_config(server, secret_file)})
    await _connect(servers, monkeypatch, outcomes)

    assert [getattr(o, "status_code", o) for o in outcomes] == [200]
    assert server.requests == [
        ("POST", "/oauth/token", server.requests[0][2]),
        ("POST", "/mcp", "Bearer tok-live"),
    ]
    assert server.requests[0][2].startswith("Basic ")


@pytest.mark.asyncio
async def test_operator_config_marking_comes_from_tools_mcp_servers(
    secret_file: Path, tmp_path: Path
) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"workspace": str(tmp_path)}},
            "tools": {"mcpServers": {"gmail": _server_dict(str(secret_file))}},
        }
    )
    servers = mcp_mod._configured_servers(config)

    assert servers["gmail"]._operator_configured is True
    assert config.tools.mcp_servers["gmail"]._operator_configured is False
    assert mcp_mod._operator_loopback_origins(servers["gmail"]) == frozenset(
        {("https", "127.0.0.1", 8790)}
    )


@pytest.mark.asyncio
async def test_unmarked_loopback_server_is_still_refused(
    loopback_servers, secret_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that did not come from operator config (e.g. a workspace plugin)."""
    server = loopback_servers()
    outcomes: list[object] = []

    await _connect({"plugin": _loopback_config(server, secret_file)}, monkeypatch, outcomes)

    assert outcomes == []  # rejected before any transport was opened
    assert server.requests == []


@pytest.mark.asyncio
async def test_web_fetch_to_loopback_stays_blocked_with_the_same_config(
    loopback_servers, secret_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nanobot.agent.tools.web import WebFetchTool

    server = loopback_servers()
    servers = mcp_mod._mark_operator_configured({"gmail": _loopback_config(server, secret_file)})
    outcomes: list[object] = []
    await _connect(servers, monkeypatch, outcomes)
    seen_before = list(server.requests)

    for path in ("/mcp", "/oauth/token", "/"):
        result = await WebFetchTool().execute(f"{server.base}{path}")
        assert "URL validation failed" in str(result)

    assert server.requests == seen_before


@pytest.mark.asyncio
async def test_redirect_from_mcp_server_to_another_loopback_port_is_refused(
    loopback_servers, secret_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = loopback_servers()
    server = loopback_servers(redirect_to=f"{other.base}/mcp")
    outcomes: list[object] = []

    servers = mcp_mod._mark_operator_configured({"gmail": _loopback_config(server, secret_file)})
    await _connect(servers, monkeypatch, outcomes)

    assert len(outcomes) == 1
    assert isinstance(outcomes[0], httpx.RequestError)
    assert "Blocked" in str(outcomes[0])
    assert other.requests == []
    assert ("POST", "/mcp", "Bearer tok-live") in server.requests
