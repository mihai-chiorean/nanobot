"""Owner ``POST /api/sessions/<key>/title`` (MIT-1413).

Production (``feat/shared-rooms`` @ 83028651) lets the owner rename a
conversation through the WebUI; on the 0.3.0 line the route went missing, so
the iOS rename (``AppModel.renameConversation`` ->
``ZiggyRESTClient.updateConversationTitle``) 404s.  Pins the restored
contract: the owner API bearer is required (room credentials live in a
separate pool and the trusted-proxy marker is not accepted), unknown,
undecodable and non-canonical keys 404, blank/oversized/control-character
titles 400, the rename
persists into the session file so the ``/api/sessions`` list index picks it
up, and shared-room sessions stay under the control plane's revision protocol
(409), never an owner split-brain write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services

OWNER_CHAT = "chat_owner"
ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
ROOM_ID = "room_" + "a" * 32
PARTICIPANT_ID = "participant_" + "b" * 32
OTHER_CHAT = "chat_other"


class _Headers(dict):
    """Minimal aiohttp-like headers: case-insensitive ``get``."""

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


class _Connection:
    remote_address = ("127.0.0.1", 41000)

    def respond(self, status: int, text: str) -> Any:
        return (status, text)


def _config() -> WebSocketConfig:
    return WebSocketConfig.model_validate(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": 18999,
            "path": "/ws",
            "websocketRequiresToken": False,
            "tokenIssueSecret": "tenant-issue-secret",
            "sharedRoomsEnabled": True,
        }
    )


@pytest.fixture
def env(tmp_path: Path) -> Any:
    """A channel wired like the gateway, plus one seeded owner conversation.

    The conversation carries a user message so the sidebar title is derived
    from its content; every rename assertion below is a transition *away* from
    that derived title, so a pass cannot be a no-op.
    """
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    session.add_message("user", "where should we go?")
    session.add_message("assistant", "Lisbon")
    sessions.save(session, fsync=True)
    other = sessions.get_or_create(f"websocket:{OTHER_CHAT}")
    other.add_message("user", "unrelated note")
    sessions.save(other, fsync=True)

    config = _config()
    bus = MessageBus()
    gateway = build_gateway_services(
        config=config,
        bus=bus,
        session_manager=sessions,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(config, bus, gateway=gateway)
    token = channel.gateway.http.tokens.issue_api_token(60)
    return channel, sessions, token


def _request(
    path: str,
    *,
    method: str = "POST",
    body: Any = None,
    token: str | None = None,
) -> TransportRequest:
    headers = _Headers()
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return TransportRequest(
        method=method,
        path=path,
        headers=headers,
        body=json.dumps(body).encode() if body is not None else b"",
        raw_path=path,
    )


def _title_path(key: str) -> str:
    # The canonical spelling: the session key percent-encoded with no safe
    # characters, as production's gate demands (the control plane's route
    # builder quotes the id). The handler 404s any other spelling.
    return f"/api/sessions/{quote(key, safe='')}/title"


def _post_title(channel: WebSocketChannel, key: str, body: Any, token: str | None):
    return channel._dispatch_http(
        _Connection(),
        _request(_title_path(key), body=body, token=token),
    )


def _body(response: Any) -> dict[str, Any]:
    assert response is not None
    return json.loads(bytes(response.body).decode())


def _text(response: Any) -> str:
    assert response is not None
    return bytes(response.body).decode()


# ---------------------------------------------------------------------------
# Happy path + persistence into the session list index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_rename_round_trips_and_persists_to_list(env: Any) -> None:
    """Acceptance (1): owner POSTs {"title": "Trip plan"} -> 200; the
    session list shows the new title (rename flow end-to-end)."""
    channel, sessions, token = env
    key = f"websocket:{OWNER_CHAT}"

    # Before: the row is titleless (the sidebar would label it from the
    # first user message; the rename must replace that fallback, not sit
    # unused in the file).
    listing = await channel._dispatch_http(
        _Connection(), _request("/api/sessions", method="GET", token=token)
    )
    assert listing.status_code == 200
    row = next(item for item in _body(listing)["sessions"] if item["key"] == key)
    assert row["title"] == ""
    assert row["preview"] == "where should we go?"

    response = await _post_title(channel, key, {"title": " Trip plan "}, token)
    assert response.status_code == 200
    # Shape from production's handler: the trimmed title is echoed back.
    assert _body(response) == {"session_key": key, "title": "Trip plan"}

    # The rename must be visible on the very next list read -- no restart,
    # no manual cache kick (the index re-scans because the manager rewrote
    # the file; a stale cache here is the bug this pins).
    listing = await channel._dispatch_http(
        _Connection(), _request("/api/sessions", method="GET", token=token)
    )
    row = next(item for item in _body(listing)["sessions"] if item["key"] == key)
    assert row["title"] == "Trip plan"

    # Persisted in the session file itself (not only in the index), with the
    # same user-explicit marker the ported manager contract documents -- an
    # explicit rename must survive reboots and re-derives, per
    # ``set_session_title``'s "clients must not substitute a message preview"
    # note.
    persisted = sessions.read_session_file(key)
    assert persisted is not None
    assert persisted["metadata"]["title"] == "Trip plan"
    assert persisted["metadata"]["title_user_defined"] is True


# ---------------------------------------------------------------------------
# Title validation (400) -- and nothing is written on rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_title_is_rejected(env: Any) -> None:
    """Acceptance (2): empty title -> 400, prod's error body shape."""
    channel, sessions, token = env
    key = f"websocket:{OWNER_CHAT}"
    response = await _post_title(channel, key, {"title": ""}, token)
    assert response.status_code == 400
    assert _text(response) == "invalid title"
    # Rejected before the write: the stored title is untouched.
    persisted = sessions.read_session_file(key)
    assert persisted is not None
    assert "title" not in (persisted.get("metadata") or {})


