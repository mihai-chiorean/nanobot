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
