"""Owner-bearer ``/api/sessions/<key>/delete`` over plain HTTP (MIT-1425).

Since 5d733b1c every WebUI mutation on the 0.3.0 line must arrive over the
authenticated WebSocket, and the bare HTTP route answered 405. The iOS app
(``ZiggyRESTClient.deleteSession``, ``POST``) and the Ziggy web client
(``web/src/lib/api.ts`` ``deleteSession``, ``GET``) still delete over plain
HTTP with the owner API bearer and decode ``{"deleted": bool}``.

Owner decision (2026-09-24): allow this one route over HTTP **only** with the
owner API token (``tokens.check_api_token``, never the trusted-proxy marker),
keep the MIT-1416 active-turn 409 and the automation-consent response by
running the same handler as the WS ``session.delete`` mutation, refuse room
credentials, and leave every other WebUI mutation WS-only (405).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.session.manager import SessionManager
from nanobot.session.webui_turns import (
    clear_websocket_turn_if_current,
    register_queued_websocket_turn_if_idle,
    websocket_turn_wall_started_at,
)
from nanobot.webui.gateway_services import build_gateway_services

PROXY_ASSERTION_HEADER = "X-Ziggy-Proxy-Assertion"
ROOM_ID = "room_" + "a" * 32
ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
PARTICIPANT_ID = "participant_" + "b" * 32


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


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[WebSocketChannel, SessionManager, str, str]]:
    """A proxied gateway (as ziggy-control fronts it) with one owner session."""
    sessions = SessionManager(tmp_path / "ws")
    chat_id = f"del-{uuid.uuid4().hex[:12]}"
    key = f"websocket:{chat_id}"
    session = sessions.get_or_create(key)
    session.add_message("user", "hello")
    session.add_message("assistant", "hi")
    sessions.save(session, fsync=True)

    config = WebSocketConfig.model_validate(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": 18999,
            "path": "/ws",
            "websocketRequiresToken": False,
            "tokenIssueSecret": "tenant-issue-secret",
            "sharedRoomsEnabled": True,
            "trustedProxyAuth": {
                "trustedPeerCidrs": ["127.0.0.1/32"],
                "assertionHeader": PROXY_ASSERTION_HEADER,
            },
        }
    )
    gateway = build_gateway_services(
        config=config,
        bus=MessageBus(),
        session_manager=sessions,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(config, MessageBus(), gateway=gateway)
    token = channel.gateway.http.tokens.issue_api_token(60)
    yield channel, sessions, token, key


def _request(
    path: str,
    *,
    method: str = "POST",
    token: str | None = None,
    proxied: bool = False,
) -> TransportRequest:
    headers = _Headers()
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if proxied:
        headers[PROXY_ASSERTION_HEADER] = "vouched"
    return TransportRequest(method=method, path=path, headers=headers, body=b"", raw_path=path)


def _delete_path(key: str) -> str:
    return f"/api/sessions/{quote(key, safe='')}/delete"


async def _dispatch(channel: WebSocketChannel, request: TransportRequest) -> Any:
    return await channel._dispatch_http(_Connection(), request)


def _body(response: Any) -> Any:
    return json.loads(bytes(response.body).decode())


# -- Case 1: owner bearer deletes an idle session ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "GET"])
async def test_owner_bearer_deletes_idle_session(env: Any, method: str) -> None:
    """iOS sends POST, the web client GET; both get 200 ``{"deleted": true}``."""
    channel, sessions, token, key = env
    path = sessions._get_session_path(key)
    assert path.is_file()

    response = await _dispatch(channel, _request(_delete_path(key), method=method, token=token))

    assert response.status_code == 200, response.body
    assert _body(response) == {"deleted": True}
    assert not path.exists()


# -- Case 2: active turn -> 409 ---------------------------------------------


@pytest.mark.asyncio
async def test_owner_bearer_delete_during_active_turn_is_409(env: Any) -> None:
    channel, sessions, token, key = env
    chat_id = key.removeprefix("websocket:")
    owner = register_queued_websocket_turn_if_idle(chat_id, f"turn-{chat_id}")
    assert owner is not None
    try:
        response = await _dispatch(channel, _request(_delete_path(key), token=token))

        assert response.status_code == 409, response.body
        assert bytes(response.body) == b"conversation is active"
        assert sessions._get_session_path(key).is_file()
        assert websocket_turn_wall_started_at(chat_id) is not None
    finally:
        assert clear_websocket_turn_if_current(chat_id, owner) is True


# -- Case 3: no owner token -> 401 ------------------------------------------


@pytest.mark.asyncio
async def test_no_token_is_401(env: Any) -> None:
    channel, sessions, _, key = env

    response = await _dispatch(channel, _request(_delete_path(key)))

    assert response.status_code == 401, response.body
    assert sessions._get_session_path(key).is_file()


@pytest.mark.asyncio
async def test_trusted_proxy_without_owner_token_is_401(env: Any) -> None:
    """The trusted-proxy marker alone is not an owner credential."""
    channel, sessions, token, key = env

    proxied = _request(_delete_path(key), proxied=True)
    response = await _dispatch(channel, proxied)

    # Non-vacuity: dispatch really stamped the proxy mark on this request.
    assert getattr(proxied, "_nanobot_trusted_proxy_authenticated", False) is True
    assert response.status_code == 401, response.body
    assert sessions._get_session_path(key).is_file()

    # The owner bearer still deletes through the same proxy.
    owner = _request(_delete_path(key), token=token, proxied=True)
    response = await _dispatch(channel, owner)
    assert response.status_code == 200, response.body
    assert not sessions._get_session_path(key).exists()


@pytest.mark.asyncio
async def test_room_credential_is_401(env: Any) -> None:
    channel, sessions, _, key = env
    assert channel.rooms is not None
    room_token, _credential = channel.rooms.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id=PARTICIPANT_ID,
        display_name="Guest",
        role="participant",
    )

    response = await _dispatch(channel, _request(_delete_path(key), token=room_token))

    assert response.status_code == 401, response.body
    assert sessions._get_session_path(key).is_file()


# -- Case 4: every other WebUI mutation stays WS-only -----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/webui/automations/delete",
        "/api/webui/skills/delete",
        "/api/webui/sidebar-state/update",
        "/api/webui/recovery/dismiss",
    ],
)
async def test_other_mutations_over_http_are_still_405(env: Any, path: str) -> None:
    channel, _, token, _ = env

    response = await _dispatch(channel, _request(path, token=token))

    assert response.status_code == 405, response.body


@pytest.mark.asyncio
async def test_other_methods_on_delete_route_are_405(env: Any) -> None:
    channel, sessions, token, key = env

    response = await _dispatch(channel, _request(_delete_path(key), method="PUT", token=token))

    assert response.status_code == 405, response.body
    assert sessions._get_session_path(key).is_file()
