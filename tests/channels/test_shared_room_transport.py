"""The aiohttp transport and its wiring into the real HTTP handler (MIT-1010).

Upstream's ``websockets`` listener cannot read an HTTP request body, so the
shared-room control plane -- four JSON POSTs from ``ziggy-control`` -- is served
over the fork's aiohttp transport instead.  These tests exercise the *real*
``GatewayHTTPHandler.dispatch`` with the transport's request object rather than
a stand-in, because the seam between them is where the interesting breakage
lives: ``dispatch`` stamps attributes onto the request with ``setattr``, which a
slotted dataclass silently turns into a 500 for every HTTP route.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services

SECRET = "tenant-issue-secret"
OWNER_CHAT = "chat_owner"
ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
ROOM_ID = "room_" + "a" * 32


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


def _config(**kw: Any) -> WebSocketConfig:
    payload: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 18999,
        "path": "/ws",
        "websocketRequiresToken": False,
        "tokenIssueSecret": SECRET,
        "sharedRoomsEnabled": True,
    }
    payload.update(kw)
    return WebSocketConfig.model_validate(payload)


@pytest.fixture
def channel(tmp_path: Path) -> WebSocketChannel:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    session.add_message("user", "private question")
    sessions.save(session, fsync=True)

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
    return WebSocketChannel(config, bus, gateway=gateway)


def _request(path: str, *, method: str = "POST", body: Any = None) -> TransportRequest:
    return TransportRequest(
        method=method,
        path=path,
        headers=_Headers({"Authorization": f"Bearer {SECRET}"}),
        body=json.dumps(body).encode() if body is not None else b"",
        raw_path=path,
    )


# --------------------------------------------------------------------------
# Transport selection
# --------------------------------------------------------------------------


def test_shared_rooms_force_the_body_capable_transport() -> None:
    assert _config().transport == "aiohttp"
    assert _config(sharedRoomsEnabled=False, sharedRoomCollaborationEnabled=True).transport == (
        "aiohttp"
    )


def test_transport_defaults_to_upstreams_listener() -> None:
    plain = WebSocketConfig.model_validate({"host": "127.0.0.1", "port": 18999})
    assert plain.transport == "websockets"


def test_a_runtime_without_rooms_has_no_room_routes(tmp_path: Path) -> None:
    config = _config(sharedRoomsEnabled=False)
    bus = MessageBus()
    gateway = build_gateway_services(
        config=config,
        bus=bus,
        session_manager=SessionManager(tmp_path),
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(config, bus, gateway=gateway)
    assert channel.rooms is None
    assert gateway.http.shared_rooms is None


# --------------------------------------------------------------------------
# The transport <-> GatewayHTTPHandler seam
# --------------------------------------------------------------------------


def test_transport_request_accepts_the_attributes_dispatch_stamps() -> None:
    """Regression: ``slots=True`` here makes every HTTP route 500.

    ``GatewayHTTPHandler.dispatch`` does
    ``setattr(request, "_nanobot_trusted_proxy_authenticated", ...)`` on every
    request (webui/ws_http.py). A slotted dataclass raises ``AttributeError``.
    """
    request = _request("/ws")
    setattr(request, "_nanobot_trusted_proxy_authenticated", True)
    setattr(request, "_nanobot_webui_mutation_payload", {"a": 1})
    assert getattr(request, "_nanobot_trusted_proxy_authenticated") is True


@pytest.mark.asyncio
async def test_room_create_round_trips_through_the_real_http_handler(
    channel: WebSocketChannel,
) -> None:
    """End-to-end over ``_dispatch_http``, the path the aiohttp transport uses."""
    response = await channel._dispatch_http(
        _Connection(),
        _request(
            "/auth/shared-rooms",
            body={
                "source_session_key": f"websocket:{OWNER_CHAT}",
                "chat_id": ROOM_CHAT,
                "room_id": ROOM_ID,
                "mode": "legacy",
                "snapshot_message_count": None,
                "snapshot_sha256": "",
                "selected_results": None,
                "expires_at": None,
                "title": "Shared conversation",
                "owner_display_name": "Mihai",
            },
        ),
    )
    assert response is not None
    assert response.status_code == 201
    payload = json.loads(bytes(response.body).decode())
    assert payload["session_key"] == f"websocket:{ROOM_CHAT}"
    assert channel.rooms is not None
    assert channel.rooms.is_shared_room(ROOM_CHAT)


@pytest.mark.asyncio
async def test_unknown_api_route_still_404s_through_the_transport(
    channel: WebSocketChannel,
) -> None:
    response = await channel._dispatch_http(
        _Connection(),
        _request("/api/definitely-not-a-route", method="GET"),
    )
    assert response is not None
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_websocket_upgrade_path_falls_through_to_the_handshake(
    channel: WebSocketChannel,
) -> None:
    """A non-room GET on a normal path must not be swallowed by the room router."""
    response = await channel._dispatch_http(
        _Connection(),
        _request("/api/sessions", method="GET"),
    )
    # Either a real response or None (fall through), but never an exception.
    assert response is None or hasattr(response, "status_code")


def test_check_api_token_shim_reaches_the_http_handler(channel: WebSocketChannel) -> None:
    """The transport's pre-body auth gate must resolve on the channel."""
    assert channel.check_api_token(_request("/api/x", method="POST")) is False


