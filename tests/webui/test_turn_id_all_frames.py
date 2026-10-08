"""TP-01 (MIT-1840): every websocket ``message`` frame keeps or mints a turn_id.

Design ``docs/design/turn-provenance.md`` §1 (repo ``mihai-chiorean/ziggy``).
The ``turn_id`` was previously kept only when the frame said ``webui: true``
(``inbound_commands.py``), and ``message_accepted`` was sent only for webui
frames. Neither the iOS app nor the web app sends that flag, so their answers
carried no turn id. Now ``client_turn_metadata`` runs for every ``message``
frame: the client's id survives when it matches the normalizer's regex, a
uuid4 is minted otherwise; ``message_accepted`` is sent whenever the frame
carried a non-empty string ``turn_id``, announcing the normalised id. The
``webui`` flag keeps its other meanings (trusted shell, transcript source).

The fixtures follow ``test_worker_user_message_journaling.py``: the real
gateway services plus a mocked bus; the lifecycle steps (run-status,
``turn_end``) follow the turn-registry tests in
``nanobot/channels/websocket/tests/test_websocket_channel.py``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import TurnEndEvent
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.session import webui_turns as wth
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.metadata import WEBUI_TURN_METADATA_KEY
from nanobot.webui.transcript import read_transcript_lines

CHAT = "all_frames_chat"
SESSION_KEY = f"websocket:{CHAT}"
WEBUI_CHAT = "webui_frames_chat"
WEBUI_SESSION_KEY = f"websocket:{WEBUI_CHAT}"


@pytest.fixture(autouse=True)
def isolate_turn_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("nanobot.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")
    for registry in (
        wth._WEBSOCKET_ACTIVE_TURNS,
        wth._WEBSOCKET_TURN_WALL_STARTED_AT,
        wth._WEBSOCKET_TURN_IDS,
        wth._WEBSOCKET_TURN_OWNERS,
    ):
        registry.clear()
    yield
    for registry in (
        wth._WEBSOCKET_ACTIVE_TURNS,
        wth._WEBSOCKET_TURN_WALL_STARTED_AT,
        wth._WEBSOCKET_TURN_IDS,
        wth._WEBSOCKET_TURN_OWNERS,
    ):
        registry.clear()


def _make_channel(workspace: Path) -> WebSocketChannel:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    config = WebSocketConfig.model_validate({
        "enabled": True,
        "allowFrom": ["*"],
        "websocketRequiresToken": False,
    })
    gateway = build_gateway_services(
        config=config,
        bus=bus,
        session_manager=SessionManager(workspace),
        static_dist_path=None,
        workspace_path=workspace,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(config, bus, gateway=gateway)
    channel.bus = bus  # type: ignore[attr-defined,assignment]
    return channel


def _frame(
    chat_id: str,
    content: str,
    *,
    turn_id: str | None = None,
    client_message_id: str | None = None,
    webui: bool = False,
) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "message", "chat_id": chat_id, "content": content}
    if turn_id is not None:
        frame["turn_id"] = turn_id
    if client_message_id is not None:
        frame["client_message_id"] = client_message_id
    if webui:
        frame["webui"] = True
    return frame


def _payloads(connection: AsyncMock) -> list[dict[str, Any]]:
    return [json.loads(call.args[0]) for call in connection.send.await_args_list]


def _event_payloads(connection: AsyncMock, event: str) -> list[dict[str, Any]]:
    return [payload for payload in _payloads(connection) if payload.get("event") == event]


def _is_uuid4(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return uuid.UUID(value).version == 4
    except ValueError:
        return False


def _inbound_metadata(channel: WebSocketChannel, index: int = 0) -> dict[str, Any]:
    inbound = channel.bus.publish_inbound.await_args_list[index].args[0]  # type: ignore[attr-defined,no-any-return]
    return inbound.metadata


async def _run_turn(
    channel: WebSocketChannel,
    connection: AsyncMock,
    chat_id: str,
    index: int = 0,
) -> dict[str, Any]:
    """Simulate the agent loop: bind the turn as running, then send its turn_end.

    Mirrors production: the session hooks call
    ``publish_turn_run_status(bus, msg, "running")`` (which registers the
    turn in ``_WEBSOCKET_ACTIVE_TURNS`` from the metadata turn id and
    publishes the goal_status event) and the loop finally sends the
    ``TurnEndEvent`` with the turn metadata.
    """
    inbound = channel.bus.publish_inbound.await_args_list[index].args[0]  # type: ignore[attr-defined]
    metadata = dict(inbound.metadata)

    await wth.publish_turn_run_status(channel.bus, inbound, "running", started_at=1234.5)
    goal_out = channel.bus.publish_outbound.await_args.args[0]  # type: ignore[attr-defined]
    await channel.send(goal_out)
    await channel.send(
        OutboundMessage(
            channel="websocket",
            chat_id=chat_id,
            content="",
            metadata=metadata,
            event=TurnEndEvent(),
        )
    )
    return metadata


@pytest.mark.asyncio
async def test_non_webui_frame_with_turn_id_keeps_it(tmp_path: Path) -> None:
    """The MIT-1840 gap: a frame with no ``webui`` flag keeps the client turn id.

    Fails on current ``ziggy-main``: ``message_accepted`` is gated on
    ``is_webui`` and the turn id is only read inside the webui branch, so the
    app-style frame gets neither.
    """
    channel = _make_channel(tmp_path)
    connection = AsyncMock()

    await channel._dispatch_envelope(  # pyright: ignore[reportPrivateUsage]
        connection,
        "app-1",
        _frame(CHAT, "what is my schedule", turn_id="t-123"),
    )

    accepted = _event_payloads(connection, "message_accepted")
    assert accepted, _payloads(connection)
    assert accepted[0]["turn_id"] == "t-123"
    assert accepted[0]["chat_id"] == CHAT
    metadata = _inbound_metadata(channel)
    assert metadata[WEBUI_TURN_METADATA_KEY] == "t-123"
    # Negative control: accepting the id must not promote the frame to webui.
    assert metadata.get("webui") is not True

    await _run_turn(channel, connection, CHAT)
    turn_ends = _event_payloads(connection, "turn_end")
    assert turn_ends, _payloads(connection)
    assert turn_ends[-1]["turn_id"] == "t-123"


@pytest.mark.asyncio
async def test_non_webui_frame_without_turn_id_gets_minted_uuid(tmp_path: Path) -> None:
    """Old app builds and ziggy-worker send no id: the server mints a uuid4."""
    channel = _make_channel(tmp_path)
    connection = AsyncMock()

    await channel._dispatch_envelope(  # pyright: ignore[reportPrivateUsage]
        connection,
        "worker-1",
        _frame(CHAT, "rebuild the index"),
    )

    # Negative control: a minted id acknowledges nothing; message_accepted
    # still requires the frame itself to have carried a turn_id.
    assert _event_payloads(connection, "message_accepted") == []

    metadata = await _run_turn(channel, connection, CHAT)
    assert _is_uuid4(metadata[WEBUI_TURN_METADATA_KEY])
    turn_ends = _event_payloads(connection, "turn_end")
    assert turn_ends, _payloads(connection)
    assert _is_uuid4(turn_ends[-1].get("turn_id"))


@pytest.mark.asyncio
async def test_invalid_turn_id_is_replaced(tmp_path: Path) -> None:
    """An id outside ``normalize_webui_turn_id``'s regex never reaches the wire."""
    channel = _make_channel(tmp_path)
    connection = AsyncMock()
    raw = "bad id with spaces"

    await channel._dispatch_envelope(  # pyright: ignore[reportPrivateUsage]
        connection,
        "app-1",
        _frame(CHAT, "replace this id", turn_id=raw),
    )

    metadata = _inbound_metadata(channel)
    minted = metadata[WEBUI_TURN_METADATA_KEY]
    assert minted != raw
    assert _is_uuid4(minted)

    await _run_turn(channel, connection, CHAT)
    turn_ends = _event_payloads(connection, "turn_end")
    assert turn_ends, _payloads(connection)
    assert turn_ends[-1]["turn_id"] == minted


