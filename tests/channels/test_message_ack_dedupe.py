"""Owner-chat ``message.ack`` and ``client_message_id`` de-duplication (MIT-1402).

Production parity: ``nanobot/channels/websocket.py`` on ``feat/shared-rooms``
(``_send_message_ack``, ``_accepted_client_messages``, the ``ChatInboxStore``
accept -> claim_for_enqueue path). The iOS outbox resends a message every
12 s (up to 5 attempts) until it sees ``accepted`` or ``duplicate``, so a
resend must be acknowledged without starting a second turn.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.channels.websocket.chat_inbox import ChatInboxStore
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.session import webui_turns as wth
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services

CHAT_ID = "abc123"
CLIENT_ID = "6cbeadb5-2fa4-4992-8eb6-36a5df57b9c9"
# The iOS decoder's ``MessageAcknowledgement.Status`` cases.
IOS_ACK_STATUSES = {"accepted", "duplicate", "rejected"}


def _make_channel(workspace: Path | None) -> WebSocketChannel:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    cfg = {"enabled": True, "allowFrom": ["*"], "websocketRequiresToken": False}
    parsed = WebSocketConfig.model_validate(cfg)
    gateway = build_gateway_services(
        config=parsed,
        bus=bus,
        session_manager=SessionManager(workspace) if workspace is not None else None,
        static_dist_path=None,
        workspace_path=workspace or Path.cwd(),
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(cfg, bus, gateway=gateway)


def _frame(client_message_id: Any = CLIENT_ID, content: str = "hello") -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "message", "chat_id": CHAT_ID, "content": content}
    if client_message_id is not None:
        frame["client_message_id"] = client_message_id
    return frame


def _events(connection: AsyncMock) -> list[dict[str, Any]]:
    return [json.loads(call.args[0]) for call in connection.send.await_args_list]


def _acks(connection: AsyncMock) -> list[dict[str, Any]]:
    return [event for event in _events(connection) if event["event"] == "message.ack"]


@pytest.fixture(autouse=True)
def isolate_websocket_turn_state() -> None:
    wth._WEBSOCKET_ACTIVE_TURNS.clear()
    wth._WEBSOCKET_TURN_WALL_STARTED_AT.clear()
    wth._WEBSOCKET_TURN_IDS.clear()
    wth._WEBSOCKET_TURN_OWNERS.clear()
    yield
    wth._WEBSOCKET_ACTIVE_TURNS.clear()
    wth._WEBSOCKET_TURN_WALL_STARTED_AT.clear()
    wth._WEBSOCKET_TURN_IDS.clear()
    wth._WEBSOCKET_TURN_OWNERS.clear()


@pytest.mark.asyncio
async def test_new_client_message_is_accepted_once_and_published_once(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    connection = AsyncMock()

    await channel._dispatch_envelope(connection, "client-1", _frame(CLIENT_ID.upper()))

    assert _acks(connection) == [{
        "event": "message.ack",
        "chat_id": CHAT_ID,
        "client_message_id": CLIENT_ID,
        "status": "accepted",
    }]
    channel.bus.publish_inbound.assert_awaited_once()
    published: InboundMessage = channel.bus.publish_inbound.await_args.args[0]
    assert published.chat_id == CHAT_ID
    assert published.content == "hello"
    assert published.metadata["client_message_id"] == CLIENT_ID


@pytest.mark.asyncio
async def test_resent_client_message_is_duplicate_and_not_published(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    first, resend = AsyncMock(), AsyncMock()

    await channel._dispatch_envelope(first, "client-1", _frame())
    await channel._dispatch_envelope(resend, "client-1", _frame())

    assert [ack["status"] for ack in _acks(resend)] == ["duplicate"]
    assert _acks(resend)[0]["client_message_id"] == CLIENT_ID
    channel.bus.publish_inbound.assert_awaited_once()


@pytest.mark.asyncio
async def test_resend_without_workspace_uses_in_memory_ledger() -> None:
    channel = _make_channel(None)
    assert channel.chat_inbox is None
    connection = AsyncMock()

    await channel._dispatch_envelope(connection, "client-1", _frame())
    await channel._dispatch_envelope(connection, "client-1", _frame())

    assert [ack["status"] for ack in _acks(connection)] == ["accepted", "duplicate"]
    channel.bus.publish_inbound.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", ["not-a-uuid", "", 42, CLIENT_ID + "0"])
async def test_invalid_client_message_id_is_refused(tmp_path: Path, bad_id: Any) -> None:
    channel = _make_channel(tmp_path)
    connection = AsyncMock()

    await channel._dispatch_envelope(connection, "client-1", _frame(bad_id))

    # Production cannot ack a frame whose id is unusable; it sends an error.
    assert _events(connection) == [{"event": "error", "detail": "invalid client_message_id"}]
    channel.bus.publish_inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_refused_message_with_valid_id_is_rejected_ack(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    connection = AsyncMock()

    await channel._dispatch_envelope(connection, "client-1", _frame(content="   "))

    assert _acks(connection) == [{
        "event": "message.ack",
        "chat_id": CHAT_ID,
        "client_message_id": CLIENT_ID,
        "status": "rejected",
        "detail": "missing content",
    }]
    assert all(event["event"] != "error" for event in _events(connection))
    channel.bus.publish_inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_reused_id_with_different_content_is_rejected(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    connection = AsyncMock()

    await channel._dispatch_envelope(connection, "client-1", _frame(content="first"))
    await channel._dispatch_envelope(connection, "client-1", _frame(content="second"))

    assert [ack["status"] for ack in _acks(connection)] == ["accepted", "rejected"]
    assert _acks(connection)[1]["detail"] == (
        "client_message_id was already used for different content"
    )
    channel.bus.publish_inbound.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_survives_restart_with_same_inbox(tmp_path: Path) -> None:
    before = _make_channel(tmp_path)
    await before._dispatch_envelope(AsyncMock(), "client-1", _frame())

    after = _make_channel(tmp_path)  # new process, same workspace inbox
    connection = AsyncMock()
    await after._dispatch_envelope(connection, "client-1", _frame())

    assert [ack["status"] for ack in _acks(connection)] == ["duplicate"]
    after.bus.publish_inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_after_turn_marked_processed(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    await channel._dispatch_envelope(AsyncMock(), "client-1", _frame())
    published: InboundMessage = channel.bus.publish_inbound.await_args.args[0]

    loop = AgentLoop.__new__(AgentLoop)
    loop.sessions = SimpleNamespace(workspace=tmp_path)  # type: ignore[assignment]
    await loop._mark_chat_message_processed(published)

    assert await ChatInboxStore(tmp_path).recoverable() == []
    connection = AsyncMock()
    await _make_channel(tmp_path)._dispatch_envelope(connection, "client-1", _frame())
    assert [ack["status"] for ack in _acks(connection)] == ["duplicate"]


@pytest.mark.asyncio
async def test_failed_publish_releases_claim_so_resend_is_accepted(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    channel.bus.publish_inbound.side_effect = [RuntimeError("bus down"), None]
    connection = AsyncMock()

    with pytest.raises(RuntimeError):
        await channel._dispatch_envelope(connection, "client-1", _frame())
    assert _acks(connection) == []

    await channel._dispatch_envelope(connection, "client-1", _frame())
    assert [ack["status"] for ack in _acks(connection)] == ["accepted"]
    assert channel.bus.publish_inbound.await_count == 2


@pytest.mark.asyncio
async def test_ack_decodes_as_ios_message_acknowledgement(tmp_path: Path) -> None:
    """Mirror ``MessageAcknowledgement.init(from:)`` in ios WebSocketModels.swift."""
    channel = _make_channel(tmp_path)
    connection = AsyncMock()

    await channel._dispatch_envelope(connection, "client-1", _frame())
    await channel._dispatch_envelope(connection, "client-1", _frame())
    await channel._dispatch_envelope(connection, "client-1", _frame(content=""))

    acks = _acks(connection)
    assert len(acks) == 3
    for ack in acks:
        assert isinstance(ack["client_message_id"], str) and ack["client_message_id"]
        assert isinstance(ack["chat_id"], str) and ack["chat_id"]
        assert ack["status"] in IOS_ACK_STATUSES
        detail = ack.get("detail", ack.get("message"))
        assert detail is None or isinstance(detail, str)


@pytest.mark.asyncio
async def test_frames_without_client_message_id_are_unchanged(tmp_path: Path) -> None:
    channel = _make_channel(tmp_path)
    connection = AsyncMock()

    await channel._dispatch_envelope(connection, "client-1", _frame(None))
    await channel._dispatch_envelope(connection, "client-1", _frame(None))
    await channel._dispatch_envelope(connection, "client-1", _frame(None, content=" "))

    assert _acks(connection) == []
    assert channel.bus.publish_inbound.await_count == 2
    assert _events(connection)[-1] == {
        "event": "error",
        "detail": "missing content",
        "chat_id": CHAT_ID,
    }


# -- Shared rooms: production acked room turns and echoed them to the room ----


@pytest.mark.asyncio
async def test_room_guest_ask_is_acked_broadcast_and_deduplicated(tmp_path: Path) -> None:
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.websocket.transport import TransportRequest

    secret = "tenant-issue-secret"
    owner_chat, room_chat = "chat_owner", "11111111-2222-3333-4444-555555555555"
    sessions = SessionManager(tmp_path)
    source = sessions.get_or_create(f"websocket:{owner_chat}")
    source.add_message("user", "hi")
    sessions.save(source, fsync=True)
    config = WebSocketConfig.model_validate({
        "enabled": True,
        "allowFrom": ["*"],
        "websocketRequiresToken": False,
        "tokenIssueSecret": secret,
        "sharedRoomsEnabled": True,
    })
    bus = MessageBus()
    channel = WebSocketChannel(
        config,
        bus,
        gateway=build_gateway_services(
            config=config,
            bus=bus,
            session_manager=sessions,
            static_dist_path=None,
            workspace_path=tmp_path,
            default_restrict_to_workspace=False,
            runtime_model_name=None,
            runtime_surface="browser",
            runtime_capabilities_overrides=None,
        ),
    )

    class _Connection:
        remote_address = ("127.0.0.1", 41000)

        def respond(self, status: int, text: str) -> Any:
            return (status, text)

    class _Headers(dict):
        def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
            return next((v for k, v in self.items() if k.lower() == key.lower()), default)

    body = {
        "source_session_key": f"websocket:{owner_chat}",
        "chat_id": room_chat,
        "room_id": "room_" + "a" * 32,
        "title": "Shared conversation",
        "owner_display_name": "Mihai",
    }
    await channel._dispatch_http(
        _Connection(),
        TransportRequest(
            method="POST",
            path="/auth/shared-rooms",
            headers=_Headers({"Authorization": f"Bearer {secret}"}),
            body=json.dumps(body).encode(),
            raw_path="/auth/shared-rooms",
        ),
    )
    assert channel.rooms is not None
    token, _ = channel.rooms.mint(
        room_id=body["room_id"],
        chat_id=room_chat,
        participant_id="participant_" + "e" * 32,
        display_name="Guest",
        role="contributor",
    )
    guest = _Connection()
    assert channel.gateway.endpoint.authorize_websocket_handshake(
        guest, {"token": [token]}, None
    ) is None
    sent: list[dict[str, Any]] = []

    async def _capture(connection: Any, event: str, **fields: Any) -> None:
        sent.append({"event": event, **fields})

    channel._send_event = _capture  # type: ignore[method-assign]
    frame = {
        "type": "message",
        "chat_id": room_chat,
        "content": "what now?",
        "client_message_id": CLIENT_ID,
    }

    await channel._commands.dispatch(guest, "client-1", dict(frame))
    await channel._commands.dispatch(guest, "client-1", dict(frame))

    assert [
        (event["event"], event.get("status")) for event in sent
    ] == [
        ("message.ack", "accepted"),
        ("participant.message", None),
        ("message.ack", "duplicate"),
    ]
    echoed = sent[1]
    assert echoed["client_message_id"] == CLIENT_ID
    assert echoed["participant_id"] == "participant_" + "e" * 32
    assert echoed["display_name"] == "Guest"
    assert bus.inbound_size == 1