# --------------------------------------------------------------------------
# Handshake
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_room_token_binds_the_connection_at_handshake(
    channel: WebSocketChannel,
) -> None:
    await channel._dispatch_http(
        _Connection(),
        _request(
            "/auth/shared-rooms",
            body={
                "source_session_key": f"websocket:{OWNER_CHAT}",
                "chat_id": ROOM_CHAT,
                "room_id": ROOM_ID,
                "title": "Shared conversation",
                "owner_display_name": "Mihai",
            },
        ),
    )
    assert channel.rooms is not None
    token, _ = channel.rooms.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id="participant_" + "b" * 32,
        display_name="Guest",
        role="contributor",
    )
    connection = _Connection()
    assert channel._authorize_websocket_handshake(connection, {"token": [token]}) is None
    credential = channel.room_credential(connection)
    assert credential is not None
    assert credential.chat_id == ROOM_CHAT
    # A guest socket opens straight into its room, not a fresh chat.
    assert credential.chat_id != str(channel.config.port)


@pytest.mark.asyncio
async def test_a_bogus_room_token_does_not_bind(channel: WebSocketChannel) -> None:
    connection = _Connection()
    channel._authorize_websocket_handshake(connection, {"token": ["nbrt_nope"]})
    assert channel.room_credential(connection) is None


# --------------------------------------------------------------------------
# Frame-shape bypass (security review, CRITICAL B)
# --------------------------------------------------------------------------


