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


# -- worker reconciliation read (review F1, MIT-1623) ----------------------------
#
# The owner ``GET /api/sessions/<key>/messages`` route was removed (MIT-1623):
# once MIT-1489 backfilled transcripts and MIT-1622 journaled worker user
# messages, ziggy-worker reconciles its turn from the same
# ``/webui-thread?projection=events`` path every other consumer uses
# (client.go ``FinalMessage`` -> ``reconciledFinalFromEvents``). These tests
# pin the removal (generic API 404 for every caller), the events path serving
# the same turn, and the room-scoped ``/messages`` feature route
# (``shared_rooms_http.py``) surviving the removal untouched.

PROXY_ASSERTION_HEADER = "X-Ziggy-Proxy-Assertion"
WORKER_CLIENT_MESSAGE_ID = "3f2a9c1e-58d4-4b6f-9e2c-7d0a1b3c5e6f"
ROOM_ISSUE_SECRET = "tenant-issue-secret"
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


def _text_of(response: Any) -> str:
    return bytes(response.body).decode()


@pytest.mark.asyncio
async def test_worker_reconcile_reads_turn_from_webui_thread_events_after_final_stream_end(
    tmp_path: Path,
) -> None:
    """The ziggy-worker flow after MIT-1623: ``stream_end`` with
    ``resuming: false`` triggers a background GET
    ``/api/sessions/websocket:<chat>/webui-thread?projection=events`` with the
    owner API token (services/ziggy-worker/internal/control/client.go
    FinalMessage). The turn must reconcile from the journal -- the
    ``user_message`` carrying the ``client_message_id`` plus the assistant
    final -- and the removed owner ``/messages`` route must answer the
    generic API 404 for every caller, bearer or not."""
    import asyncio

    from nanobot.channels.websocket.tests.test_websocket_http_routes import _ch, _free_port
    from nanobot.channels.websocket.tests.ws_test_client import http_get
    from nanobot.session.manager import SessionManager

    sessions = SessionManager(tmp_path / "workspace")
    chat_id = "worker-chat"

    port = _free_port()
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = _ch(bus, session_manager=sessions, port=port)
    server = asyncio.create_task(channel.start())
    try:
        ws = AsyncMock()
        channel._attach(ws, chat_id)
        connection = AsyncMock()
        connection.remote_address = ("127.0.0.1", 5000)

        # Worker ingress, exactly as ziggy-worker posts it: no ``webui``
        # flag, a ``client_message_id`` for exactly-once delivery (MIT-1622
        # journals the user message) and ``explicit_final_message``.
        await channel._dispatch_envelope(
            connection,
            "worker",
            {
                "type": "message",
                "chat_id": chat_id,
                "content": "what is on my calendar?",
                "client_message_id": WORKER_CLIENT_MESSAGE_ID,
                "explicit_final_message": True,
            },
        )

        # The agent's reply, as the runner emits it over this channel.
        meta = {"explicit_final_message": True}
        await channel.send_delta(chat_id, "Two meetings today.", meta, stream_id="sid")
        await channel.send_delta(chat_id, "", meta, stream_id="sid", stream_end=True)
        stream_end = _frames(ws)[-1]
        assert stream_end["event"] == "stream_end"
        assert stream_end["resuming"] is False  # the worker's reconcile trigger
        await ChannelManager._send_once(
            channel,
            OutboundMessage(
                channel="websocket",
                chat_id=chat_id,
                content="Two meetings today.",
                metadata=dict(meta),
            ),
        )

        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        encoded_key = "websocket%3A" + chat_id
        thread = await http_get(
            f"http://127.0.0.1:{port}/api/sessions/{encoded_key}/webui-thread?projection=events",
            headers=auth,
        )
        replay = await http_get(
            f"http://127.0.0.1:{port}/api/sessions/{encoded_key}/webui-thread",
            headers=auth,
        )
        messages_url = f"http://127.0.0.1:{port}/api/sessions/{encoded_key}/messages"
        owner = await http_get(messages_url, headers=auth)
        anonymous = await http_get(messages_url)
        wrong = await http_get(messages_url, headers={"Authorization": "Bearer not-a-token"})
    finally:
        await channel.stop()
        await server

    assert thread.status_code == 200, thread.text
    body = thread.json()
    assert body.get("projection") == "events"
    events: list[dict[str, Any]] = body["events"]
    users = [event for event in events if event.get("event") == "user_message"]
    assert [(u.get("text"), u.get("client_message_id")) for u in users] == [
        ("what is on my calendar?", WORKER_CLIENT_MESSAGE_ID),
    ]
    # The worker's completion candidates (client.go
    # reconciledFinalFromEvents): the kindless message row or the final
    # stream boundary after the turn's user_message.
    finals = [
        event
        for event in events[events.index(users[0]) + 1 :]
        if event.get("event") == "stream_end"
        or (
            event.get("event") == "message"
            and not event.get("kind")
            and not event.get("tool_events")
        )
    ]
    assert finals, events
    assert all(event.get("text") == "Two meetings today." for event in finals)

    # Nothing was lost with the raw-session read: the default projection
    # replays the same turn the removed route served, id included.
    assert replay.status_code == 200, replay.text
    replay_messages = replay.json()["messages"]
    assert [(m.get("role"), m.get("content")) for m in replay_messages] == [
        ("user", "what is on my calendar?"),
        ("assistant", "Two meetings today."),
    ]
    replay_users = [m for m in replay_messages if m.get("role") == "user"]
    assert replay_users[-1].get("client_message_id") == WORKER_CLIENT_MESSAGE_ID

    # The removed owner route: the generic API 404 for every caller, and it
    # never carries transcript data.
    for response in (owner, anonymous, wrong):
        assert response.status_code == 404, response.text
        assert response.text == "API route not found"
        assert "Two meetings" not in response.text


