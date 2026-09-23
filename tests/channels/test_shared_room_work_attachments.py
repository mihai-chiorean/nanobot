"""Work tasks cannot carry attachments into a shared room (MIT-1399).

``work.create`` and ``work.message`` are owner-only (guests are refused by the
WebUI command router), but they take any ``chat_id``. Without a room check an
owner could create a Work task against a room's chat with attachments and
bypass the rejection ``_dispatch_message`` applies to room messages, putting a
file in front of every guest.
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
PLAIN_CHAT = "99999999-8888-7777-6666-555555555555"
REJECTION_TEXT = "Attachments are not available in shared rooms yet."
STORED = "/owner/media/websocket/pixel.png"
MEDIA = [{"name": "pixel.png", "data_url": "data:image/png;base64,AAAA"}]


class _Headers(dict):
    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


class _Connection:
    remote_address = ("127.0.0.1", 41002)

    def respond(self, status: int, text: str) -> Any:
        return (status, text)


@pytest.fixture
def channel(tmp_path: Path) -> WebSocketChannel:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    session.add_message("user", "private question")
    sessions.save(session, fsync=True)
    config = WebSocketConfig.model_validate({
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 18997,
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


class _Harness:
    def __init__(self, channel: WebSocketChannel) -> None:
        self.channel = channel
        self.sent: list[dict[str, Any]] = []
        self.stored: list[list[Any]] = []

        async def _capture(conn: Any, event: str, **fields: Any) -> None:
            self.sent.append({"event": event, **fields})

        def _store(media: list[Any]) -> tuple[list[str], str | None]:
            self.stored.append(media)
            return [STORED], None

        channel.webui_send_event = _capture  # type: ignore[assignment]
        channel.store_work_attachments = _store  # type: ignore[assignment]

    async def send(self, envelope: dict[str, Any]) -> None:
        await self.channel._commands.dispatch(  # pyright: ignore[reportPrivateUsage]
            _Connection(), "owner-1", envelope,
        )

    def errors(self) -> list[dict[str, Any]]:
        return [f for f in self.sent if f["event"] == "error"]

    def created_task_id(self) -> str:
        created = [f for f in self.sent if f["event"] == "work.created"]
        assert created, self.sent
        return str(created[-1]["task_id"])


async def _create_room(channel: WebSocketChannel) -> None:
    assert channel.gateway.http.shared_rooms is not None
    await channel.gateway.http.shared_rooms.create_room(
        TransportRequest(
            method="POST",
            path="/auth/shared-rooms",
            headers=_Headers({"Authorization": f"Bearer {SECRET}"}),
            body=json.dumps({
                "source_session_key": f"websocket:{OWNER_CHAT}",
                "chat_id": ROOM_CHAT,
                "room_id": ROOM_ID,
                "title": "Shared conversation",
                "owner_display_name": "Mihai",
            }).encode(),
            raw_path="/auth/shared-rooms",
        )
    )
    assert channel.rooms is not None and channel.rooms.is_shared_room(ROOM_CHAT)


def _assert_rejected(harness: _Harness) -> None:
    errors = harness.errors()
    assert len(errors) == 1, harness.sent
    assert errors[0]["detail"] == "attachment_rejected"
    assert errors[0]["message"] == REJECTION_TEXT
    assert harness.stored == []


@pytest.mark.asyncio
async def test_work_create_with_media_in_a_room_is_rejected(
    channel: WebSocketChannel,
) -> None:
    await _create_room(channel)
    harness = _Harness(channel)
    assert channel.work is not None

    await harness.send({
        "type": "work.create",
        "chat_id": ROOM_CHAT,
        "content": "look at this",
        "media": MEDIA,
    })

    _assert_rejected(harness)
    assert not [f for f in harness.sent if f["event"] == "work.created"]
    assert channel.work.store.list_tasks(limit=10) == []
    assert channel.bus.inbound_size == 0


@pytest.mark.asyncio
async def test_work_message_with_media_in_a_room_is_rejected(
    channel: WebSocketChannel,
) -> None:
    await _create_room(channel)
    harness = _Harness(channel)
    await harness.send({"type": "work.create", "chat_id": ROOM_CHAT, "content": "plan it"})
    task_id = harness.created_task_id()
    while channel.bus.inbound_size:
        await channel.bus.consume_inbound()
    harness.sent.clear()

    await harness.send({
        "type": "work.message",
        "task_id": task_id,
        "content": "and this file",
        "media": MEDIA,
    })

    _assert_rejected(harness)
    assert channel.bus.inbound_size == 0


@pytest.mark.asyncio
async def test_work_attachments_on_a_non_room_chat_still_work(
    channel: WebSocketChannel,
) -> None:
    await _create_room(channel)
    harness = _Harness(channel)

    await harness.send({
        "type": "work.create",
        "chat_id": PLAIN_CHAT,
        "content": "look at this",
        "media": MEDIA,
    })

    assert harness.errors() == []
    harness.created_task_id()
    assert harness.stored == [MEDIA]
    assert channel.bus.inbound_size == 1
    msg = await channel.bus.consume_inbound()
    assert msg.media == [STORED]
    assert "shared_room" not in msg.metadata
