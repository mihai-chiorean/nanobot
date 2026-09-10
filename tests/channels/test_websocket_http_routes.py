"""End-to-end tests for the embedded webui's HTTP routes on the WebSocket channel."""

import asyncio
import functools
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import jwt
import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric import rsa

from nanobot.channels.websocket import WebSocketChannel
from nanobot.session.manager import Session, SessionManager

_PORT = 29900


def _ch(
    bus: Any,
    *,
    session_manager: SessionManager | None = None,
    static_dist_path: Path | None = None,
    active_session_keys: Any = None,
    port: int = _PORT,
    **extra: Any,
) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": port,
        "path": "/",
        "websocketRequiresToken": False,
    }
    cfg.update(extra)
    return WebSocketChannel(
        cfg,
        bus,
        session_manager=session_manager,
        static_dist_path=static_dist_path,
        active_session_keys=active_session_keys,
    )


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


async def _http_get(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return await asyncio.to_thread(
        functools.partial(httpx.get, url, headers=headers or {}, timeout=5.0)
    )


def _seed_session(workspace: Path, key: str = "websocket:test") -> SessionManager:
    sm = SessionManager(workspace)
    s = Session(key=key)
    s.add_message("user", "hi")
    s.add_message("assistant", "hello back")
    sm.save(s)
    return sm


def _seed_many(workspace: Path, keys: list[str]) -> SessionManager:
    sm = SessionManager(workspace)
    for k in keys:
        s = Session(key=k)
        s.add_message("user", f"hi from {k}")
        sm.save(s)
    return sm


@pytest.mark.asyncio
async def test_bootstrap_returns_token_for_localhost(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_session(tmp_path)
    channel = _ch(bus, session_manager=sm, port=29901)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get("http://127.0.0.1:29901/webui/bootstrap")
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"].startswith("nbwt_")
        assert body["ws_path"] == "/"
        assert body["expires_in"] > 0
        assert isinstance(body.get("model_name"), str)
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_sessions_routes_require_bearer_token(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_session(tmp_path, key="websocket:abc")
    channel = _ch(bus, session_manager=sm, port=29902)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        # Unauthenticated → 401.
        deny = await _http_get("http://127.0.0.1:29902/api/sessions")
        assert deny.status_code == 401

        # Mint a token via bootstrap, then call the API with it.
        boot = await _http_get("http://127.0.0.1:29902/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        listing = await _http_get("http://127.0.0.1:29902/api/sessions", headers=auth)
        assert listing.status_code == 200
        keys = [s["key"] for s in listing.json()["sessions"]]
        assert "websocket:abc" in keys
        summary = next(s for s in listing.json()["sessions"] if s["key"] == "websocket:abc")
        assert summary["preview"] == "hi"
        # Server stays an opaque source: filesystem paths must not leak to the wire.
        assert all("path" not in s for s in listing.json()["sessions"])

        msgs = await _http_get(
            "http://127.0.0.1:29902/api/sessions/websocket:abc/messages",
            headers=auth,
        )
        assert msgs.status_code == 200
        body = msgs.json()
        assert body["key"] == "websocket:abc"
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_private_published_file_route_is_grant_scoped_and_no_store(
    bus: MagicMock, tmp_path: Path
) -> None:
    manager = SessionManager(tmp_path)
    session = Session(key="websocket:download")
    file_id = manager.store_published_snapshot("report.md", b"# Snapshot\n")
    url = manager.published_file_url(session.key, file_id)
    session.add_message("assistant", f"Here: [report.md]({url})")
    manager.grant_published_files(session, {file_id: "report.md"}, message_start=0)
    manager.save(session)
    channel = _ch(bus, session_manager=manager, port=29930)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        unauthenticated = await _http_get(f"http://127.0.0.1:29930{url}")
        assert unauthenticated.status_code == 404
        boot = await _http_get("http://127.0.0.1:29930/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}
        downloaded = await _http_get(f"http://127.0.0.1:29930{url}", headers=auth)
        assert downloaded.status_code == 200
        assert downloaded.content == b"# Snapshot\n"
        assert downloaded.headers["content-type"] == "text/markdown; charset=utf-8"
        assert downloaded.headers["cache-control"] == "private, no-store"
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert "attachment" in downloaded.headers["content-disposition"]
        assert "report.md" in downloaded.headers["content-disposition"]
        history = await _http_get(
            "http://127.0.0.1:29930/api/sessions/websocket%3Adownload/messages",
            headers=auth,
        )
        assert history.status_code == 200
        assert "published_file_provenance" not in history.json()["metadata"]
        assert all("_published_message_id" not in message for message in history.json()["messages"])

        # URL decoding, arbitrary query params, invalid ids, and a valid id
        # under a different session all fail with the same opaque response.
        for path in (
            url.replace("%3A", ":"),
            f"{url}/",
            f"{url}?x",
            f"{url}?x=",
            f"{url}?path=/etc/passwd",
            url[:-1] + "A",
            manager.published_file_url("websocket:other", file_id),
        ):
            denied = await _http_get(f"http://127.0.0.1:29930{path}", headers=auth)
            assert denied.status_code == 404, path
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_sessions_list_only_returns_websocket_sessions_by_default(
    bus: MagicMock, tmp_path: Path
) -> None:
    # Seed a realistic multi-channel disk state: CLI, Slack, Lark and
    # websocket sessions all live in the same ``sessions/`` directory.
    sm = _seed_many(
        tmp_path,
        [
            "cli:direct",
            "slack:C123",
            "lark:oc_abc",
            "websocket:alpha",
            "websocket:beta",
        ],
    )
    channel = _ch(bus, session_manager=sm, port=29906)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29906/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        listing = await _http_get("http://127.0.0.1:29906/api/sessions", headers=auth)
        assert listing.status_code == 200
        keys = {s["key"] for s in listing.json()["sessions"]}
        # Only websocket-channel sessions are part of the webui surface; CLI /
        # Slack / Lark rows would be non-resumable from the browser.
        assert keys == {"websocket:alpha", "websocket:beta"}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_conversation_search_is_bounded_and_user_visible_only(
    bus: MagicMock, tmp_path: Path
) -> None:
    sm = SessionManager(tmp_path)
    recent = Session(
        key="websocket:recent",
        metadata={"title": "Local inference notes"},
    )
    recent.add_message("system", "needle hidden system prompt")
    recent.add_message("tool", "needle hidden tool output")
    recent.add_message("user", "Compare prefix caching for the local model")
    recent.add_message("assistant", "Prefix caching reduces repeated prompt work")
    sm.save(recent)
    hidden = Session(key="cli:hidden")
    hidden.add_message("user", "prefix caching in a hidden channel")
    sm.save(hidden)

    channel = _ch(bus, session_manager=sm, port=29927)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        denied = await _http_get(
            "http://127.0.0.1:29927/api/search/conversations?q=prefix"
        )
        assert denied.status_code == 401
        boot = await _http_get("http://127.0.0.1:29927/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}

        response = await _http_get(
            "http://127.0.0.1:29927/api/search/conversations?q=prefix&limit=10",
            headers=auth,
        )

        assert response.status_code == 200
        results = response.json()["results"]
        assert len(results) == 2
        assert {item["role"] for item in results} == {"user", "assistant"}
        assert {item["session_key"] for item in results} == {"websocket:recent"}
        assert all(item["title"] == "Local inference notes" for item in results)
        assert all(len(item["snippet"]) <= 246 for item in results)
        assert all("path" not in item for item in results)

        hidden_result = await _http_get(
            "http://127.0.0.1:29927/api/search/conversations?q=needle",
            headers=auth,
        )
        assert hidden_result.status_code == 200
        assert hidden_result.json()["results"] == []

        short = await _http_get(
            "http://127.0.0.1:29927/api/search/conversations?q=x",
            headers=auth,
        )
        assert short.status_code == 400
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_activity_route_matches_webui_contract(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_many(tmp_path, ["websocket:active", "websocket:waiting", "cli:hidden"])
    waiting = sm.get_or_create("websocket:waiting")
    waiting.add_message("assistant", "Choose one", buttons=[["Continue"]])
    sm.save(waiting)
    channel = _ch(
        bus,
        session_manager=sm,
        active_session_keys=lambda: {"websocket:active"},
        port=29911,
    )
    live_connection = AsyncMock()
    channel._attach(live_connection, "waiting")
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        denied = await _http_get("http://127.0.0.1:29911/api/activity")
        assert denied.status_code == 401
        boot = await _http_get("http://127.0.0.1:29911/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}

        response = await _http_get("http://127.0.0.1:29911/api/activity", headers=auth)

        assert response.status_code == 200
        rows = {row["key"]: row for row in response.json()["activity"]}
        assert set(rows) == {"websocket:active", "websocket:waiting"}
        assert rows["websocket:active"]["status"] == "active"
        assert rows["websocket:active"]["chat_id"] == "active"
        assert rows["websocket:waiting"]["status"] == "waiting"
        assert rows["websocket:waiting"]["preview"] == "hi from websocket:waiting"
        assert rows["websocket:waiting"]["last_role"] == "assistant"
        assert rows["websocket:waiting"]["last_text"] == "Choose one"
        assert "path" not in rows["websocket:waiting"]
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_delete_removes_file(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_session(tmp_path, key="websocket:doomed")
    channel = _ch(bus, session_manager=sm, port=29903)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29903/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        path = sm._get_session_path("websocket:doomed")
        assert path.exists()
        resp = await _http_get(
            "http://127.0.0.1:29903/api/sessions/websocket:doomed/delete",
            headers=auth,
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert not path.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_delete_rejects_active_conversation(
    bus: MagicMock, tmp_path: Path
) -> None:
    sm = _seed_session(tmp_path, key="websocket:active-delete")
    channel = _ch(
        bus,
        session_manager=sm,
        active_session_keys=lambda: {"websocket:active-delete"},
        port=29928,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29928/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}

        path = sm._get_session_path("websocket:active-delete")
        response = await _http_get(
            "http://127.0.0.1:29928/api/sessions/websocket:active-delete/delete",
            headers=auth,
        )

        assert response.status_code == 409
        assert path.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_routes_accept_percent_encoded_websocket_keys(
    bus: MagicMock, tmp_path: Path
) -> None:
    sm = _seed_session(tmp_path, key="websocket:encoded-key")
    channel = _ch(bus, session_manager=sm, port=29910)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29910/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        msgs = await _http_get(
            "http://127.0.0.1:29910/api/sessions/websocket%3Aencoded-key/messages",
            headers=auth,
        )
        assert msgs.status_code == 200
        assert msgs.json()["key"] == "websocket:encoded-key"

        path = sm._get_session_path("websocket:encoded-key")
        assert path.exists()
        deleted = await _http_get(
            "http://127.0.0.1:29910/api/sessions/websocket%3Aencoded-key/delete",
            headers=auth,
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert not path.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_routes_reject_non_websocket_keys(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_many(
        tmp_path,
        [
            "websocket:kept",
            "cli:direct",
            "slack:C123",
        ],
    )
    channel = _ch(bus, session_manager=sm, port=29909)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29909/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # The webui list already hides non-websocket sessions; handcrafted URLs
        # should hit the same boundary rather than exposing or deleting them.
        msgs = await _http_get(
            "http://127.0.0.1:29909/api/sessions/cli:direct/messages",
            headers=auth,
        )
        assert msgs.status_code == 404

        doomed = sm._get_session_path("slack:C123")
        assert doomed.exists()
        deny_delete = await _http_get(
            "http://127.0.0.1:29909/api/sessions/slack:C123/delete",
            headers=auth,
        )
        assert deny_delete.status_code == 404
        assert doomed.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_routes_reject_invalid_key(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_session(tmp_path)
    channel = _ch(bus, session_manager=sm, port=29904)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _http_get("http://127.0.0.1:29904/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # Invalid characters in the key -> regex match fails -> 404
        # (route doesn't match, falls through to channel 404).
        resp = await _http_get(
            "http://127.0.0.1:29904/api/sessions/bad%20key/messages",
            headers=auth,
        )
        assert resp.status_code in {400, 404}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_static_serves_index_when_dist_present(bus: MagicMock, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>nbweb</title>")
    (dist / "favicon.svg").write_text("<svg/>")
    sm = _seed_session(tmp_path / "ws_state")
    channel = _ch(bus, session_manager=sm, static_dist_path=dist, port=29905)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        # Bare ``GET /`` is a browser opening the app: it must return the SPA
        # index.html, not the WS-upgrade handler's 401/426.
        root = await _http_get("http://127.0.0.1:29905/")
        assert root.status_code == 200
        assert "nbweb" in root.text
        asset = await _http_get("http://127.0.0.1:29905/favicon.svg")
        assert asset.status_code == 200
        assert "<svg" in asset.text
        # Unknown SPA route falls back to index.html.
        spa = await _http_get("http://127.0.0.1:29905/sessions/abc")
        assert spa.status_code == 200
        assert "nbweb" in spa.text
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_static_rejects_path_traversal(bus: MagicMock, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("ok")
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")
    channel = _ch(bus, static_dist_path=dist, port=29906)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get("http://127.0.0.1:29906/../secret.txt")
        # Normalized by httpx into /secret.txt → falls back to index.html, not 'classified'.
        assert "classified" not in resp.text
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_unknown_route_returns_404(bus: MagicMock) -> None:
    channel = _ch(bus, port=29907)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get("http://127.0.0.1:29907/api/unknown")
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_api_token_pool_purges_expired(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_session(tmp_path)
    channel = _ch(bus, session_manager=sm, port=29908)
    # Don't start a server — directly inject and validate.
    import time as _time

    channel._api_tokens["expired"] = _time.monotonic() - 1
    channel._api_tokens["live"] = _time.monotonic() + 60

    class _FakeReq:
        path = "/api/sessions"
        headers = {"Authorization": "Bearer expired"}

    assert channel._check_api_token(_FakeReq()) is False

    class _LiveReq:
        path = "/api/sessions"
        headers = {"Authorization": "Bearer live"}

    assert channel._check_api_token(_LiveReq()) is True

    class _QueryReq:
        path = "/api/sessions?token=live"
        headers = {}

    assert channel._check_api_token(_QueryReq()) is False


@pytest.mark.asyncio
async def test_clerk_bootstrap_token_serves_rest_and_one_websocket(
    bus: MagicMock, tmp_path: Path
) -> None:
    issuer = "https://clerk.example.test"
    audience = "ziggy-control"
    authorized_party = "https://ziggy-control.example.test"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    channel = _ch(
        bus,
        session_manager=_seed_session(tmp_path, key="websocket:clerk"),
        port=29921,
        websocketRequiresToken=True,
        authIssuer=issuer,
        authJwksUrl=f"{issuer}/.well-known/jwks.json",
        authAudience=audience,
        authAllowedEmails=["tenant@example.com"],
        authAuthorizedParties=[authorized_party],
    )
    channel._clerk_verifier._fetch_jwks = AsyncMock(return_value={"keys": [jwk]})
    now = int(time.time())

    def identity_token(email: str) -> str:
        return jwt.encode(
            {
                "iss": issuer,
                "sub": "user_test",
                "iat": now,
                "exp": now + 300,
                "email": email,
                "aud": audience,
                "azp": authorized_party,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        local_bootstrap = await _http_get("http://127.0.0.1:29921/webui/bootstrap")
        assert local_bootstrap.status_code == 404

        missing = await _http_get("http://127.0.0.1:29921/auth/bootstrap")
        assert missing.status_code == 401
        forbidden = await _http_get(
            "http://127.0.0.1:29921/auth/bootstrap",
            headers={"Authorization": f"Bearer {identity_token('other@example.com')}"},
        )
        assert forbidden.status_code == 403

        bootstrap = await _http_get(
            "http://127.0.0.1:29921/auth/bootstrap",
            headers={"Authorization": f"Bearer {identity_token('TENANT@example.com')}"},
        )
        assert bootstrap.status_code == 200
        token = bootstrap.json()["token"]
        rest_headers = {"Authorization": f"Bearer {token}"}
        rest = await _http_get("http://127.0.0.1:29921/api/sessions", headers=rest_headers)
        assert rest.status_code == 200

        async with websockets.connect(f"ws://127.0.0.1:29921/?token={token}") as client:
            assert json.loads(await client.recv())["event"] == "ready"

        rest_after_ws = await _http_get("http://127.0.0.1:29921/api/sessions", headers=rest_headers)
        assert rest_after_ws.status_code == 200
        with pytest.raises(websockets.exceptions.InvalidStatus) as reused:
            async with websockets.connect(f"ws://127.0.0.1:29921/?token={token}"):
                pass
        assert reused.value.response.status_code == 401
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_private_issue_token_serves_rest_before_and_after_one_websocket(
    bus: MagicMock, tmp_path: Path
) -> None:
    channel = _ch(
        bus,
        session_manager=_seed_session(tmp_path, key="websocket:private-route"),
        port=29922,
        tokenIssuePath="/auth/token",
        tokenIssueSecret="server-only-secret",
        websocketRequiresToken=True,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        issue = await _http_get(
            "http://127.0.0.1:29922/auth/token",
            headers={"Authorization": "Bearer server-only-secret"},
        )
        assert issue.status_code == 200
        token = issue.json()["token"]
        assert channel._issued_tokens[token] == channel._api_tokens[token]

        rest_headers = {"Authorization": f"Bearer {token}"}
        assert (
            await _http_get("http://127.0.0.1:29922/api/sessions", headers=rest_headers)
        ).status_code == 200

        async with websockets.connect(f"ws://127.0.0.1:29922/?token={token}") as client:
            assert json.loads(await client.recv())["event"] == "ready"

        assert token not in channel._issued_tokens
        assert token in channel._api_tokens
        assert (
            await _http_get("http://127.0.0.1:29922/api/sessions", headers=rest_headers)
        ).status_code == 200
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_private_issue_token_expires_from_both_pools(bus: MagicMock, tmp_path: Path) -> None:
    channel = _ch(
        bus,
        session_manager=_seed_session(tmp_path, key="websocket:private-expiry"),
        port=29923,
        tokenIssuePath="/auth/token",
        tokenIssueSecret="server-only-secret",
        websocketRequiresToken=True,
    )
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        issue = await _http_get(
            "http://127.0.0.1:29923/auth/token",
            headers={"Authorization": "Bearer server-only-secret"},
        )
        token = issue.json()["token"]
        expiry = time.monotonic() - 1
        channel._issued_tokens[token] = expiry
        channel._api_tokens[token] = expiry

        rest = await _http_get(
            "http://127.0.0.1:29923/api/sessions",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert rest.status_code == 401
        with pytest.raises(websockets.exceptions.InvalidStatus) as expired:
            async with websockets.connect(f"ws://127.0.0.1:29923/?token={token}"):
                pass
        assert expired.value.response.status_code == 401
        assert token not in channel._issued_tokens
        assert token not in channel._api_tokens
    finally:
        await channel.stop()
        await server_task