@pytest.mark.asyncio
async def test_owner_messages_path_404s_for_proxied_and_owner_requests(
    tmp_path: Path,
) -> None:
    """MIT-1623 replacement for the ``/messages`` trusted-proxy test: with
    the route gone, neither a trusted-proxy-marked request with no bearer --
    the revoked/expired-room-guest fall-through the old handler had to reject
    with 401 -- nor the owner API token reads the transcript at ``/messages``.
    The generic API 404 answers, and ``/webui-thread`` still serves the owner
    (non-vacuity: the 404 is the route being gone, not a broken harness)."""
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _ch, _free_port
    from nanobot.channels.websocket.transport import TransportRequest
    from nanobot.session.manager import SessionManager
    from nanobot.webui.transcript import write_session_messages_as_transcript

    sessions = SessionManager(tmp_path / "workspace")
    session = sessions.get_or_create("websocket:owner-chat")
    session.add_message("assistant", "private owner reply")
    sessions.save(session)
    # MIT-1489 backfill shape: a journaled transcript is what makes
    # /webui-thread serve this session.
    write_session_messages_as_transcript(
        "websocket:owner-chat",
        [{"role": "assistant", "content": "private owner reply"}],
    )
    channel = _ch(
        MagicMock(),
        session_manager=sessions,
        port=_free_port(),
        trustedProxyAuth={
            "trustedPeerCidrs": ["127.0.0.1/32"],
            "assertionHeader": PROXY_ASSERTION_HEADER,
        },
    )

    path = "/api/sessions/websocket%3Aowner-chat/messages"
    proxied = TransportRequest(
        method="GET",
        path=path,
        headers=_Headers({PROXY_ASSERTION_HEADER: "room-guest"}),
        body=b"",
        raw_path=path,
    )
    response = await channel._dispatch_http(_Connection(), proxied)
    assert response is not None
    # Non-vacuity: dispatch really stamped the proxy mark on this request.
    assert getattr(proxied, "_nanobot_trusted_proxy_authenticated", False) is True
    assert response.status_code == 404
    assert _text_of(response) == "API route not found"

    token = channel.gateway.tokens.issue_api_token(60)
    owner = TransportRequest(
        method="GET",
        path=path,
        headers=_Headers({"Authorization": f"Bearer {token}"}),
        body=b"",
        raw_path=path,
    )
    response = await channel._dispatch_http(_Connection(), owner)
    assert response is not None
    assert response.status_code == 404
    assert "private owner reply" not in _text_of(response)

    thread_path = "/api/sessions/websocket%3Aowner-chat/webui-thread"
    thread = await channel._dispatch_http(
        _Connection(),
        TransportRequest(
            method="GET",
            path=thread_path,
            headers=_Headers({"Authorization": f"Bearer {token}"}),
            body=b"",
            raw_path=thread_path,
        ),
    )
    assert thread is not None
    assert thread.status_code == 200
    assert "private owner reply" in _text_of(thread)


