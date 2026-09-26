"""Ziggy-local (fork): regression tests for _ZiggyTurnHook.

The Discord progress heartbeat and the per-iteration LLM telemetry used to
live in ``_LoopHook``, wired into the positional-callback pipeline that
upstream removed in the 2026-09 merge. They were re-applied as an
``AgentHook`` built by a turn-hook factory. These tests pin the behaviour so
the next upstream merge cannot silently drop it again.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.hook import AgentHookContext, AgentTurnHookContext
from nanobot.agent.loop import _ZiggyTurnHook
from nanobot.bus.events import OutboundMessage


class _FakeBus:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


def _loop_stub() -> Any:
    loop = MagicMock()
    loop.bus = _FakeBus()
    loop.model = "test-model"
    loop._audit_logger = MagicMock()
    return loop


def _turn(metadata: dict[str, Any] | None = None) -> AgentTurnHookContext:
    return AgentTurnHookContext(
        channel="discord",
        chat_id="chat-1",
        session_key="discord:chat-1",
        metadata=metadata if metadata is not None else {"_wants_stream": True},
    )


@pytest.mark.asyncio
async def test_heartbeat_emits_status_delta_on_first_iteration() -> None:
    loop = _loop_stub()
    hook = _ZiggyTurnHook(loop, _turn())

    await hook.before_iteration(AgentHookContext(iteration=0, messages=[]))

    assert len(loop.bus.sent) == 1
    msg = loop.bus.sent[0]
    assert msg.channel == "discord"
    assert msg.chat_id == "chat-1"
    # Keyed by chat_id only — no _stream_id — so the status survives tool-call
    # segment boundaries and keeps being edited in place.
    assert msg.metadata["_status_delta"] is True
    assert "_stream_id" not in msg.metadata
    assert "Getting started" in msg.content


@pytest.mark.asyncio
async def test_heartbeat_silent_when_channel_did_not_ask_for_streaming() -> None:
    loop = _loop_stub()
    hook = _ZiggyTurnHook(loop, _turn(metadata={}))

    await hook.before_iteration(AgentHookContext(iteration=1, messages=[]))
    await hook.before_execute_tools(AgentHookContext(iteration=1, messages=[]))

    assert loop.bus.sent == []


@pytest.mark.asyncio
async def test_heartbeat_summarises_the_running_tool() -> None:
    from nanobot.providers.base import ToolCallRequest

    loop = _loop_stub()
    hook = _ZiggyTurnHook(loop, _turn())
    ctx = AgentHookContext(
        iteration=0,
        messages=[],
        tool_calls=[
            ToolCallRequest(id="1", name="exec", arguments={"command": "go test ./..."}),
            ToolCallRequest(id="2", name="read_file", arguments={"path": "x.py"}),
        ],
    )

    await hook.before_execute_tools(ctx)

    assert len(loop.bus.sent) == 1
    content = loop.bus.sent[0].content
    assert "exec" in content
    assert "go test" in content
    assert "+1 more" in content


@pytest.mark.asyncio
async def test_after_iteration_records_llm_call_in_the_audit_log() -> None:
    from nanobot.providers.base import LLMResponse, LLMUsage

    loop = _loop_stub()
    hook = _ZiggyTurnHook(loop, _turn())
    ctx = AgentHookContext(
        iteration=0,
        messages=[],
        response=LLMResponse(content="hi", ttft_ms=42),
        latency_ms=123.4,
        usage=LLMUsage.reported(input_tokens=10, output_tokens=3),
    )

    await hook.after_iteration(ctx)

    loop._audit_logger.log_llm_call.assert_called_once()
    kwargs = loop._audit_logger.log_llm_call.call_args.kwargs
    assert kwargs["channel"] == "discord"
    assert kwargs["session_id"] == "chat-1"
    assert kwargs["model"] == "test-model"
    assert kwargs["latency_ms"] == pytest.approx(123.4)
    assert kwargs["ttft_ms"] == 42
    assert kwargs["tokens_in"] == 10
    assert kwargs["tokens_out"] == 3


@pytest.mark.asyncio
async def test_after_iteration_logs_null_tokens_without_usage() -> None:
    from nanobot.providers.base import LLMResponse

    loop = _loop_stub()
    hook = _ZiggyTurnHook(loop, _turn())
    ctx = AgentHookContext(
        iteration=0, messages=[], response=LLMResponse(content="hi"), latency_ms=5.0, usage=None
    )

    await hook.after_iteration(ctx)

    kwargs = loop._audit_logger.log_llm_call.call_args.kwargs
    assert kwargs["tokens_in"] is None
    assert kwargs["tokens_out"] is None


@pytest.mark.asyncio
async def test_after_iteration_streams_reasoning_only_while_tools_are_running() -> None:
    from nanobot.providers.base import LLMResponse, ToolCallRequest

    loop = _loop_stub()
    hook = _ZiggyTurnHook(loop, _turn())
    response = LLMResponse(content=None, reasoning_content="First I check the config. Then I")

    # Final-content iteration: the answer is already streaming into the channel
    # buffer, so clobbering it with a reasoning snippet would be wrong.
    await hook.after_iteration(
        AgentHookContext(iteration=0, messages=[], response=response, tool_calls=[])
    )
    assert loop.bus.sent == []

    # Tool-calling iteration: the loop is continuing, so surface the reasoning.
    await hook.after_iteration(
        AgentHookContext(
            iteration=1,
            messages=[],
            response=response,
            tool_calls=[ToolCallRequest(id="1", name="exec", arguments={})],
        )
    )
    assert len(loop.bus.sent) == 1
    assert "First I check the config" in loop.bus.sent[0].content


@pytest.mark.asyncio
async def test_audit_failure_never_breaks_the_turn() -> None:
    loop = _loop_stub()
    loop._audit_logger.log_llm_call.side_effect = RuntimeError("audit sink down")
    hook = _ZiggyTurnHook(loop, _turn())

    # Must not raise.
    await hook.after_iteration(AgentHookContext(iteration=0, messages=[], latency_ms=1.0))


# ---------------------------------------------------------------------------
# The other half of the heartbeat: ChannelManager must route a _status_delta
# message to channel.send_status() and must NOT fall through to channel.send().
# This branch is checked before upstream's typed-event dispatch because
# _status_delta is not an upstream event type.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_manager_routes_status_delta_to_send_status() -> None:
    from unittest.mock import AsyncMock

    from nanobot.channels.manager import ChannelManager

    channel = MagicMock()
    channel.send_status = AsyncMock()
    channel.send = AsyncMock()
    channel.send_delta = AsyncMock()

    msg = OutboundMessage(
        channel="discord",
        chat_id="chat-1",
        content="thinking...",
        metadata={"_status_delta": True},
    )

    await ChannelManager._send_once(channel, msg)

    channel.send_status.assert_awaited_once_with("chat-1", "thinking...", msg.metadata)
    channel.send.assert_not_awaited()
    channel.send_delta.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_manager_drops_status_delta_for_channels_without_send_status() -> None:
    """A channel that never implemented send_status must not receive the
    heartbeat as an ordinary message — that would spam the chat."""
    from unittest.mock import AsyncMock

    from nanobot.channels.manager import ChannelManager

    channel = MagicMock(spec=["send", "send_delta"])
    channel.send = AsyncMock()
    channel.send_delta = AsyncMock()

    msg = OutboundMessage(
        channel="telegram",
        chat_id="chat-1",
        content="thinking...",
        metadata={"_status_delta": True},
    )

    await ChannelManager._send_once(channel, msg)

    channel.send.assert_not_awaited()
    channel.send_delta.assert_not_awaited()
