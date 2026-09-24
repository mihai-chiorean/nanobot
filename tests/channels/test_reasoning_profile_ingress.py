"""MIT-1410: ``reasoning_profile`` ingress on the chat frame and REST chat API.

Production parity (``feat/shared-rooms`` 1ff35d02 / cfccc2a2): the client's
product profile choice (``auto``/``fast``/``deep``) rides the inbound
websocket ``message`` frame and the REST chat body into the turn metadata,
where the MIT-1409 loop binding resolves it; ``auto`` turns may take the
policy's one bounded escalation step.  Room guests keep the default profile:
their frames must not steer the owner's model selection, so the field is
ignored there -- the same hardening rule the shared-room gates apply to
media and commands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nanobot.agent.context import TranscriptInput
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import RequestContext
from nanobot.api.server import create_app
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket.transport import TransportRequest
from nanobot.providers.base import GenerationSettings, LLMResponse
from nanobot.session.manager import SessionManager
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.webui.gateway_services import build_gateway_services

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

API_KEY = "secret"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"}

OWNER_CHAT = "chat_owner"
ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
ROOM_ID = "room_" + "a" * 32
SECRET = "tenant-issue-secret"


# ---------------------------------------------------------------------------
# Owner ``message`` frames: the profile reaches the published metadata.
# ---------------------------------------------------------------------------


def _owner_channel(workspace: Path) -> WebSocketChannel:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    cfg = {"enabled": True, "allowFrom": ["*"], "websocketRequiresToken": False}
    parsed = WebSocketConfig.model_validate(cfg)
    gateway = build_gateway_services(
        config=parsed,
        bus=bus,
        session_manager=SessionManager(workspace),
        static_dist_path=None,
        workspace_path=workspace,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(cfg, bus, gateway=gateway)


def _message_frame(chat_id: str, **fields: Any) -> dict[str, Any]:
    return {"type": "message", "chat_id": chat_id, "content": "hello", **fields}


def _published(channel: WebSocketChannel) -> InboundMessage:
    assert channel.bus.publish_inbound.await_count == 1
    published = channel.bus.publish_inbound.await_args.args[0]
    assert isinstance(published, InboundMessage)
    return published


@pytest.mark.asyncio
async def test_the_owner_message_profile_reaches_the_inbound_metadata(tmp_path: Path) -> None:
    channel = _owner_channel(tmp_path)

    await channel._dispatch_envelope(
        AsyncMock(), "client-1", _message_frame("abc123", reasoning_profile="deep")
    )

    assert _published(channel).metadata["reasoning_profile"] == "deep"


@pytest.mark.asyncio
async def test_an_absent_profile_leaves_the_key_absent(tmp_path: Path) -> None:
    channel = _owner_channel(tmp_path)

    await channel._dispatch_envelope(AsyncMock(), "client-1", _message_frame("abc123"))

    assert "reasoning_profile" not in _published(channel).metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire_value",
    ["turbo", "", "   ", "think", "think-code", "FAST;", 42, True, ["fast"], {"fast": True}],
)
async def test_a_value_outside_the_chat_trio_is_dropped(tmp_path: Path, wire_value: Any) -> None:
    channel = _owner_channel(tmp_path)

    await channel._dispatch_envelope(
        AsyncMock(), "client-1", _message_frame("abc123", reasoning_profile=wire_value)
    )

    published = _published(channel)
    assert "reasoning_profile" not in published.metadata
    assert published.content == "hello"  # dropped, not rejected: the turn still runs


@pytest.mark.asyncio
async def test_the_profile_survives_a_client_metadata_echo(tmp_path: Path) -> None:
    """A client that echoes a metadata blob cannot forge the server-owned key."""
    channel = _owner_channel(tmp_path)

    await channel._dispatch_envelope(
        AsyncMock(),
        "client-1",
        _message_frame(
            "abc123",
            reasoning_profile="deep",
            metadata={"reasoning_profile": "fast", "sender_id": "someone-else"},
        ),
    )

    assert _published(channel).metadata["reasoning_profile"] == "deep"


# ---------------------------------------------------------------------------
# Room guests: the field is ignored, rooms keep the default profile.
# ---------------------------------------------------------------------------


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
def room_channel(tmp_path: Path) -> WebSocketChannel:
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


async def _room_guest_connection(channel: WebSocketChannel) -> _Connection:
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
async def test_a_room_guest_frame_cannot_pick_the_profile(room_channel: WebSocketChannel) -> None:
    connection = await _room_guest_connection(room_channel)
    sent: list[dict[str, Any]] = []

    async def _capture(conn: Any, event: str, **fields: Any) -> None:
        sent.append({"event": event, **fields})

    room_channel.webui_send_event = _capture  # type: ignore[assignment]
    await room_channel._commands.dispatch(
        connection,
        "client-1",
        _message_frame(ROOM_CHAT, reasoning_profile="deep"),
    )
    assert not [event for event in sent if event["event"] == "error"], sent
    assert room_channel.bus.inbound_size == 1
    msg = await room_channel.bus.consume_inbound()
    assert msg.metadata.get("shared_room") is True  # the room path really ran
    assert "reasoning_profile" not in msg.metadata


@pytest.mark.asyncio
async def test_the_same_frame_on_a_plain_chat_still_keeps_the_profile(
    room_channel: WebSocketChannel,
) -> None:
    """Control for the guest rule: the ignore is room-scoped, not global."""
    await room_channel._commands.dispatch(
        AsyncMock(),
        "client-2",
        _message_frame("abc123", reasoning_profile="deep"),
    )
    assert room_channel.bus.inbound_size == 1
    msg = await room_channel.bus.consume_inbound()
    assert msg.metadata["reasoning_profile"] == "deep"


# ---------------------------------------------------------------------------
# REST chat API: the body field reaches process_direct's metadata.
# ---------------------------------------------------------------------------


def _make_mock_agent() -> MagicMock:
    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="mock response")
    agent.aclose = AsyncMock()
    return agent


@pytest_asyncio.fixture
async def aiohttp_client():
    clients: list[TestClient] = []

    async def _make_client(app):
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    try:
        yield _make_client
    finally:
        for client in clients:
            await client.close()


async def _post_chat(client: Any, body: dict[str, Any]) -> int:
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH_HEADERS,
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], **body},
    )
    return response.status


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_the_rest_body_profile_reaches_the_turn_metadata(aiohttp_client) -> None:
    agent = _make_mock_agent()
    app = create_app(agent, model_name="test-model", api_key=API_KEY)
    client = await aiohttp_client(app)

    assert await _post_chat(client, {"reasoning_profile": "fast"}) == 200

    _args, kwargs = agent.process_direct.await_args
    assert kwargs["metadata"] == {"reasoning_profile": "fast"}


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_a_rest_body_without_a_profile_keeps_metadata_empty(aiohttp_client) -> None:
    agent = _make_mock_agent()
    app = create_app(agent, model_name="test-model", api_key=API_KEY)
    client = await aiohttp_client(app)

    assert await _post_chat(client, {}) == 200

    _args, kwargs = agent.process_direct.await_args
    assert kwargs["metadata"] == {}


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize("wire_value", ["turbo", "think", "", 42, None])
async def test_an_invalid_rest_profile_is_dropped_not_400(
    aiohttp_client, wire_value: Any
) -> None:
    agent = _make_mock_agent()
    app = create_app(agent, model_name="test-model", api_key=API_KEY)
    client = await aiohttp_client(app)

    assert await _post_chat(client, {"reasoning_profile": wire_value}) == 200

    _args, kwargs = agent.process_direct.await_args
    assert kwargs["metadata"] == {}


# ---------------------------------------------------------------------------
# Runner: the bounded auto escalation hook (production cfccc2a2).
# ---------------------------------------------------------------------------


class _LoopHarness:
    """Drives the full loop with a scripted provider, recording generation kwargs."""

    def __init__(self, tmp_path: Path, responses: list[LLMResponse]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"

        async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
            self.calls.append(kwargs)
            if self._responses:
                return self._responses.pop(0)
            return LLMResponse(content="done", tool_calls=[], finish_reason="stop")

        provider.chat_stream_with_retry = chat_stream_with_retry
        provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(content="done", tool_calls=[], finish_reason="stop")
        )
        self.provider = provider
        self.loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=tmp_path,
            model="test-model",
        )
        self.runtime = LLMRuntime(
            provider=provider,
            model="test-model",
            generation=GenerationSettings(
                temperature=0.7,
                max_tokens=4096,
                reasoning_effort="low",
            ),
            context_window_tokens=200_000,
        )

    async def run_turn(self, metadata: dict[str, Any], *, text: str = "hello") -> None:
        await self.loop._run_agent_loop(
            TranscriptInput(history=[], current_message=text, media=[]),
            runtime=self.runtime,
            request_context=RequestContext(
                channel="test",
                chat_id="c1",
                session_key="test:c1",
                runtime=self.runtime,
                metadata=dict(metadata),
            ),
        )


def _blank() -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[], finish_reason="stop")


def _text(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=[], finish_reason="stop")


@pytest.mark.asyncio
async def test_an_auto_turn_escalates_once_when_the_policy_trigger_fires(
    tmp_path: Path,
) -> None:
    harness = _LoopHarness(
        tmp_path,
        [_blank(), _blank(), _text("here you go")],
    )

    await harness.run_turn({"reasoning_profile": "auto"})

    efforts = [call.get("reasoning_effort") for call in harness.calls]
    assert len(efforts) >= 2
    assert efforts[0] == "none"  # auto resolved to fast for this greeting
    assert set(efforts[1:]) == {"high"}  # and it never climbs past the one rung
    escalated = harness.calls[1]
    assert escalated.get("temperature") == 1.0
    assert escalated.get("max_tokens") == 32_768
    assert escalated.get("model") == "test-model"
    # The admitted runtime is a frozen value and was never mutated in place.
    assert harness.runtime.generation.reasoning_effort == "low"
    assert harness.runtime.generation.max_tokens == 4096


@pytest.mark.asyncio
async def test_an_explicit_fast_turn_never_escalates(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path, [_blank(), _blank(), _text("ok")])

    await harness.run_turn({"reasoning_profile": "fast"})

    efforts = [call.get("reasoning_effort") for call in harness.calls]
    assert len(efforts) >= 2
    assert set(efforts) == {"none"}  # the client's fast choice sticks, blank or not


@pytest.mark.asyncio
async def test_an_explicit_deep_turn_never_escalates(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path, [_blank(), _blank(), _text("ok")])

    await harness.run_turn({"reasoning_profile": "deep"})

    efforts = [call.get("reasoning_effort") for call in harness.calls]
    assert len(efforts) >= 2
    assert set(efforts) == {"high"}  # pinned high from the start, never higher


@pytest.mark.asyncio
async def test_a_profile_less_turn_never_escalates(tmp_path: Path) -> None:
    harness = _LoopHarness(tmp_path, [_blank(), _blank(), _text("ok")])

    await harness.run_turn({})

    efforts = [call.get("reasoning_effort") for call in harness.calls]
    assert len(efforts) >= 2
    assert set(efforts) == {"low"}  # the admitted default, unescalated