@pytest.mark.asyncio
async def test_whitespace_only_title_is_rejected(env: Any) -> None:
    channel, _, token = env
    response = await _post_title(
        channel, f"websocket:{OWNER_CHAT}", {"title": "   "}, token
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_non_string_and_missing_title_are_rejected(env: Any) -> None:
    channel, _, token = env
    key = f"websocket:{OWNER_CHAT}"
    for body in ({"title": 123}, {"title": None}, {}, {"name": "x"}):
        response = await _post_title(channel, key, body, token)
        assert response.status_code == 400, body


@pytest.mark.asyncio
async def test_oversized_title_is_rejected(env: Any) -> None:
    """The cap prod enforces (1..120 after trim) is a route contract too."""
    channel, _, token = env
    key = f"websocket:{OWNER_CHAT}"
    response = await _post_title(channel, key, {"title": "x" * 121}, token)
    assert response.status_code == 400
    # Exactly at the cap is accepted -- proves the bound is 120, not "any".
    ok = "x" * 120
    response = await _post_title(channel, key, {"title": f"  {ok}  "}, token)
    assert response.status_code == 200
    assert _body(response) == {"session_key": key, "title": ok}


@pytest.mark.asyncio
async def test_control_characters_in_title_are_rejected(env: Any) -> None:
    """Parity with prod's Cc rejection: a control-char title never persists."""
    channel, sessions, token = env
    key = f"websocket:{OWNER_CHAT}"
    for title in ("line\x01break", "bell\x07", "nul\x00end"):
        response = await _post_title(channel, key, {"title": title}, token)
        assert response.status_code == 400, repr(title)
    persisted = sessions.read_session_file(key)
    assert persisted is not None
    assert "title" not in (persisted.get("metadata") or {})


@pytest.mark.asyncio
async def test_malformed_body_is_rejected_without_write(env: Any) -> None:
    """Garbage bytes -> 400 via the shared request_json gate; no write."""
    channel, sessions, token = env
    key = f"websocket:{OWNER_CHAT}"
    request = _request(_title_path(key), token=token)
    request.body = b"{not json"
    response = await channel._dispatch_http(_Connection(), request)
    assert response.status_code == 400
    persisted = sessions.read_session_file(key)
    assert persisted is not None
    assert "title" not in (persisted.get("metadata") or {})


# ---------------------------------------------------------------------------
# Unknown keys (404) and the key-scheme gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_key_returns_404(env: Any) -> None:
    """Acceptance (3): key well-formed but never created -> 404.

    Same answer as a non-WebUI key: an absent session and a key the route
    does not own are indistinguishable from outside, and neither may be
    written (the probe must not create a file -- asserted in the unknown-
    key variant test).
    """
    channel, sessions, token = env
    response = await _post_title(
        channel, "websocket:tab-does-not-exist", {"title": "Ghost"}, token
    )
    assert response.status_code == 404
    assert _text(response) == "session not found"
    assert sessions.read_session_file("websocket:tab-does-not-exist") is None


@pytest.mark.asyncio
async def test_non_websocket_key_cannot_be_renamed_via_this_route(env: Any) -> None:
    """A key from another channel's namespace must 404, like production."""
    channel, sessions, token = env
    persisted_before = sessions.read_session_file(f"websocket:{OWNER_CHAT}")
    response = await _post_title(channel, "signal:1234", {"title": "Cross-talk"}, token)
    assert response.status_code == 404
    # The probe neither created anything nor touched the real session.
    assert sessions.read_session_file("signal:1234") is None
    assert sessions.read_session_file(f"websocket:{OWNER_CHAT}") == persisted_before


# ---------------------------------------------------------------------------
# Authorization (401) -- room credentials are not owner credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_token_is_unauthorized(env: Any) -> None:
    """Acceptance (4a): no token -> 401, prod's error body shape."""
    channel, sessions, _ = env
    key = f"websocket:{OWNER_CHAT}"
    response = await _post_title(channel, key, {"title": "Sneaky"}, token=None)
    assert response.status_code == 401
    assert _text(response) == "Unauthorized"
    persisted = sessions.read_session_file(key)
    assert persisted is not None
    assert "title" not in (persisted.get("metadata") or {})


@pytest.mark.asyncio
async def test_room_credential_is_refused(env: Any) -> None:
    """Acceptance (4b): a room token is not an owner credential.

    Room credentials live in ``SharedRoomStore._api_tokens`` and are validated
    by ``check_api_token`` only for room-scoped paths; for the owner
    ``/api/sessions/...`` namespace the shared token check must refuse them,
    so a guest can never reach a mutation route that rewrites a session file.
    """
    channel, sessions, _ = env
    assert channel.rooms is not None
    room_token, _credential = channel.rooms.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id=PARTICIPANT_ID,
        display_name="Guest",
        role="participant",
    )
    key = f"websocket:{OWNER_CHAT}"
    response = await _post_title(channel, key, {"title": "Guest room"}, token=room_token)
    assert response.status_code == 401
    persisted = sessions.read_session_file(key)
    assert persisted is not None
    assert "title" not in (persisted.get("metadata") or {})


