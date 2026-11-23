from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import tool_parameters_schema
from nanobot.agent.turn_delivery import TurnDeliveryFactory
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager
from nanobot.session.recovery import (
    PENDING_USER_TURN_KEY,
    RECOVERY_METADATA_KEY,
    RUNTIME_CHECKPOINT_KEY,
    RecoveryCoordinator,
)
from nanobot.session.turn_continuation import INTERNAL_CONTINUATION_META


@pytest.mark.asyncio
async def test_recovery_continuation_runs_without_a_sustained_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:chat")
    session.metadata.update({
        "webui": True,
        RECOVERY_METADATA_KEY: {
            "status": "awaiting_user",
            "recovery_id": "recovery-1",
            "attempts": 0,
        },
    })
    sessions.save(session)
    recovery = RecoveryCoordinator(sessions, bus)

    loop = AgentLoop.__new__(AgentLoop)
    loop.sessions = sessions
    loop._unified_session = False
    loop._recovery_admission = recovery
    loop._session_locks = {}
    loop._concurrency_gate = None
    loop._automation_turn_coordinators = []
    loop._discarding_sessions = set()
    loop._preserve_inflight_turns_on_shutdown = False
    loop.turn_delivery_factory = TurnDeliveryFactory(bus)
    process_message = AsyncMock(return_value=OutboundMessage(
        channel="websocket",
        chat_id="chat",
        content="done",
    ))
    monkeypatch.setattr(loop, "_process_message", process_message)

    await recovery.handle_action(
        "continue",
        {"chat_id": "chat", "recovery_id": "recovery-1"},
    )
    continuation = bus.inbound.get_nowait()

    assert continuation.metadata[INTERNAL_CONTINUATION_META] is True
    await loop._dispatch_one(continuation, asyncio.Queue())

    process_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_only_auto_continue_admits_and_runs_its_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:chat")
    session.messages.append({"role": "user", "content": "read the logs"})
    session.metadata["webui"] = True
    session.metadata[PENDING_USER_TURN_KEY] = True
    session.metadata[RUNTIME_CHECKPOINT_KEY] = {
        "phase": "awaiting_tools",
        "assistant_message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "read_file"}}],
        },
        "completed_tool_results": [],
        "pending_tool_calls": [
            {"id": "call-1", "function": {"name": "read_file"}}
        ],
        "turn_had_write": False,
    }
    sessions.save(session)
    recovery = RecoveryCoordinator(
        sessions,
        bus,
        tool_is_read_only=lambda name: name == "read_file",
    )

    loop = AgentLoop.__new__(AgentLoop)
    loop.sessions = sessions
    loop._unified_session = False
    loop._recovery_admission = recovery
    loop._session_locks = {}
    loop._concurrency_gate = None
    loop._automation_turn_coordinators = []
    loop._discarding_sessions = set()
    loop._preserve_inflight_turns_on_shutdown = False
    loop.turn_delivery_factory = TurnDeliveryFactory(bus)
    process_message = AsyncMock(return_value=OutboundMessage(
        channel="websocket",
        chat_id="chat",
        content="done",
    ))
    monkeypatch.setattr(loop, "_process_message", process_message)

    await recovery.scan()
    continuation = bus.inbound.get_nowait()

    assert continuation.metadata[INTERNAL_CONTINUATION_META] is True
    await loop._dispatch_one(continuation, asyncio.Queue())

    process_message.assert_awaited_once()
    state = sessions.get_or_create("websocket:chat").metadata[RECOVERY_METADATA_KEY]
    assert state["status"] == "resuming"
    assert state["reason"] == "read_only_resumed"


@tool_parameters(tool_parameters_schema(required=[]))
class _SendTool(Tool):
    """A non-idempotent write: the invariant is that it never runs twice."""

    def __init__(self) -> None:
        self.sent = 0

    @property
    def name(self) -> str:
        return "fake_send"

    @property
    def description(self) -> str:
        return "Send a message."

    async def execute(self, **kwargs: object) -> str:
        self.sent += 1
        return "sent"


@tool_parameters(tool_parameters_schema(required=[]))
class _HangingReadTool(Tool):
    """A read-only tool that never returns, so the process can 'crash' in it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    @property
    def name(self) -> str:
        return "fake_read"

    @property
    def description(self) -> str:
        return "Read something."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: object) -> str:
        self.started.set()
        await asyncio.Event().wait()
        return "unreachable"


def _tool_call_response(call_id: str, name: str) -> LLMResponse:
    return LLMResponse(
        content="",
        finish_reason="tool_calls",
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments={})],
    )


async def _crash_inside_read(
    tmp_path: Path,
    responses: list[LLMResponse],
) -> tuple[_SendTool, MessageBus, SessionManager]:
    """Run a real turn until it hangs in the read tool, then drop the process.

    Nothing is hand-persisted: whatever the recovery scan sees afterwards is
    exactly what the production loop wrote to disk before the 'crash'.
    """
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = AsyncMock(side_effect=responses)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    send, read = _SendTool(), _HangingReadTool()
    loop.tools.register(send)
    loop.tools.register(read)
    turn = asyncio.create_task(
        loop._process_message(
            InboundMessage(
                channel="websocket",
                sender_id="user",
                chat_id="chat",
                content="send it, then read the reply",
            )
        )
    )
    await asyncio.wait_for(read.started.wait(), timeout=10)
    # Restart: a fresh coordinator over a fresh SessionManager reads only disk.
    bus = MessageBus()
    restarted = SessionManager(tmp_path)
    coordinator = RecoveryCoordinator(
        restarted,
        bus,
        tool_is_read_only=loop.tools.is_read_only,
    )
    await coordinator.scan()
    turn.cancel()
    with contextlib.suppress(BaseException):
        await turn
    return send, bus, restarted


@pytest.mark.asyncio
async def test_completed_write_earlier_in_turn_blocks_auto_continue(tmp_path: Path) -> None:
    """Codex P1 repro: send succeeds, the next iteration checkpoints a pending
    read, crash.  The checkpoint replaced the send's, so only the sticky
    ``turn_had_write`` flag can tell recovery a write already happened."""
    send, bus, restarted = await _crash_inside_read(
        tmp_path,
        [
            _tool_call_response("call-send", "fake_send"),
            _tool_call_response("call-read", "fake_read"),
        ],
    )

    assert send.sent == 1
    assert bus.inbound.empty(), "a turn that already wrote must not auto-continue"
    state = restarted.get_or_create("websocket:chat").metadata[RECOVERY_METADATA_KEY]
    assert state["status"] == "awaiting_user"
    assert state["reason"] == "tool_state_unknown"


@pytest.mark.asyncio
async def test_read_only_turn_from_real_runner_auto_continues(tmp_path: Path) -> None:
    """The same production path with only reads in the turn still resumes."""
    send, bus, restarted = await _crash_inside_read(
        tmp_path,
        [_tool_call_response("call-read", "fake_read")],
    )

    assert send.sent == 0
    continuation = bus.inbound.get_nowait()
    assert continuation.metadata[INTERNAL_CONTINUATION_META] is True
    state = restarted.get_or_create("websocket:chat").metadata[RECOVERY_METADATA_KEY]
    assert state["status"] == "resuming"
    assert state["reason"] == "read_only_resumed"
