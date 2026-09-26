"""A websocket envelope cannot set ``work_task_id`` / ``work_mode`` (MIT-1474).

Since MIT-1379 (PR #106) ``RequestContext.metadata["work_task_id"]`` and
``["work_mode"]`` drive the ``ziggy.dev/attended`` / ``ziggy.dev/work_task_id``
MCP ``_meta`` fields, and ``scheduling_class_for_turn`` demotes a turn carrying
``work_mode: background`` to background load. Both keys are minted
server-side by ``work_stream.py::publish_work_inbound`` and must never be
copied from a client frame -- above all not from a shared-room guest, whose
turn otherwise impersonates an owner Work task: background admission for the
owner's model calls and a forged audit task id.

The guest and owner frames go through the real command router so the test
fails if the accept path ever copies envelope metadata verbatim: the forged
keys are sent both as top-level envelope fields and inside a client-supplied
``metadata`` dict, and the message that reaches the bus (and, for the owner,
the durable inbox receipt) must carry neither the keys nor their values.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services

SECRET = "tenant-issue-secret"
OWNER_CHAT = "chat_owner"
ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
ROOM_ID = "room_" + "a" * 32

# The exact failure signature: keys the accept path would honour, forged to
# values that a real server-side mint would never put on a chat frame.
FORGED_TASK_ID = "wt_forged_1474"
FORGED_WORK_MODE = "background"


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


def _forged_fields() -> dict[str, Any]:
    """Envelope fields that must never reach turn metadata, plus the
    wholesale-copy vector (a client-supplied ``metadata`` dict)."""
    return {
        "work_task_id": FORGED_TASK_ID,
        "work_mode": FORGED_WORK_MODE,
        "metadata": {
            "work_task_id": FORGED_TASK_ID,
            "work_mode": FORGED_WORK_MODE,
        },
    }


def _assert_no_forged_work_metadata(msg: InboundMessage) -> None:
    for key in ("work_task_id", "work_mode"):
        assert key not in msg.metadata, (
            f"accept path copied envelope {key!r} into inbound metadata: {msg.metadata!r}"
        )
    # Catch a copy stored under a renamed key too: the forged values themselves
    # must be nowhere in the metadata the turn will run with.
    serialized = json.dumps(msg.metadata, default=str)
    assert FORGED_TASK_ID not in serialized
    assert FORGED_WORK_MODE not in serialized


@pytest.fixture
def channel(tmp_path: Path) -> WebSocketChannel:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    session.add_message("user", "private question")
    session.add_message("assistant", "private answer")
    sessions.save(session, fsync=True)

    config = WebSocketConfig.model_validate({
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 19001,
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


async def _capture_events(channel: WebSocketChannel) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def _capture(conn: Any, event: str, **fields: Any) -> None:
        sent.append({"event": event, **fields})

    channel._send_event = _capture  # type: ignore[method-assign]
    return sent


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


@pytest.mark.asyncio
async def test_guest_envelope_cannot_forge_work_metadata(channel: WebSocketChannel) -> None:
    """A shared-room guest frame cannot steer work_task_id / work_mode."""
    connection = await _room_guest(channel)
    sent = await _capture_events(channel)

    await channel._commands.dispatch(
        connection,
        "client-1",
        {
            "type": "message",
            "chat_id": ROOM_CHAT,
            "content": "what does the report say?",
            "client_message_id": str(uuid.uuid4()),
            **_forged_fields(),
        },
    )

    assert not [e for e in sent if e["event"] == "error"], sent
    assert channel.bus.inbound_size == 1, "guest message never reached the bus"
    msg = await channel.bus.consume_inbound()
    _assert_no_forged_work_metadata(msg)
    # The guest path really ran: the server minted its room metadata, and that
    # is what the turn carries instead of the forged keys.
    assert msg.metadata.get("shared_room") is True
    assert msg.metadata.get("room_id") == ROOM_ID


@pytest.mark.asyncio
async def test_owner_chat_envelope_cannot_forge_work_metadata(
    channel: WebSocketChannel,
) -> None:
    """An owner frame cannot mint its own Work task identity either."""
    connection = _Connection()
    sent = await _capture_events(channel)
    client_message_id = str(uuid.uuid4())

    await channel._commands.dispatch(
        connection,
        "owner-1",
        {
            "type": "message",
            "chat_id": OWNER_CHAT,
            "content": "summarise the inbox",
            "client_message_id": client_message_id,
            # Negative controls: whitelisted envelope fields must still be
            # honoured, so a green pin means the accept path ran and filtered,
            # not that it dropped the frame or its metadata.
            "explicit_final_message": True,
            "reasoning_profile": "fast",
            **_forged_fields(),
        },
    )

    assert not [e for e in sent if e["event"] == "error"], sent
    assert channel.bus.inbound_size == 1, "owner message never reached the bus"
    msg = await channel.bus.consume_inbound()
    _assert_no_forged_work_metadata(msg)
    assert msg.metadata.get("explicit_final_message") is True
    assert msg.metadata.get("reasoning_profile") == "fast"
    assert "shared_room" not in msg.metadata

    # The durable inbox receipt is replayed after a restart; a leak there
    # would outlive the connection, so it is pinned too.
    inbox = channel.chat_inbox
    assert inbox is not None
    records = await inbox.recoverable()
    receipt = next(r for r in records if r.client_message_id == client_message_id)
    _assert_no_forged_work_metadata(receipt.message)


@pytest.mark.asyncio
async def test_work_stream_still_mints_work_metadata(channel: WebSocketChannel) -> None:
    """Positive control: the pinned keys are real and server-minted.

    Without this the two pin tests could be green because the keys never
    appear anywhere; they pass only while ``publish_work_inbound`` -- the sole
    minter -- still stamps them onto the bus.
    """
    assert channel.work is not None
    await channel.work.publish_work_inbound(
        {"task_id": "task_pin_1474", "session_key": f"websocket:{OWNER_CHAT}", "chat_id": OWNER_CHAT},
        sender_id="rest",
        content="work turn",
    )
    assert channel.bus.inbound_size == 1
    msg = await channel.bus.consume_inbound()
    assert msg.metadata.get("work_task_id") == "task_pin_1474"
    assert msg.metadata.get("work_mode") == "background"