@pytest.mark.asyncio
async def test_room_bearer_still_reads_room_messages_after_owner_route_removal(
    tmp_path: Path,
) -> None:
    """Regression guard (MIT-1623): the room-scoped ``/messages`` feature
    route (``shared_rooms_http.py``, out of scope here) shares the removed
    URL shape and must keep serving guests through the same dispatcher. A
    room bearer reads exactly its own room (redacted projection); the owner
    token reads nothing at ``/messages``; and the room bearer must not reach
    the unredacted ``/webui-thread`` -- the split ziggy-control's
    ``roomCredentialRequestAllowed`` comment relies on."""
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _ch, _free_port
    from nanobot.channels.websocket.transport import TransportRequest
    from nanobot.session.manager import SessionManager

    sessions = SessionManager(tmp_path / "workspace")
    owner_session = sessions.get_or_create("websocket:chat_owner")
    owner_session.add_message("user", "private question", reasoning="chain of thought")
    owner_session.add_message("assistant", "private answer")
    sessions.save(owner_session, fsync=True)

    channel = _ch(
        MagicMock(),
        session_manager=sessions,
        port=_free_port(),
        tokenIssueSecret=ROOM_ISSUE_SECRET,
        sharedRoomsEnabled=True,
    )
    shared = channel.gateway.http.shared_rooms
    assert shared is not None
    created = await shared.dispatch(
        TransportRequest(
            method="POST",
            path="/auth/shared-rooms",
            headers=_Headers({"Authorization": f"Bearer {ROOM_ISSUE_SECRET}"}),
            body=json.dumps(
                {
                    "source_session_key": "websocket:chat_owner",
                    "chat_id": ROOM_CHAT,
                    "room_id": ROOM_ID,
                    "title": "Shared conversation",
                    "owner_display_name": "Owner",
                }
            ).encode(),
            raw_path="/auth/shared-rooms",
        ),
        "/auth/shared-rooms",
    )
    assert created.status_code == 201
    assert channel.rooms is not None
    room_token, _ = channel.rooms.mint(
        room_id=ROOM_ID,
        chat_id=ROOM_CHAT,
        participant_id="participant_" + "e" * 32,
        display_name="Guest",
        role="contributor",
    )

    room_key = "websocket%3A" + ROOM_CHAT

    guest = await channel._dispatch_http(
        _Connection(),
        TransportRequest(
            method="GET",
            path=f"/api/sessions/{room_key}/messages",
            headers=_Headers({"Authorization": f"Bearer {room_token}"}),
            body=b"",
            raw_path=f"/api/sessions/{room_key}/messages",
        ),
    )
    assert guest is not None
    assert guest.status_code == 200, _text_of(guest)
    served = _text_of(guest)
    body = json.loads(served)
    assert body["key"] == f"websocket:{ROOM_CHAT}"
    assert [m["content"] for m in body["messages"]] == [
        "private question",
        "private answer",
    ]
    assert "chain of thought" not in served  # redacted projection, not the raw file

    api_token = channel.gateway.tokens.issue_api_token(60)
    owner_read = await channel._dispatch_http(
        _Connection(),
        TransportRequest(
            method="GET",
            path=f"/api/sessions/{room_key}/messages",
            headers=_Headers({"Authorization": f"Bearer {api_token}"}),
            body=b"",
            raw_path=f"/api/sessions/{room_key}/messages",
        ),
    )
    assert owner_read is not None
    assert owner_read.status_code == 404
    assert "private answer" not in _text_of(owner_read)

    thread = await channel._dispatch_http(
        _Connection(),
        TransportRequest(
            method="GET",
            path=f"/api/sessions/{room_key}/webui-thread",
            headers=_Headers({"Authorization": f"Bearer {room_token}"}),
            body=b"",
            raw_path=f"/api/sessions/{room_key}/webui-thread",
        ),
    )
    assert thread is not None
    assert thread.status_code == 401