# ---------------------------------------------------------------------------
# Shared-room sessions stay under the control plane (409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_room_session_rename_is_refused(env: Any) -> None:
    """An owner rename must not split brain against a co-authored room.

    The room's title belongs to the control plane's revision-mirrored
    protocol (the ported manager refuses ``shared``; the room routes call
    ``set_session_title`` with a ``room_id`` + revision).  Refused before any
    write, so the room's stored title is unchanged.
    """
    channel, sessions, token = env
    assert channel.rooms is not None
    room_key = f"websocket:{ROOM_CHAT}"
    room = sessions.get_or_create(room_key)
    room.add_message("user", "room question")
    room.metadata.update(
        {"shared_room": True, "room_id": ROOM_ID, "title": "Room title", "title_user_edited": True}
    )
    sessions.save(room, fsync=True)

    response = await _post_title(channel, room_key, {"title": "Owner hijack"}, token)
    assert response.status_code == 409
    persisted = sessions.read_session_file(room_key)
    assert persisted is not None
    assert persisted["metadata"]["title"] == "Room title"


# ---------------------------------------------------------------------------
# Method + sibling-route isolation (negative controls)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_is_not_allowed_on_the_title_route(env: Any) -> None:
    """Only POST renames (prod dispatch checked ``method != POST`` -> 405)."""
    channel, _, token = env
    response = await channel._dispatch_http(
        _Connection(),
        _request(_title_path(f"websocket:{OWNER_CHAT}"), method="GET", token=token),
    )
    assert response.status_code == 405