class _LoopConnection(_Connection):
    """A connection that replays a fixed set of frames, then closes."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self.sent: list[str] = []
        self.request = None

    def __aiter__(self):
        async def gen():
            for frame in self._frames:
                yield frame

        return gen()

    async def send(self, data: str | bytes) -> None:
        self.sent.append(data if isinstance(data, str) else data.decode())


@pytest.mark.asyncio
async def test_a_room_connection_cannot_use_the_legacy_untyped_frame_path(
    channel: WebSocketChannel,
) -> None:
    """A guest must not escape room scoping by omitting ``type`` from its frame.

    The legacy path dispatches straight to ``BaseChannel._handle_message`` with
    a fresh chat_id and hand-built metadata, so it carries no room scope and
    every room-denied tool would be allowed. Found by security review.
    """
    await channel._dispatch_http(
        _Connection(),
        _request(
            "/auth/shared-rooms",
            body={
                "source_session_key": f"websocket:{OWNER_CHAT}",
                "chat_id": ROOM_CHAT,
                "room_id": ROOM_ID,
                "title": "Shared conversation",
                "owner_display_name": "Mihai",
            },
        ),
    )
    assert channel.rooms is not None
    token, _ = channel.rooms.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id="participant_" + "c" * 32,
        display_name="Guest",
        role="contributor",
    )
    connection = _LoopConnection(
        [
            "just plain text",
            json.dumps({"content": "json without a type field"}),
        ]
    )
    assert channel._authorize_websocket_handshake(connection, {"token": [token]}) is None

    dispatched: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        dispatched.append(kwargs)

    channel._handle_message = _record  # type: ignore[assignment]
    await channel._connection_loop(connection)

    assert dispatched == [], "a room connection must not reach the legacy path"
    errors = [json.loads(frame) for frame in connection.sent]
    errors = [e for e in errors if e.get("event") == "error"]
    assert len(errors) == 2
    assert all("typed envelopes" in e["detail"] for e in errors)


@pytest.mark.asyncio
async def test_a_normal_connection_still_uses_the_legacy_frame_path(
    channel: WebSocketChannel,
) -> None:
    """The bypass fix must not break ordinary non-room clients."""
    connection = _LoopConnection(["hello from a plain client"])
    dispatched: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        dispatched.append(kwargs)

    channel._handle_message = _record  # type: ignore[assignment]
    await channel._connection_loop(connection)

    assert len(dispatched) == 1
    assert dispatched[0]["content"] == "hello from a plain client"


# --------------------------------------------------------------------------
# Pre-buffer auth gate (security review, HIGH DoS)
# --------------------------------------------------------------------------


def test_auth_routes_are_gated_before_the_body_is_buffered(
    channel: WebSocketChannel,
) -> None:
    """``/auth/*`` POST bodies must not be buffered for an unauthenticated caller."""
    good = TransportRequest(
        method="POST",
        path="/auth/shared-rooms",
        headers=_Headers({"Authorization": f"Bearer {SECRET}"}),
    )
    bad = TransportRequest(
        method="POST",
        path="/auth/shared-rooms",
        headers=_Headers({"Authorization": "Bearer nope"}),
    )
    none = TransportRequest(method="POST", path="/auth/shared-rooms", headers=_Headers({}))
    assert channel.check_issue_route_secret(good) is True
    assert channel.check_issue_route_secret(bad) is False
    assert channel.check_issue_route_secret(none) is False


def test_the_pre_buffer_gate_fails_closed_without_a_configured_secret(
    tmp_path: Path,
) -> None:
    """``issue_route_secret_matches`` returns True for an empty secret; the
    gate must not inherit that fail-open."""
    config = _config(tokenIssueSecret="")
    bus = MessageBus()
    gateway = build_gateway_services(
        config=config,
        bus=bus,
        session_manager=SessionManager(tmp_path),
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(config, bus, gateway=gateway)
    request = TransportRequest(method="POST", path="/auth/shared-rooms", headers=_Headers({}))
    assert channel.check_issue_route_secret(request) is False


# --------------------------------------------------------------------------
# Handshake wiring (tech-lead review, P0-1)
# --------------------------------------------------------------------------


def test_the_room_token_check_is_wired_into_the_endpoint(
    channel: WebSocketChannel,
) -> None:
    """The listener calls the *endpoint's* handshake, not the channel's.

    A room-token check that lives only on the channel override is dead code and
    every guest is 401'd, because websocketRequiresToken defaults True.
    """
    assert channel.gateway.endpoint.room_token_consumer is not None
    assert channel.rooms is not None
    assert channel.gateway.endpoint.room_token_consumer == channel.rooms.consume_ws_token


@pytest.mark.asyncio
async def test_a_guest_token_authorizes_through_the_real_listener_path(
    channel: WebSocketChannel,
) -> None:
    """Drive ``authorize_websocket_handshake`` -- what ``process_request`` calls."""
    await channel._dispatch_http(
        _Connection(),
        _request(
            "/auth/shared-rooms",
            body={
                "source_session_key": f"websocket:{OWNER_CHAT}",
                "chat_id": ROOM_CHAT,
                "room_id": ROOM_ID,
                "title": "Shared conversation",
                "owner_display_name": "Mihai",
            },
        ),
    )
    assert channel.rooms is not None
    token, _ = channel.rooms.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id="participant_" + "d" * 32,
        display_name="Guest",
        role="contributor",
    )
    connection = _Connection()
    result = channel.gateway.endpoint.authorize_websocket_handshake(
        connection,
        {"token": [token]},
        None,
    )
    assert result is None, "a valid room token must authorize the upgrade"
    assert channel.room_credential(connection) is not None
    # A room guest must never be promoted to a trusted WebUI connection.
    assert not channel.gateway.endpoint.is_webui_connection(connection)


def test_websocket_requires_token_would_reject_a_guest_without_the_wiring(
    tmp_path: Path,
) -> None:
    """The condition that made the dead-code bug fatal, pinned.

    Production leaves ``websocketRequiresToken`` at its ``True`` default and
    ``token`` empty, so an unrecognised token is a 401 -- which is what a guest
    got while the room check sat on the unused channel override.
    """
    config = _config(websocketRequiresToken=True)
    bus = MessageBus()
    gateway = build_gateway_services(
        config=config,
        bus=bus,
        session_manager=SessionManager(tmp_path),
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(config, bus, gateway=gateway)
    assert channel.config.websocket_requires_token is True
    assert channel.config.token.strip() == ""
    response = channel.gateway.endpoint.authorize_websocket_handshake(
        _Connection(),
        {"token": ["nbrt_not_a_real_token"]},
        None,
    )
    assert response is not None  # 401


# --------------------------------------------------------------------------
# Tenant config compatibility (tech-lead review, P0-6)
# --------------------------------------------------------------------------


def test_null_ping_interval_from_provision_tenant_still_validates() -> None:
    """All four live tenant configs carry ``"pingIntervalS": null``."""
    parsed = WebSocketConfig.model_validate({"pingIntervalS": None, "pingTimeoutS": None})
    assert parsed.ping_interval_s is None
    assert parsed.ping_timeout_s is None


def test_ping_bounds_still_apply_when_a_value_is_given() -> None:
    with pytest.raises(Exception):
        WebSocketConfig.model_validate({"pingIntervalS": 1})


# --------------------------------------------------------------------------
# Guest command confinement (tech-lead review, P0-2)
# --------------------------------------------------------------------------


async def _room_guest(channel: WebSocketChannel) -> Any:
    await channel._dispatch_http(
        _Connection(),
        _request(
            "/auth/shared-rooms",
            body={
                "source_session_key": f"websocket:{OWNER_CHAT}",
                "chat_id": ROOM_CHAT,
                "room_id": ROOM_ID,
                "title": "Shared conversation",
                "owner_display_name": "Mihai",
            },
        ),
    )
    assert channel.rooms is not None
    token, _ = channel.rooms.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id="participant_" + "e" * 32,
        display_name="Guest",
        role="contributor",
    )
    connection = _Connection()
    assert channel.gateway.endpoint.authorize_websocket_handshake(
        connection, {"token": [token]}, None
    ) is None
    return connection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "envelope",
    [
        {"type": "new_chat"},
        {"type": "fork_chat", "chat_id": f"websocket:{OWNER_CHAT}"},
        {"type": "attach", "chat_id": "22222222-3333-4444-5555-666666666666"},
        {"type": "set_workspace_scope"},
        {"type": "set_sidebar_state"},
        {"type": "webui_request", "action": "session.delete"},
        {"type": "new_temporary_chat"},
    ],
    ids=lambda e: e["type"] if isinstance(e, dict) else str(e),
)
async def test_a_guest_cannot_send_owner_commands(
    channel: WebSocketChannel,
    envelope: dict[str, Any],
) -> None:
    """new_chat / fork_chat / attach-elsewhere hydrate arbitrary sessions onto
    the caller's socket. A guest must reach none of them."""
    connection = await _room_guest(channel)
    sent: list[dict[str, Any]] = []

    async def _capture(conn: Any, event: str, **fields: Any) -> None:
        sent.append({"event": event, **fields})

    channel.webui_send_event = _capture  # type: ignore[assignment]
    await channel._commands.dispatch(connection, "client-1", envelope)

    assert sent, f"{envelope['type']} produced no rejection"
    assert sent[0]["event"] == "error"
    assert sent[0]["detail"] == "room scope violation"


@pytest.mark.asyncio
async def test_a_guest_may_attach_to_its_own_room(channel: WebSocketChannel) -> None:
    connection = await _room_guest(channel)
    sent: list[dict[str, Any]] = []

    async def _capture(conn: Any, event: str, **fields: Any) -> None:
        sent.append({"event": event, **fields})

    channel.webui_send_event = _capture  # type: ignore[assignment]
    await channel._commands.dispatch(
        connection,
        "client-1",
        {"type": "attach", "chat_id": ROOM_CHAT},
    )
    assert [e["event"] for e in sent] == ["attached"]
    assert sent[0]["chat_id"] == ROOM_CHAT


@pytest.mark.asyncio
async def test_a_normal_connection_keeps_every_command(
    channel: WebSocketChannel,
) -> None:
    """The confinement must not touch owner/WebUI sockets."""
    sent: list[dict[str, Any]] = []

    async def _capture(conn: Any, event: str, **fields: Any) -> None:
        sent.append({"event": event, **fields})

    channel.webui_send_event = _capture  # type: ignore[assignment]
    await channel._commands.dispatch(_Connection(), "client-1", {"type": "new_chat"})
    assert [e["event"] for e in sent][:1] == ["attached"]
