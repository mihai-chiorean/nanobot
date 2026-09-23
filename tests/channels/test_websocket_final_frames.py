"""Final-frame contract the ziggy-worker relies on to complete an execution.

The worker sends ``explicit_final_message: true`` on every turn and completes
an execution on a final ``message`` frame, or failing that on a ``stream_end``
whose ``resuming`` key is present and false.
"""

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.outbound_events import (
    StreamDeltaEvent,
    StreamedResponseEvent,
    StreamEndEvent,
)
from nanobot.channels.manager import ChannelManager
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.webui.gateway_services import build_gateway_services


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)


def _channel(bus: Any) -> WebSocketChannel:
    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "streaming": True,
        "websocketRequiresToken": False,
    }
    gateway = build_gateway_services(
        config=WebSocketConfig.model_validate(cfg),
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=Path.cwd(),
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(cfg, bus, gateway=gateway)


def _frames(ws: AsyncMock) -> list[dict[str, Any]]:
    return [json.loads(call.args[0]) for call in ws.send.await_args_list]


def _final_outbound(metadata: dict[str, Any], text: str) -> OutboundMessage | None:
    inbound = InboundMessage(
        channel="websocket",
        sender_id="worker",
        chat_id="chat-1",
        content="hi",
        metadata=metadata,
    )
    return AgentLoop._assemble_outbound(
        SimpleNamespace(),  # the assembler reads no loop state
        inbound,
        text,
        "completed",
        True,
    )


async def _streamed_turn(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    channel = _channel(MagicMock())
    ws = AsyncMock()
    channel._attach(ws, "chat-1")
    for event, content in (
        (StreamDeltaEvent(stream_id="sid"), "Hello "),
        (StreamDeltaEvent(stream_id="sid"), "world"),
        (StreamEndEvent(stream_id="sid"), ""),
    ):
        await ChannelManager._send_once(
            channel,
            OutboundMessage(
                channel="websocket",
                chat_id="chat-1",
                content=content,
                event=event,
                metadata=dict(metadata),
            ),
        )
    final = _final_outbound(metadata, "Hello world")
    assert final is not None
    await ChannelManager._send_once(channel, final)
    return _frames(ws)


@pytest.mark.asyncio
async def test_explicit_final_message_streamed_turn_ends_with_message_frame() -> None:
    frames = await _streamed_turn({"explicit_final_message": True})

    assert [f["event"] for f in frames] == ["delta", "delta", "stream_end", "message"]
    final = frames[-1]
    assert final["text"] == "Hello world"
    assert final["chat_id"] == "chat-1"
    assert "kind" not in final


@pytest.mark.asyncio
async def test_streamed_turn_without_flag_sends_no_extra_message_frame() -> None:
    frames = await _streamed_turn({})

    assert [f["event"] for f in frames] == ["delta", "delta", "stream_end"]


def test_explicit_final_message_only_applies_to_websocket() -> None:
    inbound = InboundMessage(
        channel="telegram",
        sender_id="u",
        chat_id="c",
        content="hi",
        metadata={"explicit_final_message": True},
    )
    out = AgentLoop._assemble_outbound(SimpleNamespace(), inbound, "x", "completed", True)
    assert out is not None
    assert isinstance(out.event, StreamedResponseEvent)


@pytest.mark.asyncio
async def test_message_envelope_carries_explicit_final_message_into_turn() -> None:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = _channel(bus)
    connection = AsyncMock()
    connection.remote_address = ("127.0.0.1", 5000)
    chat_id = str(uuid.uuid4())

    await channel._dispatch_envelope(
        connection,
        "worker",
        {
            "type": "message",
            "chat_id": chat_id,
            "content": "hi",
            "explicit_final_message": True,
        },
    )
    await channel._dispatch_envelope(
        connection,
        "worker",
        {"type": "message", "chat_id": chat_id, "content": "again"},
    )

    first, second = (call.args[0] for call in bus.publish_inbound.await_args_list)
    assert first.metadata["explicit_final_message"] is True
    assert "explicit_final_message" not in second.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("resuming", [False, True])
async def test_stream_end_always_carries_resuming(resuming: bool) -> None:
    channel = _channel(MagicMock())
    ws = AsyncMock()
    channel._attach(ws, "chat-1")

    await channel.send_delta("chat-1", "part", stream_id="sid")
    await channel.send_delta(
        "chat-1", "", stream_id="sid", stream_end=True, resuming=resuming
    )

    stream_end = _frames(ws)[-1]
    assert stream_end["event"] == "stream_end"
    assert "resuming" in stream_end
    assert stream_end["resuming"] is resuming


# -- transcript: one bubble for an explicit final (review F2) ---------------------


@pytest.mark.asyncio
async def test_explicit_final_replays_as_one_assistant_bubble() -> None:
    from nanobot.webui.transcript import build_webui_thread_response, read_transcript_lines

    frames = await _streamed_turn({"explicit_final_message": True})
    assert [f["event"] for f in frames] == ["delta", "delta", "stream_end", "message"]

    lines = read_transcript_lines("websocket:chat-1")
    assert [line["event"] for line in lines] == ["stream_end"]
    body = build_webui_thread_response("websocket:chat-1")
    assert body is not None
    assistant = [m for m in body["messages"] if m.get("role") == "assistant"]
    assert [m["content"] for m in assistant] == ["Hello world"]


@pytest.mark.asyncio
async def test_explicit_final_with_different_text_is_still_recorded() -> None:
    """An error reply after a streamed segment is new content, not a repeat."""
    from nanobot.webui.transcript import read_transcript_lines

    channel = _channel(MagicMock())
    ws = AsyncMock()
    channel._attach(ws, "chat-1")
    meta = {"explicit_final_message": True}
    await channel.send_delta("chat-1", "partial", meta, stream_id="sid")
    await channel.send_delta("chat-1", "", meta, stream_id="sid", stream_end=True)
    await ChannelManager._send_once(
        channel,
        OutboundMessage(
            channel="websocket",
            chat_id="chat-1",
            content="Sorry, something went wrong.",
            metadata=dict(meta),
        ),
    )

    lines = read_transcript_lines("websocket:chat-1")
    assert [line["event"] for line in lines] == ["stream_end", "message"]
    assert lines[-1]["text"] == "Sorry, something went wrong."


# -- worker reconciliation read (review F1) --------------------------------------


@pytest.mark.asyncio
async def test_worker_reconcile_reads_owner_session_messages_after_final_stream_end(
    tmp_path: Path,
) -> None:
    """The ziggy-worker flow: stream_end resuming:false starts a background
    GET /api/sessions/websocket:<chat>/messages with the owner API token
    (services/ziggy-worker/internal/control/client.go FinalMessage). It must be
    200 JSON with ``messages`` carrying the final reply, not 404."""
    import asyncio

    from nanobot.channels.websocket.tests.test_websocket_http_routes import _ch, _free_port
    from nanobot.channels.websocket.tests.ws_test_client import http_get
    from nanobot.session.manager import SessionManager

    sessions = SessionManager(tmp_path / "workspace")
    chat_id = "worker-chat"
    session = sessions.get_or_create(f"websocket:{chat_id}")
    session.add_message("user", "what is on my calendar?")
    session.add_message("assistant", "Two meetings today.")
    sessions.save(session)

    port = _free_port()
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = _ch(bus, session_manager=sessions, port=port)
    server = asyncio.create_task(channel.start())
    try:
        ws = AsyncMock()
        channel._attach(ws, chat_id)
        meta = {"explicit_final_message": True}
        await channel.send_delta(chat_id, "Two meetings today.", meta, stream_id="sid")
        await channel.send_delta(chat_id, "", meta, stream_id="sid", stream_end=True)
        stream_end = _frames(ws)[-1]
        assert stream_end["event"] == "stream_end"
        assert stream_end["resuming"] is False  # the worker's reconcile trigger

        token = channel.gateway.tokens.issue_api_token(300)
        url = f"http://127.0.0.1:{port}/api/sessions/websocket:{chat_id}/messages"
        owner = await http_get(url, headers={"Authorization": f"Bearer {token}"})
        anonymous = await http_get(url)
        wrong = await http_get(url, headers={"Authorization": "Bearer not-a-token"})
        other = await http_get(
            f"http://127.0.0.1:{port}/api/sessions/cli:direct/messages",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        await channel.stop()
        await server

    assert owner.status_code == 200
    assert owner.headers["content-type"].startswith("application/json")
    messages = owner.json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "what is on my calendar?"),
        ("assistant", "Two meetings today."),
    ]
    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert other.status_code == 404  # only websocket sessions, as in production