@pytest.mark.asyncio
async def test_worker_frame_transcript_rows(tmp_path: Path) -> None:
    """A worker frame (no ``webui``) journals its user row with the turn columns.

    ``_annotate_turn`` (``transcript.py``) writes ``turn_id``/``turn_phase``/
    ``turn_seq`` from the metadata turn id; a second frame in the same chat is
    a new turn and must get a different id.
    """
    channel = _make_channel(tmp_path)

    await channel._dispatch_envelope(  # pyright: ignore[reportPrivateUsage]
        AsyncMock(),
        "worker-1",
        _frame(
            CHAT,
            "first worker turn",
            client_message_id="3f2a9c1e-58d4-4b6f-9e2c-7d0a1b3c5e6f",
        ),
    )
    await channel._dispatch_envelope(  # pyright: ignore[reportPrivateUsage]
        AsyncMock(),
        "worker-1",
        _frame(
            CHAT,
            "second worker turn",
            client_message_id="7b1d2e33-9a44-4c8f-8d21-2f6c0a4e9d11",
        ),
    )

    users = [
        line for line in read_transcript_lines(SESSION_KEY) if line.get("event") == "user"
    ]
    assert len(users) == 2, read_transcript_lines(SESSION_KEY)
    first, second = users
    for row in users:
        assert isinstance(row.get("turn_id"), str) and row["turn_id"]
        assert row.get("turn_phase") == "user"
        assert isinstance(row.get("turn_seq"), int)
    assert first["text"] == "first worker turn"
    assert second["text"] == "second worker turn"
    assert first["turn_id"] != second["turn_id"]
    assert _is_uuid4(first["turn_id"])
    assert _is_uuid4(second["turn_id"])


