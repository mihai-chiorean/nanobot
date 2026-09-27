"""Regression test for MIT-1486: mid-turn websocket follow-ups must be closed
out in the durable chat inbox, not just answered.

Bug: a websocket follow-up message injected into a RUNNING turn via the
pending-queue drain (``AgentLoop._drain_pending`` / ``AgentRunner
._try_drain_injections``) was answered correctly, but its ``ChatInboxStore``
receipt (MIT-1402) was never marked processed -- ``_mark_chat_message_processed``
was only ever called for the message that *started* the turn. The receipt
stayed ``enqueued``, so the next gateway start's ``_recover_chat_inbox`` sweep
(MIT-1403) republished it as a brand-new turn, and the bot re-answered an old
follow-up.

Repro shape (matches the reviewer's manual repro: "model calls: 2 injected:
True recoverable: ['cid-2']"): a fake provider blocks its first model call
until a second websocket message has actually reached the session's pending
queue, so the runner's drain absorbs it into the same turn as a follow-up
(two model calls, one turn) rather than starting a second, independent turn.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.chat_inbox import ChatInboxStore
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.providers.base import LLMResponse
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import build_gateway_services

CHAT_ID = "chat-1"
ROOT_CLIENT_ID = "cid-1"
FOLLOWUP_CLIENT_ID = "cid-2"
SESSION_KEY = f"websocket:{CHAT_ID}"


def _websocket_message(content: str, client_message_id: str) -> InboundMessage:
    return InboundMessage(
        channel="websocket",
        sender_id="owner",
        chat_id=CHAT_ID,
        content=content,
        metadata={"webui": True, "client_message_id": client_message_id},
    )


def _make_recovery_channel(workspace: Path) -> WebSocketChannel:
    """A second, independent gateway pointed at the same workspace/inbox."""
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


async def _run_recovery_sweep_once(channel: WebSocketChannel) -> None:
    """Start the channel (which runs the MIT-1403 recovery sweep) and stop it."""
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


@pytest.mark.asyncio
async def test_mid_turn_followup_is_marked_processed_and_not_recovered(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    call_count = {"n": 0}
    provider_call_started = asyncio.Event()
    followup_delivered = asyncio.Event()

    async def chat_stream_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Block the first model call until the test has actually pushed
            # the follow-up into this turn's pending queue -- reproducing a
            # follow-up that lands while the turn is still RUNNING, not one
            # that arrives after it settles.
            provider_call_started.set()
            await asyncio.wait_for(followup_delivered.wait(), timeout=5)
            return LLMResponse(content="root answer", tool_calls=[], usage=None)
        return LLMResponse(content="root+follow-up answer", tool_calls=[], usage=None)

    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        memory_index_enabled=False,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])

    workspace = loop.sessions.workspace
    inbox = ChatInboxStore(workspace)

    root_msg = _websocket_message("root question", ROOT_CLIENT_ID)
    disposition, _ = await inbox.accept(root_msg, ROOT_CLIENT_ID)
    assert disposition == "inserted"
    assert await inbox.claim_for_enqueue(CHAT_ID, ROOT_CLIENT_ID)

    run_task = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(root_msg)

        # Wait until the turn is actually mid-flight (inside the first model
        # call) before injecting the follow-up.
        await asyncio.wait_for(provider_call_started.wait(), timeout=5)

        followup_msg = _websocket_message("also, one more thing", FOLLOWUP_CLIENT_ID)
        disposition, _ = await inbox.accept(followup_msg, FOLLOWUP_CLIENT_ID)
        assert disposition == "inserted"
        assert await inbox.claim_for_enqueue(CHAT_ID, FOLLOWUP_CLIENT_ID)
        await bus.publish_inbound(followup_msg)

        # Wait until the follow-up has actually reached the running turn's
        # pending queue (real routing through AgentLoop.run(), not a
        # test-only shortcut) before releasing the blocked model call.
        for _ in range(250):
            pending = loop._pending_queues.get(SESSION_KEY)
            if pending is not None and pending.qsize() > 0:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("follow-up never reached the session's pending queue")
        followup_delivered.set()

        # Wait for the whole turn (root + absorbed follow-up) to finish.
        for _ in range(250):
            if SESSION_KEY not in loop._pending_queues:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("turn did not complete in time")
    finally:
        loop.stop()
        await asyncio.wait_for(run_task, timeout=5)

    # The follow-up was genuinely absorbed mid-turn: one turn, two model
    # calls (matches the reviewer's repro: "model calls: 2 injected: True").
    assert call_count["n"] == 2

    # The bug: this stayed ["enqueued"] for cid-2 because only the root
    # message's receipt was ever marked processed.
    assert await inbox.recoverable() == []

    # And therefore the next gateway start's recovery sweep must republish
    # nothing -- an unfixed loop would re-send "also, one more thing" as a
    # brand-new turn here.
    recovery_channel = _make_recovery_channel(workspace)
    await _run_recovery_sweep_once(recovery_channel)
    recovery_channel.bus.publish_inbound.assert_not_awaited()
