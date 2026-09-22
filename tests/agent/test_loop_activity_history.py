"""A turn that runs tools must leave recoverable Activity rows in the session file."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.activity_history import KEY


def _make_loop(tmp_path: Path) -> tuple[AgentLoop, MessageBus]:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    tool_call = ToolCallRequest(id="tc1", name="exec", arguments={"command": "ls"})
    calls = iter([
        LLMResponse(content="", tool_calls=[tool_call]),
        LLMResponse(content="Done", tool_calls=[]),
    ])
    provider.chat_stream_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.prepare_call = MagicMock(return_value=(None, {"command": "ls"}, None))
    loop.tools.execute = AsyncMock(return_value="file.txt")
    return loop, bus


@pytest.mark.asyncio
async def test_tool_call_persists_activity_rows_before_turn_end(tmp_path: Path) -> None:
    loop, bus = _make_loop(tmp_path)

    # Durability is the point: the rows are fsync-written as the tools run, so
    # a mid-turn crash still leaves them for the interrupted turn to render.
    # The write must be the metadata-only one; a full save would rewrite and
    # fsync the whole transcript on every tool start/end.
    update_calls: list[bool] = []
    real_update = loop.sessions.update_session_metadata

    def spy_update(
        key: str, updates: dict[str, object], *, fsync: bool = False
    ) -> bool:
        if KEY in updates:
            update_calls.append(fsync)
        return real_update(key, updates, fsync=fsync)

    loop.sessions.update_session_metadata = spy_update  # type: ignore[method-assign]

    await loop._dispatch(InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="chat1",
        content="list the files",
    ))
    while bus.outbound_size > 0:
        await bus.consume_outbound()

    persisted = loop.sessions.read_session_file("websocket:chat1")
    assert persisted is not None
    records = (persisted.get("metadata") or {}).get(KEY)
    assert isinstance(records, list) and len(records) == 1
    record = records[0]
    assert record["call_id"] == "tc1"
    assert record["name"] == "exec"
    assert record["status"] == "completed"
    assert isinstance(record["before_message_count"], int)
    assert any(fsync for fsync in update_calls)


@pytest.mark.asyncio
async def test_ephemeral_turn_does_not_record_activity(tmp_path: Path) -> None:
    loop, bus = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("websocket:chat1")

    await loop._process_message_impl(
        InboundMessage(
            channel="websocket",
            sender_id="u1",
            chat_id="chat1",
            content="list the files",
        ),
        session_key=session.key,
        ephemeral=True,
    )
    while bus.outbound_size > 0:
        await bus.consume_outbound()

    assert KEY not in session.metadata
