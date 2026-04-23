"""Tests for the completion-signal fix (Part 1).

Verify:
1. on_stream_end(resuming=False) is called at end of a successful streaming turn.
2. A _streamed=True final OutboundMessage is NOT what triggers finalization —
   the explicit _stream_end path is.
3. on_stream_end is NOT double-fired on the max_iterations path.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=MagicMock(), provider=provider, workspace=tmp_path, model="test-model")


@pytest.mark.asyncio
async def test_on_stream_end_fires_on_successful_turn(tmp_path: Path) -> None:
    """on_stream_end(resuming=False) must be awaited once at the end of a normal turn."""
    loop = _make_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)

    loop._run_agent_loop = AsyncMock(return_value=(
        "answer",
        [],
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "answer"},
        ],
        "completed",
        False,
    ))

    on_stream_end = AsyncMock()

    await loop._process_message(
        InboundMessage(channel="discord", sender_id="u1", chat_id="c1", content="hi"),
        on_stream=AsyncMock(),
        on_stream_end=on_stream_end,
    )

    on_stream_end.assert_awaited_once_with(resuming=False)


@pytest.mark.asyncio
async def test_on_stream_end_not_fired_on_error_stop(tmp_path: Path) -> None:
    """When stop_reason is 'error', _streamed is not set and on_stream_end is not called."""
    loop = _make_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)

    loop._run_agent_loop = AsyncMock(return_value=(
        "Sorry, error.",
        [],
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Sorry, error."}],
        "error",
        False,
    ))

    on_stream_end = AsyncMock()
    result = await loop._process_message(
        InboundMessage(channel="discord", sender_id="u1", chat_id="c2", content="hi"),
        on_stream=AsyncMock(),
        on_stream_end=on_stream_end,
    )

    on_stream_end.assert_not_awaited()
    # _streamed must NOT be set on error path
    assert result is not None
    assert not result.metadata.get("_streamed")


@pytest.mark.asyncio
async def test_on_stream_end_not_double_fired_on_max_iterations(tmp_path: Path) -> None:
    """max_iterations path already calls on_stream_end inside _run_agent_loop; _process_message
    must not call it again (stop_reason == 'max_iterations' skips the extra call)."""
    loop = _make_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)

    on_stream_end = AsyncMock()

    # Simulate what _run_agent_loop does for max_iterations: it calls on_stream_end internally
    async def fake_run(initial_messages, *, on_stream=None, on_stream_end=None, **kw):
        # The real implementation calls on_stream_end(resuming=False) here
        if on_stream_end:
            await on_stream_end(resuming=False)
        return (
            "Max iterations reached.",
            [],
            [{"role": "user", "content": "run"}, {"role": "assistant", "content": "Max iterations reached."}],
            "max_iterations",
            False,
        )

    loop._run_agent_loop = fake_run

    await loop._process_message(
        InboundMessage(channel="discord", sender_id="u1", chat_id="c3", content="run"),
        on_stream=AsyncMock(),
        on_stream_end=on_stream_end,
    )

    # Must be called exactly once (from within fake_run, NOT again from _process_message)
    on_stream_end.assert_awaited_once_with(resuming=False)


@pytest.mark.asyncio
async def test_streamed_true_message_does_not_send_to_channel(tmp_path: Path) -> None:
    """A final message with _streamed=True must be dropped by manager._send_once,
    not forwarded to channel.send().  This test verifies the metadata is set correctly
    and that the manager routing skips it."""
    from nanobot.bus.events import OutboundMessage
    from nanobot.channels.manager import ChannelManager

    # Build a _streamed=True message as _process_message would return
    msg = OutboundMessage(
        channel="discord",
        chat_id="c1",
        content="final answer",
        metadata={"_streamed": True},
    )

    mock_channel = AsyncMock()
    # _send_once should not call channel.send() for _streamed=True
    from nanobot.channels.base import BaseChannel
    mock_channel_obj = MagicMock(spec=BaseChannel)
    mock_channel_obj.send = AsyncMock()
    mock_channel_obj.send_delta = AsyncMock()

    await ChannelManager._send_once(mock_channel_obj, msg)

    mock_channel_obj.send.assert_not_awaited()
    mock_channel_obj.send_delta.assert_not_awaited()
