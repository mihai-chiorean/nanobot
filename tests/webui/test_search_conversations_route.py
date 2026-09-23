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


# -- Production row-semantics parity ------------------------------------------
#
# ``search_conversations_parity.json`` was generated by running production's
# own ``SessionManager.search_sessions`` (origin/feat/shared-rooms fb077e59) on
# the raw session records it contains; see the fixture's ``_comment``. The same
# records are written verbatim into this branch's session store and the route
# must return byte-identical rows.

_PARITY_FIXTURE = Path(__file__).parent / "fixtures" / "search_conversations_parity.json"


def _load_parity() -> dict[str, Any]:
    return json.loads(_PARITY_FIXTURE.read_text(encoding="utf-8"))


def _seed_raw(workspace: Path, sessions_data: list[dict[str, Any]]) -> SessionManager:
    sessions = SessionManager(workspace)
    for session in sessions_data:
        path = sessions._get_session_path(session["key"])
        path.write_text(
            "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in session["lines"]),
            encoding="utf-8",
        )
    return sessions


def _search(sessions: SessionManager, q: str, limit: int = 20) -> list[dict[str, Any]]:
    from nanobot.webui.ws_http import _search_conversation_rows

    return _search_conversation_rows(sessions, q, limit=limit)


@pytest.mark.parametrize(
    "case", _load_parity()["cases"], ids=lambda c: f"{c['q']}|{c['limit']}"
)
def test_rows_match_production_search_sessions(tmp_path: Path, case: dict[str, Any]) -> None:
    sessions = _seed_raw(tmp_path / "ws", _load_parity()["sessions"])
    assert _search(sessions, case["q"], case["limit"]) == case["expected"]


@pytest.mark.asyncio
async def test_route_serves_production_rows_over_http(tmp_path: Path) -> None:
    from urllib.parse import urlencode

    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port

    data = _load_parity()
    sessions = _seed_raw(tmp_path / "ws", data["sessions"])
    port = _free_port()
    channel = _channel(sessions, port)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        for case in data["cases"]:
            qs = urlencode({"q": case["q"], "limit": case["limit"]})
            response = await _get(channel, port, f"/api/search/conversations?{qs}", token=token)
            assert response.status_code == 200, response.content
            assert response.json() == {"results": case["expected"]}, case["q"]
    finally:
        await channel.stop()


def test_title_and_message_matches_yield_title_row_then_newest_messages(
    tmp_path: Path,
) -> None:
    sessions = _seed_raw(tmp_path / "ws", _load_parity()["sessions"])
    rows = [r for r in _search(sessions, "invoice") if r["session_key"] == "websocket:alpha"]
    # Title matches and six visible messages match: production caps a session at
    # five rows total -- the title row first, then the four newest messages.
    assert [r["role"] for r in rows] == ["title", "assistant", "user", "assistant", "user"]
    assert rows[0]["snippet"] == "Invoice review"
    assert "message_index" not in rows[0]
    assert rows[0]["timestamp"] == rows[0]["updated_at"] == "2026-09-20T10:00:00"
    assert [r["message_index"] for r in rows[1:]] == [7, 6, 5, 4]
    # system/tool records containing the needle are never searched.
    assert all(r["role"] in {"title", "user", "assistant"} for r in rows)


def test_untitled_session_gets_no_title_row(tmp_path: Path) -> None:
    sessions = _seed_raw(tmp_path / "ws", _load_parity()["sessions"])
    rows = _search(sessions, "invoice")
    for key in ("websocket:beta", "websocket:delta"):  # no title / blank title
        session_rows = [r for r in rows if r["session_key"] == key]
        assert session_rows, key
        assert all(r["role"] != "title" for r in session_rows), session_rows
        assert all(r["title"] == "" for r in session_rows), session_rows
    # A title-only match with no message hits still yields exactly the title row.
    gamma = [r for r in rows if r["session_key"] == "websocket:gamma"]
    assert [(r["role"], r["snippet"]) for r in gamma] == [("title", "Invoice follow-ups")]


def test_non_webui_sessions_are_never_searched(tmp_path: Path) -> None:
    sessions = _seed_raw(tmp_path / "ws", _load_parity()["sessions"])
    # slack/telegram/cli sessions are the newest and match on title and text.
    keys = {r["session_key"] for r in _search(sessions, "invoice", 50)}
    assert keys == {"websocket:alpha", "websocket:beta", "websocket:gamma", "websocket:delta"}
    assert _search(sessions, "slack invoice") == []
    assert _search(sessions, "telegram invoice") == []
    assert _search(sessions, "cli invoice") == []


def test_multiline_message_matches_collapsed_query_with_single_line_snippet(
    tmp_path: Path,
) -> None:
    sessions = _seed_raw(tmp_path / "ws", _load_parity()["sessions"])
    rows = _search(sessions, "quarterly   invoice")
    assert [(r["session_key"], r["message_index"]) for r in rows] == [
        ("websocket:beta", 1),
        ("websocket:beta", 0),
    ]
    assert rows[1]["snippet"] == "Remind me: the quarterly invoice is due Friday. Thanks!"
    for row in _search(sessions, "invoice", 50):
        assert "\n" not in row["snippet"] and "\t" not in row["snippet"]
        assert "  " not in row["snippet"]


class _FakeRequest:
    """Minimal bearer-token request stand-in for the owner-token store."""

    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}
        self.path = "/api/search/conversations?q=invoice"