@pytest.mark.asyncio
async def test_renaming_one_session_leaves_the_other_untouched(env: Any) -> None:
    """Negative control: the route writes exactly the addressed file.

    A bug that globbed the sessions dir (or wrote a second file) would show
    up as the untouched sibling's title changing; both siblings are seeded
    and only one is addressed.
    """
    channel, sessions, token = env
    target = f"websocket:{OWNER_CHAT}"
    sibling = f"websocket:{OTHER_CHAT}"

    response = await _post_title(channel, target, {"title": "Renamed"}, token)
    assert response.status_code == 200

    persisted = sessions.read_session_file(target)
    assert persisted is not None
    assert persisted["metadata"]["title"] == "Renamed"
    sibling_persisted = sessions.read_session_file(sibling)
    assert sibling_persisted is not None
    assert "title" not in (sibling_persisted.get("metadata") or {})

    listing = await channel._dispatch_http(
        _Connection(), _request("/api/sessions", method="GET", token=token)
    )
    rows = {item["key"]: item for item in _body(listing)["sessions"]}
    assert rows[target]["title"] == "Renamed"
    assert rows[sibling]["title"] == ""
    assert rows[sibling]["preview"] == "unrelated note"


# ---------------------------------------------------------------------------
# Trusted-proxy marker is not an owner credential (PR #76 review, blocking)
# ---------------------------------------------------------------------------

PROXY_ASSERTION_HEADER = "X-Ziggy-Proxy-Assertion"


