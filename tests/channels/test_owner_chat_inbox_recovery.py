"""Owner-chat inbox recovery at gateway start-up (MIT-1403).

Production parity: ``nanobot/channels/websocket.py::_recover_chat_inbox`` on
``feat/shared-rooms`` (commit a90a5b2d), called from ``start()`` before the
listener accepts sockets. A message acked ``accepted`` whose turn never
completed must be re-published exactly once by the next gateway start, and
the user transcript event must carry ``client_message_id`` so the iOS client
can match its optimistic bubble (``ConversationHistory.swift:203``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.channels.websocket.chat_inbox import ChatInboxStore
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.transcript import (
    WebUITranscriptRecorder,
    _session_user_event,
    build_user_transcript_event,
)

CHAT_ID = "abc123"
CLIENT_ID = "6cbeadb5-2fa4-4992-8eb6-36a5df57b9c9"


def _make_channel(workspace: Path) -> WebSocketChannel:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "websocketRequiresToken": False,
        "port": 0,  # ephemeral: never collide with a live gateway
    }
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


def _owner_message(content: str = "recover me") -> InboundMessage:
    return InboundMessage(
        channel="websocket",
        sender_id="owner",
        chat_id=CHAT_ID,
        content=content,
        metadata={"webui": True, "client_message_id": CLIENT_ID},
    )


async def _seed(workspace: Path, *, state: str, content: str = "recover me") -> None:
    """Seed the inbox the way the owner send-path leaves it on a crash."""
    inbox = ChatInboxStore(workspace)
    disposition, record = await inbox.accept(_owner_message(content), CLIENT_ID)
    assert disposition == "inserted"
    if state == "enqueued":
        # acked accepted and enqueued; the turn never completed.
        assert await inbox.claim_for_enqueue(CHAT_ID, CLIENT_ID)
    elif state == "retry_wait":
        await inbox.prepare_retry(CHAT_ID, CLIENT_ID)
    elif state != "stored":
        raise ValueError(state)
    records = await inbox.recoverable()
    assert [r.state for r in records] == [state]


async def _run_once(channel: WebSocketChannel) -> None:
    """Start the channel (which runs the recovery sweep) and stop it."""
    task = asyncio.create_task(channel.start())
    try:
        for _ in range(250):
            if task.done():
                await task  # re-raise a start-up failure
            if channel._running:
                break
            await asyncio.sleep(0.02)
        assert channel._running, "listener never came up"
    finally:
        await channel.stop()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _complete_turn(workspace: Path, published: InboundMessage) -> None:
    """Mark the receipt processed the way the agent loop does on completion."""
    loop = AgentLoop.__new__(AgentLoop)
    loop.sessions = SimpleNamespace(workspace=workspace)  # type: ignore[assignment]
    await loop._mark_chat_message_processed(published)


@pytest.mark.asyncio
@pytest.mark.parametrize("seed_state", ["enqueued", "stored", "retry_wait"])
async def test_accepted_unfinished_message_is_republished_once_on_start(
    tmp_path: Path,
    seed_state: str,
) -> None:
    await _seed(tmp_path, state=seed_state)

    channel = _make_channel(tmp_path)
    await _run_once(channel)

    channel.bus.publish_inbound.assert_awaited_once()
    published: InboundMessage = channel.bus.publish_inbound.await_args.args[0]
    assert published.channel == "websocket"
    assert published.chat_id == CHAT_ID
    assert published.content == "recover me"
    assert published.metadata["client_message_id"] == CLIENT_ID
    # In-flight marking: the entry stays claimed until the turn completes.
    assert [r.state for r in await ChatInboxStore(tmp_path).recoverable()] == ["enqueued"]


@pytest.mark.asyncio
async def test_completed_entry_is_not_republished(tmp_path: Path) -> None:
    await _seed(tmp_path, state="enqueued")
    await ChatInboxStore(tmp_path).mark_processed(CHAT_ID, CLIENT_ID)

    channel = _make_channel(tmp_path)
    await _run_once(channel)

    channel.bus.publish_inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_start_after_turn_completes_publishes_nothing(tmp_path: Path) -> None:
    await _seed(tmp_path, state="enqueued")

    first = _make_channel(tmp_path)
    await _run_once(first)
    assert first.bus.publish_inbound.await_count == 1
    published: InboundMessage = first.bus.publish_inbound.await_args.args[0]

    await _complete_turn(tmp_path, published)
    assert await ChatInboxStore(tmp_path).recoverable() == []

    second = _make_channel(tmp_path)
    await _run_once(second)
    second.bus.publish_inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_start_without_completion_recovers_the_unfinished_turn(
    tmp_path: Path,
) -> None:
    """Crash before the turn completes: the next start must retry, not drop."""
    await _seed(tmp_path, state="enqueued")

    first = _make_channel(tmp_path)
    await _run_once(first)
    assert first.bus.publish_inbound.await_count == 1

    second = _make_channel(tmp_path)
    await _run_once(second)
    assert second.bus.publish_inbound.await_count == 1


@pytest.mark.asyncio
async def test_publish_failure_releases_claim_for_the_next_start(tmp_path: Path) -> None:
    await _seed(tmp_path, state="stored")

    channel = _make_channel(tmp_path)
    channel.bus.publish_inbound.side_effect = RuntimeError("bus down")
    task = asyncio.create_task(channel.start())
    try:
        with pytest.raises(RuntimeError):
            # The sweep runs before the listener binds, so start() itself
            # must fail fast; a listener that stays up means no recovery.
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
    finally:
        if not task.done():
            task.cancel()
        await channel.stop()
        await asyncio.gather(task, return_exceptions=True)

    # The claim was released, so the entry is recoverable again, un-claimed.
    assert [r.state for r in await ChatInboxStore(tmp_path).recoverable()] == ["stored"]


@pytest.mark.asyncio
async def test_start_without_workspace_skips_the_sweep() -> None:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "websocketRequiresToken": False,
        "port": 0,
    }
    parsed = WebSocketConfig.model_validate(cfg)
    gateway = build_gateway_services(
        config=parsed,
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=Path.cwd(),
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(cfg, bus, gateway=gateway)
    await _run_once(channel)
    assert channel.chat_inbox is None
    bus.publish_inbound.assert_not_awaited()


# -- Transcript: client_message_id on the user event (MIT-1010 item 3) --------


def test_build_user_transcript_event_omits_absent_client_message_id() -> None:
    event = build_user_transcript_event("chat1", "hello")
    assert event is not None
    assert "client_message_id" not in event


def test_recorder_echoes_client_message_id_from_inbound_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner send path hands the inbound metadata straight to the recorder."""
    recorder = WebUITranscriptRecorder()
    appended: list[dict[str, Any]] = []
    monkeypatch.setattr(
        recorder,
        "append",
        lambda chat_id, event: appended.append(event) or True,
    )

    assert recorder.append_user_message(
        "chat1", "hello", metadata={"client_message_id": CLIENT_ID}
    )
    assert appended[0]["client_message_id"] == CLIENT_ID

    appended.clear()
    assert recorder.append_user_message("chat2", "hello", metadata={})
    assert "client_message_id" not in appended[0]

    appended.clear()
    assert recorder.append_user_message(
        "chat3", "hello", metadata={"client_message_id": 42}
    )
    assert "client_message_id" not in appended[0]


def test_session_replay_echoes_persisted_client_message_id() -> None:
    persisted = {
        "role": "user",
        "content": "hello",
        "client_message_id": CLIENT_ID,
    }
    event = _session_user_event("websocket:chat1", persisted)
    assert event is not None
    assert event["client_message_id"] == CLIENT_ID

    event = _session_user_event("websocket:chat1", {"role": "user", "content": "hello"})
    assert event is not None
    assert "client_message_id" not in event
