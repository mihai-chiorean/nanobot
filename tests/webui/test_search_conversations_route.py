"""``GET /api/search/conversations`` -- owner-gated conversation search (MIT-1412).

0.3.0 parity. Production (``feat/shared-rooms`` a6f0c196) serves this route for
the iOS conversation-search screen
(``ZiggyRESTClient.searchConversations`` -> ``ConversationSearchResult``);
``ziggy-main`` had the backing ``WebuiSessionAccess.search`` but no route, so
search returned 404 and the screen was empty.

The wire shape is pinned to production's -- ``{"results": [{session_key, title,
snippet, role, updated_at, message_index?, timestamp?}]}`` -- because the iOS
decoder treats ``session_key``/``snippet``/``role`` as non-optional and would
drop a malformed row, silently emptying the screen. The owner API token is
required and room credentials are refused, as for the other owner-only
``/api/sessions`` routes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.channels.websocket.rooms import SharedRoomStore
from nanobot.session.manager import SessionManager

_INVOICE_SESSION = "websocket:owner-invoice"
_RECIPE_SESSION = "websocket:owner-recipe"
_ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
_ROOM_ID = "room_" + "a" * 32
_PARTICIPANT_ID = "participant_" + "b" * 32


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)


def _seed_sessions(workspace: Path) -> SessionManager:
    sessions = SessionManager(workspace)
    invoice = sessions.get_or_create(_INVOICE_SESSION)
    invoice.metadata["title"] = "Q4 finance thread"
    invoice.add_message("user", "Please email me the invoice for October.")
    invoice.add_message("assistant", "Sure, sending the October invoice now.")
    sessions.save(invoice)

    recipe = sessions.get_or_create(_RECIPE_SESSION)
    recipe.metadata["title"] = "Ramen weekend"
    recipe.add_message("user", "Best ramen near Berkeley.")
    recipe.add_message("assistant", "Try tonkotsu on Telegraph.")
    sessions.save(recipe)
    return sessions


def _channel(session_manager: SessionManager, port: int) -> Any:
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _ch

    return _ch(MagicMock(), session_manager=session_manager, port=port)


async def _get(channel: Any, port: int, path: str, *, token: str | None = None) -> Any:
    from nanobot.channels.websocket.tests.ws_test_client import http_get

    headers = {"Authorization": f"Bearer {token}"} if token is not None else None
    return await http_get(f"http://127.0.0.1:{port}{path}", headers=headers)


@pytest.mark.asyncio
async def test_owner_search_returns_the_matching_session_in_prod_shape(
    tmp_path: Path,
) -> None:
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port

    sessions = _seed_sessions(tmp_path / "ws")
    port = _free_port()
    channel = _channel(sessions, port)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        response = await _get(channel, port, "/api/search/conversations?q=invoice", token=token)

        assert response.status_code == 200, response.content
        results = response.json()["results"]
        assert {row["session_key"] for row in results} == {_INVOICE_SESSION}
        assert results, "search must not return an empty list for a real hit"
        for row in results:
            assert set(row) >= {
                "session_key",
                "title",
                "snippet",
                "role",
                "updated_at",
            }
            assert isinstance(row["snippet"], str) and row["snippet"]
            assert "invoice" in row["snippet"].casefold()
            assert "path" not in row
            assert len(row["snippet"]) <= 246
        assert {row["role"] for row in results} & {"user", "assistant"}
        # The non-matching session must not leak in.
        assert _RECIPE_SESSION not in {row["session_key"] for row in results}
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_search_requires_the_owner_token(tmp_path: Path) -> None:
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port

    sessions = _seed_sessions(tmp_path / "ws")
    port = _free_port()
    channel = _channel(sessions, port)
    try:
        anonymous = await _get(channel, port, "/api/search/conversations?q=invoice")
        assert anonymous.status_code == 401, anonymous.content
        wrong = await _get(
            channel, port, "/api/search/conversations?q=invoice", token="not-a-token"
        )
        assert wrong.status_code == 401, wrong.content
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_room_credential_is_refused_on_search(tmp_path: Path) -> None:
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port

    sessions = _seed_sessions(tmp_path / "ws")
    # Mint a genuine, live guest room credential through the real store -- not a
    # made-up token string. Room tokens are an audience distinct from owner API
    # tokens and are never accepted on owner-only routes.
    store = SharedRoomStore(sessions, token_ttl_s=300)
    room_token, _credential = store.mint(
        room_id=_ROOM_ID,
        chat_id=_ROOM_CHAT,
        participant_id=_PARTICIPANT_ID,
        display_name="Guest",
        role="contributor",
    )
    assert room_token.startswith("nbrt_")
    assert store.api_credential(room_token) is not None  # the credential is real

    port = _free_port()
    channel = _channel(sessions, port)
    try:
        assert channel.gateway.tokens.check_api_token(_FakeRequest(room_token)) is False
        response = await _get(
            channel, port, "/api/search/conversations?q=invoice", token=room_token
        )
        assert response.status_code == 401, response.content
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_missing_q_matches_prod_empty_behaviour(tmp_path: Path) -> None:
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port

    sessions = _seed_sessions(tmp_path / "ws")
    port = _free_port()
    channel = _channel(sessions, port)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        # Production 400s a missing/too-short q (feat/shared-rooms a6f0c196);
        # it does NOT fall through to an empty list.
        missing = await _get(channel, port, "/api/search/conversations", token=token)
        assert missing.status_code == 400, missing.content
        short = await _get(channel, port, "/api/search/conversations?q=x", token=token)
        assert short.status_code == 400, short.content
        # A well-formed query with no matches returns 200 with an empty list.
        none = await _get(
            channel, port, "/api/search/conversations?q=zzznomatch", token=token
        )
        assert none.status_code == 200, none.content
        assert none.json()["results"] == []
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_search_ignores_the_trusted_proxy_shortcut(tmp_path: Path) -> None:
    """A proxied request is not an owner: the search route must use the token
    store directly, so a trusted-proxy-authenticated request with no owner token
    gets 401 rather than being admitted by the ``check_api_token`` shortcut."""
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port
    from nanobot.channels.websocket.transport import TransportRequest

    sessions = _seed_sessions(tmp_path / "ws")
    port = _free_port()
    channel = _channel(sessions, port)
    try:
        handler = channel.gateway.http

        class _Headers(dict):
            def get(self, key: str, default: Any = None) -> Any:  # type: ignore[reportUnknownParameter]
                for existing, value in self.items():
                    if existing.lower() == key.lower():
                        return value
                return default

        path = "/api/search/conversations?q=invoice"
        proxied = TransportRequest(
            method="GET", path=path, headers=_Headers(), body=b"", raw_path=path
        )
        setattr(proxied, "_nanobot_trusted_proxy_authenticated", True)
        assert (await handler._handle_conversation_search(proxied)).status_code == 401

        owner = TransportRequest(
            method="GET",
            path=path,
            headers=_Headers({"Authorization": f"Bearer {channel.gateway.tokens.issue_api_token(60)}"}),
            body=b"",
            raw_path=path,
        )
        response = await handler._handle_conversation_search(owner)
        assert response.status_code == 200
        payload = json.loads(bytes(response.body).decode())
        assert {row["session_key"] for row in payload["results"]} == {_INVOICE_SESSION}
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_search_never_surfaces_hidden_or_internal_message_text(
    tmp_path: Path,
) -> None:
    """Negative control: the needle appears only in system/tool/reasoning text,
    which the search visibility filter excludes -- so the session must not be
    returned. Mirrors the 0.2.x test's 'needle hidden in system prompt' case."""
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port

    sessions = SessionManager(tmp_path / "ws")
    hidden = sessions.get_or_create("websocket:internal-digest")
    hidden.metadata["title"] = "Weekly digest"
    hidden.add_message("user", "Summarize the quarter.")
    hidden.add_message("system", "invoice total hidden behind the paywall")
    hidden.add_message("tool", "invoice fetched from internal ledger")
    sessions.save(hidden)

    port = _free_port()
    channel = _channel(sessions, port)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        response = await _get(
            channel, port, "/api/search/conversations?q=invoice", token=token
        )
        assert response.status_code == 200, response.content
        assert response.json()["results"] == []
    finally:
        await channel.stop()


class _FakeRequest:
    """Minimal bearer-token request stand-in for the owner-token store."""

    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}
        self.path = "/api/search/conversations?q=invoice"

