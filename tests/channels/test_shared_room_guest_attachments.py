"""No attachments in a shared room, from a guest or the owner (MIT-1399).

Production (``feat/shared-rooms``, ``nanobot/channels/websocket.py`` ~2762)
rejects any room message carrying ``media`` with ``attachment_rejected`` and
"Attachments are not available in shared rooms yet." On 0.3.0 the message path
in ``nanobot/webui/inbound_commands.py`` stored guest files in the owner's
media directory and fed them to the room turn. Production also rejected the
owner's own media inside a room (``room = scoped_room or owner credential``),
since an owner file is served to every guest as a signed media URL.
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
REJECTION_TEXT = "Attachments are not available in shared rooms yet."
# 1x1 transparent PNG.
PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class _Headers(dict):
    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


class _Connection:
    remote_address = ("127.0.0.1", 41000)

    def respond(self, status: int, text: str) -> Any:
        return (status, text)


def _request(path: str, body: Any) -> TransportRequest:
    return TransportRequest(
        method="POST",
        path=path,
        headers=_Headers({"Authorization": f"Bearer {SECRET}"}),
        body=json.dumps(body).encode(),
        raw_path=path,
    )


@pytest.fixture
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the owner's media directory at a tmp dir we can inspect."""
    root = tmp_path / "owner-media"

    def _media_dir(channel: str | None = None) -> Path:
        path = root / channel if channel else root
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr("nanobot.webui.media_gateway.get_media_dir", _media_dir)
    return root


@pytest.fixture
def channel(tmp_path: Path, media_root: Path) -> WebSocketChannel:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    session.add_message("user", "private question")
    session.add_message("assistant", "private answer")
    sessions.save(session, fsync=True)

    config = WebSocketConfig.model_validate({
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 18999,
        "path": "/ws",
        "websocketRequiresToken": False,
        "tokenIssueSecret": SECRET,
        "sharedRoomsEnabled": True,
    })
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


async def _room_guest(channel: WebSocketChannel) -> Any:
    await channel._dispatch_http(
        _Connection(),
        _request(
            "/auth/shared-rooms",
            {
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


def _stored_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def _capture(channel: WebSocketChannel) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def _send(conn: Any, event: str, **fields: Any) -> None:
        sent.append({"event": event, **fields})

    channel.webui_send_event = _send  # type: ignore[assignment]
    return sent


def _media() -> list[dict[str, Any]]:
    return [{"name": "pixel.png", "data_url": PNG_DATA_URL}]


@pytest.mark.asyncio
async def test_a_guest_attachment_is_rejected_and_never_stored(
    channel: WebSocketChannel,
    media_root: Path,
) -> None:
    connection = await _room_guest(channel)
    sent = _capture(channel)

    await channel._commands.dispatch(
        connection,
        "client-1",
        {
            "type": "message",
            "chat_id": ROOM_CHAT,
            "content": "look at this",
            "media": _media(),
        },
    )

    errors = [e for e in sent if e["event"] == "error"]
    assert len(errors) == 1, sent
    assert errors[0]["detail"] == "attachment_rejected"
    assert errors[0]["message"] == REJECTION_TEXT
    assert errors[0]["chat_id"] == ROOM_CHAT
    assert _stored_files(media_root) == []
    assert channel.bus.inbound_size == 0


@pytest.mark.asyncio
async def test_a_guest_message_without_media_is_a_normal_room_turn(
    channel: WebSocketChannel,
    media_root: Path,
) -> None:
    connection = await _room_guest(channel)
    sent = _capture(channel)

    await channel._commands.dispatch(
        connection,
        "client-1",
        {"type": "message", "chat_id": ROOM_CHAT, "content": "hello", "media": []},
    )

    assert not [e for e in sent if e["event"] == "error"], sent
    assert channel.bus.inbound_size == 1
    msg = await channel.bus.consume_inbound()
    assert msg.metadata.get("shared_room") is True
    assert msg.content == "hello"
    assert not msg.media
    assert _stored_files(media_root) == []


@pytest.mark.asyncio
async def test_an_owner_attachment_is_still_stored_and_delivered(
    channel: WebSocketChannel,
    media_root: Path,
) -> None:
    sent = _capture(channel)

    await channel._commands.dispatch(
        _Connection(),
        "client-1",
        {
            "type": "message",
            "chat_id": OWNER_CHAT,
            "content": "look at this",
            "media": _media(),
        },
    )

    assert not [e for e in sent if e["event"] == "error"], sent
    stored = _stored_files(media_root)
    assert len(stored) == 1
    assert channel.bus.inbound_size == 1
    msg = await channel.bus.consume_inbound()
    assert "shared_room" not in msg.metadata
    assert [Path(p).resolve() for p in msg.media] == [stored[0].resolve()]


async def _create_room(channel: WebSocketChannel) -> None:
    await channel._dispatch_http(
        _Connection(),
        _request(
            "/auth/shared-rooms",
            {
                "source_session_key": f"websocket:{OWNER_CHAT}",
                "chat_id": ROOM_CHAT,
                "room_id": ROOM_ID,
                "title": "Shared conversation",
                "owner_display_name": "Mihai",
            },
        ),
    )
    assert channel.rooms is not None
    assert channel.rooms.is_shared_room(ROOM_CHAT)


async def _owner_room_message_with_media(channel: WebSocketChannel) -> list[dict[str, Any]]:
    sent = _capture(channel)
    await channel._commands.dispatch(
        _Connection(),
        "client-1",
        {
            "type": "message",
            "chat_id": ROOM_CHAT,
            "content": "look at this",
            "media": _media(),
        },
    )
    return sent


def _assert_rejected(sent: list[dict[str, Any]], channel: WebSocketChannel, root: Path) -> None:
    errors = [e for e in sent if e["event"] == "error"]
    assert len(errors) == 1, sent
    assert errors[0]["detail"] == "attachment_rejected"
    assert errors[0]["message"] == REJECTION_TEXT
    assert errors[0]["chat_id"] == ROOM_CHAT
    assert _stored_files(root) == []
    assert channel.bus.inbound_size == 0


@pytest.mark.asyncio
async def test_an_owner_attachment_inside_a_room_is_rejected(
    channel: WebSocketChannel,
    media_root: Path,
) -> None:
    await _create_room(channel)

    sent = await _owner_room_message_with_media(channel)

    _assert_rejected(sent, channel, media_root)


@pytest.mark.asyncio
async def test_an_owner_attachment_in_a_revoked_room_is_rejected(
    channel: WebSocketChannel,
    media_root: Path,
) -> None:
    """Fail closed: a revoked room is still a room, not the owner's chat."""
    await _create_room(channel)
    assert channel.rooms is not None
    channel.rooms.revoke(room_id=ROOM_ID, chat_id=ROOM_CHAT)

    sent = await _owner_room_message_with_media(channel)

    _assert_rejected(sent, channel, media_root)


@pytest.mark.asyncio
async def test_an_owner_attachment_outside_the_room_still_works_once_a_room_exists(
    channel: WebSocketChannel,
    media_root: Path,
) -> None:
    await _create_room(channel)
    sent = _capture(channel)

    await channel._commands.dispatch(
        _Connection(),
        "client-1",
        {
            "type": "message",
            "chat_id": OWNER_CHAT,
            "content": "look at this",
            "media": _media(),
        },
    )

    assert not [e for e in sent if e["event"] == "error"], sent
    assert len(_stored_files(media_root)) == 1
    assert channel.bus.inbound_size == 1
