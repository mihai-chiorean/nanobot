"""A shared-room guest must never reach the owner's slash-command router (MIT-1398).

Production (``feat/shared-rooms``, ``nanobot/agent/loop.py`` 1615/1626) skips
all command dispatch for a ``shared_room`` turn. On 0.3.0 the priority path and
the mid-turn injection path were guarded, but the turn-pipeline stage
``AgentLoop._dispatch_command`` was not: an idle-room guest sending ``/new``,
``/model <x>`` or ``/pairing ...`` acted on the owner's agent.

The guest frame goes through the real WebSocket runtime so the room metadata is
minted the way production mints it, then the resulting inbound message runs
through a real ``AgentLoop`` over the same workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.config.schema import ModelPresetConfig
from nanobot.providers.base import GenerationSettings, LLMResponse
from nanobot.session.manager import SessionManager
from nanobot.session.model_selection import model_preset_from_metadata
from nanobot.webui.gateway_services import build_gateway_services

SECRET = "tenant-issue-secret"
OWNER_CHAT = "chat_owner"
ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
ROOM_ID = "room_" + "a" * 32
ROOM_REPLY = "Normal room reply."


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


async def _guest_inbound(channel: WebSocketChannel, content: str) -> InboundMessage:
    """Send ``content`` as a room guest and return what reached the bus."""
    connection = await _room_guest(channel)
    sent: list[dict[str, Any]] = []

    async def _capture(conn: Any, event: str, **fields: Any) -> None:
        sent.append({"event": event, **fields})

    channel.webui_send_event = _capture  # type: ignore[assignment]
    await channel._commands.dispatch(
        connection,
        "client-1",
        {"type": "message", "chat_id": ROOM_CHAT, "content": content},
    )
    assert not [e for e in sent if e["event"] == "error"], sent
    assert channel.bus.inbound_size == 1
    msg = await channel.bus.consume_inbound()
    assert msg.metadata.get("shared_room") is True
    return msg


def _loop(workspace: Path) -> tuple[AgentLoop, AsyncMock]:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    chat = AsyncMock(return_value=LLMResponse(content=ROOM_REPLY, finish_reason="stop"))
    provider.chat_with_retry = chat
    provider.chat_stream_with_retry = chat
    provider.estimate_prompt_tokens = MagicMock(return_value=(100, "test"))
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        model="test-model",
        model_presets={
            "x": ModelPresetConfig(model="test-model-x", context_window_tokens=64_000),
        },
    )
    return loop, chat


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/new", "/model x", "/pairing list"])
async def test_a_room_guest_command_is_an_ordinary_room_message(
    channel: WebSocketChannel,
    tmp_path: Path,
    command: str,
) -> None:
    msg = await _guest_inbound(channel, command)
    loop, chat = _loop(tmp_path)
    room_key = msg.session_key
    before = len(loop.sessions.get_or_create(room_key).messages)
    assert before > 0, "the room clone should carry the shared transcript"

    reply = await loop._process_message(msg)

    # Processed as a normal room turn: the model answered, not a command.
    assert reply is not None
    assert reply.content == ROOM_REPLY
    assert chat.await_count >= 1
    room = loop.sessions.get_or_create(room_key)
    # /new did not clear the room session; the guest text was recorded.
    assert len(room.messages) > before
    assert any(
        m.get("role") == "user" and command in str(m.get("content"))
        for m in room.messages
    )
    # /model x did not switch anyone's model preset.
    assert model_preset_from_metadata(room.metadata) is None
    assert loop.model_preset is None
    owner = loop.sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    assert model_preset_from_metadata(owner.metadata) is None
    # /pairing produced no pairing reply.
    assert "pair" not in reply.content.lower()


@pytest.mark.asyncio
async def test_the_owner_webui_new_still_clears_the_session(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create(f"websocket:{OWNER_CHAT}")
    session.add_message("user", "private question")
    session.add_message("assistant", "private answer")
    sessions.save(session, fsync=True)
    loop, chat = _loop(tmp_path)

    reply = await loop._process_message(InboundMessage(
        channel="websocket",
        sender_id="owner",
        chat_id=OWNER_CHAT,
        content="/new",
        metadata={"webui": True},
    ))

    assert reply is not None
    assert reply.content != ROOM_REPLY
    chat.assert_not_awaited()
    assert loop.sessions.get_or_create(f"websocket:{OWNER_CHAT}").messages == []