@pytest.mark.asyncio
async def test_goal_status_active_turn_id(tmp_path: Path) -> None:
    """The turn id in metadata registers the turn: goal_status carries it.

    ``publish_turn_run_status`` binds ``_WEBSOCKET_ACTIVE_TURNS[chat]`` from
    the metadata turn id (``webui_turns.py``) and the outbound projection
    (``outbound_projection.py``) puts the turn id on the goal_status wire
    frame.
    """
    channel = _make_channel(tmp_path)
    connection = AsyncMock()

    await channel._dispatch_envelope(  # pyright: ignore[reportPrivateUsage]
        connection,
        "app-1",
        _frame(CHAT, "start a turn", turn_id="t-goal-1"),
    )

    inbound = channel.bus.publish_inbound.await_args_list[0].args[0]  # type: ignore[attr-defined]
    await wth.publish_turn_run_status(channel.bus, inbound, "running", started_at=1234.5)
    assert wth.websocket_turn_id(CHAT) == "t-goal-1"

    goal_out = channel.bus.publish_outbound.await_args.args[0]  # type: ignore[attr-defined]
    await channel.send(goal_out)
    goal_statuses = _event_payloads(connection, "goal_status")
    assert goal_statuses, _payloads(connection)
    assert goal_statuses[-1]["status"] == "running"
    assert goal_statuses[-1]["turn_id"] == "t-goal-1"


@pytest.mark.asyncio
async def test_webui_frame_unchanged(tmp_path: Path) -> None:
    """Regression: the webui frame keeps its full pre-MIT-1840 behaviour."""
    channel = _make_channel(tmp_path)
    connection = AsyncMock()

    await channel._dispatch_envelope(  # pyright: ignore[reportPrivateUsage]
        connection,
        "owner-1",
        _frame(WEBUI_CHAT, "remember the milk", turn_id="t-web-1", webui=True),
    )

    metadata = _inbound_metadata(channel)
    assert metadata.get("webui") is True
    assert metadata[WEBUI_TURN_METADATA_KEY] == "t-web-1"
    accepted = _event_payloads(connection, "message_accepted")
    assert accepted, _payloads(connection)
    assert accepted[0]["turn_id"] == "t-web-1"

    users = [
        line
        for line in read_transcript_lines(WEBUI_SESSION_KEY)
        if line.get("event") == "user"
    ]
    assert users, read_transcript_lines(WEBUI_SESSION_KEY)
    assert users[0]["turn_id"] == "t-web-1"

    await _run_turn(channel, connection, WEBUI_CHAT)
    turn_ends = _event_payloads(connection, "turn_end")
    assert turn_ends, _payloads(connection)
    assert turn_ends[-1]["turn_id"] == "t-web-1"