@pytest.fixture
def proxied_env(tmp_path: Path) -> Any:
    """The gateway fronted by a trusted proxy (as ziggy-control fronts it).

    ``dispatch`` stamps ``_nanobot_trusted_proxy_authenticated`` from the
    peer address plus a non-empty assertion header alone, so a proxied room
    guest (or anything else the proxy forwards) carries the mark without ever
    presenting the owner API token.
    """
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    session.add_message("user", "where should we go?")
    sessions.save(session, fsync=True)

    config = WebSocketConfig.model_validate(
        {
            **_config().model_dump(by_alias=True),
            "trustedProxyAuth": {
                "trustedPeerCidrs": ["127.0.0.1/32"],
                "assertionHeader": PROXY_ASSERTION_HEADER,
            },
        }
    )
    bus = MessageBus()
    gateway = build_gateway_services(
        config=config,
        bus=bus,
        session_manager=sessions,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(config, bus, gateway=gateway)
    token = channel.gateway.http.tokens.issue_api_token(60)
    return channel, sessions, token


@pytest.mark.asyncio
async def test_a_trusted_proxy_request_is_not_an_owner_credential_for_rename(
    proxied_env: Any,
) -> None:
    """Regression (PR #76 review, blocking).

    ``WebUIHTTPRouter.check_api_token`` returns True for any request the
    trusted proxy vouched for. The rename route must demand the owner API
    token itself (``tokens.check_api_token``), as ``/api/work`` and
    ``/api/sessions/<key>/messages`` do, or a proxied request with no bearer
    can rename any owner conversation.
    """
    channel, sessions, token = proxied_env
    key = f"websocket:{OWNER_CHAT}"

    proxied = _request(_title_path(key), body={"title": "Proxy hijack"})
    proxied.headers[PROXY_ASSERTION_HEADER] = "room-guest"
    response = await channel._dispatch_http(_Connection(), proxied)
    # Non-vacuity: dispatch really stamped the proxy mark on this request.
    assert getattr(proxied, "_nanobot_trusted_proxy_authenticated", False) is True
    assert response.status_code == 401
    assert _text(response) == "Unauthorized"
    persisted = sessions.read_session_file(key)
    assert persisted is not None
    assert "title" not in (persisted.get("metadata") or {})

    # Non-vacuity: the owner bearer still renames through the same proxy.
    owner = _request(_title_path(key), body={"title": "Owner rename"}, token=token)
    owner.headers[PROXY_ASSERTION_HEADER] = "owner"
    response = await channel._dispatch_http(_Connection(), owner)
    assert response.status_code == 200
    assert _body(response) == {"session_key": key, "title": "Owner rename"}


# ---------------------------------------------------------------------------
# Key gate parity with production: malformed / non-canonical keys -> 404
# ---------------------------------------------------------------------------


async def _post_raw(
    channel: WebSocketChannel,
    path: str,
    token: str,
    *,
    raw_path: str | None = None,
) -> Any:
    request = _request(path, body={"title": "Aliased"}, token=token)
    request.raw_path = raw_path if raw_path is not None else path
    return await channel._dispatch_http(_Connection(), request)


def _owner_title_untouched(sessions: SessionManager) -> bool:
    persisted = sessions.read_session_file(f"websocket:{OWNER_CHAT}")
    return persisted is not None and "title" not in (persisted.get("metadata") or {})


@pytest.mark.asyncio
async def test_undecodable_key_returns_404_like_production(env: Any) -> None:
    """A key that does not decode to a legal session id is 'session not found'
    (production folds it into the one 404), never a distinguishable 400."""
    channel, sessions, token = env
    for raw_key in ("bad%20key", "%ZZ", "a%2Fb", "x" * 129):
        response = await _post_raw(channel, f"/api/sessions/{raw_key}/title", token)
        assert response.status_code == 404, raw_key
        assert _text(response) == "session not found"
    assert _owner_title_untouched(sessions)


@pytest.mark.asyncio
async def test_non_canonical_key_spelling_returns_404(env: Any) -> None:
    """``websocket:chat`` unencoded is an alias of the canonical
    ``websocket%3Achat``; production refuses it so URL normalization never
    turns an alternate spelling into a valid session identifier."""
    channel, sessions, token = env
    literal = f"/api/sessions/websocket:{OWNER_CHAT}/title"
    response = await _post_raw(channel, literal, token)
    assert response.status_code == 404
    assert _text(response) == "session not found"
    # Lower-case percent escape: decodes to the same key, still not canonical.
    lower = f"/api/sessions/websocket%3a{OWNER_CHAT}/title"
    response = await _post_raw(channel, lower, token)
    assert response.status_code == 404
    assert _owner_title_untouched(sessions)


@pytest.mark.asyncio
async def test_raw_target_must_match_the_canonical_path(env: Any) -> None:
    """The routed path can be canonical while the original request target is
    not (a trailing slash or query the parser dropped); production compares
    the raw target too."""
    channel, sessions, token = env
    canonical = _title_path(f"websocket:{OWNER_CHAT}")
    for raw_path in (canonical + "/", canonical + "?x=1"):
        response = await _post_raw(channel, canonical, token, raw_path=raw_path)
        assert response.status_code == 404, raw_path
    assert _owner_title_untouched(sessions)


@pytest.mark.asyncio
async def test_canonical_encoded_key_is_still_renamed(env: Any) -> None:
    """Positive control for the gate: ``websocket%3A<chat>`` renames (200)."""
    channel, sessions, token = env
    key = f"websocket:{OWNER_CHAT}"
    canonical = f"/api/sessions/websocket%3A{OWNER_CHAT}/title"
    assert canonical == _title_path(key)
    response = await _post_raw(channel, canonical, token)
    assert response.status_code == 200
    assert _body(response) == {"session_key": key, "title": "Aliased"}
    persisted = sessions.read_session_file(key)
    assert persisted is not None
    assert persisted["metadata"]["title"] == "Aliased"
